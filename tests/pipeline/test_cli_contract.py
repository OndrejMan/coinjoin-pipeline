import argparse
import unittest
from typing import cast

from client.cli_options import COINJOIN_TYPES, DEFAULT_COINJOIN_TYPE
from client.research import parser as research_parser
from client.wrapper import (
    RUNS_ROOT_CONTAINER,
    build_parser,
    compose_command,
    compose_env,
    container_command,
    container_runtime,
    pipeline_stage,
    run_command,
    stage_separator,
)

# argparse exposes subparser metadata only through these stable internal fields.
# pylint: disable=protected-access


def subparser(root: argparse.ArgumentParser, name: str) -> argparse.ArgumentParser:
    action = next(action for action in root._actions if isinstance(action, argparse._SubParsersAction))
    return cast(argparse.ArgumentParser, action.choices[name])


def option(parser: argparse.ArgumentParser, name: str) -> argparse.Action:
    return next(action for action in parser._actions if name in action.option_strings)


class SharedCliContractTests(unittest.TestCase):
    def test_all_analysis_commands_share_coinjoin_contract(self):
        wrapper = build_parser()
        external = subparser(subparser(research_parser(), "external"), "analyze")
        parsers = [subparser(wrapper, name) for name in ("analyze", "export", "full-run")]

        for command_parser in [*parsers, external]:
            coinjoin = option(command_parser, "--coinjoin-type")
            self.assertEqual(coinjoin.choices, COINJOIN_TYPES)
            self.assertEqual(coinjoin.default, DEFAULT_COINJOIN_TYPE)

    def test_all_analysis_commands_reject_unknown_coinjoin_type(self):
        wrapper = build_parser()
        for action in ("analyze", "export", "full-run"):
            with self.subTest(action=action), self.assertRaises(SystemExit):
                wrapper.parse_args([action, "--engine", "wasabi", "--coinjoin-type", "unknown"])

        with self.assertRaises(SystemExit):
            research_parser().parse_args(
                ["external", "analyze", "--run-id", "run", "--coinjoin-type", "unknown"]
            )

    def test_wrapper_compatibility_facade_exports_existing_symbols(self):
        self.assertEqual(RUNS_ROOT_CONTAINER, "/runs/emulation/logs")
        for exported in (
            compose_command,
            compose_env,
            container_command,
            container_runtime,
            pipeline_stage,
            run_command,
            stage_separator,
        ):
            self.assertTrue(callable(exported))

    def test_wrapper_wildcard_import_preserves_historical_public_api(self):
        namespace: dict[str, object] = {}

        exec("from client.wrapper import *", namespace)  # noqa: S102  # pylint: disable=exec-used

        for name in (
            "RUNS_ROOT_CONTAINER",
            "build_parser",
            "compose_env",
            "run_command",
            "run_parallel_analysis",
        ):
            self.assertIn(name, namespace)


if __name__ == "__main__":
    unittest.main()
