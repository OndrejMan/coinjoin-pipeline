"""PBS defaults shared by rendering, submission, and command builders."""

DEFAULT_BLOCKSCI_NCPUS = 8
DEFAULT_BLOCKSCI_MEM = "64gb"
DEFAULT_BLOCKSCI_SCRATCH = "100gb"
DEFAULT_BLOCKSCI_WALLTIME = "24:00:00"

DEFAULT_COINJOIN_ANALYSIS_NCPUS = 4
DEFAULT_COINJOIN_ANALYSIS_MEM = "16gb"
DEFAULT_COINJOIN_ANALYSIS_SCRATCH = "50gb"
DEFAULT_COINJOIN_ANALYSIS_WALLTIME = "04:00:00"

DEFAULT_UNIFIED_REPORT_NCPUS = 2
DEFAULT_UNIFIED_REPORT_MEM = "8gb"
DEFAULT_UNIFIED_REPORT_SCRATCH = "10gb"
DEFAULT_UNIFIED_REPORT_WALLTIME = "01:00:00"

DEFAULT_BLOCKSCI_IMAGE = "docker://ghcr.io/ondrejman/blocksci-complete:latest"
DEFAULT_COINJOIN_ANALYSIS_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-analysis:latest"
DEFAULT_MAPPINGS_ENUMERATOR_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-mappings-enumerator:latest"
DEFAULT_SAKE_IMAGE = "docker://ghcr.io/ondrejman/coinjoin-mappings-sake:latest"
BLOCKSCI_IMAGE_PYTHON_COMMAND = (
    "PYTHONPATH=/blocksci/.venv/lib/python3.8/site-packages:/mnt/blocksci/blockscipy "
    "/usr/bin/python3"
)

POLL_INTERVAL_SECONDS = 30
# How much of a failed stage's job log to echo, and how long to wait for PBS
# to copy that log back after the marker appears.
STAGE_LOG_TAIL_LINES = 80
STAGE_LOG_SETTLE_SECONDS = 15
# Single source of truth for qstat job_state handling. "X" is emitted for
# finished subjobs (and by some PBS Pro builds for expired jobs), so it is
# terminal here as well as in the watcher -- previously the watcher treated it
# as terminal while pbs_job_probe/wait_for_pbs_marker raised on it.
# src/coinjoin_pipeline/watch.py cannot import this module (pipeline/ is a
# subprocess runtime root, not a packaged module), so it keeps a copy that
# tests/pipeline/test_pbs.py::PBSStateSetParityTest pins to these values.
PBS_TERMINAL_STATES = {"C", "F", "X"}
PBS_QUEUED_STATES = {"H", "Q", "W"}
PBS_ACTIVE_STATES = {"B", "E", "M", "R", "S", "T", "U"} | PBS_QUEUED_STATES
PBS_QUEUE_MARGIN_SECONDS = 60 * 60
