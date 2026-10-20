"""The external comparison judges only heights BlockSci scanned (plan F3)."""

from coinjoin_pipeline.configuration import PipelineConfiguration
from coinjoin_pipeline.execution.pbs.commands import blocksci_external_report_pbs_command
from exporters.normalization import restrict_baseline_to_window

WINDOW = {"start_height": 100, "end_height": 200, "end_exclusive": True, "index_blocks": 201, "pin_height": 200}


def record(txid, height=7):
    # Dumplings stores the position in the block where the report expects a height.
    return {"txid": txid, "block_height": height}


def test_only_baseline_inside_the_window_is_compared_at_its_blocksci_height():
    records = {"in": record("in"), "below": record("below"), "above": record("above"), "absent": record("absent")}
    heights = {"in": 150, "below": 99, "above": 200, "absent": None}

    kept, coverage = restrict_baseline_to_window(records, WINDOW, heights)

    assert list(kept) == ["in"]
    assert kept["in"]["block_height"] == 150
    assert coverage["baseline_in_window"] == 1
    assert coverage["baseline_outside_window"] == 2
    assert coverage["baseline_not_in_index"] == 1
    assert coverage["baseline_total"] == 4


def test_without_a_window_the_baseline_is_untouched():
    records = {"a": record("a")}

    assert restrict_baseline_to_window(records, None, None) == (records, None)


def test_without_heights_the_report_says_so_instead_of_dropping_everything():
    records = {"a": record("a")}

    kept, coverage = restrict_baseline_to_window(records, WINDOW, None)

    assert kept == records
    assert coverage["baseline_heights_available"] is False


def test_the_external_command_passes_the_window_and_threshold():
    command = blocksci_external_report_pbs_command(
        "mainnet-compare",
        PipelineConfiguration.from_flat(
            {
                "coinjoin_type": "wasabi2",
                "min_input_count": 15,
                "blocksci_pin_height": 850000,
                "blocksci_scan_start_height": 741213,
                "blocksci_scan_end_height": 850001,
            }
        ),
    )

    assert "--min-input-count 15" in command
    assert command.endswith("--pin-height 850000 --scan-start-height 741213 --scan-end-height 850001")
