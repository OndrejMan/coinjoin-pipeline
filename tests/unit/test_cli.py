from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from coinjoin_pipeline.cli import (
    add_effective_image_arguments,
    main,
    parse_host_options,
)
from coinjoin_pipeline.commands import (
    action_from,
    launcher_command,
    validate_passthrough,
)
from coinjoin_pipeline.images import resolve_images
from coinjoin_pipeline.manifest import atomic_write
from coinjoin_pipeline.builder import Command, parse_command, render_command
from coinjoin_pipeline.pipeline_image import Configuration, runtime_command
from coinjoin_pipeline.host import required_image_components
from coinjoin_pipeline.runs import manifest_target, run_id_for, valid_run_id


class CliTests(unittest.TestCase):
    def test_image_version_and_override_precedence(self) -> None:
        images = resolve_images("thesis-2026-07", {"blocksci": "local/blocksci:test"})
        self.assertTrue(images.pipeline.endswith(":thesis-2026-07"))
        self.assertEqual(images.blocksci, "local/blocksci:test")

    def test_invalid_image_and_version_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            resolve_images("bad tag", {})
        with self.assertRaises(ValueError):
            resolve_images("ok", {"blocksci": "not valid image"})

    def test_host_options_are_removed_from_pipeline_arguments(self) -> None:
        args, host = parse_host_options(
            [
                "full-run",
                "--engine",
                "joinmarket",
                "--version",
                "v1",
                "--runtime=podman",
            ]
        )
        self.assertEqual(args, ["full-run", "--engine", "joinmarket"])
        self.assertEqual(host["version"], "v1")
        self.assertEqual(host["runtime"], "podman")

    def test_full_run_manifest_uses_precomputed_run_id(self) -> None:
        target = manifest_target(
            "full-run",
            ["full-run", "--engine", "joinmarket"],
            Path("/runs"),
            "2026-07-12_12-00_default-joinmarket",
        )
        self.assertEqual(
            target,
            Path("/runs/2026-07-12_12-00_default-joinmarket/research_manifest.json"),
        )

    def test_run_id_validation_matches_emulator_rules(self) -> None:
        self.assertTrue(valid_run_id(run_id_for(["full-run", "--engine", "joinmarket"])))
        self.assertTrue(valid_run_id("2026-07-12_22-37_default-joinmarket"))
        self.assertFalse(valid_run_id("-leading-dash"))
        self.assertFalse(valid_run_id("a/../b"))
        self.assertFalse(valid_run_id("x" * 64))

    def test_stage_actions_require_explicit_run(self) -> None:
        self.assertEqual(action_from(["analyze", "--engine", "joinmarket"]), "analyze")
        errors = validate_passthrough(["analyze", "--engine", "joinmarket"], "analyze")
        self.assertTrue(any("requires --run-dir" in error for error in errors))
        self.assertEqual(
            action_from(["coinjoin", "--run-dir", "run-1"]), "coinjoin-analysis"
        )

    def test_pbs_images_are_pinned_from_effective_version(self) -> None:
        args = add_effective_image_arguments(
            "full-run",
            ["full-run", "--engine", "wasabi", "--mappingsPbs"],
            resolve_images("v1", {}),
        )
        self.assertIn(
            "docker://ghcr.io/ondrejman/coinjoin-mappings-enumerator:v1", args
        )
        self.assertIn("docker://ghcr.io/ondrejman/coinjoin-mappings-sake:v1", args)

    def test_test_values_are_explicit_opt_in(self) -> None:
        for enabled in (False, True):
            arguments = ["full-run", "--engine", "wasabi", "--dry-run"]
            if enabled:
                arguments.append("--test-values")
            output = io.StringIO()
            with (
                redirect_stdout(output),
                mock.patch("coinjoin_pipeline.cli.doctor_check", return_value=[]),
            ):
                code = main(arguments)

            self.assertEqual(code, 0)
            rendered_command = output.getvalue()
            if enabled:
                self.assertIn("--test-values", rendered_command)
            else:
                self.assertNotIn("--test-values", rendered_command)

    def test_cleanup_requires_confirmation(self) -> None:
        self.assertIn(
            "clean is destructive; pass --yes or --dry-run",
            validate_passthrough(["clean"], "clean"),
        )

    def test_runtime_rendering_quotes_paths(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = launcher_command(
                root / "launcher with spaces.sh",
                "docker",
                ["full-run", "--engine", "joinmarket"],
                resolve_images("v1", {}),
                root / "runs with spaces",
                "coinjoin-pipeline full-run",
            )
            self.assertIn("'", command.rendered())
            self.assertIn("launcher with spaces.sh", command.rendered())

    def test_manifest_redacts_sensitive_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "research_manifest.json"
            atomic_write(target, {"token": "secret", "nested": {"password": "hidden"}})
            self.assertEqual(
                json.loads(target.read_text()),
                {"nested": {"password": "<redacted>"}, "token": "<redacted>"},
            )

    def test_dry_run_renders_without_runtime_access(self) -> None:
        output = io.StringIO()
        with (
            redirect_stdout(output),
            mock.patch("coinjoin_pipeline.cli.doctor_check", return_value=[]),
        ):
            code = main(
                ["full-run", "--engine", "joinmarket", "--version", "v1", "--dry-run"]
            )
        self.assertEqual(code, 0)
        self.assertIn("Generated runtime command:", output.getvalue())

    def test_mutating_command_uses_explicit_latest_by_default(self) -> None:
        output = io.StringIO()
        with (
            redirect_stdout(output),
            mock.patch("coinjoin_pipeline.cli.doctor_check", return_value=[]),
        ):
            code = main(["full-run", "--engine", "joinmarket", "--dry-run"])
        self.assertEqual(code, 0)
        self.assertIn("coinjoin-pipeline:latest", output.getvalue())

    def test_latest_defaults_match_published_runtime_images(self) -> None:
        images = resolve_images(None, {})
        self.assertEqual(images.blocksci, "ghcr.io/ondrejman/blocksci-complete:latest")
        self.assertTrue(
            all(image.endswith(":latest") for image in images.as_dict().values())
        )

    def test_recreate_checks_only_images_used_by_that_stage(self) -> None:
        self.assertEqual(
            required_image_components(
                "recreate", ["recreate", "--driver", "kubernetes"]
            ),
            {"pipeline", "emulator"},
        )

    def test_direct_pbs_does_not_check_images_with_frontend_runtime(self) -> None:
        with mock.patch.dict("os.environ", {"PBS_FRONTEND_DIRECT": "1"}):
            self.assertEqual(
                required_image_components(
                    "coinjoin-analysis", ["coinjoin-analysis", "--analysisPbs"]
                ),
                set(),
            )

    def test_direct_pbs_skips_frontend_container_preflight(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            with (
                mock.patch.dict("os.environ", {"PBS_FRONTEND_DIRECT": "1"}),
                mock.patch(
                    "coinjoin_pipeline.doctor.shutil.which",
                    return_value="/usr/bin/qsub",
                ),
                mock.patch("coinjoin_pipeline.cli.doctor_check") as check,
                mock.patch("coinjoin_pipeline.cli.run", return_value=0),
                redirect_stdout(io.StringIO()),
            ):
                code = main(
                    ["coinjoin-analysis", "--run-dir", directory, "--analysisPbs"]
                )
        self.assertEqual(code, 0)
        check.assert_not_called()

    def test_metadata_required_fields_and_choices_are_enforced(self) -> None:
        self.assertTrue(
            any(
                "requires --engine" in error
                for error in validate_passthrough(["full-run", "--dry-run"], "full-run")
            )
        )
        self.assertTrue(
            any(
                "must be one of" in error
                for error in validate_passthrough(
                    [
                        "full-run",
                        "--engine",
                        "joinmarket",
                        "--driver",
                        "invalid",
                        "--dry-run",
                    ],
                    "full-run",
                )
            )
        )

    def test_unified_report_resource_overrides_require_both_s3_analyzers(self) -> None:
        arguments = [
            "pbs-from-s3",
            "--run-id",
            "run-1",
            "--artifact-uri",
            "s3://bucket/runs",
            "--s3-endpoint-url",
            "https://s3.example.invalid",
            "--s3-credentials-file",
            "/storage/user/.aws/credentials",
            "--s3-profile",
            "coinjoin",
            "--engine",
            "wasabi",
            "--analysisPbs",
            "--pbs-unified-report-ncpus",
            "1",
        ]
        self.assertTrue(
            any(
                "require both --analysisPbs and --blocksciPbs" in error
                for error in validate_passthrough(arguments, "pbs-from-s3")
            )
        )
        arguments.append("--blocksciPbs")
        self.assertEqual(validate_passthrough(arguments, "pbs-from-s3"), [])

    def test_environment_image_overrides_preserve_legacy_workflows(self) -> None:
        environment = {
            "WRAPPER_IMAGE": "wrapper:test",
            "BLOCKSCI_IMAGE": "blocksci:test",
            "COINJOIN_EMULATOR_IMAGE": "emulator:test",
            "COINJOIN_ANALYSIS_IMAGE": "analysis:test",
        }
        with mock.patch.dict("os.environ", environment, clear=False):
            _, host = parse_host_options(["full-run", "--engine", "joinmarket"])
            from coinjoin_pipeline.host import (
                image_overrides,
                required_image_components,
            )
            from coinjoin_pipeline.images import all_images_overridden

            overrides = image_overrides(host)
            self.assertTrue(
                all_images_overridden(
                    overrides, required_image_components("full-run", ["full-run"])
                )
            )

    def test_builder_round_trips_generated_host_options(self) -> None:
        command = Command(
            "full-run",
            runtime="podman",
            version="v1",
            options=[("--engine", "joinmarket")],
        )
        parsed = parse_command(render_command(command))
        self.assertEqual(parsed.runtime, "podman")
        self.assertEqual(parsed.version, "v1")
        self.assertEqual(parsed.action, "full-run")

    def test_pipeline_image_uses_socket_and_executes_wrapper_arguments(self) -> None:
        config = Configuration(
            runtime="docker",
            image="coinjoin-pipeline:v1",
            kubeconfig=Path("/tmp/kubeconfig"),
            logs_dir=Path("/tmp/runs"),
            build=False,
            pipeline_arguments=("full-run", "--engine", "joinmarket"),
            socket=Path("/var/run/docker.sock"),
            source_root=None,
        )
        command = runtime_command(config)
        self.assertIn("/var/run/docker.sock:/var/run/docker.sock", command)
        self.assertNotIn("--privileged", command)
        self.assertEqual(
            command[-4:],
            ["--driver", "kubernetes", "--kubeconfig", "/root/.kube/config"],
        )


if __name__ == "__main__":
    unittest.main()
