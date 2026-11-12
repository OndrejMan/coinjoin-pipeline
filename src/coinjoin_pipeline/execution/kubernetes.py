"""kubectl access and the permission preflight of the Kubernetes driver."""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


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
