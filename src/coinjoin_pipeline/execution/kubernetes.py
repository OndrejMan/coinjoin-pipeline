"""Kubernetes access preflight for the emulation pipeline."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from coinjoin_pipeline.paths import EXECUTION_ROOT
from coinjoin_pipeline.storage.s3 import (
    PROBE_QUEUED,
    PROBE_RUNNING,
    PROBE_TERMINAL,
    PROBE_UNKNOWN,
    REQUIRED_EXPORTERS,
)

S3_CONTROLLER_RESOURCE_REQUESTS = {"cpu": "250m", "memory": "512Mi"}
S3_CONTROLLER_RESOURCE_LIMITS = {"cpu": "1", "memory": "1Gi"}
S3_UPLOADER_RESOURCE_REQUESTS = {"cpu": "100m", "memory": "128Mi"}
S3_UPLOADER_RESOURCE_LIMITS = {"cpu": "500m", "memory": "512Mi"}
S3_JOB_TTL_SECONDS_AFTER_FINISHED = 3600
S3_JOB_START_TIMEOUT_SECONDS = 1800
S3_JOB_DEADLINE_GRACE_SECONDS = 60
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
    """Resolve an optional kubeconfig argument using the established default."""
    return Path(value).expanduser().resolve() if value else Path.home() / ".kube" / "config"


def kubectl(
    kubeconfig: Path, *arguments: str, namespace: str | None = None, input_text: str | None = None
) -> subprocess.CompletedProcess[str]:
    """Run kubectl against one kubeconfig; FileNotFoundError when kubectl is missing."""
    command = ["kubectl", "--kubeconfig", str(kubeconfig)]
    if namespace is not None:
        command += ["--namespace", namespace]
    return subprocess.run(
        [*command, *arguments],
        input=input_text,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or "").strip()


def _preflight(kubeconfig: Path, *arguments: str, namespace: str | None = None) -> str:
    """Run one preflight command and exit with the usage code when it fails."""
    try:
        completed = kubectl(kubeconfig, *arguments, namespace=namespace)
    except FileNotFoundError:
        print("[ERROR] kubectl is required for the Kubernetes auth preflight.", file=sys.stderr)
        sys.exit(2)
    stdout, stderr = (completed.stdout or "").strip(), (completed.stderr or "").strip()
    if completed.returncode != 0:
        print(f"[ERROR] Kubernetes preflight command failed: {shlex.join(completed.args)}", file=sys.stderr)
        for text in (stdout, stderr):
            if text:
                print(text, file=sys.stderr)
        sys.exit(2)
    if stderr:
        print(f"[kubectl] {stderr}", file=sys.stderr)
    return stdout


def _require_permissions(kubeconfig: Path, checks: tuple[tuple[str, str], ...], namespace: str | None) -> None:
    for verb, resource in checks:
        output = _preflight(kubeconfig, "auth", "can-i", verb, resource, namespace=namespace)
        answer = next((line for line in reversed(output.splitlines()) if line.strip()), "")
        if answer.strip().lower() != "yes":
            print(f"[ERROR] Kubernetes permission denied: can-i {verb} {resource} returned {output!r}", file=sys.stderr)
            sys.exit(2)


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
    secret_name: str,
) -> None:
    """Validate permissions and prerequisites for the S3-compatible emulation Job."""
    if not reuse_namespace:
        raise RuntimeError(
            "Kubernetes S3-compatible mode requires an existing namespace and Secret; pass --reuse-namespace"
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
        ),
        namespace,
    )
    _preflight(kubeconfig_path, "get", "namespace", namespace)
    # A missing Secret would leave the Job pod in CreateContainerConfigError
    # until the emulation timeout, so check it up front.
    _preflight(kubeconfig_path, "get", "secret", secret_name, namespace=namespace)
    print(f"[kubernetes] S3-compatible auth preflight OK for namespace={namespace}")


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
# uploader image would otherwise block the frontend until --emulation-timeout.
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


def delete_s3_emulation_support_resources(kubeconfig_path: Path, namespace: str, resource_name: str) -> None:
    """Best-effort rollback when owner references could not be installed."""
    for resource_type in S3_JOB_OWNED_RESOURCE_TYPES:
        if not _delete(kubeconfig_path, namespace, resource_type, resource_name):
            return


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
    reuse_namespace: bool = False,
    distributor_startup_timeout: str | None = None,
    btc_node_image: str | None = None,
    kubernetes_image_pull_policy: str | None = None,
    btc_node_initial_block_count: str | None = None,
) -> str:
    """Render a kubectl-compatible JSON resource list for in-cluster emulation."""
    name = s3_emulation_job_name(run_id)
    labels = {"app.kubernetes.io/name": "coinjoin-s3", "coinjoin.run-id": run_id}
    btc_node_image_arg = ' --btc-node-image "$BTC_NODE_IMAGE"' if btc_node_image else ""
    controller = (
        'python manager.py --driver kubernetes --in-cluster --engine "$ENGINE" run '
        '--scenario /config/scenario.json --namespace "$NAMESPACE" --reuse-namespace '
        '--disable-port-forward --image-prefix "$IMAGE_PREFIX" --run-id "$RUN_ID" '
        '--btc-node-arg=-blocksxor=0 --download-btc-data "/app/logs/$RUN_ID/bitcoin_data" '
        "--controller-done-marker /app/logs/.controller.done "
        "--controller-failed-marker /app/logs/.controller.failed"
        f"{btc_node_image_arg}"
    )
    access = (EXECUTION_ROOT / "k8s_s3_access.sh").read_text(encoding="utf-8")
    prefix_preflight = access + (EXECUTION_ROOT / "k8s_s3_prefix_preflight.sh").read_text(encoding="utf-8").replace(
        "@REQUIRED_EXPORTERS@", " ".join(REQUIRED_EXPORTERS)
    )
    uploader = access + (EXECUTION_ROOT / "k8s_s3_uploader.sh").read_text(encoding="utf-8").rstrip("\n")

    def field_ref(variable: str, path: str) -> dict:
        return {"name": variable, "valueFrom": {"fieldRef": {"fieldPath": path}}}

    def container(title: str, image: str, script: str, environment: list[dict], limits: tuple, mounts: dict) -> dict:
        requests, maximum = limits
        return {
            "name": title,
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

    # The emulator makes this pod the owner of every pod and service it
    # creates, so Kubernetes removes them even when the controller is
    # killed before its own cleanup finishes.
    env: list[dict] = [
        {"name": "NAMESPACE", "value": namespace},
        {"name": "RUN_ID", "value": run_id},
        {"name": "ENGINE", "value": engine},
        {"name": "IMAGE_PREFIX", "value": image_prefix},
        field_ref("COINJOIN_OWNER_POD_NAME", "metadata.name"),
        field_ref("COINJOIN_OWNER_POD_UID", "metadata.uid"),
        field_ref("COINJOIN_OWNER_POD_NAMESPACE", "metadata.namespace"),
    ]
    # The controller runs in a separate container, so overrides set on the
    # frontend reach it only explicitly; the emulator validates each one.
    for variable, value in (
        ("COINJOIN_DISTRIBUTOR_STARTUP_TIMEOUT", distributor_startup_timeout),
        ("BTC_NODE_IMAGE", btc_node_image),
        ("KUBERNETES_IMAGE_PULL_POLICY", kubernetes_image_pull_policy),
        ("COINJOIN_BTC_NODE_INITIAL_BLOCK_COUNT", btc_node_initial_block_count),
    ):
        if value:
            env.append({"name": variable, "value": value})
    artifact_env: list[dict] = [
        {"name": "RUN_ID", "value": run_id},
        {"name": "ARTIFACT_URI", "value": artifact_uri},
        {"name": "S3_ENDPOINT_URL", "value": endpoint_url},
        *[
            {
                "name": key,
                "valueFrom": {
                    "secretKeyRef": {"name": secret_name, "key": key, "optional": key == "S3_DEFAULT_REGION"}
                },
            }
            for key in ("S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "S3_DEFAULT_REGION")
        ],
    ]
    uploader_env: list[dict] = [
        {"name": "NAMESPACE", "value": namespace},
        {"name": "EMULATION_TIMEOUT_SECONDS", "value": str(emulation_timeout_seconds)},
        field_ref("POD_NAME", "metadata.name"),
        *artifact_env,
    ]
    small = (S3_UPLOADER_RESOURCE_REQUESTS, S3_UPLOADER_RESOURCE_LIMITS)
    metadata = {"name": name, "namespace": namespace, "labels": labels}
    resources: list[dict] = [
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": metadata},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata, "data": {"scenario.json": scenario_json}},
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "Role",
            "metadata": {"name": name, "namespace": namespace},
            "rules": [
                {
                    "apiGroups": [""],
                    "resources": ["pods", "services"],
                    "verbs": ["create", "get", "list", "watch", "delete"],
                },
                {"apiGroups": [""], "resources": ["pods/log", "pods/status"], "verbs": ["get"]},
                {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]},
                {"apiGroups": [""], "resources": ["events"], "verbs": ["get", "list", "watch"]},
            ],
        },
        {
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "RoleBinding",
            "metadata": {"name": name, "namespace": namespace},
            "subjects": [{"kind": "ServiceAccount", "name": name, "namespace": namespace}],
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": name},
        },
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": metadata,
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
                        "securityContext": {
                            "runAsNonRoot": True,
                            "runAsUser": 1000,
                            "runAsGroup": 1000,
                            "fsGroup": 1000,
                            "seccompProfile": {"type": "RuntimeDefault"},
                        },
                        "volumes": [
                            {"name": "artifacts", "emptyDir": {}},
                            {"name": "credentials", "emptyDir": {"medium": "Memory"}},
                            {"name": "scenario", "configMap": {"name": name}},
                        ],
                        "initContainers": [
                            container(
                                "prefix-preflight",
                                uploader_image,
                                prefix_preflight,
                                artifact_env,
                                small,
                                {"credentials": "/credentials"},
                            )
                        ],
                        "containers": [
                            container(
                                "controller",
                                emulator_image,
                                controller,
                                env,
                                (S3_CONTROLLER_RESOURCE_REQUESTS, S3_CONTROLLER_RESOURCE_LIMITS),
                                {"artifacts": "/app/logs", "scenario": "/config"},
                            ),
                            container(
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
    if not reuse_namespace:
        resources.insert(0, {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": resources}, indent=2)


def apply_s3_emulation_resources(manifest: str, kubeconfig_path: Path) -> None:
    """Apply the resource list, then make the Job the owner of its support objects.

    Owner references let Kubernetes collect the ServiceAccount, ConfigMap, Role
    and RoleBinding with the Job; until they are in place a failure removes them here.
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

    def roll_back(message: str) -> RuntimeError:
        delete_s3_emulation_job(kubeconfig_path, namespace, job_name)
        delete_s3_emulation_support_resources(kubeconfig_path, namespace, job_name)
        return RuntimeError(message)

    # The manifest orders the support resources before the Job, so a partial
    # apply can leave them behind with no Job to own them.
    applied = subprocess.run(
        ["kubectl", "--kubeconfig", str(kubeconfig_path), "apply", "-f", "-"], input=manifest, text=True, check=False
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
    for resource_type in S3_JOB_OWNED_RESOURCE_TYPES:
        patched = kubectl(
            kubeconfig_path, "patch", resource_type, job_name, "--type=merge", "-p", owner_patch, namespace=namespace
        )
        if patched.returncode:
            raise roll_back(f"could not attach Job ownership to {resource_type}/{job_name}: {_output(patched)}")
