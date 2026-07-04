"""Inner report integration diagnostics for unified reports."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Callable, Protocol, cast

from exporters.artifact_paths import emulator_dir
from exporters.common import (
    JsonObject,
    JsonValue,
    load_json,
    safe_attr,
    to_json_text,
)
from exporters.normalization import block_height_from_path, is_coinbase_tx

IMAGE_COMPONENTS = ("blocksci", "coinjoin_analysis", "coinjoin_emulator", "wrapper")
TARGET_DETAIL_LIMIT = 100


class Blockchain(Protocol):
    """BlockSci chain operations used by integration diagnostics."""

    def __len__(self) -> int: ...

    def tx_with_hash(self, txid: str) -> object: ...


class BlocksciModule(Protocol):
    """BlockSci module surface used by integration diagnostics."""

    @property
    def heuristics(self) -> object: ...

    def Blockchain(self, config_path: str) -> Blockchain: ...


def docker_image_provenance(image: str | None, runtime: str | None = None) -> JsonObject:
    if not image:
        return {"reference": image, "image_id": None, "repo_digest": None, "inspect_error": "image reference missing"}
    runtime_command = runtime or os.environ.get("CONTAINER_RUNTIME") or "docker"
    try:
        result = subprocess.run(
            [
                runtime_command,
                "image",
                "inspect",
                image,
                "--format",
                "{{.Id}}\n{{json .RepoDigests}}",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        return {
            "reference": image,
            "image_id": None,
            "repo_digest": None,
            "inspect_error": str(exc),
        }

    lines = result.stdout.splitlines()
    image_id = lines[0].strip() if lines else None
    repo_digest = None
    if len(lines) > 1:
        try:
            repo_digests = json.loads(lines[1])
        except json.JSONDecodeError:
            repo_digests = []
        if repo_digests:
            repo_digest = str(repo_digests[0])

    return {
        "reference": image,
        "image_id": image_id or None,
        "repo_digest": repo_digest,
        "inspect_error": None,
    }


def build_image_diagnostics(
    images: dict[str, str | None],
    image_ids: dict[str, str | None] | None = None,
    image_digests: dict[str, str | None] | None = None,
) -> tuple[dict[str, JsonObject], list[str]]:
    image_ids = image_ids or {}
    image_digests = image_digests or {}
    diagnostics: dict[str, JsonObject] = {}
    problems: list[str] = []

    for component in IMAGE_COMPONENTS:
        reference = images.get(component)
        inspected = docker_image_provenance(reference)
        image_id = image_ids.get(component) or to_json_text(inspected.get("image_id"))
        repo_digest = image_digests.get(component) or to_json_text(inspected.get("repo_digest"))
        component_problems = []
        if not reference:
            component_problems.append("missing image reference")
        if not image_id:
            component_problems.append("missing image id")
        if not repo_digest:
            component_problems.append("missing repo digest")
        inspect_error = inspected.get("inspect_error")
        if inspect_error and (not image_id or not repo_digest):
            component_problems.append(f"image inspect failed: {inspect_error}")

        status = "ok" if not component_problems else "not_ok"
        if component_problems:
            problems.append(f"{component} image provenance is incomplete: {', '.join(component_problems)}")
        diagnostics[component] = {
            "reference": reference,
            "image_id": image_id,
            "repo_digest": repo_digest,
            "status": status,
            "problems": component_problems,
        }

    return diagnostics, problems


def exported_block_targets(run_dir: Path) -> tuple[list[JsonObject], JsonObject]:
    block_dir = emulator_dir(run_dir) / "data" / "btc-node"
    targets: list[JsonObject] = []
    exported_heights: list[int] = []
    if not block_dir.exists():
        return targets, {
            "exported_block_count": 0,
            "max_exported_block_height": None,
            "block_dir": str(block_dir),
            "status": "not_ok",
        }

    for block_path in sorted(block_dir.glob("block_*.json"), key=lambda path: block_height_from_path(path) or -1):
        block = load_json(block_path)
        height = block.get("height")
        if height is None:
            height = block_height_from_path(block_path)
        if height is not None:
            exported_heights.append(int(height))

        for tx in block.get("tx", []):
            txid = to_json_text(tx.get("txid"))
            if not txid or is_coinbase_tx(tx):
                continue
            targets.append(
                {
                    "txid": txid,
                    "exported_block_height": int(height) if height is not None else None,
                    "source_block_file": block_path.name,
                }
            )

    return targets, {
        "exported_block_count": len(exported_heights),
        "max_exported_block_height": max(exported_heights) if exported_heights else None,
        "block_dir": str(block_dir),
        "status": "ok" if exported_heights else "not_ok",
    }


def normalize_joinmarket_detector_result(raw: JsonValue) -> str:
    if isinstance(raw, bool):
        return "true" if raw else "false"
    if isinstance(raw, int):
        return {0: "true", 1: "false", 2: "timeout"}.get(raw, f"unknown:{raw}")
    name = safe_attr(raw, "name")
    if name is not None:
        return str(name).lower()
    text = str(raw).lower()
    if text.endswith(".true") or text == "true":
        return "true"
    if text.endswith(".false") or text == "false":
        return "false"
    if text.endswith(".timeout") or text == "timeout":
        return "timeout"
    return text


def call_joinmarket_detector(
    blocksci_module: BlocksciModule,
    tx: object,
    detector: str,
    min_base_fee: int,
    percentage_fee: float,
    max_depth: int,
) -> str:
    heuristics = safe_attr(blocksci_module, "heuristics")
    coinjoin_heuristics = safe_attr(heuristics, "coinjoin", heuristics)
    method_name = "is_possible_coinjoin" if detector == "possible" else "is_definite_coinjoin"
    method = safe_attr(coinjoin_heuristics, method_name)
    if method is None:
        method = safe_attr(heuristics, method_name)
    if method is None:
        raise RuntimeError(f"BlockSci does not expose {method_name}")
    detector_factory = cast(Callable[[int, float, int], object], method)
    proxy_or_value = detector_factory(min_base_fee, percentage_fee, max_depth)
    value = proxy_or_value(tx) if callable(proxy_or_value) else proxy_or_value
    return normalize_joinmarket_detector_result(value)


def build_target_diagnostics(
    chain: Blockchain,
    blocksci_module: BlocksciModule,
    targets: list[JsonObject],
    blocksci_detected_txids: set[str],
    coinjoin_type: str,
    joinmarket_detector: str,
    joinmarket_min_base_fee: int,
    joinmarket_percentage_fee: float,
    joinmarket_max_depth: int,
) -> tuple[JsonObject, JsonObject, list[str]]:
    details: list[JsonObject] = []
    missing = []
    height_mismatches = []
    detector_checked = 0
    detector_agreements = 0
    detector_disagreements = []
    detector_timeouts = []
    detector_errors = []

    for target in targets:
        txid = str(target["txid"])
        exported_height = target.get("exported_block_height")
        detail: JsonObject = {
            "txid": txid,
            "exported_block_height": exported_height,
            "present_in_blocksci": False,
            "blocksci_block_height": None,
            "detected_by_bulk_filter": txid in blocksci_detected_txids,
        }
        tx = None
        try:
            tx = chain.tx_with_hash(txid)
            blocksci_txid = to_json_text(safe_attr(tx, "hash"))
            blocksci_height = safe_attr(tx, "block_height")
            detail["blocksci_block_height"] = int(str(blocksci_height)) if blocksci_height is not None else None
            detail["present_in_blocksci"] = blocksci_txid in {None, txid}
        except Exception as exc:
            detail["lookup_error"] = str(exc)

        if not detail["present_in_blocksci"]:
            missing.append(txid)
        elif exported_height is not None and detail.get("blocksci_block_height") != exported_height:
            height_mismatches.append(txid)

        if tx is not None and coinjoin_type == "joinmarket":
            detector_checked += 1
            try:
                detector_result = call_joinmarket_detector(
                    blocksci_module,
                    tx,
                    joinmarket_detector,
                    joinmarket_min_base_fee,
                    joinmarket_percentage_fee,
                    joinmarket_max_depth,
                )
                detail["direct_detector_result"] = detector_result
                direct_detected = detector_result == "true"
                detail["direct_detector_detected"] = direct_detected
                if detector_result == "timeout":
                    detector_timeouts.append(txid)
                if direct_detected == detail["detected_by_bulk_filter"]:
                    detector_agreements += 1
                else:
                    detector_disagreements.append(txid)
            except Exception as exc:
                detail["direct_detector_error"] = str(exc)
                detector_errors.append(txid)

        details.append(detail)

    target_status = "ok" if not missing and not height_mismatches else "not_ok"
    target_problems = []
    if missing:
        target_problems.append(f"{len(missing)} exported target txid(s) are missing from BlockSci")
    if height_mismatches:
        target_problems.append(f"{len(height_mismatches)} exported target txid(s) have BlockSci height mismatches")

    detector_available = coinjoin_type == "joinmarket"
    detector_status = "ok"
    detector_problems = []
    if detector_disagreements:
        detector_status = "not_ok"
        detector_problems.append(f"{len(detector_disagreements)} direct detector result(s) disagree with bulk filter")
    if detector_errors:
        detector_status = "not_ok"
        detector_problems.append(f"{len(detector_errors)} direct detector check(s) failed")
    if detector_timeouts:
        detector_status = "not_ok"
        detector_problems.append(f"{len(detector_timeouts)} direct detector check(s) timed out")
    if not detector_available:
        detector_status = "unavailable"

    return (
        {
            "total": len(targets),
            "present": len(targets) - len(missing),
            "missing": len(missing),
            "height_mismatches": len(height_mismatches),
            "status": target_status,
            "problems": target_problems,
            "details": details,
        },
        {
            "available": detector_available,
            "checked": detector_checked,
            "agreements": detector_agreements,
            "disagreements": len(detector_disagreements),
            "timeouts": len(detector_timeouts),
            "errors": len(detector_errors),
            "status": detector_status,
            "problems": detector_problems,
            "disagreement_txids": detector_disagreements[:TARGET_DETAIL_LIMIT],
            "timeout_txids": detector_timeouts[:TARGET_DETAIL_LIMIT],
            "error_txids": detector_errors[:TARGET_DETAIL_LIMIT],
        },
        target_problems + detector_problems,
    )


def build_chain_diagnostics(chain: Blockchain, exported_summary: JsonObject) -> tuple[JsonObject, list[str]]:
    problems = []
    block_count = len(chain)
    chain_height = block_count - 1 if block_count > 0 else None
    max_exported_height = exported_summary.get("max_exported_block_height")
    status = "ok"
    if max_exported_height is None:
        status = "not_ok"
        problems.append("No exported block files were found for integration diagnostics")
    elif chain_height != max_exported_height:
        status = "not_ok"
        problems.append(
            f"BlockSci chain height {chain_height} does not match max exported block height {max_exported_height}"
        )
    return (
        {
            "blocksci_block_count": block_count,
            "blocksci_chain_height": chain_height,
            **exported_summary,
            "status": status,
        },
        problems,
    )


def build_integration_diagnostics(
    run_dir: Path,
    config_path: Path,
    blocksci_module: BlocksciModule,
    blocksci_records: dict[str, JsonObject],
    coinjoin_type: str,
    images: dict[str, str | None],
    image_ids: dict[str, str | None] | None = None,
    image_digests: dict[str, str | None] | None = None,
    joinmarket_detector: str = "definite",
    joinmarket_min_base_fee: int = 5000,
    joinmarket_percentage_fee: float = 0.00004,
    joinmarket_max_depth: int = 200000,
) -> JsonObject:
    problems: list[str] = []
    image_diagnostics, image_problems = build_image_diagnostics(images, image_ids, image_digests)
    problems.extend(image_problems)

    targets, exported_summary = exported_block_targets(run_dir)
    try:
        chain = blocksci_module.Blockchain(str(config_path))
        chain_diagnostics, chain_problems = build_chain_diagnostics(chain, exported_summary)
        target_diagnostics, detector_diagnostics, target_problems = build_target_diagnostics(
            chain,
            blocksci_module,
            targets,
            set(blocksci_records),
            coinjoin_type,
            joinmarket_detector,
            joinmarket_min_base_fee,
            joinmarket_percentage_fee,
            joinmarket_max_depth,
        )
        problems.extend(chain_problems)
        problems.extend(target_problems)
    except Exception as exc:
        chain_diagnostics = {
            **exported_summary,
            "blocksci_block_count": None,
            "blocksci_chain_height": None,
            "status": "not_ok",
            "error": str(exc),
        }
        target_diagnostics = {
            "total": len(targets),
            "present": 0,
            "missing": len(targets),
            "height_mismatches": 0,
            "status": "not_ok",
            "problems": ["BlockSci chain could not be opened for target diagnostics"],
            "details": [],
        }
        detector_diagnostics = {
            "available": coinjoin_type == "joinmarket",
            "checked": 0,
            "agreements": 0,
            "disagreements": 0,
            "timeouts": 0,
            "errors": 0,
            "status": "not_ok",
            "problems": ["BlockSci chain could not be opened for detector diagnostics"],
            "disagreement_txids": [],
            "timeout_txids": [],
            "error_txids": [],
        }
        problems.append(f"BlockSci diagnostics failed: {exc}")

    status = "ok" if not problems else "not_ok"
    return {
        "status": status,
        "problems": problems,
        "images": image_diagnostics,
        "chain": chain_diagnostics,
        "target_txids": target_diagnostics,
        "detector": detector_diagnostics,
    }
