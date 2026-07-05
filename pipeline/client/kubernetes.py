"""Kubernetes access preflight for the emulation pipeline."""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
from pathlib import Path


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
    reuse_namespace: bool = False,
) -> str:
    """Render a kubectl-compatible JSON resource list for in-cluster emulation."""
    name = f"coinjoin-s3-{run_id}".lower().replace("_", "-").replace(".", "-")
    name = name[:63].rstrip("-")
    labels = {"app.kubernetes.io/name": "coinjoin-s3", "coinjoin.run-id": run_id}
    controller = (
        'python manager.py --driver kubernetes --engine "$ENGINE" run '
        '--scenario /config/scenario.json --namespace "$NAMESPACE" --reuse-namespace '
        '--disable-port-forward --image-prefix "$IMAGE_PREFIX" --run-id "$RUN_ID" '
        '--btc-node-arg=-blocksxor=0 --download-btc-data "/app/logs/$RUN_ID/bitcoin_data" '
        "--controller-done-marker /app/logs/.controller.done "
        "--controller-failed-marker /app/logs/.controller.failed"
    )
    uploader = r"""set -euo pipefail
command -v s5cmd >/dev/null || { echo "s5cmd is required" >&2; exit 1; }
mkdir -p /credentials "/artifacts/$RUN_ID/.k8s" "/artifacts/$RUN_ID/.pipeline"
umask 077
printf '[coinjoin]\naws_access_key_id = %s\naws_secret_access_key = %s\n' \
  "$S3_ACCESS_KEY_ID" "$S3_SECRET_ACCESS_KEY" > /credentials/credentials
cp -R /app/exporters "/artifacts/$RUN_ID/.pipeline/exporters"
s5() {
  env -u AWS_ACCESS_KEY_ID -u AWS_SECRET_ACCESS_KEY -u AWS_SESSION_TOKEN \
    -u AWS_PROFILE -u AWS_DEFAULT_PROFILE -u AWS_REGION -u AWS_DEFAULT_REGION \
    s5cmd --credentials-file /credentials/credentials \
    --profile coinjoin --endpoint-url "$S3_ENDPOINT_URL" "$@"
}
while [ ! -f /artifacts/.controller.done ] && [ ! -f /artifacts/.controller.failed ]; do
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
    uploader_env = [
        {"name": "NAMESPACE", "value": namespace},
        {
            "name": "POD_NAME",
            "valueFrom": {"fieldRef": {"fieldPath": "metadata.name"}},
        },
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
                    "resources": ["pods", "services", "pods/log"],
                    "verbs": ["create", "get", "list", "watch", "delete"],
                },
                {"apiGroups": [""], "resources": ["pods/exec"], "verbs": ["create", "get"]},
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
                "template": {
                    "metadata": {"labels": labels},
                    "spec": {
                        "serviceAccountName": name,
                        "restartPolicy": "Never",
                        "volumes": [
                            {"name": "artifacts", "emptyDir": {}},
                            {"name": "credentials", "emptyDir": {"medium": "Memory"}},
                            {"name": "scenario", "configMap": {"name": name}},
                        ],
                        "containers": [
                            {
                                "name": "controller",
                                "image": emulator_image,
                                "command": ["sh", "-c", controller],
                                "env": env,
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
    command = ["kubectl", "--kubeconfig", str(kubeconfig_path), "apply", "-f", "-"]
    completed = subprocess.run(command, input=manifest, text=True, check=False)
    if completed.returncode:
        raise RuntimeError(f"kubectl apply failed with exit {completed.returncode}")
