"""Kubernetes access preflight for the emulation pipeline."""

from __future__ import annotations

import hashlib
import json
import shlex
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from client.artifacts import PROBE_QUEUED, PROBE_RUNNING, PROBE_TERMINAL, PROBE_UNKNOWN

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
CONTROLLER_LOG_TAIL_LINES = 100
CONTROLLER_FATAL_SUMMARY_MARKERS = ("Kubernetes CPU quota exhausted",)


def run_kubectl_preflight_command(command: list[str]) -> str:
    try:
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print(
            "[ERROR] kubectl is required for Kubernetes auth preflight inside the wrapper container.",
            file=sys.stderr,
        )
        sys.exit(2)
    stdout = (completed.stdout or "").strip()
    stderr = (completed.stderr or "").strip()
    if completed.returncode != 0:
        print(f"[ERROR] Kubernetes preflight command failed: {shlex.join(command)}", file=sys.stderr)
        if stdout:
            print(stdout, file=sys.stderr)
        if stderr:
            print(stderr, file=sys.stderr)
        sys.exit(2)
    if stderr:
        print(f"[kubectl] {stderr}", file=sys.stderr)
    return stdout


def kubectl_auth_can_i(kubeconfig_path: Path, verb: str, resource: str, namespace: str | None = None) -> None:
    command = ["kubectl", "--kubeconfig", str(kubeconfig_path), "auth", "can-i", verb, resource]
    if namespace is not None:
        command.extend(["--namespace", namespace])
    output = run_kubectl_preflight_command(command)
    answer = next((line for line in reversed(output.splitlines()) if line.strip()), "")
    if answer.strip().lower() != "yes":
        print(f"[ERROR] Kubernetes permission denied: {shlex.join(command)} returned {output!r}", file=sys.stderr)
        sys.exit(2)


def kubernetes_auth_preflight(kubeconfig_path: Path, namespace: str, reuse_namespace: bool) -> None:
    """Validate Kubernetes permissions with the mounted kubeconfig."""
    print(f"[kubernetes] Auth preflight using kubeconfig: {kubeconfig_path}")
    run_kubectl_preflight_command(["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "--raw=/version"])

    for verb, resource in (
        ("create", "pods"),
        ("create", "services"),
        ("delete", "pods"),
        ("delete", "services"),
    ):
        kubectl_auth_can_i(kubeconfig_path, verb, resource, namespace)

    if reuse_namespace:
        run_kubectl_preflight_command(["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "namespace", namespace])
        for verb, resource in (
            ("list", "pods"),
            ("list", "services"),
            ("delete", "pods"),
            ("delete", "services"),
        ):
            kubectl_auth_can_i(kubeconfig_path, verb, resource, namespace)
    else:
        kubectl_auth_can_i(kubeconfig_path, "create", "namespaces")
        kubectl_auth_can_i(kubeconfig_path, "delete", "namespaces")

    print(f"[kubernetes] Auth preflight OK for namespace={namespace}, reuse_namespace={reuse_namespace}")


def kubernetes_s3_auth_preflight(
    kubeconfig_path: Path,
    namespace: str,
    reuse_namespace: bool,
    secret_name: str,
) -> None:
    """Validate permissions and prerequisites for the S3-compatible emulation Job."""
    print(f"[kubernetes] S3-compatible auth preflight using kubeconfig: {kubeconfig_path}")
    run_kubectl_preflight_command(["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "--raw=/version"])
    for verb, resource in (
        ("create", "jobs.batch"),
        ("create", "configmaps"),
        ("create", "serviceaccounts"),
        ("create", "roles.rbac.authorization.k8s.io"),
        ("create", "rolebindings.rbac.authorization.k8s.io"),
        ("patch", "serviceaccounts"),
        ("patch", "configmaps"),
        ("patch", "roles.rbac.authorization.k8s.io"),
        ("patch", "rolebindings.rbac.authorization.k8s.io"),
        ("delete", "serviceaccounts"),
        ("delete", "configmaps"),
        ("delete", "roles.rbac.authorization.k8s.io"),
        ("delete", "rolebindings.rbac.authorization.k8s.io"),
        ("get", "jobs.batch"),
        ("delete", "jobs.batch"),
    ):
        kubectl_auth_can_i(kubeconfig_path, verb, resource, namespace)
    if reuse_namespace:
        run_kubectl_preflight_command(["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "namespace", namespace])
        # A missing Secret would leave the Job pod in CreateContainerConfigError
        # until the emulation timeout, so check it up front.
        run_kubectl_preflight_command(
            ["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "secret", secret_name, "--namespace", namespace]
        )
    else:
        raise RuntimeError(
            "Kubernetes S3-compatible mode requires an existing namespace and Secret; "
            "pass --reuse-namespace"
        )
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
    {"ErrImagePull", "ImagePullBackOff", "InvalidImageName", "CreateContainerConfigError"}
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


def kubernetes_job_probe(kubeconfig_path: Path, namespace: str, job_name: str) -> Callable[[], str]:
    """Build a kubectl-backed liveness probe for ``wait_for_s3_marker``.

    kubectl errors are inconclusive so polling continues on ``PROBE_UNKNOWN``.
    """
    job_seen = False

    def pod_state() -> str:
        command = [
            "kubectl", "--kubeconfig", str(kubeconfig_path), "get", "pods",
            "--namespace", namespace, "--selector", f"job-name={job_name}", "-o", "json",
        ]
        try:
            result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return PROBE_UNKNOWN
        if result.returncode != 0:
            return PROBE_UNKNOWN
        try:
            pods = json.loads(result.stdout).get("items") or []
        except json.JSONDecodeError:
            return PROBE_UNKNOWN
        if not pods:
            return PROBE_QUEUED
        for pod in pods:
            reason = unstartable_pod_reason(pod)
            if reason:
                print(
                    f"[kubernetes] Job {job_name} cannot start its containers ({reason})",
                    file=sys.stderr,
                )
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

    def probe() -> str:
        nonlocal job_seen
        command = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig_path),
            "get",
            "job",
            job_name,
            "--namespace",
            namespace,
            "-o",
            "json",
        ]
        try:
            result = subprocess.run(command, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except FileNotFoundError:
            return PROBE_UNKNOWN
        if result.returncode != 0:
            if "notfound" in (result.stderr or "").lower().replace(" ", ""):
                # Deleted after we saw it running means it terminated without markers.
                return PROBE_TERMINAL if job_seen else PROBE_UNKNOWN
            return PROBE_UNKNOWN
        job_seen = True
        try:
            status = json.loads(result.stdout).get("status", {})
        except json.JSONDecodeError:
            return PROBE_UNKNOWN
        for condition in status.get("conditions") or []:
            if condition.get("type") in {"Complete", "Failed"} and condition.get("status") == "True":
                return PROBE_TERMINAL
        return pod_state()

    return probe


def collect_s3_emulation_diagnostics(kubeconfig_path: Path, namespace: str, job_name: str) -> str:
    """Best-effort kubectl evidence for a failed or vanished emulation Job."""
    sections: list[str] = []
    for description, command in (
        ("job description", ["describe", "job", job_name]),
        ("controller logs", ["logs", f"job/{job_name}", "-c", "controller", "--tail=-1"]),
        ("uploader logs", ["logs", f"job/{job_name}", "-c", "uploader", "--tail=100"]),
    ):
        full = ["kubectl", "--kubeconfig", str(kubeconfig_path), "--namespace", namespace, *command]
        try:
            result = subprocess.run(full, check=False, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
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
            output = "\n".join(output.splitlines()[-CONTROLLER_LOG_TAIL_LINES:])
        sections.append(f"--- {description} ---\n{output}")
    return "\n".join(sections)


def delete_s3_emulation_job(kubeconfig_path: Path, namespace: str, job_name: str) -> None:
    """Best-effort frontend cleanup after a failed or timed-out marker wait."""
    command = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig_path),
        "--namespace",
        namespace,
        "delete",
        "job",
        job_name,
        "--ignore-not-found",
        "--wait=false",
    ]
    try:
        result = subprocess.run(
            command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError:
        print(f"[WARN] kubectl unavailable; could not delete Job {job_name}", file=sys.stderr)
        return
    if result.returncode != 0:
        print(
            f"[WARN] failed to delete Kubernetes Job {job_name}: "
            f"{(result.stderr or result.stdout or '').strip()}",
            file=sys.stderr,
        )


def delete_s3_emulation_support_resources(
    kubeconfig_path: Path,
    namespace: str,
    resource_name: str,
) -> None:
    """Best-effort rollback when owner references could not be installed."""
    for resource_type in S3_JOB_OWNED_RESOURCE_TYPES:
        command = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig_path),
            "--namespace",
            namespace,
            "delete",
            resource_type,
            resource_name,
            "--ignore-not-found",
            "--wait=false",
        ]
        try:
            result = subprocess.run(
                command,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except FileNotFoundError:
            print(
                f"[WARN] kubectl unavailable; could not delete "
                f"{resource_type}/{resource_name}",
                file=sys.stderr,
            )
            return
        if result.returncode != 0:
            print(
                f"[WARN] failed to delete {resource_type}/{resource_name}: "
                f"{(result.stderr or result.stdout or '').strip()}",
                file=sys.stderr,
            )


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
) -> str:
    """Render a kubectl-compatible JSON resource list for in-cluster emulation."""
    name = s3_emulation_job_name(run_id)
    labels = {"app.kubernetes.io/name": "coinjoin-s3", "coinjoin.run-id": run_id}
    joinmarket_fallback = (
        " --joinmarket-descriptor-regtest-fallback" if engine == "joinmarket" else ""
    )
    controller = (
        'python manager.py --driver kubernetes --engine "$ENGINE" run '
        '--scenario /config/scenario.json --namespace "$NAMESPACE" --reuse-namespace '
        '--disable-port-forward --image-prefix "$IMAGE_PREFIX" --run-id "$RUN_ID"'
        f"{joinmarket_fallback} "
        '--btc-node-arg=-blocksxor=0 --download-btc-data "/app/logs/$RUN_ID/bitcoin_data" '
        "--controller-done-marker /app/logs/.controller.done "
        "--controller-failed-marker /app/logs/.controller.failed"
    )
    prefix_preflight = r"""set -euo pipefail
command -v s5cmd >/dev/null || { echo "s5cmd is required" >&2; exit 1; }
mkdir -p /credentials
umask 077
printf '[coinjoin]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
  "$S3_ACCESS_KEY_ID" "$S3_SECRET_ACCESS_KEY" > /credentials/credentials
s5() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION \
    s5cmd --credentials-file /credentials/credentials \
    --profile coinjoin --endpoint-url "$S3_ENDPOINT_URL" "$@"
}
# The frontend stages .pipeline/exporters/ into this prefix before creating the
# Job, so that path is expected here; anything else still means a reused run id.
# The filter must survive both `s5cmd ls` output shapes: a recursive listing of
# full keys (.pipeline/exporters/...) if the wildcard crosses "/", and a plain
# "DIR .pipeline/" row if it does not. Matching on `.pipeline` alone covers both;
# nothing else the pipeline writes carries that string, and a genuinely reused
# prefix still shows its own rows.
set +e
listing="$(s5 ls "$ARTIFACT_URI/$RUN_ID/*" 2>&1)"
status=$?
set -e
if [ "$status" -eq 0 ]; then
  unexpected="$(printf '%s\n' "$listing" | grep -v '\.pipeline' || true)"
  if [ -n "$unexpected" ]; then
    echo "run prefix $ARTIFACT_URI/$RUN_ID/ already contains artifacts; choose a fresh --run-id" >&2
    printf '%s\n' "$unexpected" >&2
    rm -f /credentials/credentials
    exit 1
  fi
elif ! printf '%s\n' "$listing" | grep -qi 'no object found'; then
  printf '%s\n' "$listing" >&2
  rm -f /credentials/credentials
  exit "$status"
fi
# Ignoring the exporters is only half the check: an empty or partial staging
# step would pass it just as well, so verify both entry points are present.
for required in unified_report.py blocksci_export/analysis.py; do
  if ! s5 ls "$ARTIFACT_URI/$RUN_ID/.pipeline/exporters/$required" >/dev/null 2>&1; then
    echo "staged exporters are incomplete: missing $required" >&2
    rm -f /credentials/credentials
    exit 1
  fi
done
rm -f /credentials/credentials
exit 0
"""
    uploader = r"""set -euo pipefail
command -v s5cmd >/dev/null || { echo "s5cmd is required" >&2; exit 1; }
mkdir -p /credentials "/artifacts/$RUN_ID/.k8s"
umask 077
printf '[coinjoin]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
  "$S3_ACCESS_KEY_ID" "$S3_SECRET_ACCESS_KEY" > /credentials/credentials
s5() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION \
    s5cmd --credentials-file /credentials/credentials \
    --profile coinjoin --endpoint-url "$S3_ENDPOINT_URL" "$@"
}
remaining="$EMULATION_TIMEOUT_SECONDS"
while [ ! -f /artifacts/.controller.done ] && [ ! -f /artifacts/.controller.failed ]; do
  if [ "$remaining" -le 0 ]; then
    printf 'controller exceeded emulation timeout (%ss)\n' "$EMULATION_TIMEOUT_SECONDS" >&2
    printf 'failed\n' > /artifacts/.controller.failed
    break
  fi
  terminated_exit="$(kubectl --namespace "$NAMESPACE" get pod "$POD_NAME" \
    -o 'jsonpath={.status.containerStatuses[?(@.name=="controller")].state.terminated.exitCode}' \
    2>/dev/null || true)"
  waiting_reason="$(kubectl --namespace "$NAMESPACE" get pod "$POD_NAME" \
    -o 'jsonpath={.status.containerStatuses[?(@.name=="controller")].state.waiting.reason}' \
    2>/dev/null || true)"
  if [ -n "$terminated_exit" ]; then
    printf 'controller terminated without completion marker (exit %s)\n' "$terminated_exit" >&2
    printf 'failed\n' > /artifacts/.controller.failed
    break
  fi
  case "$waiting_reason" in
    ErrImagePull|ImagePullBackOff|InvalidImageName|CreateContainerConfigError)
      printf 'controller failed to start: %s\n' "$waiting_reason" >&2
      printf 'failed\n' > /artifacts/.controller.failed
      break
      ;;
  esac
  sleep 2
  remaining=$((remaining - 2))
done
if [ -f /artifacts/.controller.failed ]; then
  printf 'failed\n' > "/artifacts/$RUN_ID/.k8s/upload.failed"
  s5 cp "/artifacts/$RUN_ID/.k8s/upload.failed" "$ARTIFACT_URI/$RUN_ID/.k8s/upload.failed" || true
  s5 sync "/artifacts/$RUN_ID/" "$ARTIFACT_URI/$RUN_ID/" || true
  rm -f /credentials/credentials
  exit 1
fi
s5 sync "/artifacts/$RUN_ID/" "$ARTIFACT_URI/$RUN_ID/"
printf 'done\n' > "/artifacts/$RUN_ID/.k8s/upload.done"
s5 cp "/artifacts/$RUN_ID/.k8s/upload.done" "$ARTIFACT_URI/$RUN_ID/.k8s/upload.done"
rm -f /credentials/credentials"""
    env = [
        {"name": "NAMESPACE", "value": namespace},
        {"name": "RUN_ID", "value": run_id},
        {"name": "ENGINE", "value": engine},
        {"name": "IMAGE_PREFIX", "value": image_prefix},
    ]
    artifact_env = [
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
    uploader_env = [
        {"name": "NAMESPACE", "value": namespace},
        {
            "name": "EMULATION_TIMEOUT_SECONDS",
            "value": str(emulation_timeout_seconds),
        },
        {
            "name": "POD_NAME",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
        },
        *artifact_env,
    ]
    resources = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
        },
        {
            "apiVersion": "v1",
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "data": {"scenario.json": scenario_json},
        },
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
                {
                    "apiGroups": [""],
                    "resources": ["pods/log", "pods/status"],
                    "verbs": ["get"],
                },
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
            "metadata": {"name": name, "namespace": namespace, "labels": labels},
            "spec": {
                "backoffLimit": 0,
                "activeDeadlineSeconds": (
                    scheduling_timeout_seconds
                    + emulation_timeout_seconds
                    + S3_JOB_DEADLINE_GRACE_SECONDS
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
                            {
                                "name": "prefix-preflight",
                                "image": uploader_image,
                                "command": ["sh", "-c", prefix_preflight],
                                "env": artifact_env,
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": S3_UPLOADER_RESOURCE_REQUESTS,
                                    "limits": S3_UPLOADER_RESOURCE_LIMITS,
                                },
                                "volumeMounts": [
                                    {"name": "credentials", "mountPath": "/credentials"},
                                ],
                            }
                        ],
                        "containers": [
                            {
                                "name": "controller",
                                "image": emulator_image,
                                "command": ["sh", "-c", controller],
                                "env": env,
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": S3_CONTROLLER_RESOURCE_REQUESTS,
                                    "limits": S3_CONTROLLER_RESOURCE_LIMITS,
                                },
                                "volumeMounts": [
                                    {"name": "artifacts", "mountPath": "/app/logs"},
                                    {"name": "scenario", "mountPath": "/config", "readOnly": True},
                                ],
                            },
                            {
                                "name": "uploader",
                                "image": uploader_image,
                                "command": ["sh", "-c", uploader],
                                "env": uploader_env,
                                "securityContext": {
                                    "allowPrivilegeEscalation": False,
                                    "capabilities": {"drop": ["ALL"]},
                                },
                                "resources": {
                                    "requests": S3_UPLOADER_RESOURCE_REQUESTS,
                                    "limits": S3_UPLOADER_RESOURCE_LIMITS,
                                },
                                "volumeMounts": [
                                    {"name": "artifacts", "mountPath": "/artifacts"},
                                    {"name": "credentials", "mountPath": "/credentials"},
                                ],
                            },
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
    payload = json.loads(manifest)
    jobs = [item for item in payload.get("items", []) if item.get("kind") == "Job"]
    if len(jobs) != 1:
        raise RuntimeError("S3 Kubernetes manifest must contain exactly one Job")
    metadata = jobs[0].get("metadata") or {}
    job_name = metadata.get("name")
    namespace = metadata.get("namespace")
    if not job_name or not namespace:
        raise RuntimeError("S3 Kubernetes Job must declare metadata.name and namespace")

    command = ["kubectl", "--kubeconfig", str(kubeconfig_path), "apply", "-f", "-"]
    completed = subprocess.run(command, input=manifest, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"kubectl apply failed with exit {completed.returncode}")

    uid_command = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig_path),
        "--namespace",
        namespace,
        "get",
        "job",
        job_name,
        "-o",
        "jsonpath={.metadata.uid}",
    ]
    uid_result = subprocess.run(
        uid_command,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    job_uid = (uid_result.stdout or "").strip()
    if uid_result.returncode or not job_uid:
        delete_s3_emulation_job(kubeconfig_path, namespace, job_name)
        delete_s3_emulation_support_resources(
            kubeconfig_path, namespace, job_name
        )
        raise RuntimeError(
            f"could not read Kubernetes Job UID for lifecycle ownership: "
            f"{(uid_result.stderr or uid_result.stdout or '').strip()}"
        )

    owner_patch = json.dumps(
        {
            "metadata": {
                "ownerReferences": [
                    {
                        "apiVersion": "batch/v1",
                        "kind": "Job",
                        "name": job_name,
                        "uid": job_uid,
                        "controller": True,
                        "blockOwnerDeletion": False,
                    }
                ]
            }
        }
    )
    for resource_type in S3_JOB_OWNED_RESOURCE_TYPES:
        patch_command = [
            "kubectl",
            "--kubeconfig",
            str(kubeconfig_path),
            "--namespace",
            namespace,
            "patch",
            resource_type,
            job_name,
            "--type=merge",
            "-p",
            owner_patch,
        ]
        patch_result = subprocess.run(
            patch_command,
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if patch_result.returncode:
            delete_s3_emulation_job(kubeconfig_path, namespace, job_name)
            delete_s3_emulation_support_resources(
                kubeconfig_path, namespace, job_name
            )
            raise RuntimeError(
                f"could not attach Job ownership to {resource_type}/{job_name}: "
                f"{(patch_result.stderr or patch_result.stdout or '').strip()}"
            )
