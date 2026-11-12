"""PBS defaults shared by rendering and command builders."""

from dataclasses import dataclass
from typing import Literal, get_args


# MetaCentrum's scratch_local is a spinning disk on most nodes; the SSD scratch keeps
# downloading, checksumming and unpacking from bounding every stage.
PBSScratchType = Literal["ssd", "local"]
# The configuration takes the literal from here, so this module never imports it back.
PBS_SCRATCH_TYPES: tuple[str, ...] = get_args(PBSScratchType)


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
}

BLOCKSCI_IMAGE_PYTHON_COMMAND = (
    "PYTHONPATH=/blocksci/.venv/lib/python3.8/site-packages:/mnt/blocksci/blockscipy /usr/bin/python3"
)
