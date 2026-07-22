import json
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def test_kubernetes_small_scenario_is_packaged_and_current() -> None:
    canonical = REPOSITORY_ROOT / "scenarios" / "overactive-k8s-small.json"
    packaged = (
        REPOSITORY_ROOT
        / "src"
        / "coinjoin_pipeline"
        / "resources"
        / "scenarios"
        / "overactive-k8s-small.json"
    )

    assert json.loads(packaged.read_text(encoding="utf-8")) == json.loads(
        canonical.read_text(encoding="utf-8")
    )
