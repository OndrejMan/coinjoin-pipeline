#!/bin/sh
# The pipeline CLI straight from its sources, as runIt.sh runs a checkout without an install.
# A checkout mounted over COINJOIN_PIPELINE_HOME replaces the image's copy.
checkout="${COINJOIN_PIPELINE_HOME:-/workspace/coinjoin-pipeline}"
export PYTHONPATH="$checkout/src:$checkout/pipeline${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m coinjoin_pipeline.cli "$@"
