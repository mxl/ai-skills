#!/usr/bin/env python3
"""Explicit review/prepare/execute entry point, separate from read-only audit."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import sys

sys.dont_write_bytecode = True

from audit_runtime import GuardUnavailable, enable_no_hydration


class _PrivateParser(argparse.ArgumentParser):
    def error(self, message):
        self.exit(2, "cleanup_actions: invalid arguments; use --help for syntax\n")


def _public(value):
    """Only render adapter evidence after removing local account identifiers."""
    from audit_all import _sanitize_value

    return _sanitize_value(value, os.environ.get("HOME", ""))


def _emit(value):
    print(json.dumps(_public(value), ensure_ascii=True, allow_nan=False))


def _absolute_path(value):
    expanded = os.path.expanduser(value)
    if ".." in Path(expanded).parts:
        raise ValueError("parent traversal is not accepted")
    return os.path.abspath(expanded)


def _parser():
    parser = _PrivateParser(
        description="Review cache evidence and prepare individually approved actions; never auto-clean.",
        allow_abbrev=False,
    )
    commands = parser.add_subparsers(dest="command", required=True, parser_class=_PrivateParser)
    review = commands.add_parser("review", help="Probe owner evidence without cleaning")
    review.add_argument("--adapter", choices=("uv", "go", "npm", "pnpm", "homebrew"), required=True)
    prepare = commands.add_parser("prepare", help="Write a private plan; does not approve or execute")
    prepare.add_argument("--adapter", choices=("uv", "go"), required=True)
    prepare.add_argument("--output", required=True, help="New private plan file; existing paths are refused")
    show = commands.add_parser("show", help="Review a fresh saved plan and its exact digest")
    show.add_argument("--plan", required=True)
    execute = commands.add_parser("execute", help="Run only an independently reviewed and approved plan")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--confirm-digest", required=True,
                         help="Exact SHA-256 from the plan selected by the user; no wildcard")
    execute.add_argument("--journal-dir", required=True, help="Private local replay/reconciliation journal")
    cloud = commands.add_parser("cloud-metadata", help="Bounded guarded metadata; no sync proof or eviction")
    cloud.add_argument("--root", required=True)
    cloud.add_argument("--max-entries", type=int, default=1000)
    cloud.add_argument("--max-depth", type=int, default=3)
    cloud.add_argument("--timeout", type=float, default=10.0)
    return parser


def _plan_summary(plan):
    from cleanup_contract import plan_digest

    return {
        "status": "PREPARED-NOT-APPROVED",
        "adapter": plan["adapter"], "action": plan["action"],
        "target": plan["target"], "version": plan["version"],
        "scope": plan["scope"], "risk": plan["risk"],
        "expires_at": plan["expires_at"],
        "estimated_reclaimable_bytes": plan["estimated_reclaimable_bytes"],
        "digest": plan_digest(plan),
        "notice": "Preparing or showing this plan does not authorize execution.",
    }


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "execute" and not re.fullmatch(r"[0-9a-f]{64}", args.confirm_digest):
        parser.error("--confirm-digest must be the exact lowercase SHA-256 of the selected plan")
    try:
        enable_no_hydration()
    except GuardUnavailable:
        _emit({"status": "BLOCKED", "reason": "no-hydration guard unavailable"})
        return 78

    try:
        if args.command == "review":
            from cache_adapters import discover

            evidence = discover(args.adapter)
            _emit({"mode": "review", "actionable": False, "evidence": evidence})
            return 0
        if args.command == "prepare":
            from cache_adapters import prepare
            from cleanup_contract import validate_plan, write_private_json

            plan = validate_plan(prepare(args.adapter))
            output = _absolute_path(args.output)
            write_private_json(output, plan)
            summary = _plan_summary(plan)
            summary["plan_file"] = os.path.relpath(output, Path.cwd())
            _emit(summary)
            return 0
        if args.command in ("show", "execute"):
            from cleanup_contract import load_plan

            plan = load_plan(_absolute_path(args.plan))
            if args.command == "show":
                _emit(_plan_summary(plan))
                return 0
            from cleanup_executor import execute

            outcome = execute(plan, confirmed_digest=args.confirm_digest,
                              journal_dir=_absolute_path(args.journal_dir))
            _emit(outcome)
            return 0 if outcome.get("status") in ("EXECUTED", "UNCHANGED") else 1
        if args.command == "cloud-metadata":
            from cloud_inventory import inventory

            _emit(inventory(_absolute_path(args.root), max_entries=args.max_entries,
                            max_depth=args.max_depth, timeout=args.timeout))
            return 0
    except KeyboardInterrupt:
        _emit({"status": "PARTIAL", "reason": "cancelled; reconcile journal before retrying any action"})
        return 130
    except ImportError:
        _emit({"status": "BLOCKED", "reason": "required adapter is not installed"})
        return 78
    except (ValueError, OSError, RuntimeError):
        # Do not leak private paths or owner stderr through exceptions.
        _emit({"status": "BLOCKED", "reason": "plan, target, owner evidence or private storage validation failed"})
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
