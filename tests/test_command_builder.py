#!/usr/bin/env python3
"""Unit tests for the dependency-free command rendering core."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


MODULE_PATH = Path(__file__).parents[1] / "command_builder.py"
PROJECT_DIR = MODULE_PATH.parent
SNAPSHOT_PATH = PROJECT_DIR / "command_metadata.json"
GENERATOR_PATH = PROJECT_DIR / "scripts" / "generate-command-metadata.py"
WRAPPER_ROOT = PROJECT_DIR / "pipeline"
SPEC = importlib.util.spec_from_file_location("command_builder", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)
METADATA_MODULE = sys.modules["coinjoin_pipeline.command_metadata"]
GENERATOR_SPEC = importlib.util.spec_from_file_location("command_metadata_generator", GENERATOR_PATH)
assert GENERATOR_SPEC and GENERATOR_SPEC.loader
GENERATOR_MODULE = importlib.util.module_from_spec(GENERATOR_SPEC)
sys.modules[GENERATOR_SPEC.name] = GENERATOR_MODULE
GENERATOR_SPEC.loader.exec_module(GENERATOR_MODULE)


class CommandBuilderTests(unittest.TestCase):
    def test_documented_kubernetes_pbs_command(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "joinmarket"),
                ("--driver", "kubernetes"),
                ("--namespace", "coinjoin"),
                ("--kubeconfig", "${HOME}/.kube/config"),
                ("--analysisPbs", None),
                ("--blocksciPbs", None),
                ("--pbs-bitcoin-datadir", "/storage/<site>/<user>/bitcoin-regtest-data"),
            ],
        )

        self.assertEqual(MODULE.render_command(command), """coinjoin-pipeline --runtime docker full-run \\
  --engine joinmarket \\
  --driver kubernetes \\
  --namespace coinjoin \\
  --kubeconfig \"${HOME}/.kube/config\" \\
  --analysisPbs \\
  --blocksciPbs \\
  --pbs-bitcoin-datadir '/storage/<site>/<user>/bitcoin-regtest-data'""")
    def test_podman_runtime_and_shell_quoting(self) -> None:
        command = MODULE.Command(
            action="analyze",
            runtime="podman",
            options=[("--engine", "wasabi"), ("--run-dir", "run with spaces")],
        )
        self.assertTrue(MODULE.render_command(command).startswith("coinjoin-pipeline --runtime podman analyze"))
        self.assertIn("--run-dir 'run with spaces'", MODULE.render_command(command))

    def test_nested_research_command(self) -> None:
        command = MODULE.Command(
            action="external analyze",
            options=[("--run-id", "mainnet-2026"), ("--resume", None)],
        )
        self.assertEqual(
            MODULE.render_command(command),
            "coinjoin-pipeline --runtime docker external analyze \\\n  --run-id mainnet-2026 \\\n  --resume",
        )

    def test_parse_multiline_command_for_editing(self) -> None:
        command = MODULE.parse_command(
            """CONTAINER_SOCKET=/run/podman/podman.sock ./runIt.sh container podman full-run \\
              --engine joinmarket \\
              --driver=kubernetes \\
              --namespace 'coinjoin research' \\
              --analysisPbs"""
        )
        self.assertEqual(command.runtime, "podman")
        self.assertEqual(command.action, "full-run")
        self.assertEqual(command.env, [("CONTAINER_SOCKET", "/run/podman/podman.sock")])
        self.assertEqual(MODULE.option_value(command, "--namespace"), "coinjoin research")
        self.assertTrue(MODULE.has_option(command, "--analysisPbs"))

    def test_leading_env_assignments_round_trip_through_render(self) -> None:
        source = (
            "CONTAINER_SOCKET=/run/podman/podman.sock "
            "WRAPPER_IMAGE=ghcr.io/ondrejman/coinjoin-pipeline:latest "
            "./runIt.sh container podman full-run \\\n"
            "  --engine joinmarket \\\n"
            "  --analysisPbs"
        )
        command = MODULE.parse_command(source)
        self.assertEqual(
            command.env,
            [
                ("CONTAINER_SOCKET", "/run/podman/podman.sock"),
                ("WRAPPER_IMAGE", "ghcr.io/ondrejman/coinjoin-pipeline:latest"),
            ],
        )
        rendered = MODULE.render_command(command)
        self.assertTrue(
            rendered.startswith(
                "CONTAINER_SOCKET=/run/podman/podman.sock "
                "WRAPPER_IMAGE=ghcr.io/ondrejman/coinjoin-pipeline:latest "
                "coinjoin-pipeline --runtime podman full-run"
            ),
            rendered,
        )
        # Re-parsing the rendered command must preserve the same environment.
        reparsed = MODULE.parse_command(rendered)
        self.assertEqual(reparsed.env, command.env)
        self.assertEqual(reparsed.action, command.action)
        self.assertEqual(reparsed.runtime, command.runtime)

    def test_validation_rejects_incompatible_storage_and_action_flags(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "joinmarket"),
                ("--driver", "kubernetes"),
                ("--kubernetes-btc-datadir", "/storage/kubernetes"),
                ("--blocksciPbs", None),
                ("--pbs-bitcoin-datadir", "/storage/pbs"),
                ("--all-runs", None),
            ],
        )
        validation = MODULE.validate_command(command)
        self.assertTrue(any("does not support: --all-runs" in error for error in validation.errors))
        self.assertTrue(any("to be identical" in error for error in validation.errors))

    def test_validation_accepts_complete_s3_full_run(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "wasabi"),
                ("--driver", "kubernetes"),
                ("--artifact-backend", "s3"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.cl4.du.cesnet.cz"),
                ("--s3-secret-name", "coinjoin-s3"),
                ("--s3-credentials-file", "/storage/user/.aws/credentials"),
                ("--s3-profile", "coinjoin"),
                ("--run-id", "run-1"),
                ("--reuse-namespace", None),
                ("--analysisPbs", None),
                ("--blocksciPbs", None),
                ("--mappingsPbs", None),
            ],
        )
        self.assertEqual(MODULE.validate_command(command).errors, [])

    def test_s3_mappings_and_blocksci_allow_report_resource_overrides(self) -> None:
        command = MODULE.Command(
            action="pbs-from-s3",
            options=[
                ("--run-id", "run-1"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.example.test"),
                ("--s3-credentials-file", "/storage/user/.aws/credentials"),
                ("--s3-profile", "coinjoin"),
                ("--engine", "wasabi"),
                ("--coinjoin-type", "wasabi2"),
                ("--blocksciPbs", None),
                ("--mappingsPbs", None),
                ("--pbs-unified-report-ncpus", "1"),
            ],
        )

        self.assertEqual(MODULE.validate_command(command).errors, [])

    def test_validation_accepts_versioned_s3_blocksci_update(self) -> None:
        command = MODULE.Command(
            action="pbs-from-s3",
            options=[
                ("--engine", "joinmarket"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.cl4.du.cesnet.cz"),
                ("--s3-credentials-file", "/storage/user/.aws/credentials"),
                ("--s3-profile", "coinjoin"),
                ("--run-id", "mainnet-850100"),
                ("--blocksciPbs", None),
                ("--blocksci-workflow", "cached"),
                ("--blocksci-task", "update"),
                ("--blocksci-cache-source-run-id", "mainnet-850000"),
                ("--blocksci-external-bitcoin-datadir", "/storage/user/bitcoin"),
                ("--blocksci-network", "bitcoin"),
                ("--blocksci-max-block", "850100"),
            ],
        )

        self.assertEqual(MODULE.validate_command(command).errors, [])

    def test_validation_requires_s3_full_run_transport_and_stages(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "wasabi"),
                ("--driver", "kubernetes"),
                ("--artifact-backend", "s3"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(any("--s3-credentials-file" in error for error in errors))
        self.assertTrue(any("--s3-secret-name" in error for error in errors))
        self.assertTrue(any("both --analysisPbs and --blocksciPbs" in error for error in errors))
        self.assertTrue(any("--reuse-namespace" in error for error in errors))

    def test_validation_rejects_s3_full_run_parallel_and_shared_storage_flags(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "wasabi"),
                ("--driver", "kubernetes"),
                ("--artifact-backend", "s3"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.cl4.du.cesnet.cz"),
                ("--s3-secret-name", "coinjoin-s3"),
                ("--s3-credentials-file", "/storage/user/.aws/credentials"),
                ("--s3-profile", "coinjoin"),
                ("--run-id", "run-1"),
                ("--reuse-namespace", None),
                ("--analysisPbs", None),
                ("--blocksciPbs", None),
                ("--parallel", None),
                ("--copy-to-host", None),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(any("--parallel" in error for error in errors))
        self.assertTrue(any("--copy-to-host" in error for error in errors))

    def test_validation_requires_existing_namespace_for_s3_emulate(self) -> None:
        command = MODULE.Command(
            action="emulate",
            options=[
                ("--engine", "wasabi"),
                ("--driver", "kubernetes"),
                ("--artifact-backend", "s3"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.cl4.du.cesnet.cz"),
                ("--s3-secret-name", "coinjoin-s3"),
                ("--run-id", "run-1"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(any("--reuse-namespace" in error for error in errors))

    def test_validation_requires_external_inputs_unless_resuming(self) -> None:
        command = MODULE.Command(action="external analyze", options=[("--run-id", "mainnet")])
        validation = MODULE.validate_command(command)
        self.assertIn(
            "A new external run requires --bitcoin-datadir and --baseline.",
            validation.errors,
        )

    def test_explanation_describes_runtime_driver_pbs_and_safety(self) -> None:
        command = MODULE.Command(
            action="full-run",
            runtime="podman",
            options=[
                ("--engine", "joinmarket"),
                ("--driver", "kubernetes"),
                ("--namespace", "coinjoin"),
                ("--analysisPbs", None),
                ("--dry-run", None),
            ],
        )
        explanation = "\n".join(MODULE.explain_command(command))
        self.assertIn("Local runtime: podman", explanation)
        self.assertIn("Kubernetes namespace coinjoin", explanation)
        self.assertIn("PBS stages: coinjoin-analysis", explanation)
        self.assertIn("dry-run", explanation)

    def test_analyze_rejects_analysis_pbs_but_allows_blocksci_pbs(self) -> None:
        analyze_analysis_pbs = MODULE.Command(
            action="analyze",
            options=[
                ("--engine", "joinmarket"),
                ("--run-dir", "run-1"),
                ("--analysisPbs", None),
            ],
        )
        errors = MODULE.validate_command(analyze_analysis_pbs).errors
        self.assertTrue(any("does not support: --analysisPbs" in error for error in errors))
        self.assertTrue(
            any("--analysisPbs is supported only by full-run and coinjoin-analysis" in error for error in errors)
        )

        analyze_blocksci_pbs = MODULE.Command(
            action="analyze",
            options=[
                ("--engine", "joinmarket"),
                ("--run-dir", "run-1"),
                ("--blocksciPbs", None),
                ("--pbs-bitcoin-datadir", "/storage/btc"),
            ],
        )
        self.assertEqual(MODULE.validate_command(analyze_blocksci_pbs).errors, [])

    def test_coinjoin_analysis_rejects_blocksci_pbs_flags(self) -> None:
        command = MODULE.Command(
            action="coinjoin-analysis",
            options=[
                ("--run-dir", "run-1"),
                ("--blocksciPbs", None),
                ("--pbs-blocksci-image", "img.sif"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        unsupported = [error for error in errors if "does not support" in error]
        self.assertTrue(unsupported)
        # Both flags are reported together in a single "does not support" message.
        self.assertTrue(any("--blocksciPbs" in error for error in unsupported))
        self.assertTrue(any("--pbs-blocksci-image" in error for error in unsupported))
        self.assertTrue(any("--blocksciPbs is supported only by full-run and analyze" in error for error in errors))

    def test_coinjoin_analysis_all_runs_rejects_analysis_pbs(self) -> None:
        command = MODULE.Command(
            action="coinjoin-analysis",
            options=[("--all-runs", None), ("--analysisPbs", None)],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(
            any("--all-runs cannot be combined with --analysisPbs" in error for error in errors)
        )

    def test_pbs_blocksci_image_requires_blocksci_pbs(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "joinmarket"),
                ("--analysisPbs", None),
                ("--pbs-blocksci-image", "blocksci.sif"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(any("--pbs-blocksci-image requires --blocksciPbs" in error for error in errors))

    def test_pbs_coinjoin_analysis_image_requires_analysis_pbs(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "joinmarket"),
                ("--blocksciPbs", None),
                ("--pbs-bitcoin-datadir", "/storage/btc"),
                ("--pbs-coinjoin-analysis-image", "cj.sif"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(
            any("--pbs-coinjoin-analysis-image requires --analysisPbs" in error for error in errors)
        )

    def test_pbs_bitcoin_datadir_requires_blocksci_pbs(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "joinmarket"),
                ("--analysisPbs", None),
                ("--pbs-bitcoin-datadir", "/storage/btc"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(any("--pbs-bitcoin-datadir requires --blocksciPbs" in error for error in errors))

    def test_pbs_shared_resource_requires_any_pbs_stage(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket"), ("--pbs-ncpus", "8")],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(
            any("PBS resource/image options require --analysisPbs, --blocksciPbs, or --mappingsPbs" in error
                for error in errors)
        )

    def test_stage_specific_pbs_resource_requires_matching_stage(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[
                ("--engine", "wasabi"),
                ("--analysisPbs", None),
                ("--pbs-analysis-mem", "32gb"),
                ("--pbs-blocksci-mem", "2tb"),
            ],
        )
        errors = MODULE.validate_command(command).errors
        self.assertTrue(
            any("blocksci-specific PBS resources require --blocksciPbs" in error for error in errors)
        )

    def test_mappings_pbs_requires_wasabi_and_wasabi2(self) -> None:
        wrong_engine = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket"), ("--mappingsPbs", None)],
        )
        self.assertTrue(any("requires --engine wasabi" in error
                            for error in MODULE.validate_command(wrong_engine).errors))

        wrong_type = MODULE.Command(
            action="full-run",
            options=[("--engine", "wasabi"), ("--coinjoin-type", "joinmarket"), ("--mappingsPbs", None)],
        )
        self.assertTrue(any("requires --coinjoin-type wasabi2" in error
                            for error in MODULE.validate_command(wrong_type).errors))

    def test_mapping_numeric_parameters_are_validated(self) -> None:
        for flag, value in (("--mapping-timeout", "0"), ("--mapping-retry-timeout", "-1"),
                            ("--mapping-mining-fee-rate", "-1"),
                            ("--mapping-coordination-fee-rate", "bad"), ("--sake-seed", "bad")):
            with self.subTest(flag=flag):
                command = MODULE.Command(
                    action="mappings",
                    options=[("--engine", "wasabi"), ("--run-dir", "run-1"),
                             ("--mappingsPbs", None), (flag, value)],
                )
                self.assertTrue(MODULE.validate_command(command).errors)

    def test_run_timezone_validation_matches_wrapper(self) -> None:
        valid = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket"), ("--run-timezone", "Europe/Prague")],
        )
        self.assertEqual(MODULE.validate_command(valid).errors, [])

        invalid = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket"), ("--run-timezone", "not/a-timezone")],
        )
        errors = MODULE.validate_command(invalid).errors
        self.assertTrue(any("--run-timezone must be a valid IANA timezone" in error for error in errors))

    def test_joinmarket_detector_choices_match_wrapper(self) -> None:
        valid = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket"), ("--joinmarket-detector", "definite")],
        )
        self.assertEqual(MODULE.validate_command(valid).errors, [])

        invalid = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket"), ("--joinmarket-detector", "strict")],
        )
        errors = MODULE.validate_command(invalid).errors
        self.assertTrue(
            any("--joinmarket-detector must be one of: possible, definite" in error for error in errors)
        )

    def test_blocksci_script_must_be_an_existing_file(self) -> None:
        existing = MODULE.Command(
            action="analyze",
            options=[
                ("--engine", "joinmarket"),
                ("--run-dir", "run-1"),
                ("--blocksci-script", str(MODULE_PATH)),
            ],
        )
        self.assertEqual(MODULE.validate_command(existing).errors, [])

        missing = MODULE.Command(
            action="analyze",
            options=[
                ("--engine", "joinmarket"),
                ("--run-dir", "run-1"),
                ("--blocksci-script", "/no/such/script.py"),
            ],
        )
        errors = MODULE.validate_command(missing).errors
        self.assertTrue(any("--blocksci-script not found or not a file" in error for error in errors))

    def test_reusable_blocksci_parse_from_s3_is_valid(self) -> None:
        command = MODULE.Command(
            action="pbs-from-s3",
            options=[
                ("--run-id", "run-1"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.example.invalid"),
                ("--s3-credentials-file", "/storage/user/.aws/credentials"),
                ("--s3-profile", "coinjoin"),
                ("--engine", "wasabi"),
                ("--blocksciPbs", None),
                ("--blocksci-workflow", "reusable"),
                ("--blocksci-task", "parse"),
            ],
        )
        self.assertEqual(MODULE.validate_command(command).errors, [])

    def test_external_bitcoin_parse_from_s3_is_valid(self) -> None:
        command = MODULE.Command(
            action="pbs-from-s3",
            options=[
                ("--run-id", "run-1"),
                ("--artifact-uri", "s3://bucket/runs"),
                ("--s3-endpoint-url", "https://s3.example.invalid"),
                ("--s3-credentials-file", "/storage/user/.aws/credentials"),
                ("--s3-profile", "coinjoin"),
                ("--engine", "wasabi"),
                ("--blocksciPbs", None),
                ("--blocksci-workflow", "reusable"),
                ("--blocksci-task", "parse"),
                ("--blocksci-external-bitcoin-datadir", "/storage/external/bitcoin"),
                ("--blocksci-network", "bitcoin"),
                ("--blocksci-max-block", "850000"),
            ],
        )
        self.assertEqual(MODULE.validate_command(command).errors, [])

    def test_external_analyze_validates_network_and_coinjoin_type_choices(self) -> None:
        valid = MODULE.Command(
            action="external analyze",
            options=[
                ("--run-id", "mainnet"),
                ("--bitcoin-datadir", "/btc"),
                ("--baseline", "/baseline/coinjoin_tx_info.json"),
                ("--network", "bitcoin"),
                ("--coinjoin-type", "joinmarket"),
            ],
        )
        self.assertEqual(MODULE.validate_command(valid).errors, [])

        invalid = MODULE.Command(
            action="external analyze",
            options=[
                ("--run-id", "mainnet"),
                ("--bitcoin-datadir", "/btc"),
                ("--baseline", "/baseline/coinjoin_tx_info.json"),
                ("--network", "testnet"),
                ("--coinjoin-type", "whirlpool"),
            ],
        )
        errors = MODULE.validate_command(invalid).errors
        self.assertTrue(any("--network must be one of: bitcoin" in error for error in errors))
        self.assertTrue(
            any("--coinjoin-type must be one of: wasabi2, joinmarket" in error for error in errors)
        )

    def test_snapshot_drives_every_supported_flag(self) -> None:
        metadata = MODULE.command_metadata()
        self.assertEqual(
            set(metadata),
            {
                "emulate", "clean", "analyze", "export", "coinjoin-analysis", "mappings",
                "initialize", "full-run", "pbs-from-s3", "runs list", "runs inspect", "runs validate",
                "scenarios list", "scenarios show", "scenarios validate", "external analyze",
            },
        )
        for action, command in metadata.items():
            expected = set(command.options) - MODULE.SEMANTICALLY_DISABLED_FLAGS.get(action, set())
            self.assertEqual(MODULE.supported_flags(action), expected, action)
            for flag, option in command.options.items():
                self.assertEqual(MODULE.takes_value(flag), option.takes_value, (action, flag))

    def test_snapshot_preserves_alias_choices_defaults_and_help(self) -> None:
        metadata = MODULE.command_metadata()
        script = metadata["full-run"].options["--blocksci-script"]
        self.assertIn("--blocksci-script", script.aliases)
        self.assertIn("--blocksciScript", script.aliases)
        detector = metadata["full-run"].options["--joinmarket-detector"]
        self.assertEqual(detector.choices, ("possible", "definite"))
        self.assertEqual(detector.default, "definite")
        self.assertIn("subset detector", detector.help)

    def test_snapshot_loader_rejects_missing_malformed_and_unsupported_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            missing = root / "missing.json"
            with self.assertRaisesRegex(RuntimeError, "file is missing"):
                METADATA_MODULE.load_metadata_snapshot(missing)

            malformed = root / "malformed.json"
            malformed.write_text("{", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cannot be read as JSON"):
                METADATA_MODULE.load_metadata_snapshot(malformed)

            unsupported = root / "unsupported.json"
            unsupported.write_text(
                json.dumps({"schema_version": 999, "commands": {}}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "unsupported schema_version"):
                METADATA_MODULE.load_metadata_snapshot(unsupported)

    def test_metadata_loader_works_in_a_standalone_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            standalone = Path(temporary) / "coinjoin-pipeline"
            standalone.mkdir()
            shutil.copy2(PROJECT_DIR / "command_builder.py", standalone)
            shutil.copytree(PROJECT_DIR / "src", standalone / "src")
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "import command_builder as c; "
                        "cmd=c.Command('full-run', options=[('--engine','joinmarket'),"
                        "('--driver','kubernetes'),('--blocksciPbs',None),"
                        "('--pbs-bitcoin-datadir','/storage/btc')]); "
                        "assert not c.validate_command(cmd).errors; print(c.render_command(cmd))"
                    ),
                ],
                cwd=standalone,
                env={**os.environ, "PYTHONPATH": str(standalone), "PYTHONDONTWRITEBYTECODE": "1"},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("coinjoin-pipeline --runtime docker full-run", completed.stdout)
            self.assertIn("--driver kubernetes", completed.stdout)

    @unittest.skipUnless(WRAPPER_ROOT.is_dir(), "bundled wrapper parsers are unavailable")
    def test_generator_is_deterministic_and_check_detects_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "metadata.json"
            command = [
                sys.executable,
                str(GENERATOR_PATH),
                "--wrapper-root",
                str(WRAPPER_ROOT),
                "--output",
                str(output),
            ]
            first = subprocess.run(command, text=True, capture_output=True, check=False)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_content = output.read_text(encoding="utf-8")
            environment = {
                **os.environ,
                "PBS_BITCOIN_DATADIR": "/storage/private/site",
                "CONTAINER_RUNTIME": "podman",
                "EMULATION_LOGS_DIR": "/private/emulation/logs",
            }
            second = subprocess.run(
                command,
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(output.read_text(encoding="utf-8"), first_content)
            generated = json.loads(first_content)
            self.assertIsNone(
                generated["commands"]["full-run"]["options"]["--pbs-bitcoin-datadir"]["default"]
            )

            stale = json.loads(first_content)
            stale["commands"]["full-run"]["options"]["--engine"]["help"] = "stale"
            output.write_text(json.dumps(stale), encoding="utf-8")
            checked = subprocess.run([*command, "--check"], text=True, capture_output=True, check=False)
            self.assertEqual(checked.returncode, 1)
            self.assertIn("changed option: full-run --engine", checked.stderr)

            output.write_text(json.dumps({"commands": {"full-run": "value"}}), encoding="utf-8")
            malformed = subprocess.run([*command, "--check"], text=True, capture_output=True, check=False)
            self.assertEqual(malformed.returncode, 1)
            self.assertIn("invalid command record: full-run", malformed.stderr)
            self.assertNotIn("Traceback", malformed.stderr)

    @unittest.skipUnless(WRAPPER_ROOT.is_dir(), "bundled wrapper parsers are unavailable")
    def test_generator_restores_parser_environment(self) -> None:
        expected = {
            "PBS_BITCOIN_DATADIR": "/storage/restore-me",
            "CONTAINER_RUNTIME": "podman",
            "EMULATION_LOGS_DIR": "/tmp/restore-me",
        }
        with mock.patch.dict(os.environ, expected, clear=False):
            GENERATOR_MODULE.generate_snapshot(WRAPPER_ROOT)
            self.assertEqual(
                {key: os.environ.get(key) for key in expected},
                expected,
            )

    def test_contextual_help_uses_parser_help_and_default(self) -> None:
        namespace_help = MODULE.contextual_help("full-run", "--namespace")
        self.assertIn("Kubernetes namespace", namespace_help)
        self.assertIn("default: coinjoin", namespace_help)
        self.assertEqual(MODULE.metadata_default("full-run", "--namespace"), "coinjoin")

    def test_preflight_forces_dry_run_without_mutating_original(self) -> None:
        command = MODULE.Command(
            action="clean",
            runtime="podman",
            options=[("--yes", None)],
            env=[("CONTAINER_SOCKET", "/run/podman/podman.sock")],
        )
        preflight = MODULE.preflight_command(command)
        self.assertEqual(command.options, [("--yes", None)])
        self.assertEqual(preflight.options, [("--dry-run", None)])
        self.assertEqual(preflight.env, command.env)
        self.assertEqual(
            MODULE.command_argv(preflight)[:3],
            ["coinjoin-pipeline", "--runtime", "podman"],
        )

    def test_preflight_rejects_commands_without_dry_run(self) -> None:
        with self.assertRaisesRegex(ValueError, "does not support --dry-run"):
            MODULE.preflight_command(MODULE.Command(action="runs list"))

    def test_run_preflight_executes_argv_without_a_shell(self) -> None:
        command = MODULE.Command(
            action="full-run",
            options=[("--engine", "joinmarket")],
            env=[("WRAPPER_IMAGE", "example/wrapper:test")],
        )
        completed = mock.Mock(returncode=0)
        with mock.patch.object(MODULE.subprocess, "run", return_value=completed) as run:
            self.assertEqual(MODULE.run_preflight(command), 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv[-1], "--dry-run")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["env"]["WRAPPER_IMAGE"], "example/wrapper:test")

    def test_completion_discovers_run_ids_kubeconfig_and_images(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            runs_root = Path(temporary)
            (runs_root / "run-2026").mkdir()
            with mock.patch.dict(
                os.environ,
                {
                    "EMULATION_LOGS_DIR": temporary,
                    "BLOCKSCI_IMAGE": "example/blocksci:test",
                },
                clear=False,
            ):
                self.assertIn("run-2026", MODULE.completion_values("--run-dir"))
                self.assertIn("example/blocksci:test", MODULE.completion_values("--blocksci-image"))
        self.assertIn("${HOME}/.kube/config", MODULE.completion_values("--kubeconfig"))
        self.assertTrue(MODULE.completion_values("--scenario"))
        self.assertIn(
            "docker://ghcr.io/ondrejman/blocksci-complete:latest",
            MODULE.completion_values("--pbs-blocksci-image"),
        )


if __name__ == "__main__":
    unittest.main()
