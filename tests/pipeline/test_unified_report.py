import sys
import tempfile
import types
import unittest
from argparse import ArgumentTypeError
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[2] / "pipeline"
sys.path.insert(0, str(PROJECT_ROOT))

try:
    import blocksci as _blocksci
except ImportError:
    sys.modules["blocksci"] = types.SimpleNamespace(  # type: ignore[assignment]
        Blockchain=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("BlockSci is required for export_blocksci_records integration tests.")
        ),
        heuristics=types.SimpleNamespace(),
    )
else:
    _ = _blocksci

from exporters.blocksci_export import detector as unified_report
from exporters.blocksci_export.detector import export_blocksci_records
from exporters.common import save_json
from exporters.parameters import Detector  # noqa: E402
from exporters.parameters import parse_min_input_count


class UnifiedReportTest(unittest.TestCase):
    def test_wasabi_export_uses_raw_transaction_detector(self):
        class RawChain:
            def __init__(self):
                self.calls = []

            def __len__(self):
                return 10

            def filter_coinjoin_txes_raw(self, *args):
                self.calls.append(args)
                return []

        chain = RawChain()
        fake_blocksci = types.SimpleNamespace(Blockchain=lambda _config: chain)
        with mock.patch.object(unified_report, "blocksci", fake_blocksci):
            records = export_blocksci_records(Path("/tmp/config.json"), Detector("wasabi2", None))

        self.assertEqual(records, {})
        self.assertEqual(chain.calls, [(0, 10, "wasabi2")])

    def test_min_input_count_requires_a_positive_integer_or_default(self):
        self.assertIsNone(parse_min_input_count("default"))
        self.assertEqual(parse_min_input_count("7"), 7)
        for value in ("0", "-1", "not-a-number"):
            with self.subTest(value=value), self.assertRaises(ArgumentTypeError):
                parse_min_input_count(value)

    def test_save_json_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = Path(tmpdir) / "report.json"

            save_json(output_path, {"b": 1, "a": {"d": 4, "c": 3}})

            self.assertEqual(
                output_path.read_text(encoding="utf-8"),
                '{\n  "a": {\n    "c": 3,\n    "d": 4\n  },\n  "b": 1\n}\n',
            )


if __name__ == "__main__":
    unittest.main()
