import pytest

from coinjoin_pipeline.arguments import load_configuration
from coinjoin_pipeline.context import resolve_configuration


@pytest.mark.parametrize(
    "flags",
    [
        ("--blocksci-script", "--blocksciScript"),
        ("--blocksciScript", "--blocksci-script"),
    ],
)
def test_option_aliases_follow_cli_order(flags):
    config = load_configuration(["analyze", "--run-dir", "run", flags[0], "old.py", flags[1], "new.py"])
    assert config.blocksci.script == "new.py"


@pytest.mark.parametrize("action", ["analyze", "export", "full-run"])
def test_analysis_actions_reject_unknown_coinjoin_type(action):
    with pytest.raises(SystemExit):
        load_configuration([action, "--coinjoin-type", "unknown"])


def test_repeated_values_are_resolved_only_once():
    config = load_configuration(
        [
            "emulate",
            "--engine",
            "wasabi",
            "--engine=joinmarket",
            "--run-id=old",
            "--run-id",
            "new",
        ]
    )
    assert config.engine == "joinmarket"
    assert config.run_id == "new"
    assert resolve_configuration(config).coinjoin_type == "joinmarket"
