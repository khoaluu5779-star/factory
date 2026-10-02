"""Command line entry point: `python -m factory <command>`."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from factory.contracts.channel_config import list_channels, load_channel
from factory.contracts.director_plan import director_plan_json_schema
from factory.contracts.enums import RunOutcome
from factory.contracts.errors import FactoryError
from factory.contracts.ids import resolve_run_id
from factory.core.events import LoggingSink
from factory.core.orchestrator import Orchestrator
from factory.core.policy import Policy, load_global_mode
from factory.state.file_store import FileStateStore
from factory.stages.fakes import build_fake_pipeline

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = REPO_ROOT / "schemas" / "director_plan.schema.json"


def _schema_text() -> str:
    return json.dumps(director_plan_json_schema(), indent=2, sort_keys=True) + "\n"


def cmd_validate_config(args: argparse.Namespace) -> int:
    channels = [args.channel] if args.channel else list_channels(REPO_ROOT / "channels")
    if not channels:
        print("no channels found", file=sys.stderr)
        return 1
    bad = 0
    for name in channels:
        try:
            cfg = load_channel(REPO_ROOT / "channels", name)
            print(f"OK   {name} (lifecycle: {cfg.lifecycle_state})")
        except FactoryError as exc:
            bad += 1
            print(f"FAIL {name}: {exc}", file=sys.stderr)
    return 1 if bad else 0


def cmd_schema(args: argparse.Namespace) -> int:
    text = _schema_text()
    if args.write:
        SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
        SCHEMA_PATH.write_text(text, encoding="utf-8")
        print(f"wrote {SCHEMA_PATH}")
        return 0
    if not SCHEMA_PATH.is_file() or SCHEMA_PATH.read_text(encoding="utf-8") != text:
        print("schemas/director_plan.schema.json is out of date; run: python -m factory schema --write", file=sys.stderr)
        return 1
    print("schema up to date")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    if args.pipeline != "fake":
        print("only the 'fake' pipeline exists so far; real stages are not implemented", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    channel = load_channel(REPO_ROOT / "channels", args.channel)
    mode = load_global_mode(os.environ)
    orch = Orchestrator(
        pipeline=build_fake_pipeline(),
        store=FileStateStore(Path(args.state_dir)),
        workspace_root=Path(args.workspace_dir),
        policy=Policy(mode),
        events=LoggingSink(),
    )
    if args.resume:
        result = orch.resume(args.resume, channel)
    else:
        run_id = args.run_id or resolve_run_id(os.environ, datetime.now(timezone.utc))
        result = orch.run_channel(channel, run_id)
    print(f"outcome={result.outcome} job={result.job.job_id if result.job else '-'} reason={result.reason or '-'}")
    return 1 if result.outcome is RunOutcome.FAILED else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="factory")
    sub = p.add_subparsers(dest="command", required=True)

    v = sub.add_parser("validate-config", help="validate channel configs")
    v.add_argument("--channel")
    v.set_defaults(func=cmd_validate_config)

    s = sub.add_parser("schema", help="check or write schemas/director_plan.schema.json")
    s.add_argument("--write", action="store_true")
    s.set_defaults(func=cmd_schema)

    r = sub.add_parser("run", help="run one job for a channel (mode from FACTORY_MODE)")
    r.add_argument("--channel", required=True)
    r.add_argument("--run-id")
    r.add_argument("--resume", metavar="JOB_ID")
    r.add_argument("--pipeline", default="fake")
    r.add_argument("--state-dir", default=".factory-state")
    r.add_argument("--workspace-dir", default="workspace")
    r.set_defaults(func=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except FactoryError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
