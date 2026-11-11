"""What a probe of a remote job reports while the frontend waits for that job's S3 marker.

A PBS job (``pbs.submission.pbs_job_probe``) and the Kubernetes emulation Job
(``kubernetes.kubernetes_job_probe``) both answer with one of these states;
``s3_markers.wait_for_s3_marker`` decides its deadlines by them.
"""

PROBE_RUNNING = "running"
PROBE_QUEUED = "queued"
PROBE_TERMINAL = "terminal"
PROBE_UNKNOWN = "unknown"
