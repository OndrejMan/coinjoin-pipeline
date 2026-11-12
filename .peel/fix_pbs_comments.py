"""rebase --exec helper: comments that explain PBS, S3 or Kubernetes behavior in trees without them."""
from pathlib import Path

NO_PBS = not Path("src/coinjoin_pipeline/execution/pbs/apptainer.py").exists()
NO_S3_JOB = not Path("src/coinjoin_pipeline/execution/s3_emulation.py").exists()
NO_K8S = not Path("src/coinjoin_pipeline/execution/kubernetes.py").exists()
HAS_UPLOADER = Path("container/uploader.image").exists()


def edit(path: str, old: str, new: str, when: bool) -> None:
    p = Path(path)
    if not when or not p.exists():
        return
    text = p.read_text()
    if old in text:
        p.write_text(text.replace(old, new))


edit(
    "pipeline/exporters/artifact_paths.py",
    "The host package, the exporters, the S3 PBS jobs and the tests import these names.\n"
    "The jobs ship this module to the compute node, so it stays standard library only.\n"
    "Shell job templates keep the paths spelled out for readability; a test checks that\n"
    "they use only the directory names defined here.\n",
    "The host package, the exporters and the tests import these names.\n",
    NO_PBS,
)
edit(
    "pipeline/exporters/worker.py",
    '"""The same analysis payload runs under Docker and Apptainer."""\n',
    '"""The analysis payload the BlockSci container runs."""\n',
    NO_PBS,
)
edit(
    "pipeline/exporters/integration_diagnostics.py",
    "        # A repo digest identifies the image by itself. Singularity on PBS has\n"
    "        # no daemon to report a local image id, so requiring one there would\n"
    "        # make a digest-pinned run incomplete forever.\n",
    "        # A repo digest identifies the image by itself, without a local image id.\n",
    NO_PBS,
)
edit(
    "pipeline/exporters/integration_diagnostics.py",
    "# Only the images the report can actually inspect. The uploader and the PBS\n"
    "# unified-report image are pulled by Kubernetes and Singularity, never by the\n"
    "# daemon this code can reach, so `docker image inspect` would always fail and\n"
    "# every run would report two spurious provenance problems. Their references and\n"
    "# digests still reach the manifest through build_run_manifest.\n",
    "# Only the images the report can actually inspect. The uploader image is pulled\n"
    "# by Kubernetes, never by the daemon this code can reach, so `docker image inspect`\n"
    "# would always fail and every run would report a spurious provenance problem. Its\n"
    "# reference and digest, like the report image's, still reach the manifest through\n"
    "# build_run_manifest.\n",
    NO_PBS and HAS_UPLOADER,
)
edit(
    "tests/pipeline/test_unified_report.py",
    "        # Singularity on PBS has no daemon that could report an image id.\n",
    "        # A digest-pinned image needs no local image id.\n",
    NO_PBS,
)
edit(
    "pipeline/exporters/manifest.py",
    "        # The report can run from a tree that has no checkout behind it: the PBS\n"
    "        # node executes exporters downloaded from S3, where `git rev-parse`\n"
    "        # yields null. The tree hash names the exact code that produced the\n",
    "        # The report can run from a tree that has no checkout behind it: the\n"
    "        # report container mounts only the exporters, where `git rev-parse`\n"
    "        # yields null. The tree hash names the exact code that produced the\n",
    NO_PBS,
)
edit(
    "pipeline/exporters/common.py",
    "    The PBS/S3 node runs a downloaded exporter tree with no ``.git``, so this is\n"
    "    None there and the tree hash carries the provenance instead.\n",
    "    The report container mounts the exporter tree without its ``.git``, so this\n"
    "    is None there and the tree hash carries the provenance instead.\n",
    NO_PBS,
)
edit(
    "pipeline/exporters/common.py",
    "# Bytecode and caches are excluded from the staged upload, so hashing them would\n"
    "# make the frontend and node hashes disagree over noise.\n",
    "# Bytecode and caches depend on what ran before, so hashing them would make two\n"
    "# runs of the same code disagree over noise.\n",
    NO_PBS,
)
edit(
    "src/coinjoin_pipeline/execution/containers.py",
    '    """Copy a user analysis script into the run so local and PBS jobs see identical input."""\n',
    '    """Copy a user analysis script into the run, where the BlockSci container sees it."""\n',
    NO_PBS,
)
edit(
    "src/coinjoin_pipeline/execution/stages.py",
    "No Docker, Kubernetes, PBS or subprocess code lives here: this is the dependency\n",
    "No Docker or subprocess code lives here: this is the dependency\n" if NO_K8S
    else "No Docker, Kubernetes or subprocess code lives here: this is the dependency\n",
    NO_PBS,
)
edit(
    "src/coinjoin_pipeline/execution/local.py",
    "These paths serve local development and reproduction. The thesis results come\n"
    "from the Kubernetes → S3 → PBS path dispatched in ``orchestrator.py``.\n",
    "These paths serve local development and reproduction.\n",
    NO_S3_JOB,
)
edit(
    "src/coinjoin_pipeline/execution/__init__.py",
    '"""Orchestration of a resolved configuration: the Kubernetes → S3 → PBS path and experimental local paths."""\n',
    '"""Orchestration of a resolved configuration over the experimental local paths."""\n',
    NO_S3_JOB,
)
edit(
    "pipeline/exporters/common.py",
    "    produced by, which ``git rev-parse`` cannot do for a staged tree (no\n",
    "    produced by, which ``git rev-parse`` cannot do for a mounted tree (no\n",
    NO_PBS,
)
