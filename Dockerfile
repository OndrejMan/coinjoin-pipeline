# Heavy pipeline-wrapper image. The separately installed Python package remains thin.
FROM docker:29-cli

ARG S5CMD_VERSION=2.3.0
ARG TARGETARCH
RUN apk add --no-cache bash ca-certificates curl kubectl python3 py3-pip py3-rich tzdata \
    && arch="${TARGETARCH:-$(uname -m)}" \
    && case "$arch" in amd64|x86_64) s5arch=64bit ;; arm64|aarch64) s5arch=ARM64 ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac \
    && curl -fsSL "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-${s5arch}.tar.gz" \
      | tar -xz -C /usr/local/bin s5cmd \
    && s5cmd version

COPY pipeline/client/wrapper.py /app/wrapper.py
COPY pipeline/client/cli_options.py /app/client/cli_options.py
COPY pipeline/client/kubernetes.py /app/client/kubernetes.py
COPY pipeline/client/pipeline_logging.py /app/client/pipeline_logging.py
COPY pipeline/client/runtime.py /app/client/runtime.py
COPY pipeline/client/pbs.py /app/client/pbs.py
COPY pipeline/client/artifacts.py /app/client/artifacts.py
COPY pipeline/client/blocksci_template.sh pipeline/client/coinjoin_analysis_template.sh pipeline/client/mappings_template.sh /app/client/
COPY pipeline/client/blocksci_s3_template.sh pipeline/client/coinjoin_analysis_s3_template.sh pipeline/client/unified_report_s3_template.sh /app/client/
COPY pipeline/client/research.py /app/client/research.py
COPY pipeline/client/run_catalog.py /app/client/run_catalog.py
COPY pipeline/client/scenarios.py /app/client/scenarios.py
COPY pipeline/exporters /app/exporters
COPY scenarios /app/scenarios
COPY pipeline/analysis.sh /analysis.sh
COPY pipeline/recreate.sh /recreate.sh
COPY pipeline/delete.sh /delete.sh
COPY pipeline/compose.yaml /compose.yaml
COPY pipeline/client/develop.sh /app/develop.sh

RUN chmod +x /analysis.sh /recreate.sh /delete.sh /app/develop.sh /app/wrapper.py

WORKDIR /app
ENTRYPOINT ["python3", "/app/wrapper.py"]
