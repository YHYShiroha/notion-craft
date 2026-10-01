#!/usr/bin/env python3
"""Credential helper for the `notion` DSH skill.

Usage:
  python notion_auth.py set-token <token> [--note "..."]   # store the token
  python notion_auth.py check                              # verify token + print identity
  python notion_auth.py where                              # show which token source wins
  python notion_auth.py clear                              # delete the stored token

The token is written to ~/.dsh/notion-credentials.json (override with
NOTION_CREDENTIALS_FILE). It is never written to the audit log or to SKILL.md.
Resolution order at runtime: --token flag, then NOTION_TOKEN /
NOTION_API_KEY / NOTION_INTEGRATION_TOKEN, then the credentials file.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notion_client import (  # noqa: E402
    CREDENTIALS_PATH,
    DEFAULT_VERSION,
    NotionAPI,
    NotionError,
    UsageError,
    resolve_token,
    save_token,
)

ENV_NAMES = ("NOTION_TOKEN", "NOTION_API_KEY", "NOTION_INTEGRATION_TOKEN")


def cmd_set_token(args) -> int:
    path = save_token(args.token, note=args.note)
    print(f"stored token in {path}")
    print("verifying...")
    try:
        me = NotionAPI(args.token, version=args.api_version).users_me()
    except NotionError as exc:
        print(f"warning: stored, but verification failed: {exc}", file=sys.stderr)
        print("check that the token is a Notion *integration* secret (ntn_... or secret_...).",
              file=sys.stderr)
        return 1
    print(f"ok: {me.get('name')} ({(me.get('bot') or {}).get('workspace_name')})")
    return 0


def cmd_check(args) -> int:
    try:
        token = resolve_token(args.token)
    except UsageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    try:
        me = NotionAPI(token, version=args.api_version).users_me()
    except NotionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"name": me.get("name"), "type": me.get("type"),
                      "workspace_name": (me.get("bot") or {}).get("workspace_name"),
                      "api_version": args.api_version}, ensure_ascii=False, indent=2))
    return 0


def cmd_where(args) -> int:
    if args.token:
        print("source: --token flag")
        return 0
    for name in ENV_NAMES:
        value = os.environ.get(name)
        if value and value.strip():
            print(f"source: environment variable {name} (token {_mask(value)})")
            return 0
    if CREDENTIALS_PATH.is_file():
        try:
            payload = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
            token = payload.get("token", "")
        except (OSError, json.JSONDecodeError):
            token = ""
        print(f"source: credentials file {CREDENTIALS_PATH} (token {_mask(token)})")
        return 0
    print("source: none. No token found; run `notion_auth.py set-token <token>`.")
    return 2


def cmd_clear(args) -> int:
    if not CREDENTIALS_PATH.is_file():
        print(f"nothing to clear: {CREDENTIALS_PATH} does not exist")
        return 0
    if not args.yes:
        print(f"would delete {CREDENTIALS_PATH}. Re-run with --yes to confirm.")
        return 0
    CREDENTIALS_PATH.unlink()
    print(f"deleted {CREDENTIALS_PATH}")
    return 0


def _mask(token: str) -> str:
    token = (token or "").strip()
    if len(token) <= 8:
        return "****"
    return f"{token[:4]}...{token[-4:]}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="notion_auth.py",
                                     description="Manage the Notion token for the `notion` skill.")
    parser.add_argument("--token", help="token to use instead of stored/env (for check/where)")
    parser.add_argument("--api-version", default=os.environ.get("NOTION_VERSION", DEFAULT_VERSION))
    sub = parser.add_subparsers(dest="command", required=True)

    set_parser = sub.add_parser("set-token", help="store a token and verify it")
    set_parser.add_argument("token")
    set_parser.add_argument("--note", help="optional note stored next to the token")

    sub.add_parser("check", help="verify the resolved token")
    sub.add_parser("where", help="show which credential source is used")

    clear = sub.add_parser("clear", help="delete the stored token")
    clear.add_argument("--yes", action="store_true", help="actually delete")

    args = parser.parse_args(argv)
    handlers = {"set-token": cmd_set_token, "check": cmd_check, "where": cmd_where,
                "clear": cmd_clear}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
