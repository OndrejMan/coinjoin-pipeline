"""Defaults shared by the executable wrapper and its public CLI parser."""

VALID_DRIVERS = ("docker", "kubernetes")
DEFAULT_DRIVER = "docker"
DEFAULT_MIN_INPUT_COUNT: int | None = None
DEFAULT_JOINMARKET_DETECTOR = "definite"
DEFAULT_JOINMARKET_MIN_BASE_FEE = 5000
DEFAULT_JOINMARKET_PERCENTAGE_FEE = 0.00004
DEFAULT_JOINMARKET_MAX_DEPTH = 200000
DEFAULT_K8S_NAMESPACE = "coinjoin"
DEFAULT_K8S_IMAGE_PREFIX = "ghcr.io/ondrejman/"
DEFAULT_RUN_TIMEZONE = "Europe/Prague"
