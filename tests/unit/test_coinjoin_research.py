"""Exercise adapters against original helper functions with no HTTP, images or emulator."""

import ast
from decimal import Decimal
import json
import logging
import math
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from exporters import coinjoin_research as research

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.configuration import PipelineConfiguration, ConfigurationError
from coinjoin_pipeline.execution.coinjoin_tools import command_argv
from coinjoin_pipeline.execution.coinjoin_tools import run_local
from coinjoin_pipeline.execution.pbs.templates_s3 import render_coinjoin_tools_s3_pbs
from coinjoin_pipeline.storage.s3 import S3Target

ANALYZER = Path(__file__).resolve().parents[3] / "coinjoin-analysis"


@pytest.fixture
def workspace(tmp_path):
    source, output = tmp_path / "input", tmp_path / "results"
    source.mkdir()
    output.mkdir()
    return source, output


def test_research_cli_parses_typed_indices_and_retains_literal_values():
    config = load_configuration(
        [
            "coinjoin-analysis",
            "--run-dir",
            "r1",
            "--analysis-program",
            "analyze_tx_size",
            "--analysis-data-mode",
            "mainnet",
            "--analysis-tx-hex",
            "abcd",
            "--analysis-input-index",
            "0",
            "--analysis-input-index",
            "2",
            "--analysis-output-index",
            "1",
        ]
    )
    config.validate()
    assert config.coinjoin_analysis.research.input_indices == (0, 2)
    argv = command_argv(config.coinjoin_analysis, "r1")
    assert argv[:2] == ["python", "/work/coinjoin_research.py"]
    assert json.loads(argv[argv.index("--settings") + 1])["input_indices"] == [0, 2]


@pytest.mark.parametrize(
    "options,match",
    [
        ({"program": "generate_llm_inputs", "data_mode": "mainnet"}, "wallet labels"),
        (
            {"program": "update_txids_with_wabisator", "data_mode": "emulation", "research": {"allow_network": True}},
            "mainnet",
        ),
        ({"program": "update_txids_with_wabisator", "data_mode": "mainnet"}, "allow-network"),
        ({"program": "analyze_tx_size", "research": {"tx_hex": "ab", "input_indices": [-1]}}, "non-negative"),
        ({"program": "analyze_tx_size", "research": {"tx_hex": "ab", "input_indices": [0, 0]}}, "not repeat"),
        ({"program": "analyze_tx_size", "research": {"tx_hex": "abc", "input_indices": [0]}}, "hexadecimal"),
        (
            {"program": "analyze_tx_size", "research": {"tx_hex": "ab", "input_indices": [0], "allow_network": True}},
            "mainnet",
        ),
        ({"program": "generate_llm_inputs", "research": {"transaction_file": "../outside"}}, "not supported"),
    ],
)
def test_modes_and_research_parameters_are_checked_before_submission(options, match):
    config = PipelineConfiguration.from_mapping(
        {"action": "coinjoin-analysis", "run_dir": "r1", "coinjoin_analysis": options}
    )
    with pytest.raises(ConfigurationError, match=match):
        config.validate()


def fake_transaction_library(monkeypatch):
    transaction = SimpleNamespace(vin=[0, 1], vout=[0, 1], GetTxid=lambda: bytes.fromhex("ab" * 32)[::-1])
    compute = Mock(return_value=(123, 456))
    library = SimpleNamespace(
        CTransaction=SimpleNamespace(deserialize=lambda _: transaction), compute_partial_vsize=compute
    )
    monkeypatch.setitem(sys.modules, "cj_process.cj_analysis", library)
    monkeypatch.setitem(sys.modules, "cj_analysis", library)
    return compute


def test_size_uses_existing_function_and_serializes_vbytes(monkeypatch, workspace):
    source, output = workspace
    compute = fake_transaction_library(monkeypatch)
    result = research.analyze_size(
        source, output, {"tx_hex": "aabb", "input_indices": [0], "output_indices": [1]}, "emulation", ANALYZER
    )
    compute.assert_called_once_with("aabb", [0], [1])
    assert result["selected_vsize"] == 123 and result["transaction_vsize"] == 456
    assert result["unit"] == "vbyte"
    assert (output / "transaction.hex").read_text() == "aabb\n"


def test_size_finds_raw_hex_in_exported_blocks_without_online_lookup(monkeypatch, workspace):
    source, output = workspace
    fake_transaction_library(monkeypatch)
    block = source / "r1/data/btc-node/block_1.json"
    block.parent.mkdir(parents=True)
    block.write_text(json.dumps({"tx": [{"txid": "ab" * 32, "hex": "aabb"}]}))
    monkeypatch.setattr(research, "RecordedRequests", Mock(side_effect=AssertionError("no HTTP")))
    result = research.analyze_size(source, output, {"txid": "ab" * 32, "input_indices": [0]}, "emulation", ANALYZER)
    assert result["source"] == "r1/data/btc-node/block_1.json"


def test_size_checks_indices_and_txid_before_recording_a_result(monkeypatch, workspace):
    source, output = workspace
    fake_transaction_library(monkeypatch)
    for settings in (
        {"input_indices": [2]},
        {"output_indices": [-1]},
        {"input_indices": [True]},
        {"input_indices": [0.0]},
        {"txid": "ff" * 32},
    ):
        with pytest.raises(ValueError):
            research.analyze_size(source, output, {"tx_hex": "aabb", **settings}, "mainnet", ANALYZER)
    assert not (output / "transaction.hex").exists()


@pytest.mark.skipif(not ANALYZER.is_dir(), reason="Sibling analyzer source is not installed")
def test_mainnet_size_can_use_the_original_online_lookup_with_recorded_response(monkeypatch, workspace):
    source, output = workspace
    fake_transaction_library(monkeypatch)
    urls = []

    def get(url, *, timeout):
        assert timeout == 30
        urls.append(url)
        return SimpleNamespace(text="aabb", status_code=200)

    monkeypatch.setitem(
        sys.modules,
        "requests",
        SimpleNamespace(Session=lambda: SimpleNamespace(get=get), RequestException=RuntimeError),
    )
    result = research.analyze_size(
        source, output, {"txid": "ab" * 32, "input_indices": [0], "allow_network": True}, "mainnet", ANALYZER
    )
    assert urls == ["https://mempool.space/api/tx/" + "ab" * 32 + "/hex"]
    assert result["source"] == "mempool.space"
    assert (output / "sources/requests.json").is_file()


@pytest.mark.skipif(not ANALYZER.is_dir(), reason="Sibling analyzer source is not installed")
def test_mainnet_refresh_calls_original_helper_and_retains_sources(monkeypatch, workspace):
    source, output = workspace
    existing = {"manual": {"known": "manual"}, "crawl_wasabist": {}, "crawl_wabisator": {}, "crawl_crocsapi": {}}
    mapping = source / "txid_coord.json"
    mapping.write_text(json.dumps(existing))
    responses = {
        "https://wabisator.com/db.json": {
            "coordinator": [{"txid": "known"}, {"txid": "new"}, {"txid": "bad"}, {"txid": "not broadcasted"}]
        },
        "https://mempool.space/api/tx/new": {},
        "https://mempool.space/api/tx/bad": {},
    }
    urls = []

    def get(url, *, timeout):
        assert timeout == 30
        urls.append(url)
        return SimpleNamespace(text=json.dumps(responses[url]), status_code=404 if url.endswith("/bad") else 200)

    monkeypatch.setitem(
        sys.modules,
        "requests",
        SimpleNamespace(Session=lambda: SimpleNamespace(get=get), RequestException=RuntimeError),
    )
    result = research.update_coordinators(
        source, output, {"allow_network": True, "coordinator_file": "txid_coord.json"}, "mainnet", ANALYZER
    )
    saved = json.loads((output / "txid_coord_updated.json").read_text())
    assert saved["crawl_wabisator"] == {"known": "coordinator", "new": "coordinator"}
    assert "https://mempool.space/api/tx/known" not in urls
    assert len(json.loads((output / "sources/requests.json").read_text())) == 3
    assert json.loads(mapping.read_text()) == existing
    assert result["updated_wabisator_txids"] == 2


def test_research_helpers_reject_mainnet_labels_and_emulation_network_access(workspace):
    source, output = workspace
    with pytest.raises(ValueError, match="ground truth"):
        research.generate_inputs(source, output, {}, "mainnet", ANALYZER, "r1", [])
    with pytest.raises(ValueError, match="mainnet"):
        research.update_coordinators(source, output, {"allow_network": True}, "emulation", ANALYZER)
    with pytest.raises(ValueError, match="raw transaction"):
        research.analyze_size(source, output, {"txid": "ab" * 32}, "emulation", ANALYZER)


@pytest.mark.parametrize("wallet", [None, "UNKNOWN", "Unknown", "", "other"])
def test_llm_refuses_missing_ground_truth_before_loading_the_generator(workspace, wallet):
    source, output = workspace
    (source / "r1").mkdir()
    (source / "r1/coinjoin_tx_info.json").write_text(
        json.dumps({"coinjoins": {"a": {"inputs": {"0": {"wallet_name": wallet}}, "outputs": {}}}})
    )
    with pytest.raises(ValueError, match="known wallet_name"):
        research.generate_inputs(source, output, {}, "emulation", ANALYZER, "r1", [])


def test_local_refresh_stages_its_helper_even_when_the_image_omits_scripts(monkeypatch, tmp_path):
    run = tmp_path / "r1"
    run.mkdir()
    config = PipelineConfiguration.from_mapping(
        {
            "action": "coinjoin-analysis",
            "run_dir": "r1",
            "coinjoin_analysis": {
                "program": "update_txids_with_wabisator",
                "data_mode": "mainnet",
                "research": {"allow_network": True},
            },
        }
    )
    config.validate()

    def execute(argv, **kwargs):
        work = Path(argv[argv.index("--volume") + 1].removesuffix(":/work:rw"))
        assert (work / "coinjoin_research.py").is_file()
        assert (work / "wabisator_source.py").is_file()
        assert (work / "input").is_dir()
        assert "--settings" in argv
        kwargs["stdout"].write("mock refresh\n")

    monkeypatch.setattr("coinjoin_pipeline.execution.coinjoin_tools.subprocess.run", execute)
    output = run_local(config, run)
    assert (output / "analysis.log").read_text() == "mock refresh\n"


@pytest.mark.skipif(not ANALYZER.is_dir(), reason="Sibling analyzer source is not installed")
def test_packaged_updater_preserves_the_original_source():
    source = Path(__file__).resolve().parents[2] / "src/coinjoin_pipeline/execution/pbs/templates/wabisator_source.txt"
    assert source.read_bytes() == (ANALYZER / "scripts/update_txids_with_wabisator.py").read_bytes()


@pytest.mark.skipif(not ANALYZER.is_dir(), reason="Sibling analyzer source is not installed")
def test_original_llm_generator_saves_vectors_without_running_its_unfinished_main(monkeypatch, workspace):
    source, output = workspace
    generator_source = (ANALYZER / "src/cj_process/generate_llm_inputs.py").read_text()
    ast.parse(generator_source.partition("\ndef compute_distance_single_wallet(")[0], feature_version=(3, 11))
    for module in ("numpy", "seaborn", "matplotlib", "matplotlib.pyplot"):
        monkeypatch.setitem(sys.modules, module, ModuleType(module))
    # Use the actual link function as well; plotting libraries are not needed for JSON.
    tree = ast.parse((ANALYZER / "src/cj_process/cj_analysis.py").read_text())
    link = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "compute_link_between_inputs_and_outputs"
    )
    namespace = {"math": math, "logging": logging}
    exec(compile(ast.Module(body=[link], type_ignores=[]), "upstream_link", "exec"), namespace)
    monkeypatch.setitem(
        sys.modules,
        "cj_process.cj_analysis",
        SimpleNamespace(
            btc_to_sats=lambda value: int(Decimal(str(value)) * 100_000_000),
            compute_link_between_inputs_and_outputs=namespace[link.name],
        ),
    )
    dataset = {
        "coinjoins": {
            "a": {
                "txid": "a",
                "broadcast_time": "1",
                "inputs": {"0": {"txid": "funding", "address": "in", "value": 1, "wallet_name": "wallet-1"}},
                "outputs": {"0": {"address": "middle", "value": 1, "wallet_name": "wallet-1"}},
            },
            "b": {
                "txid": "b",
                "broadcast_time": "2",
                "inputs": {"0": {"txid": "a", "address": "middle", "value": 1, "wallet_name": "wallet-1"}},
                "outputs": {"0": {"address": "out", "value": 1, "wallet_name": "wallet-1"}},
            },
        }
    }
    path = source / "r1/coinjoin_tx_info.json"
    path.parent.mkdir()
    path.write_text(json.dumps(dataset))
    result = research.generate_inputs(source, output, {"min_wallets": 1, "depth": 2}, "emulation", ANALYZER, "r1", [])
    events = json.loads((output / "llm_inputs.json").read_text())
    assert result["vectors"] == 2
    assert events["a"][0]["first_input_wallet"] == "wallet-1"
    assert events["a"][0]["last_output_wallet"] == "wallet-1"
    assert events["a"][0]["first_input_wallet_mix_leave_distribution"] == {"1": 1}
    assert json.loads(path.read_text()) == dataset


def test_pbs_embeds_adapter_without_rebuilding_or_replacing_the_analyzer(tmp_path):
    import subprocess

    config = PipelineConfiguration.from_mapping(
        {
            "action": "pbs-from-s3",
            "run_id": "r1",
            "stages": {"analysis": True},
            "coinjoin_analysis": {"program": "analyze_tx_size", "research": {"tx_hex": "aabb", "input_indices": [0]}},
            "artifacts": {
                "uri": "s3://bucket/runs",
                "endpoint_url": "https://s3.example",
                "profile": "test",
                "credentials_file": "/tmp/credentials",
            },
        }
    )
    script = render_coinjoin_tools_s3_pbs(S3Target.from_args(config), "docker://test/image", config.coinjoin_analysis)
    assert "COINJOIN_RESEARCH_ADAPTER" in script
    assert "def analyze_size(" in script
    assert "python /work/coinjoin_research.py" in script
    assert 's5 sync "$RUN_PREFIX/coinjoin_emulator_data/data/*"' not in script
    # The adapter is valid on the analyzer image's Python 3.11 and exporters' 3.8 floor.
    ast.parse(Path(research.__file__).read_text(), feature_version=(3, 8))
    job = tmp_path / "job.sh"
    job.write_text(script)
    subprocess.run(["bash", "-n", str(job)], check=True)
