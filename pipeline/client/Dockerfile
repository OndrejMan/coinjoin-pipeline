FROM docker:29-cli

ARG S5CMD_VERSION=2.3.0
ARG TARGETARCH
RUN apk add --no-cache bash ca-certificates curl kubectl python3 py3-pip py3-rich tzdata \
    && arch="${TARGETARCH:-$(uname -m)}" \
    && case "$arch" in amd64|x86_64) s5arch=64bit ;; arm64|aarch64) s5arch=ARM64 ;; *) echo "unsupported architecture: $arch" >&2; exit 1 ;; esac \
    && curl -fsSL "https://github.com/peak/s5cmd/releases/download/v${S5CMD_VERSION}/s5cmd_${S5CMD_VERSION}_Linux-${s5arch}.tar.gz" \
      | tar -xz -C /usr/local/bin s5cmd \
    && s5cmd version

COPY client/wrapper.py /app/wrapper.py
COPY client/cli_options.py /app/client/cli_options.py
COPY client/kubernetes.py /app/client/kubernetes.py
COPY client/pipeline_logging.py /app/client/pipeline_logging.py
COPY client/runtime.py /app/client/runtime.py
COPY client/pbs.py /app/client/pbs.py
COPY client/artifacts.py /app/client/artifacts.py
COPY client/blocksci_template.sh client/coinjoin_analysis_template.sh client/mappings_template.sh /app/client/
COPY client/blocksci_s3_template.sh client/coinjoin_analysis_s3_template.sh client/unified_report_s3_template.sh /app/client/
COPY client/research.py /app/client/research.py
COPY client/run_catalog.py /app/client/run_catalog.py
COPY client/scenarios.py /app/client/scenarios.py
COPY exporters /app/exporters
COPY analysis.sh /analysis.sh
COPY recreate.sh /recreate.sh
COPY delete.sh /delete.sh
COPY compose.yaml /compose.yaml
COPY client/develop.sh /app/develop.sh
RUN chmod +x /analysis.sh /recreate.sh /delete.sh /app/develop.sh /app/wrapper.py

WORKDIR /app
ENTRYPOINT ["python3", "/app/wrapper.py"]
