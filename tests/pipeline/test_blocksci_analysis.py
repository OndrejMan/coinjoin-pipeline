import argparse
import json
import sys
import types
from pathlib import Path
from unittest import mock

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "pipeline"))

from exporters.blocksci_export.analysis import write_analysis  # noqa: E402
from exporters.normalization import load_first_wasabi2_block  # noqa: E402
from exporters.blocksci_export.detector import (
    assert_real_blocksci,  # noqa: E402
    import_blocksci_bindings,  # noqa: E402
)


def test_write_analysis_persists_all_heavy_results(tmp_path: Path) -> None:
    run_dir = tmp_path / "run-1"
    config = run_dir / "blocksci_data" / "config.json"
    config.parent.mkdir(parents=True)
    config.write_text("{}", encoding="utf-8")
    args = argparse.Namespace(
        run_dir=run_dir,
        config=config,
        coinjoin_type="wasabi2",
        min_input_count=10,
    )
    fake_blocksci = mock.Mock()
    with (
        mock.patch("exporters.blocksci_export.analysis.blocksci", fake_blocksci),
        mock.patch(
            "exporters.blocksci_export.analysis.export_blocksci_records",
            return_value={"tx": {"txid": "tx"}},
        ),
        mock.patch(
            "exporters.blocksci_export.analysis.load_first_wasabi2_block",
            return_value=850237,
        ),
    ):
        output = write_analysis(args)

    artifact = json.loads(output.read_text(encoding="utf-8"))
    assert artifact["records"] == {"tx": {"txid": "tx"}}


def test_assert_real_blocksci_accepts_the_real_bindings() -> None:
    module = types.SimpleNamespace(Blockchain=object, heuristics=object())
    assert assert_real_blocksci(module) is None


def test_import_blocksci_bindings_ignores_checkout_namespace(
    tmp_path: Path,
) -> None:
    source_package = tmp_path / "blocksci" / "blockscipy" / "blocksci"
    source_package.mkdir(parents=True)
    (source_package / "__init__.py").touch()
    original_path = sys.path[:]
    fake_blocksci = types.SimpleNamespace(Blockchain=object, heuristics=object())

    def import_module(name: str) -> object:
        assert name == "blocksci"
        assert str(tmp_path) not in sys.path
        return fake_blocksci

    try:
        sys.path.insert(0, str(tmp_path))
        with mock.patch(
            "exporters.blocksci_export.detector.importlib.import_module",
            side_effect=import_module,
        ):
            assert import_blocksci_bindings() is fake_blocksci
        assert sys.path[0] == str(tmp_path)
    finally:
        sys.path[:] = original_path


def test_assert_real_blocksci_ignores_a_missing_installation() -> None:
    assert assert_real_blocksci(None) is None


def test_assert_real_blocksci_rejects_a_shadowing_directory() -> None:
    shadow = types.ModuleType("blocksci")
    shadow.__file__ = None
    shadow.__path__ = ["/mnt/exporters/blocksci"]  # type: ignore[attr-defined]

    with pytest.raises(ImportError) as excinfo:
        assert_real_blocksci(shadow)

    message = str(excinfo.value)
    assert "/mnt/exporters/blocksci" in message
    assert "Blockchain" in message
    assert "heuristics" in message


class _PrettyPrintedAddress:
    """A BlockSci address whose str() wraps the bare address in its type name."""

    def __init__(self, address_string: str, type_name: str) -> None:
        self.address_string = address_string
        self._type_name = type_name

    def __str__(self) -> str:
        return f"{self._type_name}({self.address_string})"


class _AddressWithoutString:
    def __str__(self) -> str:
        return "OpReturn()"


def test_io_records_carry_the_bare_address_for_every_script_type() -> None:
    from exporters.blocksci_export.detector import _base_io_record

    taproot = types.SimpleNamespace(
        index=2,
        value=2097152,
        address=_PrettyPrintedAddress("bcrt1pguuun", "WitnessUnknownAddress"),
    )
    opreturn = types.SimpleNamespace(index=0, value=0, address=_AddressWithoutString())
    missing = types.SimpleNamespace(index=1, value=5, address=None)

    assert _base_io_record(taproot, 9)["address"] == "bcrt1pguuun"
    assert _base_io_record(opreturn, 9)["address"] == "OpReturn()"
    assert _base_io_record(missing, 9)["address"] is None


@pytest.mark.parametrize(("network", "height"), [("bitcoin_regtest", 0), ("bitcoin", 741213)])
def test_first_wasabi2_block_comes_from_the_blocksci_chain_configuration(tmp_path, network, height):
    """The layout ``blocksci_parser generate-config`` writes, with its per-network start height."""
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps(
            {
                "chainConfig": {
                    "coinName": network,
                    "coinJoinConfiguration": {"FirstWasabi2Block": height, "FirstWasabiBlock": 0},
                },
                "parser": {},
                "version": 6,
            }
        ),
        encoding="utf-8",
    )
    assert load_first_wasabi2_block(config) == height


def test_first_wasabi2_block_is_required(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"chainConfig": {}, "FirstWasabi2Block": 5}), encoding="utf-8")
    with pytest.raises(ValueError, match="chainConfig.coinJoinConfiguration.FirstWasabi2Block"):
        load_first_wasabi2_block(config)
