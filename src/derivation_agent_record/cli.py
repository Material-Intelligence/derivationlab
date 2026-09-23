"""Command-line interface for Derivation Agent Record v1 and v1.1."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .model import ContractError, load_events
from .render import render_html
from .replay import replay_events


def _write(path: str | Path, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    # Under `python -m` argparse would name the program "__main__.py".
    prog = Path(sys.argv[0]).name if sys.argv and not sys.argv[0].endswith("__main__.py") else None
    parser = argparse.ArgumentParser(prog=prog or "python -m derivation_agent_record", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    verify = sub.add_parser("verify", help="verify hash chain and replay semantics")
    verify.add_argument("events")

    replay = sub.add_parser("replay", help="write canonical replay JSON")
    replay.add_argument("events")
    replay.add_argument("--output", required=True)

    render = sub.add_parser("render", help="write a self-contained read-only HTML audit view")
    render.add_argument("events")
    render.add_argument("--output", required=True)

    build = sub.add_parser("build", help="write canonical JSON and HTML in one verified replay")
    build.add_argument("events")
    build.add_argument("--canonical", required=True)
    build.add_argument("--html", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        events = load_events(args.events)
        result = replay_events(events)
        if args.command == "verify":
            print(
                "ok: "
                f"{result.canonical['event_log']['event_count']} events, "
                f"{result.canonical['summary']['branch_count']} branches, "
                f"{result.canonical['summary']['candidate_count']} candidates, "
                f"{result.canonical['summary']['judgement_count']} judgements"
            )
        elif args.command == "replay":
            _write(args.output, result.to_json())
        elif args.command == "render":
            _write(args.output, render_html(result.canonical, events))
        elif args.command == "build":
            _write(args.canonical, result.to_json())
            _write(args.html, render_html(result.canonical, events))
        return 0
    except ContractError as exc:
        print(f"contract error: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        # A missing file or an unwritable output directory is an operator
        # mistake, not a statement about the record, so it gets its own exit
        # code: a pipeline that gates on 2 must not read "I could not open it"
        # as "the record is unsound". A traceback would gate nothing at all.
        where = exc.filename or args.events
        print(f"error: {exc.strerror or exc}: {where}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
