"""Command-line interface for repeatable local and HPC builds."""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from .providers import describe_providers
from .spec import load_case
from .workflow import run_case


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="riftprep",
        description="Download native raw products first, then build validated RIFT inputs.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one YAML case from raw download through QC.")
    run.add_argument("config", help="Path to a case YAML file.")
    validate = commands.add_parser("validate-config", help="Parse and print a resolved case.")
    validate.add_argument("config", help="Path to a case YAML file.")
    providers = commands.add_parser("providers", help="Print the provider catalog.")
    providers.add_argument("--category", default=None)
    stages = commands.add_parser("simulate-stages", help="Run spin-up, checked depth handoff, then transient (requires RIFT GPU solver).")
    stages.add_argument("case_root")
    stages.add_argument("--executable", required=True, help="Path to the installed RIFT solver.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "simulate-stages":
        from .spinup import run_stages
        print(run_stages(args.case_root, args.executable))
        return 0
    if args.command == "run":
        result = run_case(args.config)
        print(json.dumps({key: str(value) for key, value in asdict(result).items()}, indent=2))
        return 0
    if args.command == "validate-config":
        print(json.dumps(load_case(args.config).as_dict(), indent=2, default=str))
        return 0
    if args.command == "providers":
        print(describe_providers(args.category).to_string(index=False))
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
