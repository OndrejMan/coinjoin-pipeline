"""Standalone adapters for research functions shipped in the coinjoin-analysis image.

This file is copied into each invocation, so the analyzer image need not be changed.
The original entry points with fixed paths are never executed.
"""

import argparse
import ast
import hashlib
import importlib
import json
from pathlib import Path
import re
import sys
from types import SimpleNamespace


def input_path(root, relative):
    candidate = (root / relative).resolve()
    if candidate != root.resolve() and root.resolve() not in candidate.parents:
        raise ValueError("The selected file must be inside the input dataset")
    return candidate


def analyzer_root():
    package = importlib.import_module("cj_process")
    return Path(package.__file__).resolve().parent.parent.parent


def load_functions(path, *, llm=False):
    """Load definitions, excluding script entry points and their fixed data paths."""
    source = path.read_text(encoding="utf-8")
    if llm:
        # The unused distance-printing helper after this marker contains a Python 3.12
        # f-string in the current source; the analyzer image uses Python 3.11. All
        # generator/plot functions precede it and retain their original source.
        source = source.partition("\ndef compute_distance_single_wallet(")[0]
    tree = ast.parse(source, filename=str(path))
    body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
    if llm:
        body = [
            node
            for node in body
            if not (isinstance(node, ast.Import) and any(alias.name == "parse_cj_logs" for alias in node.names))
        ]
        # Constants required by the functions above the original __main__ block.
        body.extend(node for node in tree.body if isinstance(node, ast.Assign))
    namespace = {"__name__": "coinjoin_analysis_research_adapter", "__file__": str(path)}
    # The research files still use bare imports of their neighboring modules.
    sys.path.insert(0, str(path.parent))
    try:
        exec(compile(ast.Module(body=body, type_ignores=[]), str(path), "exec"), namespace)
    finally:
        sys.path.pop(0)
    return namespace


class RecordedRequests:
    """Keep source responses with the result and bound the existing helper's HTTP calls."""

    def __init__(self, directory):
        import requests

        self.session = requests.Session()
        self.RequestException = requests.RequestException
        self.directory = directory
        self.directory.mkdir(parents=True, exist_ok=True)
        self.records = []

    def get(self, url):
        response = self.session.get(url, timeout=30)
        name = hashlib.sha256(url.encode()).hexdigest() + ".txt"
        (self.directory / name).write_text(response.text, encoding="utf-8")
        self.records.append({"url": url, "status": response.status_code, "response": name})
        (self.directory / "requests.json").write_text(json.dumps(self.records, indent=2) + "\n", encoding="utf-8")
        return response


def transaction_hex(root, settings):
    if settings.get("tx_hex"):
        return settings["tx_hex"], "provided_hex"
    if settings.get("transaction_file"):
        path = input_path(root, settings["transaction_file"])
        raw = path.read_text(encoding="utf-8").strip()
        if raw.startswith("{"):
            record = json.loads(raw)
            return record["hex"], str(path.relative_to(root))
        return raw, str(path.relative_to(root))
    txid = settings.get("txid")
    if txid:
        # Prefer the exported transaction, even when online mainnet lookup is allowed.
        for path in sorted(root.rglob("block_*.json")):
            block = json.loads(path.read_text(encoding="utf-8"))
            block = block.get("result", block)
            for record in block.get("tx", []):
                if isinstance(record, dict) and record.get("txid") == txid and record.get("hex"):
                    return record["hex"], str(path.relative_to(root))
    return None, None


def analyze_size(root, output, settings, mode, analysis_root):
    raw, source = transaction_hex(root, settings)
    if raw is None:
        if mode != "mainnet" or not settings.get("allow_network") or not settings.get("txid"):
            raise ValueError("No raw transaction hex found; provide tx_hex or transaction_file (regtest is offline)")
        helper = load_functions(analysis_root / "src/cj_process/analyze_tx_size.py")
        helper["requests"] = RecordedRequests(output / "sources")
        raw = helper["get_transaction_hex"](settings["txid"])
        source = "mempool.space"
    if not isinstance(raw, str) or not raw or len(raw) % 2 or re.fullmatch(r"[0-9a-fA-F]+", raw) is None:
        raise ValueError("The transaction must contain non-empty, even-length hexadecimal data")
    analysis = importlib.import_module("cj_process.cj_analysis")
    # Validate indices using the same transaction decoder the upstream function uses.
    transaction = analysis.CTransaction.deserialize(bytes.fromhex(raw))
    for key, count in (("input_indices", len(transaction.vin)), ("output_indices", len(transaction.vout))):
        indices = settings.get(key, [])
        if any(type(index) is not int or index < 0 or index >= count for index in indices):
            raise ValueError("{} are outside the transaction's range".format(key))
        if len(indices) != len(set(indices)):
            raise ValueError("{} must not repeat an index".format(key))
    actual_txid = str(transaction.GetTxid()[::-1].hex())
    if settings.get("txid") and settings["txid"].lower() != actual_txid:
        raise ValueError("Raw transaction TXID does not match the selected TXID")
    selected, total = analysis.compute_partial_vsize(
        raw, list(settings.get("input_indices", [])), list(settings.get("output_indices", []))
    )
    (output / "transaction.hex").write_text(raw + "\n", encoding="utf-8")
    return {
        "txid": actual_txid,
        "source": source,
        "selected_vsize": selected,
        "transaction_vsize": total,
        "input_indices": settings.get("input_indices", []),
        "output_indices": settings.get("output_indices", []),
        "unit": "vbyte",
    }


def generate_inputs(root, output, settings, mode, analysis_root, run_id, targets):
    if mode != "emulation":
        raise ValueError("The existing LLM generator requires emulation wallet ground truth")
    relative = targets[0] if targets else run_id + "/coinjoin_tx_info.json"
    path = input_path(root, relative)
    if path.is_dir():
        path = path / "coinjoin_tx_info.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    coinjoins = data.get("coinjoins")
    if not isinstance(coinjoins, dict) or not coinjoins:
        raise ValueError("The generator needs a non-empty coinjoin_tx_info.json")
    for transaction in coinjoins.values():
        for direction in ("inputs", "outputs"):
            for coin in transaction.get(direction, {}).values():
                wallet = coin.get("wallet_name")
                if not isinstance(wallet, str) or wallet.strip().lower() in {"", "unknown", "other", "none"}:
                    raise ValueError("The LLM generator needs known wallet_name values on inputs and outputs")
    helper = load_functions(analysis_root / "src/cj_process/generate_llm_inputs.py", llm=True)
    # The research file still addresses the link helper at its old module location.
    # Bind the existing implementation in this adapter's namespace only.
    analysis = importlib.import_module("cj_process.cj_analysis")
    helper["parse_cj_logs"] = SimpleNamespace(
        als=analysis, compute_link_between_inputs_and_outputs=analysis.compute_link_between_inputs_and_outputs
    )
    helper.update(
        {
            "MIN_WALLETS_IN_COINJOIN": settings.get("min_wallets", 1),
            "ANALYSIS_DEPTH_LIMIT": settings.get("depth", 100),
            "PERMUTATE_INPUTS": settings.get("permute_inputs", False),
            "PERMUTATE_OUTPUTS": settings.get("permute_outputs", False),
            "FORCE_LIMIT_ANON_SCORE": settings.get("force_limit_anon_score", 100),
            "GENERATE_SUBPARTS_GRAPHS": False,
            "WASABIWALLET_DATA_DIR": str(output),
        }
    )
    events = helper["generate_llm_inputs"](data)
    (output / "llm_inputs.json").write_text(json.dumps(events, indent=2) + "\n", encoding="utf-8")
    if settings.get("plot") and any(events.values()):
        included = {txid: data["coinjoins"][txid] for txid, values in events.items() if values}
        helper["visualize_wallet_leave_distribution"]({"coinjoins": included}, events, str(output))
    return {
        "source": str(path.relative_to(root)),
        "root_transactions": len(events),
        "vectors": sum(len(values) for values in events.values()),
        "settings": settings,
    }


def update_coordinators(root, output, settings, mode, analysis_root):
    if mode != "mainnet" or not settings.get("allow_network"):
        raise ValueError("Coordinator refresh is a mainnet operation and needs allow_network: true")
    source = (
        input_path(root, settings["coordinator_file"])
        if settings.get("coordinator_file")
        else (analysis_root / "data/wasabi2/txid_coord.json")
    )
    data = json.loads(source.read_text(encoding="utf-8"))
    for key in ("manual", "crawl_wasabist", "crawl_wabisator", "crawl_crocsapi"):
        if not isinstance(data.get(key), dict):
            raise ValueError("Coordinator source must contain a {} mapping".format(key))
    helper_source = Path(__file__).with_name("wabisator_source.py")
    if not helper_source.is_file():
        helper_source = analysis_root / "scripts/update_txids_with_wabisator.py"
    helper = load_functions(helper_source)
    helper["requests"] = RecordedRequests(output / "sources")
    # Call the existing main with explicit files; its unconditional bottom call is excluded.
    copied_source = output / "txid_coord_source.json"
    copied_source.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    result_file = output / "txid_coord_updated.json"
    helper["main"](str(copied_source), str(result_file))
    updated = json.loads(result_file.read_text(encoding="utf-8"))
    return {
        "source": str(source),
        "previous_wabisator_txids": len(data["crawl_wabisator"]),
        "updated_wabisator_txids": len(updated["crawl_wabisator"]),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--program", required=True, choices=("analyze_tx_size", "generate_llm_inputs", "update_txids_with_wabisator")
    )
    parser.add_argument("--data-mode", required=True, choices=("emulation", "mainnet"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--input", type=Path, default=Path("/work/input"))
    parser.add_argument("--output", type=Path, default=Path("/work/results"))
    parser.add_argument("--settings", required=True)
    parser.add_argument("--target-path", action="append", default=[])
    args = parser.parse_args(argv)
    args.output.mkdir(parents=True, exist_ok=True)
    settings = json.loads(args.settings)
    root = analyzer_root()
    if args.program == "analyze_tx_size":
        result = analyze_size(args.input, args.output, settings, args.data_mode, root)
    elif args.program == "generate_llm_inputs":
        result = generate_inputs(args.input, args.output, settings, args.data_mode, root, args.run_id, args.target_path)
    else:
        result = update_coordinators(args.input, args.output, settings, args.data_mode, root)
    (args.output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
