from __future__ import annotations

from pathlib import Path
import unittest

import yaml


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


class PublishWorkflowTests(unittest.TestCase):
    """The uploader image replaced the wrapper image as the only published one.

    It is a tool image pinned by reference in container/uploader.image, so it is
    published deliberately rather than on every push, and the digest it prints is
    what a follow-up commit puts into the lock file.
    """

    def test_uploader_publish_is_manual_only(self) -> None:
        workflow = (WORKFLOWS / "publish-uploader-image.yaml").read_text(encoding="utf-8")
        triggers = workflow.split("\npermissions:\n", 1)[0]
        self.assertIn("  workflow_dispatch:", triggers)
        self.assertNotIn("  push:", triggers)
        self.assertNotIn("  workflow_call:", triggers)
        self.assertIn("file: container/uploader.Dockerfile", workflow)
        self.assertIn("platforms: linux/amd64,linux/arm64", workflow)
        self.assertIn("packages: write", workflow)

    def test_uploader_publish_reports_the_digest_to_pin(self) -> None:
        workflow = (WORKFLOWS / "publish-uploader-image.yaml").read_text(encoding="utf-8")
        self.assertIn("id: build", workflow)
        self.assertIn("steps.build.outputs.digest", workflow)
        self.assertIn("container/uploader.image", workflow)

    def test_test_workflow_no_longer_publishes_a_wrapper_image(self) -> None:
        workflow = (WORKFLOWS / "tests.yaml").read_text(encoding="utf-8")
        self.assertIn(
            "group: coinjoin-pipeline-${{ github.workflow }}-"
            "${{ github.event.pull_request.number || github.ref }}",
            workflow,
        )
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertNotIn("publish-pipeline-image", workflow)
        self.assertNotIn("WRAPPER_IMAGE", workflow)
        self.assertFalse((WORKFLOWS / "publish-pipeline-image.yaml").exists())

    def test_kubernetes_jobs_checkout_the_sources_they_build(self) -> None:
        jobs = yaml.safe_load((WORKFLOWS / "tests.yaml").read_text())["jobs"]
        for name in (
            "kubernetes-k3d", "kubernetes-pbs-wasabi", "kubernetes-pbs-joinmarket",
            "kubernetes-pbs-parallel", "kubernetes-s3-minio",
        ):
            with self.subTest(job=name):
                job = jobs[name]
                checkouts = [step for step in job["steps"] if
                             step.get("with", {}).get("repository") ==
                             "OndrejMan/coinjoin-emulator"]
                self.assertEqual(len(checkouts), 1)
                checkout = checkouts[0]
                self.assertEqual(checkout["with"]["submodules"], "recursive")
                source_dir = "${{ github.workspace }}/" + checkout["with"]["path"]
                # tests/support/local-images.sh resolves the emulator checkout
                # from this variable, whatever name the test script uses.
                self.assertEqual(job["env"]["COINJOIN_EMULATOR_SOURCE_DIR"], source_dir)
                build_steps = [i for i, step in enumerate(job["steps"])
                               if "./tests/test-" in step.get("run", "")]
                self.assertLess(job["steps"].index(checkout), min(build_steps))


if __name__ == "__main__":
    unittest.main()
