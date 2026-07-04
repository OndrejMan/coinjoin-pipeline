"""Kubernetes access preflight for the emulation pipeline."""

from __future__ import annotations

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
        run_kubectl_preflight_command(
            ["kubectl", "--kubeconfig", str(kubeconfig_path), "get", "namespace", namespace]
        )
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
