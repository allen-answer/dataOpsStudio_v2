"""Command-line adapter for the stable full-package payload updater."""

from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from updater import PayloadUpdater, RuntimeSelection, UpdateReceipt


def _updater(args: argparse.Namespace) -> PayloadUpdater:
    return PayloadUpdater(
        bundle_root=Path(args.bundle_root),
        state_root=Path(args.state_root),
    )


def _selection(value: RuntimeSelection) -> dict[str, object]:
    release = value.release
    return {
        "root": str(value.root),
        "release": None
        if release is None
        else {
            "version": release.version,
            "commit": release.commit,
            "image_version": release.image_version,
        },
    }


def _receipt(value: UpdateReceipt) -> dict[str, object]:
    return {
        "release": {
            "version": value.release.version,
            "commit": value.release.commit,
            "image_version": value.release.image_version,
        },
        "previous_version": None if value.previous is None else value.previous.version,
    }


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="DataOps Studio signed payload updater")
    commands = parser.add_subparsers(dest="command", required=True)

    def roots(command: argparse.ArgumentParser) -> None:
        command.add_argument("--bundle-root", required=True)
        command.add_argument("--state-root", required=True)

    install = commands.add_parser("install", help="verify, stage and activate an update")
    roots(install)
    install.add_argument("--package", required=True)
    install.add_argument("--trust-store", required=True)

    complete = commands.add_parser("complete", help="commit or auto-rollback after health probe")
    roots(complete)
    complete.add_argument("--result", choices=("healthy", "failed"), required=True)

    rollback = commands.add_parser("rollback", help="consume the retained manual rollback")
    roots(rollback)

    status = commands.add_parser("status", help="show the selected payload")
    roots(status)

    run = commands.add_parser("run", help="run app.launcher from the selected payload")
    roots(run)
    run.add_argument("--python", required=True)
    run.add_argument("--home", required=True)
    run.add_argument("--pg-bin-dir", required=True)
    run.add_argument("--uv", required=True)
    run.add_argument("launcher_args", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    updater = _updater(args)
    if args.command == "install":
        _print(
            _receipt(
                updater.install(
                    package=Path(args.package),
                    trust_store=Path(args.trust_store),
                )
            )
        )
        return 0
    if args.command == "complete":
        _print(_selection(updater.complete(success=args.result == "healthy")))
        return 0
    if args.command == "rollback":
        _print(_selection(updater.rollback()))
        return 0
    if args.command == "status":
        _print(_selection(updater.resolve_active()))
        return 0
    if args.command == "run":
        launcher_args = list(args.launcher_args)
        if launcher_args and launcher_args[0] == "--":
            launcher_args.pop(0)
        return updater.run_launcher(
            python=Path(args.python),
            home=Path(args.home),
            pg_bin_dir=Path(args.pg_bin_dir),
            uv=Path(args.uv),
            arguments=launcher_args,
        )
    raise AssertionError("unreachable updater command")


if __name__ == "__main__":
    raise SystemExit(main())
