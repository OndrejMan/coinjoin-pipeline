"""rebase --exec helper: the uploader image option and provenance exist only with the S3 uploader."""
from pathlib import Path

if Path("container/uploader.image").exists():
    raise SystemExit(0)


def edit(path: str, old: str, new: str = "") -> None:
    p = Path(path)
    if not p.exists():
        return
    text = p.read_text()
    if old in text:
        p.write_text(text.replace(old, new))


edit("src/coinjoin_pipeline/configuration.py", '    uploader: str | None = option(None, name="uploader_image")\n')
edit("src/coinjoin_pipeline/context.py", '        ("COINJOIN_UPLOADER_IMAGE", config.images.uploader),\n')
edit(
    "src/coinjoin_pipeline/arguments.py",
    '    "images.uploader": "S3 uploader image (default: container/uploader.image)",\n',
)
edit("src/coinjoin_pipeline/execution/containers.py", '        ("--uploader-image", env.get("COINJOIN_UPLOADER_IMAGE")),\n')
edit("pipeline/compose.yaml", "        - COINJOIN_UPLOADER_IMAGE=${COINJOIN_UPLOADER_IMAGE:-}\n")

P = "pipeline/exporters/parameters.py"
edit(P, '"coinjoin_emulator", "uploader", "unified_report")', '"coinjoin_emulator", "unified_report")')
edit(
    P,
    "        # The uploader and report images have no digest option; a pinned reference carries its own.\n",
    "        # The report image has no digest option; a pinned reference carries its own.\n",
)
edit(P, '        "uploader": values.get("uploader"),\n')
edit(P, '    parser.add_argument("--uploader-image", default=os.environ.get("COINJOIN_UPLOADER_IMAGE"))\n')

edit("pipeline/exporters/report_types.py", "    coinjoin_emulator: str | None\n    uploader: str | None\n", "    coinjoin_emulator: str | None\n")

M = "pipeline/exporters/manifest.py"
edit(M, '    ("images.uploader", ("images", "uploader")),\n')
edit(M, '    ("image_digests.uploader", ("image_digests", "uploader")),\n')
edit(
    M,
    '    components: tuple[\n'
    '        Literal["blocksci", "coinjoin_analysis", "coinjoin_emulator", "uploader", "unified_report"], ...\n'
    '    ] = (\n'
    '        "blocksci",\n'
    '        "coinjoin_analysis",\n'
    '        "coinjoin_emulator",\n'
    '        "uploader",\n'
    '        "unified_report",\n'
    '    )\n',
    '    components: tuple[Literal["blocksci", "coinjoin_analysis", "coinjoin_emulator", "unified_report"], ...] = (\n'
    '        "blocksci",\n'
    '        "coinjoin_analysis",\n'
    '        "coinjoin_emulator",\n'
    '        "unified_report",\n'
    '    )\n',
)

edit("pipeline/exporters/markdown_report.py", '    ("uploader", "uploader"),\n')

edit(
    "pipeline/exporters/integration_diagnostics.py",
    "# Only the images the report can actually inspect. The uploader and the PBS\n"
    "# unified-report image are pulled by Kubernetes and Singularity, never by the\n"
    "# daemon this code can reach, so `docker image inspect` would always fail and\n"
    "# every run would report two spurious provenance problems. Their references and\n"
    "# digests still reach the manifest through build_run_manifest.\n",
    "# Only the images that ran before the analysis. The report stage resolves its\n"
    "# own image, whose reference and digest reach the manifest through build_run_manifest.\n",
)
edit(
    "pipeline/exporters/common.py",
    '    """Read the digest straight out of a pinned reference, without Docker.\n'
    "\n"
    "    The MetaCentrum frontend has no Docker daemon, so image provenance for the\n"
    "    pinned uploader/report references has to come from the reference itself.\n"
    '    """\n',
    '    """Read the digest straight out of a pinned reference, without Docker."""\n',
)

A = "pipeline/exporters/blocksci_export/analysis.py"
edit(
    A,
    "INSPECTED_IMAGE_NAMES = IMAGE_COMPONENTS[:3]\n"
    "# The report stage resolves its own image (manifest.resolve_manifest_images).\n"
    "PROVENANCE_IMAGE_NAMES = IMAGE_COMPONENTS[:4]\n",
    "# The report stage resolves its own image (manifest.resolve_manifest_images).\n"
    "INSPECTED_IMAGE_NAMES = IMAGE_COMPONENTS[:3]\n",
)
edit(A, "            for name in PROVENANCE_IMAGE_NAMES\n", "            for name in INSPECTED_IMAGE_NAMES\n")

edit("tests/pipeline/test_blocksci_analysis.py", '        uploader_image="uploader@sha256:" + "a" * 64,\n')
T = "tests/pipeline/test_unified_report.py"
HEX = "a" * 64
edit(T, f'        "uploader": "ghcr.io/ondrejman/coinjoin-pipeline-uploader@sha256:{HEX}",\n')
edit(T, f'        "uploader": "uploader@sha256:{HEX}",\n')
edit(T, f'                "--uploader-image",\n                "uploader@sha256:{HEX}",\n')
edit(
    T,
    f'        self.assertEqual(\n            args.uploader_image,\n            "uploader@sha256:{HEX}",\n        )\n',
)
edit(T, '                    "uploader": "uploader:old",\n')
edit(T, '                        "uploader": "sha256:uploader",\n')
edit(T, '        self.assertEqual(report["run_manifest"]["image_digests"]["uploader"], "sha256:uploader")\n')
edit(
    "src/coinjoin_pipeline/context.py",
    "    for key, value in (\n"
    '        ("COINJOIN_UNIFIED_REPORT_IMAGE", config.images.unified_report),\n'
    "    ):\n"
    "        if value:\n"
    "            environment[key] = value\n",
    "    if config.images.unified_report:\n"
    '        environment["COINJOIN_UNIFIED_REPORT_IMAGE"] = config.images.unified_report\n',
)
analysis = Path(A)
if analysis.exists() and "PROVENANCE_IMAGE_NAMES = IMAGE_COMPONENTS[:4]" not in analysis.read_text():
    analysis.write_text(analysis.read_text().replace("INSPECTED_IMAGE_NAMES", "PROVENANCE_IMAGE_NAMES"))
