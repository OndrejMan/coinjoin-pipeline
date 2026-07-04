FROM docker:29-cli

RUN apk add --no-cache bash kubectl python3 py3-pip py3-rich tzdata

COPY client/wrapper.py /app/wrapper.py
COPY client/cli_options.py /app/client/cli_options.py
COPY client/kubernetes.py /app/client/kubernetes.py
COPY client/pipeline_logging.py /app/client/pipeline_logging.py
COPY client/runtime.py /app/client/runtime.py
COPY client/pbs.py /app/client/pbs.py
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
