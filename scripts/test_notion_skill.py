#!/usr/bin/env python3
"""Offline tests for the `notion` skill.

No network and no token: the HTTP layer is stubbed, so these tests are safe to
run anywhere, including CI. They cover the pieces that protect user data -- the
delete guard, id parsing, pagination, the data-source version fallback, and the
argument parser -- not the live Notion service.

Run:
  python scripts/test_notion_skill.py          # from the skill directory
  python -m unittest test_notion_skill         # from scripts/
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

# Redirect the audit log before importing the modules under test: these tests use
# stub objects, so their "writes" must never appear in the real audit trail.
os.environ["NOTION_AUDIT_LOG"] = os.devnull

sys.path.insert(0, str(Path(__file__).resolve().parent))

import notion  # noqa: E402
import notion_client  # noqa: E402

PAGE_ID = "248104cd-477e-80fd-b757-e945d38000bd"
BLOCK_ID = "148104cd-477e-80bb-928f-000ce197ddf2"


class FakeAPI:
    """Minimal stand-in for NotionAPI that records every call."""

    def __init__(self, *args, **kwargs):
        self.version = kwargs.get("version") or notion_client.DEFAULT_VERSION
        self.calls: list[tuple] = []
        self.patched: list[tuple] = []

    def get_page(self, page_id):
        self.calls.append(("get_page", page_id))
        return {
            "object": "page", "id": PAGE_ID, "url": "https://www.notion.so/x",
            "properties": {"Name": {"type": "title", "title": [
                {"plain_text": "Q1 Planning", "text": {"content": "Q1 Planning"}}]}},
            "parent": {"type": "workspace", "workspace": True},
        }

    def get_block(self, block_id):
        self.calls.append(("get_block", block_id))
        return {"object": "block", "id": BLOCK_ID, "type": "paragraph",
                "paragraph": {"rich_text": [{"plain_text": "hello world"}]},
                "has_children": False}

    def users_me(self):
        self.calls.append(("users_me",))
        return {"id": "bot-1", "name": "Test Bot", "type": "bot",
                "bot": {"workspace_name": "Test WS", "owner": {"type": "workspace"}}}

    def get(self, path, **query):
        self.calls.append(("get", path))
        raise AssertionError(f"unexpected GET {path}")

    def patch(self, path, body):
        self.calls.append(("patch", path, body))
        self.patched.append((path, body))
        return {"object": "page", "id": PAGE_ID, "url": "https://www.notion.so/x",
                "in_trash": True,
                "properties": {"Name": {"type": "title", "title": [
                    {"plain_text": "Q1 Planning", "text": {"content": "Q1 Planning"}}]}}}

    def post(self, path, body=None):
        self.calls.append(("post", path, body))
        return {"object": "page", "id": PAGE_ID, "properties": {}}

    def trash(self, object_kind, object_id):
        """Mirror NotionAPI.trash so the real PATCH body can be asserted."""
        self.calls.append(("trash", object_kind, object_id))
        self.patch(f"/{object_kind}s/{notion_client.normalize_id(object_id)}",
                   {"in_trash": True})
        if object_kind == "block":
            return {"object": "block", "id": BLOCK_ID, "type": "paragraph",
                    "paragraph": {"rich_text": [{"plain_text": "hello world"}]},
                    "in_trash": True}
        return {"object": "page", "id": PAGE_ID, "url": "https://www.notion.so/x",
                "in_trash": True,
                "properties": {"Name": {"type": "title", "title": [
                    {"plain_text": "Q1 Planning", "text": {"content": "Q1 Planning"}}]}}}


@contextlib.contextmanager
def cli(fake: FakeAPI, argv: list[str]):
    out, err = io.StringIO(), io.StringIO()
    with mock.patch.object(notion, "build_api", return_value=fake), \
            mock.patch.object(notion, "resolve_token", return_value="ntn_test"), \
            contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = notion.main(argv)
    yield code, out.getvalue(), err.getvalue()


def run(fake: FakeAPI, argv: list[str]):
    with cli(fake, argv) as result:
        return result


# --------------------------------------------------------------------------- #
# delete guard: the part that protects user data
# --------------------------------------------------------------------------- #

class DeleteGuard(unittest.TestCase):
    def test_dry_run_reports_and_deletes_nothing(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "delete", PAGE_ID])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["target"]["short_id"], "248104cd")
        self.assertEqual(payload["target"]["title"], "Q1 Planning")
        self.assertIn("nothing was deleted", payload["hint"])
        self.assertEqual([c[0] for c in fake.calls], ["get_page"])
        self.assertEqual(fake.patched, [], "dry run must not PATCH anything")

    def test_yes_without_confirm_is_refused(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "delete", PAGE_ID, "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("--yes requires --confirm", err)
        self.assertEqual(fake.patched, [])

    def test_wrong_confirm_is_refused_and_lists_candidates(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "delete", PAGE_ID, "--yes",
                                    "--confirm", "wrong-target"])
        self.assertEqual(code, 2)
        self.assertIn("does not match any target", err)
        self.assertIn("Q1 Planning", err)
        self.assertEqual(fake.patched, [])

    def test_confirm_by_exact_title_trashes_the_page(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "delete", PAGE_ID, "--yes",
                                    "--confirm", "Q1 Planning"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertFalse(payload["dry_run"])
        self.assertEqual(payload["action"], "trashed")
        self.assertEqual(len(fake.patched), 1)
        path, body = fake.patched[0]
        self.assertEqual(path, f"/pages/{notion_client.normalize_id(PAGE_ID)}")
        self.assertEqual(body, {"in_trash": True})

    def test_purge_is_refused_instead_of_faked(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "delete", PAGE_ID, "--yes",
                                    "--confirm", "Q1 Planning", "--purge"])
        self.assertEqual(code, 2)
        self.assertIn("no permanent page delete", err)
        self.assertEqual(fake.patched, [])

    def test_short_confirm_is_rejected(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "delete", PAGE_ID, "--yes", "--confirm", "24"])
        self.assertEqual(code, 2)
        self.assertIn("at least 4 characters", err)

    def test_block_dry_run_warns_about_children(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["block", "delete", BLOCK_ID])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertTrue(payload["dry_run"])
        self.assertIn("children", payload["warning"])
        self.assertEqual(fake.patched, [])

    def test_block_confirm_by_id_prefix_trashes(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["block", "delete", BLOCK_ID, "--yes",
                                    "--confirm", "148104cd"])
        self.assertEqual(code, 0, err)
        self.assertEqual(fake.patched[0][1], {"in_trash": True})

    def test_update_trash_requires_confirmation(self):
        fake = FakeAPI()
        code, out, err = run(fake, ["page", "update", PAGE_ID, "--trash", "--yes"])
        self.assertEqual(code, 2)
        self.assertIn("--confirm", err)
        self.assertEqual(fake.patched, [])

    def test_batch_guard_needs_max(self):
        targets = [{"id": f"{i:032x}", "short_id": f"{i:08x}", "title": f"row {i}"}
                   for i in range(3)]
        with self.assertRaises(notion_client.UsageError) as ctx:
            notion_client.delete_guard(yes=True, confirm="row 0", targets=targets,
                                       kind="page")
        self.assertIn("--max", str(ctx.exception))

    def test_batch_guard_enforces_max(self):
        targets = [{"id": f"{i:032x}", "short_id": f"{i:08x}", "title": f"row {i}"}
                   for i in range(5)]
        with self.assertRaises(notion_client.UsageError) as ctx:
            notion_client.delete_guard(yes=True, confirm="row 0", targets=targets,
                                       max_items=2, kind="page")
        self.assertIn("--max is 2", str(ctx.exception))

    def test_batch_guard_has_a_hard_ceiling(self):
        targets = [{"id": f"{i:032x}", "short_id": f"{i:08x}", "title": f"row {i}"}
                   for i in range(30)]
        with self.assertRaises(notion_client.UsageError) as ctx:
            notion_client.delete_guard(yes=True, confirm="row 0", targets=targets,
                                       max_items=50, kind="page")
        self.assertIn("hard ceiling", str(ctx.exception))


class AuditLog(unittest.TestCase):
    def test_writes_a_redacted_record_to_the_configured_path(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "audit.log"
            with mock.patch.object(notion_client, "AUDIT_LOG", target):
                notion_client.audit("page.delete", dry_run=False,
                                    payload={"page_id": PAGE_ID, "title": "Q1 Planning"})
            lines = target.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        record = json.loads(lines[0])
        self.assertEqual(record["event"], "page.delete")
        self.assertFalse(record["dry_run"])
        self.assertEqual(record["title"], "Q1 Planning")
        self.assertIn("at", record)
        for secret in ("ntn_", "secret_", "Bearer "):
            self.assertNotIn(secret, lines[0])


# --------------------------------------------------------------------------- #
# local commands and error mapping
# --------------------------------------------------------------------------- #

class LocalCommands(unittest.TestCase):
    def test_ref_normalises_urls_without_a_token(self):
        code, out, err = run(FakeAPI(), ["ref", f"https://www.notion.so/x/Page-{PAGE_ID}?v=1"])
        self.assertEqual(code, 0, err)
        payload = json.loads(out)
        self.assertEqual(payload["id"], notion_client.normalize_id(PAGE_ID))
        self.assertEqual(payload["short_id"], "248104cd")

    def test_whoami_reports_bot_identity(self):
        code, out, err = run(FakeAPI(), ["whoami"])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["bot"]["workspace_name"], "Test WS")


class CliErrors(unittest.TestCase):
    def test_missing_token_is_a_usage_error(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(notion, "resolve_token",
                               side_effect=notion_client.UsageError("no Notion token found")), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = notion.main(["whoami"])
        self.assertEqual(code, 2)
        self.assertIn("no Notion token found", err.getvalue())

    def test_api_error_gets_a_hint(self):
        error = notion_client.NotionError("HTTP 403: restricted", status=403,
                                          code="restricted_resource")
        with mock.patch.object(notion, "build_api") as build:
            build.return_value.users_me.side_effect = error
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = notion.main(["whoami"])
        self.assertEqual(code, 1)
        self.assertIn("share the page or database", err.getvalue())


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #

class Parsing(unittest.TestCase):
    def test_property_shorthand_types(self):
        props = notion.parse_properties([
            "Name=Hello", "Count:number=3", "Done:checkbox=true",
            "Status:select=Todo", "Tags:multi_select=a, b", "Due:date=2026-03-01",
            'Raw={"select":{"name":"x"}}',
        ])
        self.assertEqual(props["Name"], {"rich_text": [{"text": {"content": "Hello"}}]})
        self.assertEqual(props["Count"], {"number": 3.0})
        self.assertEqual(props["Done"], {"checkbox": True})
        self.assertEqual(props["Status"], {"select": {"name": "Todo"}})
        self.assertEqual(props["Tags"]["multi_select"], [{"name": "a"}, {"name": "b"}])
        self.assertEqual(props["Due"], {"date": {"start": "2026-03-01"}})
        self.assertEqual(props["Raw"], {"select": {"name": "x"}})

    def test_unknown_property_type_is_rejected(self):
        with self.assertRaises(notion_client.UsageError):
            notion.parse_properties(["X:nonsense=1"])

    def test_markdown_subset(self):
        blocks = notion.markdown_to_blocks(
            "# H1\n- item\n1. one\n- [x] done\n> quote\n---\ntext")
        self.assertEqual([b["type"] for b in blocks],
                         ["heading_1", "bulleted_list_item", "numbered_list_item",
                          "to_do", "quote", "divider", "paragraph"])
        self.assertTrue(blocks[3]["to_do"]["checked"])

    def test_markdown_code_fence(self):
        blocks = notion.markdown_to_blocks("```python\nprint(1)\n```")
        self.assertEqual(blocks[0]["type"], "code")
        self.assertEqual(blocks[0]["code"]["language"], "python")

    def test_title_marker_resolution(self):
        props = notion.resolve_title_property({"__title__": "New row"}, "名称")
        self.assertEqual(
            props, {"名称": {"title": [{"type": "text", "text": {"content": "New row"}}]}})

    def test_title_marker_without_a_title_property_is_rejected(self):
        with self.assertRaises(notion_client.UsageError):
            notion.resolve_title_property({"Status": {"select": {"name": "x"}}}, "Name")

    def test_blocks_json_must_be_a_list(self):
        args = mock.Mock(blocks_json='{"object":"block"}', markdown=None, text=None,
                         block_type="paragraph")
        with self.assertRaises(notion_client.UsageError):
            notion.build_children(args)

    def test_block_text_and_markdown_cover_common_types(self):
        for block, expected in [
            ({"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "H"}]}}, "# H"),
            ({"type": "to_do", "to_do": {"rich_text": [{"plain_text": "T"}], "checked": True}},
             "- [x] T"),
            ({"type": "divider", "divider": {}}, "---"),
            ({"type": "child_page", "child_page": {"title": "Kid"}}, "## Kid (child page)"),
        ]:
            with self.subTest(block=block["type"]):
                self.assertEqual(notion_client.block_to_markdown(block), expected)


# --------------------------------------------------------------------------- #
# HTTP layer
# --------------------------------------------------------------------------- #

class HttpLayer(unittest.TestCase):
    """Replace urlopen so URL building, pagination and version fallback are testable."""

    def setUp(self):
        self.sent: list[dict] = []
        self.responses: list[tuple[int, dict]] = []
        patcher = mock.patch.object(notion_client.urllib.request, "urlopen",
                                    self._fake_urlopen)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _fake_urlopen(self, request, timeout=None):
        body = json.loads(request.data.decode("utf-8")) if request.data else None
        self.sent.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {k.lower(): v for k, v in request.header_items()},
            "body": body,
        })
        status, payload = self.responses.pop(0)

        class Response(io.BytesIO):
            def __enter__(inner):
                return inner

            def __exit__(inner, *exc):
                return False

        if status >= 400:
            import urllib.error
            raise urllib.error.HTTPError(request.full_url, status, "err", {},
                                         io.BytesIO(json.dumps(payload).encode("utf-8")))
        return Response(json.dumps(payload).encode("utf-8"))

    def test_id_normalisation_variants(self):
        raw = "248104cd477e80fdb757e945d38000bd"
        dashed = "248104cd-477e-80fd-b757-e945d38000bd"
        for value in (raw, dashed, f"https://www.notion.so/Page-{dashed}",
                      f"https://www.notion.so/Page-{dashed}?v=148104cd477e80bb928f000ce197ddf2"):
            with self.subTest(value=value):
                self.assertEqual(notion_client.normalize_id(value), raw)
        self.assertEqual(notion_client.dashed(raw), dashed)

    def test_id_normalisation_rejects_junk(self):
        with self.assertRaises(notion_client.UsageError):
            notion_client.normalize_id("not-an-id")

    def test_headers_and_version_are_sent(self):
        self.responses = [(200, {"id": "bot-1"})]
        notion_client.NotionAPI("ntn_secret", version="2025-09-03").users_me()
        sent = self.sent[0]
        self.assertEqual(sent["url"], "https://api.notion.com/v1/users/me")
        self.assertEqual(sent["headers"]["authorization"], "Bearer ntn_secret")
        self.assertEqual(sent["headers"]["notion-version"], "2025-09-03")

    def test_pagination_follows_the_cursor(self):
        self.responses = [
            (200, {"results": [{"id": "a"}], "has_more": True, "next_cursor": "cur1"}),
            (200, {"results": [{"id": "b"}], "has_more": False, "next_cursor": None}),
        ]
        rows = notion_client.NotionAPI("t").paginate("/search", body={"query": "x"}, limit=10)
        self.assertEqual([r["id"] for r in rows], ["a", "b"])
        self.assertEqual(self.sent[0]["body"]["page_size"], 10)
        self.assertNotIn("start_cursor", self.sent[0]["body"])
        self.assertEqual(self.sent[1]["body"]["start_cursor"], "cur1")

    def test_legacy_query_falls_back_to_data_sources(self):
        self.responses = [
            (400, {"code": "validation_error",
                   "message": "Databases with multiple data sources are not supported",
                   "additional_data": {
                       "error_type": "multiple_data_sources_for_database",
                       "child_data_source_ids": ["148104cd477e80bb928f000ce197ddf2"]}}),
            (200, {"results": [{"id": "row-1"}], "has_more": False, "next_cursor": None}),
        ]
        api = notion_client.NotionAPI("t", version="2022-06-28")
        source, rows = api.query_container("248104cd477e80fdb757e945d38000bd")
        self.assertEqual(source, "148104cd477e80bb928f000ce197ddf2")
        self.assertEqual([r["id"] for r in rows], ["row-1"])
        self.assertIn("/databases/248104cd477e80fdb757e945d38000bd/query", self.sent[0]["url"])
        self.assertIn("/data_sources/148104cd477e80bb928f000ce197ddf2/query", self.sent[1]["url"])

    def test_new_version_resolves_database_to_its_first_data_source(self):
        self.responses = [
            (200, {"object": "database", "id": "248104cd477e80fdb757e945d38000bd",
                   "title": [{"plain_text": "Tasks"}],
                   "data_sources": [{"id": "148104cd477e80bb928f000ce197ddf2",
                                     "name": "Tasks"}]}),
            (200, {"results": [{"id": "row-1"}], "has_more": False, "next_cursor": None}),
        ]
        source, rows = notion_client.NotionAPI("t", version="2025-09-03").query_container(
            "248104cd477e80fdb757e945d38000bd")
        self.assertEqual(source, "148104cd477e80bb928f000ce197ddf2")
        self.assertEqual(len(rows), 1)
        self.assertIn("/databases/", self.sent[0]["url"])
        self.assertIn("/data_sources/148104cd477e80bb928f000ce197ddf2/query", self.sent[1]["url"])

    def test_comment_list_sends_block_id_as_query(self):
        self.responses = [(200, {"results": [], "has_more": False, "next_cursor": None})]
        notion_client.NotionAPI("t").paginate("/comments", method="GET",
                                              body={"block_id": "abc"}, limit=5)
        self.assertIn("block_id=abc", self.sent[0]["url"])

    def test_http_errors_map_to_notion_error(self):
        self.responses = [(401, {"code": "unauthorized", "message": "API token is invalid."})]
        with self.assertRaises(notion_client.NotionError) as ctx:
            notion_client.NotionAPI("bad").users_me()
        self.assertEqual(ctx.exception.status, 401)
        self.assertEqual(ctx.exception.code, "unauthorized")

    def test_trash_falls_back_to_archived_on_old_versions(self):
        self.responses = [
            (400, {"code": "validation_error", "message": "body failed validation: in_trash"}),
            (200, {"id": "248104cd477e80fdb757e945d38000bd", "archived": True}),
        ]
        notion_client.NotionAPI("t", version="2022-06-28").trash(
            "page", "248104cd477e80fdb757e945d38000bd")
        self.assertEqual(self.sent[1]["body"], {"archived": True})


# --------------------------------------------------------------------------- #
# argument parser regressions
# --------------------------------------------------------------------------- #

class ParserRegression(unittest.TestCase):
    def test_parser_requires_a_subcommand(self):
        with self.assertRaises(SystemExit):
            with contextlib.redirect_stderr(io.StringIO()):
                notion.build_parser().parse_args([])

    def test_format_accepted_after_subcommand(self):
        args = notion.build_parser().parse_args(["page", "children", PAGE_ID, "--format", "md"])
        self.assertEqual(notion.option(args, "format", "json"), "md")

    def test_format_accepted_before_subcommand(self):
        args = notion.build_parser().parse_args(["--format", "md", "ref", PAGE_ID])
        self.assertEqual(notion.option(args, "format", "json"), "md")

    def test_subcommand_default_does_not_clobber_main_parser_value(self):
        args = notion.build_parser().parse_args(["--api-version", "2022-06-28", "search"])
        self.assertEqual(notion.option(args, "api_version"), "2022-06-28")

    def test_api_version_accepted_after_subcommand(self):
        args = notion.build_parser().parse_args(["search", "--api-version", "2022-06-28"])
        self.assertEqual(notion.option(args, "api_version"), "2022-06-28")

    def test_every_leaf_command_is_wired_to_a_handler(self):
        """No command may be advertised in --help without a handler behind it."""
        # (command, subcommand) -> argv tail: exactly the required positionals.
        positionals = {
            ("whoami",): [], ("search",): [], ("users",): [], ("ref",): [PAGE_ID],
            ("page", "get"): [PAGE_ID], ("page", "children"): [PAGE_ID],
            ("page", "create"): ["--parent-type", "page"],
            ("page", "update"): [PAGE_ID], ("page", "append"): [PAGE_ID],
            ("page", "delete"): [PAGE_ID],
            ("block", "get"): [BLOCK_ID], ("block", "children"): [BLOCK_ID],
            ("block", "append"): [BLOCK_ID],
            ("block", "update"): [BLOCK_ID, "--text", "x"],
            ("block", "delete"): [BLOCK_ID],
            ("db", "get"): [PAGE_ID], ("db", "sources"): [PAGE_ID],
            ("db", "query"): [PAGE_ID],
            ("db", "create"): [PAGE_ID, "--title", "t", "--properties-json", "{}"],
            ("ds", "get"): [PAGE_ID], ("ds", "query"): [PAGE_ID],
            ("ds", "create-page"): [PAGE_ID], ("ds", "delete-page"): [PAGE_ID],
            ("comment", "list"): [PAGE_ID],
            ("comment", "add"): [PAGE_ID, "--text", "hi"],
        }
        self.assertEqual(set(positionals), set(notion.HANDLERS),
                         "the wiring test must cover every handler")
        for key, tail in positionals.items():
            with self.subTest(key=key):
                args = notion.build_parser().parse_args(list(key) + tail)
                resolved = (args.command,) if args.command in ("whoami", "search", "users",
                                                               "ref") \
                    else (args.command, getattr(args, f"{args.command}_command", None))
                self.assertEqual(resolved, key)
                self.assertIn(key, notion.HANDLERS)

    def test_help_lists_only_commands_with_handlers(self):
        parser = notion.build_parser()
        subactions = [action for action in parser._actions
                      if isinstance(action, notion.argparse._SubParsersAction)]
        self.assertEqual(len(subactions), 1, "expected exactly one top-level subparser set")
        for name, subparser in subactions[0].choices.items():
            if name in ("whoami", "search", "users", "ref"):
                self.assertIn((name,), notion.HANDLERS)
                continue
            nested = [action for action in subparser._actions
                      if isinstance(action, notion.argparse._SubParsersAction)]
            self.assertEqual(len(nested), 1, f"{name} should have its own subcommands")
            for leaf in nested[0].choices:
                self.assertIn((name, leaf), notion.HANDLERS,
                              f"{name} {leaf} has no handler")


if __name__ == "__main__":
    unittest.main(verbosity=2)
