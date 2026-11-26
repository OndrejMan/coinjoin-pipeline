"""Run the image's JupyterLab over an existing index; publish readiness privately."""

import json
import errno
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

# Kernel of a notebook run at start; prepare() installs it.
KERNEL = "blocksci"


def available_port(preferred):
    """The preferred port when it is free, else any free one, so two notebook jobs can share a node."""
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", preferred))
        except OSError as error:
            if error.errno != errno.EADDRINUSE:
                raise
            probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def prepare(root, settings):
    """Keep user notebooks/settings across jobs and seed only missing files."""
    from ipykernel.kernelspec import install  # pylint: disable=import-error

    install(user=True, kernel_name="blocksci", display_name="Python (BlockSci)")
    settings.mkdir(parents=True, exist_ok=True)
    overrides = settings / "overrides.json"
    if not overrides.exists():
        overrides.write_text(
            json.dumps(
                {
                    "@jupyterlab/notebook-extension:tracker": {
                        "codeCellConfig": {"lineNumbers": True, "matchBrackets": True, "autoClosingBrackets": True},
                    },
                    "@jupyterlab/completer-extension:manager": {"autoCompletion": True},
                }
            )
        )
    starter = root / "BlockSci.ipynb"
    if not starter.exists():
        cells = [
            (
                "markdown",
                "# BlockSci\n\nExisting index, mounted read-only. Run cells with **Shift+Enter**, complete code with **Tab**, show help with **Shift+Tab**. Use the debugger, Markdown/LaTeX, plots and widgets in JupyterLab. Save with **Ctrl+S**; autosave is enabled. Closing this tab leaves the PBS job running.\n\nStart with a small range; scanning the whole chain can require substantial RAM.",
            ),
            (
                "code",
                "import os\nimport blocksci\nimport pandas as pd\nimport matplotlib.pyplot as plt\nimport ipywidgets as widgets\n\nchain = blocksci.Blockchain(os.environ['BLOCKSCI_CONFIG'])\nlen(chain)",
            ),
            (
                "code",
                "# Inspect only the last ten indexed blocks.\nblocks = chain[max(0, len(chain) - 10):]\npd.DataFrame({'height': [b.height for b in blocks], 'transactions': [len(b) for b in blocks]})",
            ),
        ]
        starter.write_text(
            json.dumps(
                {
                    "nbformat": 4,
                    "nbformat_minor": 5,
                    "metadata": {
                        "kernelspec": {"name": "blocksci", "display_name": "Python (BlockSci)", "language": "python"},
                        "language_info": {"name": "python"},
                    },
                    "cells": [
                        {
                            "id": "blocksci-" + str(i),
                            "cell_type": kind,
                            "metadata": {},
                            "source": source.splitlines(True),
                            **({"execution_count": None, "outputs": []} if kind == "code" else {}),
                        }
                        for i, (kind, source) in enumerate(cells)
                    ],
                },
                indent=2,
            )
        )


def autorun_target(root, control):
    """(notebook, exit on finish) the submitter asked to run at start (control/autorun.json), or None."""
    request = control / "autorun.json"
    if not request.is_file():
        return None
    content = json.loads(request.read_text())
    name = content.get("name")
    notebook = root / str(name)
    if not isinstance(name, str) or notebook.parent != root or not notebook.is_file():
        raise RuntimeError("The notebook to run at start is missing: " + str(name))
    return notebook, content.get("exit") is True


def run_file(path):
    """Run one notebook or script headless; save a .autorun.ipynb copy after every cell. Exit status 0/1."""
    os.chdir(path.parent)
    if path.suffix == ".py":
        return subprocess.call([sys.executable, path.name])
    import nbformat  # pylint: disable=import-error,import-outside-toplevel
    from nbclient import NotebookClient  # pylint: disable=import-error,import-outside-toplevel
    from nbclient.exceptions import CellExecutionError  # pylint: disable=import-error,import-outside-toplevel

    document = nbformat.read(str(path), as_version=4)
    copy = path.with_name(path.stem + ".autorun.ipynb")
    client = NotebookClient(
        document, timeout=None, kernel_name=KERNEL, resources={"metadata": {"path": str(path.parent)}}
    )
    cells = [index for index, cell in enumerate(document.cells) if cell.cell_type == "code"]
    client.reset_execution_trackers()
    with client.setup_kernel():
        for done, index in enumerate(cells, 1):
            print("[blocksci-notebook] Autorun: cell " + str(done) + "/" + str(len(cells)), flush=True)
            try:
                client.execute_cell(document.cells[index], index)
            except CellExecutionError as error:
                nbformat.write(document, str(copy))
                reason = (str(error).strip().splitlines() or ["?"])[-1]
                print("[blocksci-notebook] Autorun failed in cell " + str(done) + ": " + reason, flush=True)
                return 1
            nbformat.write(document, str(copy))
    print("[blocksci-notebook] Autorun finished: " + copy.name, flush=True)
    return 0


def main():
    if sys.argv[1:2] == ["--run-file"]:
        return run_file(Path(sys.argv[2]))
    run_id, port = os.environ["PBS_RUN_ID"], available_port(int(sys.argv[1]))
    root, control = Path("/mnt/notebooks"), Path("/mnt/notebook-control")
    os.umask(0o077)
    os.environ["JUPYTER_RUNTIME_DIR"] = str(control / "runtime")
    os.environ["JUPYTER_DATA_DIR"] = str(control / "data")
    os.environ["JUPYTERLAB_SETTINGS_DIR"] = str(root / ".jupyter" / "user-settings")
    os.environ["JUPYTERLAB_WORKSPACES_DIR"] = str(root / ".jupyter" / "workspaces")
    os.environ["JUPYTER_TOKEN"] = secrets.token_hex(32)
    # PBS uses /usr/bin/python3: uv's interpreter lives under root's private
    # cache. Its libraries and prebuilt web assets still come from the image venv.
    os.environ["JUPYTER_PATH"] = "/blocksci/.venv/share/jupyter"
    os.environ["JUPYTER_CONFIG_PATH"] = "/blocksci/.venv/etc/jupyter"
    app = Path("/blocksci/.venv/share/jupyter/lab")
    if not (app / "static/index.html").is_file():
        raise RuntimeError("The BlockSci image is missing the prebuilt JupyterLab web assets")
    base = "/jupyter/" + run_id + "/"
    prepare(root, root / ".jupyter" / "app")
    autorun = autorun_target(root, control)
    runner = None
    # The finally block also handles readiness failures and signals.
    process = subprocess.Popen(  # pylint: disable=consider-using-with
        [
            sys.executable,
            "-m",
            "jupyterlab",
            "--no-browser",
            "--allow-root",
            "--ip=127.0.0.1",
            "--port=" + str(port),
            "--ServerApp.port_retries=0",
            "--ServerApp.base_url=" + base,
            "--ServerApp.root_dir=" + str(root),
            "--LabApp.app_dir=" + str(app),
            "--LabApp.app_settings_dir=" + str(root / ".jupyter" / "app"),
            '--ServerApp.jpserver_extensions={"jupyter_nbextensions_configurator":False}',
        ]
    )
    connection = control / "connection.json"
    stopping = False

    def stop(_signum, _frame):
        nonlocal stopping
        stopping = True
        process.terminate()
        if runner is not None and runner.poll() is None:
            runner.terminate()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        deadline = time.monotonic() + 60
        while process.poll() is None and not stopping:
            try:
                request = Request(
                    "http://127.0.0.1:" + str(port) + base + "lab",
                    headers={"Authorization": "token " + os.environ["JUPYTER_TOKEN"]},
                )
                with urlopen(request, timeout=1):
                    break
            except (URLError, OSError) as error:
                if time.monotonic() >= deadline:
                    raise RuntimeError("JupyterLab did not become ready within 60 seconds") from error
                time.sleep(0.2)
        else:
            return process.wait()
        info = {
            "runId": run_id,
            "jobId": os.environ["NOTEBOOK_JOB_ID"],
            "host": socket.getfqdn(),
            "login": os.environ["NOTEBOOK_LOGIN"],
            "port": port,
            "baseUrl": base,
            "token": os.environ["JUPYTER_TOKEN"],
        }
        temporary = connection.with_suffix(".tmp")
        temporary.write_text(json.dumps(info))
        temporary.replace(connection)
        print("[blocksci-notebook] Ready: JupyterLab accepts connections.", flush=True)
        login, host = info["login"], info["host"]
        frontend = os.environ.get("NOTEBOOK_FRONTEND")
        jump = "-J " + login + "@" + frontend + " " if frontend else ""
        print(
            "[blocksci-notebook] Tunnel: ssh -N "
            + jump
            + "-L "
            + str(port)
            + ":127.0.0.1:"
            + str(port)
            + " "
            + login
            + "@"
            + host,
            flush=True,
        )
        if autorun is None:
            return process.wait()
        notebook, exit_on_finish = autorun
        # JupyterLab stays up to watch the run. With exit on finish the job ends with the
        # notebook, and the stage then uploads the notebook directory with its results.
        print("[blocksci-notebook] Autorun: " + notebook.name + " started", flush=True)
        runner = subprocess.Popen(  # pylint: disable=consider-using-with
            [sys.executable, sys.argv[0], "--run-file", str(notebook)]
        )
        while runner.poll() is None and process.poll() is None:
            time.sleep(1)
        if runner.poll() is None:
            runner.terminate()
            runner.wait()
            return process.returncode or 1
        if exit_on_finish:
            return runner.returncode
        print("[blocksci-notebook] Autorun ended; JupyterLab stays up until Stop run or the walltime", flush=True)
        return process.wait()
    finally:
        if runner is not None and runner.poll() is None:
            runner.terminate()
            runner.wait()
        connection.unlink(missing_ok=True)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


if __name__ == "__main__":
    sys.exit(main())
