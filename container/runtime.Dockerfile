# The pipeline as a runtime image: Python, the pipeline's dependencies and its sources in
# /workspace/coinjoin-pipeline, run from there by /usr/local/bin/coinjoin-pipeline as
# runIt.sh runs a checkout. Images that drive the pipeline (cj-frontend) start from it.
#
# The sources stay a plain tree, not an installed wheel, so a checkout mounted over
# /workspace/coinjoin-pipeline replaces the code and keeps the dependencies.
FROM python:3.12-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    COINJOIN_PIPELINE_HOME=/workspace/coinjoin-pipeline

# git: a mounted checkout records its own commit and state in the run's manifest.
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# The runtime dependencies of pyproject.toml only; the package itself is not installed.
COPY pyproject.toml /tmp/pyproject.toml
RUN python3 -c 'import tomllib; print("\n".join(tomllib.load(open("/tmp/pyproject.toml", "rb"))["project"]["dependencies"]))' \
      > /tmp/requirements.txt \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm /tmp/pyproject.toml /tmp/requirements.txt

COPY pyproject.toml README.md runIt.sh /workspace/coinjoin-pipeline/
COPY src /workspace/coinjoin-pipeline/src
COPY pipeline /workspace/coinjoin-pipeline/pipeline
COPY scenarios /workspace/coinjoin-pipeline/scenarios
COPY examples /workspace/coinjoin-pipeline/examples
COPY container /workspace/coinjoin-pipeline/container
COPY scripts /workspace/coinjoin-pipeline/scripts
# scenarios/lab, examples/lab and coinjoin-runs are where a driver writes; it mounts them.
RUN mkdir -p /workspace/coinjoin-pipeline/scenarios/lab /workspace/coinjoin-pipeline/examples/lab \
      /workspace/coinjoin-pipeline/coinjoin-runs \
    && chmod -R a+rX /workspace/coinjoin-pipeline
COPY --chmod=0755 container/coinjoin-pipeline.sh /usr/local/bin/coinjoin-pipeline
RUN ln -s coinjoin-pipeline /usr/local/bin/cjp && coinjoin-pipeline version

# The commit the image was built from; the manifest uses it when the tree has no .git.
# Empty (a local build) records the source as unknown rather than a guess.
ARG REVISION=
ENV COINJOIN_PIPELINE_GIT_COMMIT=$REVISION
LABEL org.opencontainers.image.source=https://github.com/OndrejMan/coinjoin-pipeline \
      org.opencontainers.image.revision=$REVISION

WORKDIR /workspace/coinjoin-pipeline
CMD ["coinjoin-pipeline", "--help"]
