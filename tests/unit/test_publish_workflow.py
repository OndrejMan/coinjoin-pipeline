from __future__ import annotations

from pathlib import Path
import unittest


WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"


class PublishWorkflowTests(unittest.TestCase):
    def test_publish_is_reusable_and_has_no_independent_trigger(self) -> None:
        workflow = (WORKFLOWS / "publish-pipeline-image.yaml").read_text(encoding="utf-8")
        triggers = workflow.split("\npermissions:\n", 1)[0]
        self.assertIn("  workflow_call:", triggers)
        self.assertNotIn("  push:", triggers)
        self.assertNotIn("  workflow_dispatch:", triggers)
        self.assertIn("platforms: linux/amd64,linux/arm64", workflow)
        self.assertIn("type=raw,value=latest", workflow)
        self.assertIn("type=sha", workflow)

    def test_publish_waits_for_both_successful_test_branches(self) -> None:
        workflow = (WORKFLOWS / "tests.yaml").read_text(encoding="utf-8")
        self.assertIn("group: coinjoin-pipeline-${{ github.ref }}", workflow)
        self.assertIn("cancel-in-progress: true", workflow)
        publish = workflow.split("\n  publish-pipeline-image:\n", 1)[1]
        self.assertIn("- kubernetes-k3d", publish)
        self.assertIn("- kubernetes-pbs-parallel", publish)
        self.assertIn("success()", publish)
        self.assertIn("github.ref == 'refs/heads/main'", publish)
        self.assertIn("github.event_name == 'push'", publish)
        self.assertIn("github.event_name == 'workflow_dispatch'", publish)
        self.assertIn("packages: write", publish)
        self.assertIn("uses: ./.github/workflows/publish-pipeline-image.yaml", publish)


if __name__ == "__main__":
    unittest.main()
