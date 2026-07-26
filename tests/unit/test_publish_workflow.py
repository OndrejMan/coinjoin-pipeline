from __future__ import annotations

from pathlib import Path
import unittest


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
        self.assertIn("group: coinjoin-pipeline-${{ github.ref }}", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        self.assertNotIn("publish-pipeline-image", workflow)
        self.assertNotIn("WRAPPER_IMAGE", workflow)
        self.assertFalse((WORKFLOWS / "publish-pipeline-image.yaml").exists())


if __name__ == "__main__":
    unittest.main()
