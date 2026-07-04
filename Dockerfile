# Heavy pipeline-wrapper image. The separately installed Python package remains thin.
FROM docker:29-cli

RUN apk add --no-cache bash kubectl python3 py3-pip py3-rich tzdata

COPY pipeline/client/wrapper.py /app/wrapper.py
COPY pipeline/client/cli_options.py /app/client/cli_options.py
COPY pipeline/client/kubernetes.py /app/client/kubernetes.py
COPY pipeline/client/pipeline_logging.py /app/client/pipeline_logging.py
COPY pipeline/client/runtime.py /app/client/runtime.py
COPY pipeline/client/pbs.py /app/client/pbs.py
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
