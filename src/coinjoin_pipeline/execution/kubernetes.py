"""Kubernetes for the emulation: kubectl access, the auth preflights of both drivers, and the S3 Job.

The S3 emulation Job's resources, probe, diagnostics and cleanup live here, next to the
kubectl helpers and the preflight that the shared-storage Kubernetes path uses too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from coinjoin_pipeline.execution.planned_quota import PLANNED_QUOTA_ANNOTATION, planned_quota_annotation
from coinjoin_pipeline.execution.probes import PROBE_QUEUED, PROBE_RUNNING, PROBE_TERMINAL, PROBE_UNKNOWN
from coinjoin_pipeline.paths import EXECUTION_ROOT, REQUIRED_EXPORTERS

S3_CONTROLLER_RESOURCE_REQUESTS = {"cpu": "250m", "memory": "512Mi"}
S3_CONTROLLER_RESOURCE_LIMITS = {"cpu": "1", "memory": "1Gi"}
S3_UPLOADER_RESOURCE_REQUESTS = {"cpu": "100m", "memory": "128Mi"}
S3_UPLOADER_RESOURCE_LIMITS = {"cpu": "500m", "memory": "512Mi"}
S3_JOB_TTL_SECONDS_AFTER_FINISHED = 3600
S3_JOB_START_TIMEOUT_SECONDS = 1800
S3_JOB_DEADLINE_GRACE_SECONDS = 60
# Every S3 emulation Job carries both labels; the emulator labels its own pods with the run ID.
S3_EMULATION_APP_LABEL = "coinjoin-s3"
RUN_ID_LABEL = "coinjoin.run-id"
# Who launched the run (kubernetes.owner); in a shared namespace it tells two people's runs apart.
OWNER_LABEL = "coinjoin.owner"
S3_JOB_OWNED_RESOURCE_TYPES = (
    "serviceaccount",
    "configmap",
    "role.rbac.authorization.k8s.io",
    "rolebinding.rbac.authorization.k8s.io",
)
CONTROLLER_FATAL_SUMMARY_MARKERS = (
    "Kubernetes CPU quota exhausted",
    "Terminating exception:",
    "is unreachable at",
)


def resolve_kubeconfig(value: str | None) -> Path:
    """The kubeconfig option, else the first file of $KUBECONFIG, else ~/.kube/config, as kubectl reads them."""
    if not value:
        value = next((path for path in os.environ.get("KUBECONFIG", "").split(os.pathsep) if path), None)
    return Path(value).expanduser().resolve() if value else Path.home() / ".kube" / "config"


def kubectl_command(kubeconfig: Path, *arguments: str, namespace: str | None = None) -> list[str]:
    """The kubectl command line against one kubeconfig, for callers that stream its output."""
    command = ["kubectl", "--kubeconfig", str(kubeconfig)]
    if namespace is not None:
        command += ["--namespace", namespace]
    return [*command, *arguments]


def kubectl(
    kubeconfig: Path, *arguments: str, namespace: str | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run kubectl against one kubeconfig; FileNotFoundError when kubectl is missing."""
    return subprocess.run(
        kubectl_command(kubeconfig, *arguments, namespace=namespace),
        input=input_text,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _preflight(kubeconfig: Path, *arguments: str, namespace: str | None = None) -> str:
    """Run one preflight command; a failure is a usage error, like a refused option."""
    try:
        completed = kubectl(kubeconfig, *arguments, namespace=namespace)
    except FileNotFoundError as error:
        raise ValueError("kubectl is required for the Kubernetes auth preflight") from error
    stdout, stderr = (completed.stdout or "").strip(), (completed.stderr or "").strip()
    if completed.returncode != 0:
        details = "".join(f"\n{text}" for text in (stdout, stderr) if text)
        raise ValueError(f"Kubernetes preflight command failed: {shlex.join(completed.args)}{details}")
    if stderr:
        print(f"[kubectl] {stderr}", file=sys.stderr)
    return stdout


def _require_permissions(kubeconfig: Path, checks: tuple[tuple[str, str], ...], namespace: str | None) -> None:
    for verb, resource in checks:
        output = _preflight(kubeconfig, "auth", "can-i", verb, resource, namespace=namespace)
        answer = next((line for line in reversed(output.splitlines()) if line.strip()), "")
        if answer.strip().lower() != "yes":
            raise ValueError(f"Kubernetes permission denied: can-i {verb} {resource} returned {output!r}")


def kubernetes_auth_preflight(kubeconfig_path: Path, namespace: str, reuse_namespace: bool) -> None:
    """Validate the permissions the emulator controller needs with this kubeconfig."""
    print(f"[kubernetes] Auth preflight using kubeconfig: {kubeconfig_path}")
    _preflight(kubeconfig_path, "get", "--raw=/version")
    pod_and_service = (("create", "pods"), ("create", "services"), ("delete", "pods"), ("delete", "services"))
    _require_permissions(kubeconfig_path, pod_and_service, namespace)
    if reuse_namespace:
        _preflight(kubeconfig_path, "get", "namespace", namespace)
        _require_permissions(
            kubeconfig_path,
            (("list", "pods"), ("list", "services"), ("delete", "pods"), ("delete", "services")),
            namespace,
        )
    else:
        _require_permissions(kubeconfig_path, (("create", "namespaces"), ("delete", "namespaces")), None)
    print(f"[kubernetes] Auth preflight OK for namespace={namespace}, reuse_namespace={reuse_namespace}")


def kubernetes_s3_auth_preflight(
    kubeconfig_path: Path,
    namespace: str,
    reuse_namespace: bool,
    secret_name: str | None,
) -> None:
    """Validate permissions and prerequisites for the S3-compatible emulation Job.

    With ``secret_name`` the Secret must already exist; without it the frontend creates a
    temporary one per run, so it needs the right to create, own and delete Secrets.
    """
    if not reuse_namespace:
        raise RuntimeError(
            "Kubernetes S3-compatible mode requires an existing namespace and Secret; pass kubernetes.reuse_namespace"
        )
    print(f"[kubernetes] S3-compatible auth preflight using kubeconfig: {kubeconfig_path}")
    _preflight(kubeconfig_path, "get", "--raw=/version")
    owned = (
        "serviceaccounts",
        "configmaps",
        "roles.rbac.authorization.k8s.io",
        "rolebindings.rbac.authorization.k8s.io",
    )
    _require_permissions(
        kubeconfig_path,
        (
            ("create", "jobs.batch"),
            *(("create", resource) for resource in owned),
            *(("patch", resource) for resource in owned),
            *(("delete", resource) for resource in owned),
            ("get", "jobs.batch"),
            ("delete", "jobs.batch"),
            ("list", "jobs.batch"),
            ("list", "pods"),
            *(
                (("create", "secrets"), ("list", "secrets"), ("patch", "secrets"), ("delete", "secrets"))
                if secret_name is None
                else ()
            ),
        ),
        namespace,
    )
    _preflight(kubeconfig_path, "get", "namespace", namespace)
    if secret_name is not None:
        # A missing Secret would leave the Job pod in CreateContainerConfigError
        # until the emulation timeout, so check it up front.
        _preflight(kubeconfig_path, "get", "secret", secret_name, namespace=namespace)
    print(f"[kubernetes] S3-compatible auth preflight OK for namespace={namespace}")


def require_unused_run_id(kubeconfig_path: Path, namespace: str, run_id: str, *, secrets: bool) -> None:
    """Refuse a run ID that already labels a Job, pod or (temporary) Secret in the namespace.

    The Kubernetes counterpart of the empty-run-prefix check in S3: a second run under the
    same ID would share its Job, Secret and emulator pods with the first one, and both the
    cleanup and the Lab's overview select by this label. It runs before anything is created.
    """
    kinds = "jobs.batch,pods" + (",secrets" if secrets else "")
    found = _preflight(
        kubeconfig_path, "get", kinds, "--selector", f"{RUN_ID_LABEL}={run_id}", "-o", "name", namespace=namespace
    )
    taken = [line.strip() for line in found.splitlines() if line.strip()]
    if taken:
        raise ValueError(
            f"run ID {run_id} is already in use in namespace {namespace} ({', '.join(taken)}); choose another run ID"
        )


def run_labels(run_id: str, owner: str | None = None) -> dict[str, str]:
    """The labels of every object of one S3 emulation run, its owner's included when known."""
    labels = {"app.kubernetes.io/name": S3_EMULATION_APP_LABEL, RUN_ID_LABEL: run_id}
    if owner:
        labels[OWNER_LABEL] = owner
    return labels


def run_selector(run_id: str, owner: str | None = None) -> str:
    """A selector for exactly the objects ``run_labels`` marks: another owner's never match.

    Without an owner it requires the label to be absent, so a run launched without
    ``kubernetes.owner`` never selects an owner's objects that carry the same run ID.
    """
    terms = [f"{key}={value}" for key, value in run_labels(run_id, owner).items()]
    return ",".join(terms if owner else [*terms, f"!{OWNER_LABEL}"])


def s3_emulation_job_name(run_id: str) -> str:
    """Derive a DNS-safe Job name that stays unique per run id.

    Lowercasing, separator folding, and truncation are all lossy, so distinct run
    ids (``test_1``/``test.1``/``Test-1``) would otherwise share a Job object and
    watch each other's status. The suffix restores uniqueness.
    """
    digest = hashlib.sha256(run_id.encode("utf-8")).hexdigest()[:8]
    slug = f"coinjoin-s3-{run_id}".lower().replace("_", "-").replace(".", "-")
    slug = slug[: 63 - len(digest) - 1].rstrip("-")
    return f"{slug}-{digest}"


# A pod that cannot start its containers keeps the Job "active" forever, so the
# Job conditions alone never end the wait. The in-pod watchdog covers the
# controller, but it cannot cover the image it runs in itself: an unpullable
# uploader image would otherwise block the frontend until emulation_timeout.
UNSTARTABLE_WAITING_REASONS = frozenset(
    {
        "ErrImagePull",
        "ImagePullBackOff",
        "InvalidImageName",
        "CreateContainerConfigError",
    }
)


def unstartable_pod_reason(pod: dict) -> str | None:
    """Return the fatal waiting reason of any container in ``pod``, if present."""
    status = pod.get("status") or {}
    for key in ("initContainerStatuses", "containerStatuses"):
        for container in status.get(key) or []:
            reason = ((container.get("state") or {}).get("waiting") or {}).get("reason")
            if reason in UNSTARTABLE_WAITING_REASONS:
                return f"{container.get('name')}: {reason}"
    return None


def _get_json(kubeconfig: Path, namespace: str, *arguments: str) -> tuple[dict | None, str]:
    """``kubectl get ... -o json`` parsed, or None and kubectl's error when it is inconclusive."""
    try:
        result = kubectl(kubeconfig, "get", *arguments, "-o", "json", namespace=namespace)
    except FileNotFoundError:
        return None, ""
    if result.returncode != 0:
        return None, result.stderr or ""
    try:
        return json.loads(result.stdout), ""
    except json.JSONDecodeError:
        return None, ""


def _pod_state(pods: list[dict], job_name: str) -> str:
    if not pods:
        return PROBE_QUEUED
    for pod in pods:
        reason = unstartable_pod_reason(pod)
        if reason:
            print(f"[kubernetes] Job {job_name} cannot start its containers ({reason})", file=sys.stderr)
            return PROBE_TERMINAL
    for pod in pods:
        status = pod.get("status") or {}
        if status.get("phase") == "Running":
            return PROBE_RUNNING
        for key in ("initContainerStatuses", "containerStatuses"):
            for container in status.get(key) or []:
                if (container.get("state") or {}).get("running") is not None:
                    return PROBE_RUNNING
    return PROBE_QUEUED


def kubernetes_job_probe(kubeconfig_path: Path, namespace: str, job_name: str) -> Callable[[], str]:
    """Build a kubectl-backed liveness probe for ``wait_for_s3_marker``.

    kubectl errors are inconclusive so polling continues on ``PROBE_UNKNOWN``.
    """
    job_seen = False

    def probe() -> str:
        nonlocal job_seen
        job, error = _get_json(kubeconfig_path, namespace, "job", job_name)
        if job is None:
            # Deleted after we saw it running means it terminated without markers.
            gone = "notfound" in error.lower().replace(" ", "")
            return PROBE_TERMINAL if gone and job_seen else PROBE_UNKNOWN
        job_seen = True
        for condition in (job.get("status") or {}).get("conditions") or []:
            if condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True":
                return PROBE_TERMINAL
        pods, _ = _get_json(kubeconfig_path, namespace, "pods", "--selector", f"job-name={job_name}")
        return PROBE_UNKNOWN if pods is None else _pod_state(pods.get("items") or [], job_name)

    return probe


def collect_s3_emulation_diagnostics(kubeconfig_path: Path, namespace: str, job_name: str) -> str:
    """Best-effort kubectl evidence for a failed or vanished emulation Job."""
    sections: list[str] = []
    for description, command in (
        ("job description", ("describe", "job", job_name)),
        ("controller logs", ("logs", f"job/{job_name}", "-c", "controller", "--tail=-1")),
        ("uploader logs", ("logs", f"job/{job_name}", "-c", "uploader", "--tail=100")),
    ):
        try:
            result = kubectl(kubeconfig_path, *command, namespace=namespace)
        except FileNotFoundError:
            return "kubectl unavailable; no Kubernetes diagnostics collected"
        output = (result.stdout or "").strip() or (result.stderr or "").strip() or "(no output)"
        if description == "controller logs":
            fatal_lines = [
                line
                for line in output.splitlines()
                if any(marker in line for marker in CONTROLLER_FATAL_SUMMARY_MARKERS)
            ]
            if fatal_lines:
                sections.append(f"--- controller failure summary ---\n{fatal_lines[-1]}")
        sections.append(f"--- {description} ---\n{output}")
    return "\n".join(sections)


def _delete(kubeconfig_path: Path, namespace: str, resource_type: str, name: str) -> bool:
    """Best-effort deletion; returns False only when kubectl itself is missing."""
    try:
        result = kubectl(
            kubeconfig_path, "delete", resource_type, name, "--ignore-not-found", "--wait=false", namespace=namespace
        )
    except FileNotFoundError:
        print(f"[WARN] kubectl unavailable; could not delete {resource_type}/{name}", file=sys.stderr)
        return False
    if result.returncode != 0:
        print(f"[WARN] failed to delete {resource_type}/{name}: {_output(result)}", file=sys.stderr)
    return True


def delete_s3_emulation_job(kubeconfig_path: Path, namespace: str, job_name: str) -> None:
    """Best-effort frontend cleanup after a failed or timed-out marker wait."""
    _delete(kubeconfig_path, namespace, "job", job_name)


def delete_s3_emulation_run_resources(
    kubeconfig_path: Path, namespace: str, selector: str, *, temporary_secret: bool = False
) -> None:
    """Best-effort rollback of a run's Job and support objects, selected by the run's labels.

    A selector rather than names: an object of the same name that another owner's run made
    does not carry this run's labels and is left alone.
    """
    kinds = ",".join(("job", *S3_JOB_OWNED_RESOURCE_TYPES, *(("secret",) if temporary_secret else ())))
    try:
        result = kubectl(
            kubeconfig_path,
            "delete",
            kinds,
            "--selector",
            selector,
            "--ignore-not-found",
            "--wait=false",
            namespace=namespace,
        )
    except FileNotFoundError:
        print(f"[WARN] kubectl unavailable; could not delete the run's resources ({selector})", file=sys.stderr)
        return
    if result.returncode:
        print(f"[WARN] failed to delete the run's resources ({selector}): {_output(result)}", file=sys.stderr)


def _owned_resource_types(temporary_secret: bool) -> tuple[str, ...]:
    return (*S3_JOB_OWNED_RESOURCE_TYPES, "secret") if temporary_secret else S3_JOB_OWNED_RESOURCE_TYPES


def create_temporary_s3_secret(
    kubeconfig_path: Path,
    namespace: str,
    run_id: str,
    access_key_id: str,
    secret_access_key: str,
    owner: str | None = None,
) -> str:
    """Create the run's own S3 credentials Secret, named like its Job, and return the name.

    The values go to kubectl on stdin, never on its command line, and ``create`` (unlike
    ``apply``) keeps no copy of them in an annotation. An existing Secret of that name is
    never replaced: it may belong to another run or user. The Job owns the Secret once it
    exists; ``delete_temporary_s3_secret`` removes it as soon as the upload is over.
    """
    name = s3_emulation_job_name(run_id)
    encoded = {
        key: base64.b64encode(value.encode("utf-8")).decode("ascii")
        for key, value in (("S3_ACCESS_KEY_ID", access_key_id), ("S3_SECRET_ACCESS_KEY", secret_access_key))
    }
    manifest = json.dumps(
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "type": "Opaque",
            "metadata": {
                "name": name,
                "namespace": namespace,
                "labels": run_labels(run_id, owner),
            },
            "data": encoded,
        }
    )
    try:
        created = kubectl(kubeconfig_path, "create", "-f", "-", namespace=namespace, input_text=manifest)
    except FileNotFoundError as error:
        raise RuntimeError("kubectl is required to create the temporary S3 Secret") from error
    if created.returncode and "AlreadyExists" in _output(created):
        raise RuntimeError(
            f"Secret {name} already exists in namespace {namespace}; it is not this run's, "
            "so it is left alone. Choose another run ID"
        )
    if created.returncode:
        raise RuntimeError(f"could not create the temporary S3 Secret {name}: {_output(created)}")
    print(f"[kubernetes] Created temporary S3 Secret {name}; it is deleted after the upload")
    return name


def delete_temporary_s3_secret(kubeconfig_path: Path, namespace: str, run_id: str, owner: str | None = None) -> None:
    """Best-effort removal of the run's temporary Secret; the Job's deletion removes it too.

    It selects by the run's labels, not by name alone, so a Secret of the same name that
    another run or user made is never removed.
    """
    selector = run_selector(run_id, owner)
    try:
        result = kubectl(
            kubeconfig_path,
            "delete",
            "secret",
            "--selector",
            selector,
            "--ignore-not-found",
            "--wait=false",
            namespace=namespace,
        )
    except FileNotFoundError:
        print(f"[WARN] kubectl unavailable; could not delete the temporary S3 Secret ({selector})", file=sys.stderr)
        return
    if result.returncode:
        print(f"[WARN] failed to delete the temporary S3 Secret ({selector}): {_output(result)}", file=sys.stderr)
    else:
        print(f"[kubernetes] Deleted temporary S3 Secret of run {run_id}")


# What the controller needs to create, watch and exec into the emulation pods.
S3_EMULATION_ROLE_RULES = (
    {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["create", "get", "list", "watch", "delete"]},
    {"apiGroups": [""], "resources": ["pods/log", "pods/status"], "verbs": ["get"]},
    {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]},
    {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list", "watch"]},
)
S3_EMULATION_POD_SECURITY = {
    "runAsNonRoot": True,
    "runAsUser": 1000,
    "runAsGroup": 1000,
    "fsGroup": 1000,
    "seccompProfile": {"type": "RuntimeDefault"},
}


def _env(name: str, value: str) -> dict:
    return {"name": name, "value": value}


def _field_env(name: str, path: str) -> dict:
    return {"name": name, "valueFrom": {"fieldRef": {"fieldPath": path}}}


def _secret_env(secret_name: str, key: str, *, optional: bool = False) -> dict:
    return {"name": key, "valueFrom": {"secretKeyRef": {"name": secret_name, "key": key, "optional": optional}}}


def _container(name: str, image: str, script: str, environment: list[dict], limits: tuple, mounts: dict) -> dict:
    """A non-root shell container; only the scenario volume is mounted read-only."""
    requests, maximum = limits
    return {
        "name": name,
        "image": image,
        "command": ["sh", "-c", script],
        "env": environment,
        "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
        "resources": {"requests": requests, "limits": maximum},
        "volumeMounts": [
            {"name": volume, "mountPath": path, **({"readOnly": True} if volume == "scenario" else {})}
            for volume, path in mounts.items()
        ],
    }


def _execution_script(*names: str) -> str:
    return "".join((EXECUTION_ROOT / name).read_text(encoding="utf-8") for name in names)


def render_s3_emulation_resources(
    *,
    namespace: str,
    run_id: str,
    scenario_json: str,
    engine: str,
    image_prefix: str,
    emulator_image: str,
    uploader_image: str,
    artifact_uri: str,
    endpoint_url: str,
    secret_name: str,
    emulation_timeout_seconds: int = 21600,
    scheduling_timeout_seconds: int = S3_JOB_START_TIMEOUT_SECONDS,
    distributor_startup_timeout: str | None = None,
    btc_node_image: str | None = None,
    kubernetes_image_pull_policy: str | None = None,
    btc_node_initial_block_count: str | None = None,
    owner: str | None = None,
    planned_quota: str | None = None,
) -> str:
    """Render a kubectl-compatible JSON resource list for in-cluster emulation."""
    name = s3_emulation_job_name(run_id)
    labels = run_labels(run_id, owner)
    btc_node_image_arg = ' --btc-node-image "$BTC_NODE_IMAGE"' if btc_node_image else ""
    controller = (
        'python manager.py --driver kubernetes --in-cluster --engine "$ENGINE" run '
        '--scenario /config/scenario.json --namespace "$NAMESPACE" --reuse-namespace '
        '--image-prefix "$IMAGE_PREFIX" --run-id "$RUN_ID" '
        '--btc-node-arg=-blocksxor=0 --download-btc-data "/app/logs/$RUN_ID/bitcoin_data" '
        "--controller-done-marker /app/logs/.controller.done "
        "--controller-failed-marker /app/logs/.controller.failed"
        f"{btc_node_image_arg}"
    )
    prefix_preflight = _execution_script("k8s_s3_access.sh", "k8s_s3_prefix_preflight.sh").replace(
        "@REQUIRED_EXPORTERS@", " ".join(REQUIRED_EXPORTERS)
    )
    uploader = _execution_script("k8s_s3_access.sh", "k8s_s3_uploader.sh").rstrip("\n")

    # The emulator makes this pod the owner of every pod and service it
    # creates, so Kubernetes removes them even when the controller is
    # killed before its own cleanup finishes.
    env: list[dict] = [
        _env("NAMESPACE", namespace),
        _env("RUN_ID", run_id),
        _env("ENGINE", engine),
        _env("IMAGE_PREFIX", image_prefix),
        _field_env("COINJOIN_OWNER_POD_NAME", "metadata.name"),
        _field_env("COINJOIN_OWNER_POD_UID", "metadata.uid"),
        _field_env("COINJOIN_OWNER_POD_NAMESPACE", "metadata.namespace"),
    ]
    # The controller runs in a separate container, so overrides set on the
    # frontend reach it only explicitly; the emulator validates each one.
    for variable, value in (
        ("COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT", distributor_startup_timeout),
        ("BTC_NODE_IMAGE", btc_node_image),
        ("KUBERNETES_IMAGE_PULL_POLICY", kubernetes_image_pull_policy),
        ("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT", btc_node_initial_block_count),
        # The emulator labels its pods and services with it (coinjoin.owner).
        ("COINJOIN_RUN_OWNER", owner),
    ):
        if value:
            env.append(_env(variable, value))
    artifact_env: list[dict] = [
        _env("RUN_ID", run_id),
        _env("ARTIFACT_URI", artifact_uri),
        _env("S3_ENDPOINT_URL", endpoint_url),
        _secret_env(secret_name, "S3_ACCESS_KEY_ID"),
        _secret_env(secret_name, "S3_SECRET_ACCESS_KEY"),
        _secret_env(secret_name, "S3_DEFAULT_REGION", optional=True),
    ]
    uploader_env: list[dict] = [
        _env("NAMESPACE", namespace),
        _env("EMULATION_TIMEOUT_SECONDS", str(emulation_timeout_seconds)),
        _field_env("POD_NAME", "metadata.name"),
        *artifact_env,
    ]
    small = (S3_UPLOADER_RESOURCE_REQUESTS, S3_UPLOADER_RESOURCE_LIMITS)
    metadata = {"name": name, "namespace": namespace, "labels": labels}
    job_metadata = (
        {**metadata, "annotations": {PLANNED_QUOTA_ANNOTATION: planned_quota_annotation(planned_quota)}}
        if planned_quota
        else metadata
    )
    resources: list[dict] = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": {"scenario.json": scenario_json}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": metadata,
            "rules": list(S3_EMULATION_ROLE_RULES),
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": metadata,
            "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": job_metadata,
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": (
                    scheduling_timeout_seconds + emulation_timeout_seconds + S3_JOB_DEADLINE_GRACE_SECONDS
                ),
                "ttlSecondsAfterFinished": S3_JOB_TTL_SECONDS_AFTER_FINISHED,
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": name,
                        "restartPolicy": "Never",
                        "securityContext": S3_EMULATION_POD_SECURITY,
                        "volumes": [
                            {"name": "artifacts", "emptyDir": {}},
                            {"name": "credentials", "emptyDir": {"medium": "Memory"}},
                            {"name": "scenario", "configMap": {"name": name}},
                        ],
                        "initContainers": [
                            _container(
                                "prefix-preflight",
                                uploader_image,
                                prefix_preflight,
                                artifact_env,
                                small,
                                {"credentials": "/credentials"},
                            )
                        ],
                        "containers": [
                            _container(
                                "controller",
                                emulator_image,
                                controller,
                                env,
                                (S3_CONTROLLER_RESOURCE_REQUESTS, S3_CONTROLLER_RESOURCE_LIMITS),
                                {"artifacts": "/app/logs", "scenario": "/config"},
                            ),
                            _container(
                                "uploader",
                                uploader_image,
                                uploader,
                                uploader_env,
                                small,
                                {"artifacts": "/artifacts", "credentials": "/credentials"},
                            ),
                        ],
                    },
                },
            },
        },
    ]
    # No Namespace: S3 mode requires kubernetes.reuse_namespace; the namespace must already exist.
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}, indent=2)


def apply_s3_emulation_resources(manifest: str, kubeconfig_path: Path, *, temporary_secret: bool = False) -> None:
    """Apply the resource list, then make the Job the owner of its support objects.

    Owner references let Kubernetes collect the ServiceAccount, ConfigMap, Role,
    RoleBinding and a temporary S3 Secret with the Job; until they are in place a
    failure removes them here.
    """
    payload = json.loads(manifest)
    jobs = [item for item in payload.get("items", []) if item.get("kind") == "Job"]
    if len(jobs) != 1:
        raise RuntimeError("S3 Kubernetes manifest must contain exactly one Job")
    metadata = jobs[0].get("metadata") or {}
    job_name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not job_name or not namespace:
        raise RuntimeError("S3 Kubernetes Job must declare metadata.name and namespace")
    labels = metadata.get("labels") or {}
    run_id = labels.get(RUN_ID_LABEL)
    if not run_id:
        raise RuntimeError(f"S3 Kubernetes Job must carry the {RUN_ID_LABEL} label")
    selector = run_selector(run_id, labels.get(OWNER_LABEL))

    def roll_back(message: str) -> RuntimeError:
        delete_s3_emulation_run_resources(kubeconfig_path, namespace, selector, temporary_secret=temporary_secret)
        return RuntimeError(message)

    # The manifest orders the support resources before the Job, so a partial
    # apply can leave them behind with no Job to own them.
    applied = subprocess.run(
        kubectl_command(kubeconfig_path, "apply", "-f", "-"), input=manifest, text=True, check=False
    )
    if applied.returncode:
        raise roll_back(f"kubectl apply failed with exit {applied.returncode}")
    uid_result = kubectl(kubeconfig_path, "get", "job", job_name, "-o", "jsonpath={.metadata.uid}", namespace=namespace)
    job_uid = (uid_result.stdout or "").strip()
    if uid_result.returncode or not job_uid:
        raise roll_back(f"could not read Kubernetes Job UID for lifecycle ownership: {_output(uid_result)}")
    owner = {"apiVersion": "batch/v1", "kind": "Job", "name": job_name, "uid": job_uid}
    owner_patch = json.dumps(
        {"metadata": {"ownerReferences": [{**owner, "controller": True, "blockOwnerDeletion": False}]}}
    )
    for resource_type in _owned_resource_types(temporary_secret):
        patched = kubectl(
            kubeconfig_path, "patch", resource_type, job_name, "--type=merge", "-p", owner_patch, namespace=namespace
        )
        if patched.returncode:
            raise roll_back(f"could not attach Job ownership to {resource_type}/{job_name}: {_output(patched)}")
