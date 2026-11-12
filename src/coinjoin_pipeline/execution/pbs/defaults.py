"""PBS defaults shared by rendering, submission, and command builders."""

from dataclasses import dataclass

from coinjoin_pipeline.images import DEFAULT_VERSION, IMAGE_NAMES

# MetaCentrum's scratch_local is a spinning disk on most nodes; the SSD scratch keeps
# downloading, checksumming and unpacking from bounding every stage.
PBS_SCRATCH_TYPES = ("ssd", "local")
# MetaCentrum multijob scratch locations (`-l select=...:multijob=<location>`).
PBS_MULTIJOB_LOCATIONS = ("brno", "plzen", "global")


@dataclass(frozen=True)
class PBSResources:
    """Scheduler resources of one PBS job."""

    ncpus: int
    mem: str
    scratch: str
    walltime: str
    scratch_type: str = "ssd"


# Keyed by resource group: the PBS budget every stage of that group is drawn from.
DEFAULT_RESOURCES = {
    "blocksci": PBSResources(8, "64gb", "100gb", "24:00:00"),
    "analysis": PBSResources(4, "16gb", "50gb", "04:00:00"),
    "mappings": PBSResources(4, "16gb", "50gb", "04:00:00"),
    "unified_report": PBSResources(2, "8gb", "10gb", "01:00:00"),
}

DEFAULT_PBS_IMAGES = {
    component: f"docker://{IMAGE_NAMES[name]}:{DEFAULT_VERSION}"
    for component, name in (
        ("blocksci", "blocksci"),
        ("coinjoin_analysis", "coinjoin_analysis"),
        ("mappings_enumerator", "mappings"),
        ("sake", "sake"),
    )
}
BLOCKSCI_IMAGE_PYTHON_COMMAND = (
    "PYTHONPATH=/blocksci/.venv/lib/python3.8/site-packages:/mnt/blocksci/blockscipy /usr/bin/python3"
)

# qstat job_state values; status.job_phase classifies every state by them. "X" marks
# finished subjobs (and, in some PBS Pro builds, expired jobs), so it is terminal.
PBS_TERMINAL_STATES = {"C", "F", "X"}
PBS_QUEUED_STATES = {"H", "Q", "W"}
PBS_ACTIVE_STATES = {"B", "E", "M", "R", "S", "T", "U"} | PBS_QUEUED_STATES
PBS_QUEUE_MARGIN_SECONDS = 60 * 60
