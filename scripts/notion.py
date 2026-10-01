#!/usr/bin/env python3
"""`notion` DSH skill CLI.

Read and write Notion pages, databases and blocks from the terminal.

Safety defaults:
  * `page delete`, `block delete` and `ds delete-page` only *report* what they
    would remove unless you pass both `--yes` and `--confirm <exact id or title>`;
  * deletes move objects to the Notion trash (`in_trash: true`), which is
    recoverable, unless `--purge` is given;
  * every write (including dry runs) is appended to the skill's audit log.

Run `python notion.py <command> --help` for details on any command.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from notion_client import (  # noqa: E402
    DEFAULT_VERSION,
    LEGACY_VERSION,
    NotionAPI,
    NotionError,
    UsageError,
    audit,
    block_to_markdown,
    block_text,
    chunked,
    dashed,
    delete_guard,
    describe_target,
    emit,
    fail,
    flatten_blocks,
    load_json_arg,
    markdown_to_blocks,
    normalize_id,
    object_title,
    page_summary,
    parse_properties,
    resolve_token,
    rich_text_plain,
    short_id,
    text_to_blocks,
)

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2


# --------------------------------------------------------------------------- #
# global options
# --------------------------------------------------------------------------- #

def add_global_options(parser: argparse.ArgumentParser, *, suppressed: bool = False) -> None:
    """Global flags. `suppressed` lets them be repeated on every subparser so they
    are accepted both before and after the subcommand without clobbering each other."""
    default = argparse.SUPPRESS if suppressed else None
    parser.add_argument("--token", default=default,
                        help="Notion integration token (default: env or "
                             "~/.dsh/notion-credentials.json)")
    parser.add_argument("--api-version", default=default,
                        help=f"Notion-Version header (default {DEFAULT_VERSION}; use "
                             f"{LEGACY_VERSION} for pre-data-source workspaces)")
    parser.add_argument("--format", default=default or "json", choices=["json", "md", "markdown"],
                        help="output format (default json)")
    parser.add_argument("--verbose", action="store_true", default=default and argparse.SUPPRESS,
                        help="log every HTTP request to stderr")


def option(args, name: str, fallback=None):
    """Read a global option that may live on the subparser or the main parser."""
    value = getattr(args, name, None)
    return fallback if value is None else value


def add_filter_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--filter", help="Notion filter JSON, @file, or - for stdin")
    parser.add_argument("--sort", action="append", default=[],
                        help="sort JSON object, repeatable, e.g. --sort '{\"property\":\"Name\"}'")
    parser.add_argument("--limit", type=int, default=50, help="max rows (default 50, cap 100)")
    parser.add_argument("--page-size", type=int, default=100, help="rows per request (cap 100)")


def add_children_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--text", help="plain text body; one block per non-empty line "
                                       "(@file or - for stdin)")
    parser.add_argument("--markdown", help="Markdown body (#/##/###, -, 1., - [ ], >, ---, ```); "
                                           "@file or - for stdin")
    parser.add_argument("--blocks-json", help="raw Notion block array as JSON, @file, or -")
    parser.add_argument("--block-type", default="paragraph",
                        help="block type used for --text (default paragraph)")


def add_delete_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--yes", action="store_true",
                        help="actually delete; without it the command only reports")
    parser.add_argument("--confirm", help="exact id prefix or exact title of the target; "
                                          "required together with --yes")
    parser.add_argument("--purge", action="store_true",
                        help="permanently delete instead of moving to the trash "
                             "(only where the API supports it)")


def build_parser() -> argparse.ArgumentParser:
    # every subparser carries the global flags too, so `--format md` works both
    # before and after the subcommand; defaults are SUPPRESSed so the main
    # parser's value survives.
    global_parser = argparse.ArgumentParser(add_help=False)
    add_global_options(global_parser, suppressed=True)
    GLOBAL = [global_parser]  # noqa: N806 - used as argparse `parents=GLOBAL`

    parser = argparse.ArgumentParser(
        prog="notion.py",
        description="Read and write Notion content (DSH `notion` skill).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Deletes are dry runs unless --yes and --confirm are both given.",
    )
    add_global_options(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    # whoami ---------------------------------------------------------------- #
    sub.add_parser("whoami", help="validate the token and show the integration identity", parents=GLOBAL)

    # search ---------------------------------------------------------------- #
    search = sub.add_parser("search", help="search pages and data sources by title", parents=GLOBAL)
    search.add_argument("query", nargs="?", default="", help="search text (empty = everything shared)")
    search.add_argument("--type", choices=["page", "data_source", "database"], default=None,
                        help="restrict the object type")
    search.add_argument("--limit", type=int, default=25)

    # users ----------------------------------------------------------------- #
    users = sub.add_parser("users", help="list workspace users visible to the integration", parents=GLOBAL)
    users.add_argument("--limit", type=int, default=50)

    # page ------------------------------------------------------------------ #
    page = sub.add_parser("page", help="page operations", parents=GLOBAL)
    page_sub = page.add_subparsers(dest="page_command", required=True)

    page_get = page_sub.add_parser("get", help="retrieve one page and its properties", parents=GLOBAL)
    page_get.add_argument("page_id")
    page_get.add_argument("--no-properties", action="store_true")

    page_children = page_sub.add_parser("children", help="read a page body as blocks", parents=GLOBAL)
    page_children.add_argument("page_id")
    page_children.add_argument("--depth", type=int, default=2, help="recursion depth (default 2)")
    page_children.add_argument("--limit", type=int, default=200, help="max top-level blocks")

    page_create = page_sub.add_parser("create", help="create a page", parents=GLOBAL)
    page_create.add_argument("--parent-type", choices=["page", "database", "data_source", "workspace"],
                             required=True)
    page_create.add_argument("--parent-id", default="", help="parent id or url (not needed for workspace)")
    page_create.add_argument("--prop", action="append", default=[],
                             help="Property=Value, or 'Property:type=Value'; repeatable",)
    page_create.add_argument("--title", help="shortcut for the title property when creating in a page")
    page_create.add_argument("--icon", help="emoji or image url")
    page_create.add_argument("--cover", help="cover image url")
    add_children_options(page_create)

    page_update = page_sub.add_parser("update", help="update a page's properties", parents=GLOBAL)
    page_update.add_argument("page_id")
    page_update.add_argument("--prop", action="append", default=[], help="Property=Value; repeatable")
    page_update.add_argument("--title", help="shortcut to set the title property")
    page_update.add_argument("--icon", help="emoji or image url")
    page_update.add_argument("--cover", help="cover image url")
    page_update.add_argument("--trash", action="store_true", help="also move the page to the trash")
    page_update.add_argument("--yes", action="store_true", help="required with --trash")
    page_update.add_argument("--confirm", help="exact id prefix or title; required with --trash")

    page_append = page_sub.add_parser("append", help="append blocks to a page", parents=GLOBAL)
    page_append.add_argument("page_id")
    add_children_options(page_append)

    page_delete = page_sub.add_parser("delete", help="move a page to the trash (dry run by default)", parents=GLOBAL)
    page_delete.add_argument("page_id")
    add_delete_options(page_delete)

    # block ----------------------------------------------------------------- #
    block = sub.add_parser("block", help="block operations", parents=GLOBAL)
    block_sub = block.add_subparsers(dest="block_command", required=True)

    block_get = block_sub.add_parser("get", help="retrieve one block", parents=GLOBAL)
    block_get.add_argument("block_id")

    block_children = block_sub.add_parser("children", help="read a block's children", parents=GLOBAL)
    block_children.add_argument("block_id")
    block_children.add_argument("--limit", type=int, default=200)

    block_append = block_sub.add_parser("append", help="append children to a block", parents=GLOBAL)
    block_append.add_argument("block_id")
    add_children_options(block_append)

    block_update = block_sub.add_parser("update", help="replace a block's text", parents=GLOBAL)
    block_update.add_argument("block_id")
    block_update.add_argument("--text", required=True, help="new plain text content")

    block_delete = block_sub.add_parser("delete", help="remove a block (dry run by default)", parents=GLOBAL)
    block_delete.add_argument("block_id")
    add_delete_options(block_delete)

    # db / ds --------------------------------------------------------------- #
    db = sub.add_parser("db", help="database (container) operations", parents=GLOBAL)
    db_sub = db.add_subparsers(dest="db_command", required=True)

    db_get = db_sub.add_parser("get", help="retrieve a database", parents=GLOBAL)
    db_get.add_argument("database_id")

    db_sources = db_sub.add_parser("sources", help="list the data sources of a database", parents=GLOBAL)
    db_sources.add_argument("database_id")

    db_query = db_sub.add_parser("query", help="query rows (resolves the data source automatically)", parents=GLOBAL)
    db_query.add_argument("database_id")
    add_filter_options(db_query)

    db_create = db_sub.add_parser("create", help="create a database under a page", parents=GLOBAL)
    db_create.add_argument("parent_page_id")
    db_create.add_argument("--title", required=True, help="database title")
    db_create.add_argument("--properties-json", required=True,
                           help="property schema JSON, @file, or -")

    ds = sub.add_parser("ds", help="data source operations (2025-09-03 API model)", parents=GLOBAL)
    ds_sub = ds.add_subparsers(dest="ds_command", required=True)

    ds_get = ds_sub.add_parser("get", help="retrieve a data source", parents=GLOBAL)
    ds_get.add_argument("data_source_id")

    ds_query = ds_sub.add_parser("query", help="query rows of a data source", parents=GLOBAL)
    ds_query.add_argument("data_source_id")
    add_filter_options(ds_query)

    ds_create = ds_sub.add_parser("create-page", help="create a row in a data source", parents=GLOBAL)
    ds_create.add_argument("data_source_id")
    ds_create.add_argument("--prop", action="append", default=[])
    ds_create.add_argument("--title", help="shortcut for the title property")
    add_children_options(ds_create)

    ds_delete = ds_sub.add_parser("delete-page", help="move a row to the trash (dry run by default)", parents=GLOBAL)
    ds_delete.add_argument("page_id")
    add_delete_options(ds_delete)

    # comments -------------------------------------------------------------- #
    comment = sub.add_parser("comment", help="read and add comments", parents=GLOBAL)
    comment_sub = comment.add_subparsers(dest="comment_command", required=True)

    comment_list = comment_sub.add_parser("list", help="list comments on a page or block", parents=GLOBAL)
    comment_list.add_argument("target_id")
    comment_list.add_argument("--limit", type=int, default=50)

    comment_add = comment_sub.add_parser("add", help="add a comment to a page", parents=GLOBAL)
    comment_add.add_argument("page_id")
    comment_add.add_argument("--text", required=True)

    # ref ------------------------------------------------------------------- #
    ref = sub.add_parser("ref", help="convert between Notion ids and urls", parents=GLOBAL)
    ref.add_argument("value", help="a Notion url or id")

    return parser


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def build_api(args) -> NotionAPI:
    return NotionAPI(resolve_token(option(args, "token")),
                     version=option(args, "api_version",
                                    os.environ.get("NOTION_VERSION", DEFAULT_VERSION)),
                     verbose=bool(option(args, "verbose", False)))


def content_arg(value: str) -> str:
    """Resolve a body argument: `-` reads stdin, `@path` reads that file.

    Mirrors what --blocks-json already did. Without this, `--markdown -` would be
    taken literally and create a one-character paragraph containing "-".
    """
    if value == "-":
        return sys.stdin.read()
    if value.startswith("@"):
        try:
            return Path(value[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read content from {value[1:]}: {exc}") from exc
    return value


def build_children(args) -> list[dict]:
    blocks: list[dict] = []
    if getattr(args, "blocks_json", None):
        payload = load_json_arg(args.blocks_json)
        if isinstance(payload, dict) and "children" in payload:
            payload = payload["children"]
        if not isinstance(payload, list):
            raise UsageError("--blocks-json must be a JSON array of blocks")
        blocks.extend(payload)
    if getattr(args, "markdown", None):
        blocks.extend(markdown_to_blocks(content_arg(args.markdown)))
    if getattr(args, "text", None):
        blocks.extend(text_to_blocks(content_arg(args.text),
                                     getattr(args, "block_type", "paragraph")))
    return blocks


def build_properties(args) -> dict:
    """Properties from --prop, plus a temporary `__title__` marker for --title.

    The marker exists because the title property's real name is workspace
    specific ("Name", "title", "名称", ...). It is resolved against the parent
    schema by :func:`resolve_title_property` before anything is sent.
    """
    properties = parse_properties(getattr(args, "prop", None))
    title = getattr(args, "title", None)
    if title is not None:
        if "__title__" in properties:
            raise UsageError("title given twice")
        properties["__title__"] = title
    return properties


TITLE_MARKER = "__title__"


def _known_title_name(properties: dict) -> str | None:
    """Names that already carry a title-typed property value."""
    for name, value in properties.items():
        if isinstance(value, dict) and value.get("title") is not None:
            return name
    return None


def resolve_title_property(properties: dict, title_name: str | None) -> dict:
    """Replace the `__title__` marker with the parent's real title property."""
    marker = properties.pop(TITLE_MARKER, None)
    existing = _known_title_name(properties)
    if marker is None:
        if existing is None:
            raise UsageError(
                "a new page needs a title: pass --title, or --prop '<TitleProperty>=...' "
                "using the exact property name from the parent database"
            )
        return properties
    name = existing or title_name or "title"
    properties[name] = {"title": [{"type": "text", "text": {"content": str(marker)}}]}
    return properties


def parent_title_property(api: NotionAPI, parent_type: str, parent_id: str) -> str | None:
    """Ask the parent database/data source for the name of its title property."""
    if parent_type not in ("database", "data_source") or not parent_id:
        return None
    try:
        if parent_type == "data_source":
            schema = api.get(f"/data_sources/{normalize_id(parent_id)}")
        else:
            info = api.resolve_container(parent_id)
            if info["kind"] == "data_source":
                schema = info["object"]
            elif info["sources"]:
                schema = api.get(f"/data_sources/{normalize_id(info['sources'][0]['id'])}")
            else:
                schema = info["object"]
    except NotionError:
        return None
    for name, prop in ((schema.get("properties") or {}) if isinstance(schema, dict) else {}).items():
        if isinstance(prop, dict) and prop.get("type") == "title":
            return name
    return None


def parse_sorts(raw_sorts: list[str]) -> list[dict]:
    sorts = []
    for raw in raw_sorts:
        payload = load_json_arg(raw)
        if isinstance(payload, list):
            sorts.extend(payload)
        elif isinstance(payload, dict):
            sorts.append(payload)
        else:
            raise UsageError("--sort must be a JSON object or array")
    return sorts


def load_optional_json(raw: str | None) -> dict | None:
    if not raw:
        return None
    payload = load_json_arg(raw)
    if not isinstance(payload, dict):
        raise UsageError("expected a JSON object")
    return payload


def append_blocks(api: NotionAPI, block_id: str, blocks: list[dict], *,
                  dry_run_report: bool = False) -> dict:
    if not blocks:
        raise UsageError("no content to append: pass --text, --markdown or --blocks-json")
    appended: list[dict] = []
    for batch in chunked(blocks, 100):
        response = api.patch(f"/blocks/{normalize_id(block_id)}/children", {"children": batch})
        appended.extend(response.get("results") or [])
    return {
        "parent": normalize_id(block_id),
        "appended_count": len(appended),
        "appended": [{"id": item.get("id"), "short_id": short_id(item["id"]) if item.get("id") else None,
                      "type": item.get("type")} for item in appended],
    }


def fetch_page_target(api: NotionAPI, page_id: str) -> dict:
    page = api.get_page(page_id)
    return {"id": page.get("id"), "short_id": short_id(page["id"]),
            "title": object_title(page), "type": "page"}


def fetch_block_target(api: NotionAPI, block_id: str) -> dict:
    block = api.get_block(block_id)
    return {"id": block.get("id"), "short_id": short_id(block["id"]),
            "title": block_text(block) or block.get("type"),
            "type": f"block/{block.get('type')}"}


def print_rows(result: dict, fmt: str) -> None:
    if fmt == "json":
        emit(result, fmt="json")
        return
    rows = result.get("results") or []
    print(f"# {result.get('title') or result.get('source') or 'results'}")
    print(f"count: {len(rows)}  source: {result.get('source')}  has_more: {result.get('has_more')}")
    for row in rows:
        label = row.get("title") or row.get("text") or ""
        print(f"- {row.get('short_id')}  {label}")


# --------------------------------------------------------------------------- #
# command handlers
# --------------------------------------------------------------------------- #

def cmd_whoami(api: NotionAPI, args) -> dict:
    me = api.users_me()
    return {
        "bot": {
            "id": me.get("id"),
            "name": me.get("name"),
            "type": me.get("type"),
            "workspace_name": (me.get("bot") or {}).get("workspace_name"),
            "owner": ((me.get("bot") or {}).get("owner") or {}).get("type"),
        },
        "api_version": api.version,
        "note": "If a page or database is missing later, share it with this integration.",
    }


def cmd_search(api: NotionAPI, args) -> dict:
    body: dict = {"query": args.query or ""}
    wanted = args.type
    if wanted == "database" and api.version >= "2025-09-03":
        wanted = "data_source"
    if wanted:
        body["filter"] = {"property": "object", "value": wanted}
    results = api.paginate("/search", body=body, limit=max(1, args.limit))
    rows = []
    for item in results:
        rows.append({
            "id": item.get("id"),
            "short_id": short_id(item["id"]) if item.get("id") else None,
            "object": item.get("object"),
            "title": object_title(item),
            "url": item.get("url"),
            "last_edited_time": item.get("last_edited_time"),
        })
    return {"query": args.query, "count": len(rows), "results": rows}


def cmd_users(api: NotionAPI, args) -> dict:
    users = api.paginate("/users", method="GET", limit=max(1, args.limit))
    return {"count": len(users), "results": [
        {"id": u.get("id"), "name": u.get("name"), "type": u.get("type"),
         "person_email": (u.get("person") or {}).get("email")} for u in users]}


def cmd_page_get(api: NotionAPI, args) -> dict:
    page = api.get_page(args.page_id)
    summary = page_summary(page, include_props=not args.no_properties)
    if option(args, "format", "json") in ("md", "markdown"):
        summary["markdown"] = f"# {summary['title']}\n\nurl: {summary.get('url')}\n\n" + \
            "\n".join(f"- {k}: {json.dumps(v, ensure_ascii=False)}"
                      for k, v in summary["properties"].items())
    return summary


def cmd_page_children(api: NotionAPI, args) -> dict:
    blocks = api.block_children(args.page_id, limit=max(1, args.limit))
    children_of: dict[str, list[dict]] = {}
    if args.depth > 0:
        for block in blocks:
            if block.get("has_children"):
                children_of[block["id"]] = api.block_children(block["id"], limit=200)
    rows = flatten_blocks(blocks, children_of, max_depth=max(0, args.depth))
    markdown = "\n\n".join(row["markdown"] for row in rows if row.get("markdown"))
    return {"page_id": normalize_id(args.page_id), "count": len(rows), "blocks": rows,
            "markdown": markdown}


def cmd_page_create(api: NotionAPI, args) -> dict:
    properties = build_properties(args)
    if not properties:
        raise UsageError("a new page needs at least a title: pass --title or --prop")
    title_name = parent_title_property(api, args.parent_type, args.parent_id)
    properties = resolve_title_property(properties, title_name)
    children = build_children(args)
    page = api.create_page(args.parent_type, args.parent_id, properties,
                           children=children or None, icon=args.icon, cover=args.cover)
    result = page_summary(page)
    result["dry_run"] = False
    audit("page.create", dry_run=False, payload={
        "page_id": page.get("id"), "parent_type": args.parent_type,
        "parent_id": args.parent_id, "title": result["title"], "children": len(children)})
    return result


def _has_title(properties: dict) -> bool:
    return any(isinstance(v, dict) and v.get("title") is not None for v in properties.values())


def cmd_page_update(api: NotionAPI, args) -> dict:
    if not any([args.prop, args.title, args.icon, args.cover, args.trash]):
        raise UsageError("nothing to update: pass --prop/--title/--icon/--cover/--trash")
    body: dict = {}
    properties = parse_properties(args.prop)
    if args.title is not None:
        properties["title"] = {"title": [{"text": {"content": args.title}}]}
    if properties:
        body["properties"] = properties
    if args.icon:
        body["icon"] = {"type": "emoji", "emoji": args.icon} if len(args.icon) <= 8 \
            else {"type": "external", "external": {"url": args.icon}}
    if args.cover:
        body["cover"] = {"type": "external", "external": {"url": args.cover}}
    if args.trash:
        target = fetch_page_target(api, args.page_id)
        delete_guard(yes=args.yes, confirm=args.confirm, targets=[target], kind="page")
        body["in_trash"] = True
    updated = api.patch(f"/pages/{normalize_id(args.page_id)}", body)
    result = page_summary(updated)
    audit("page.update", dry_run=False, payload={
        "page_id": result.get("id"), "fields": sorted(body), "trashed": bool(args.trash)})
    return result


def cmd_page_append(api: NotionAPI, args) -> dict:
    blocks = build_children(args)
    result = append_blocks(api, args.page_id, blocks)
    audit("page.append", dry_run=False, payload={
        "page_id": normalize_id(args.page_id), "appended": result["appended_count"]})
    return result


def _delete_page(api: NotionAPI, args, *, event: str) -> dict:
    target = fetch_page_target(api, args.page_id)
    dry_run = not args.yes
    delete_guard(yes=args.yes, confirm=args.confirm, targets=[target], purge=args.purge,
                 kind="page")
    if dry_run:
        result = {"dry_run": True, "action": "trash" if not args.purge else "purge",
                  "target": target,
                  "hint": "nothing was deleted. Re-run with --yes --confirm "
                          f"{target['short_id']} to proceed."}
        audit(event, dry_run=True, payload={"page_id": target["id"], "title": target["title"]})
        return result
    if args.purge:
        raise UsageError(
            "the Notion API has no permanent page delete for integrations; use "
            "--yes without --purge to move the page to the trash (recoverable in Notion)"
        )
    deleted = api.trash("page", target["id"])
    result = {"dry_run": False, "action": "trashed", "title": object_title(deleted),
              "page_id": deleted.get("id"), "in_trash": deleted.get("in_trash")}
    audit(event, dry_run=False, payload={"page_id": deleted.get("id"), "title": result["title"]})
    return result


def cmd_page_delete(api: NotionAPI, args) -> dict:
    return _delete_page(api, args, event="page.delete")


def cmd_ds_delete_page(api: NotionAPI, args) -> dict:
    return _delete_page(api, args, event="ds.delete_page")


def cmd_block_get(api: NotionAPI, args) -> dict:
    block = api.get_block(args.block_id)
    return {"id": block.get("id"), "short_id": short_id(block["id"]), "type": block.get("type"),
            "text": block_text(block), "has_children": block.get("has_children"),
            "markdown": block_to_markdown(block)}


def cmd_block_children(api: NotionAPI, args) -> dict:
    blocks = api.block_children(args.block_id, limit=max(1, args.limit))
    rows = flatten_blocks(blocks, max_depth=0)
    return {"block_id": normalize_id(args.block_id), "count": len(rows), "blocks": rows,
            "markdown": "\n\n".join(row["markdown"] for row in rows if row.get("markdown"))}


def cmd_block_append(api: NotionAPI, args) -> dict:
    blocks = build_children(args)
    result = append_blocks(api, args.block_id, blocks)
    audit("block.append", dry_run=False, payload={
        "block_id": normalize_id(args.block_id), "appended": result["appended_count"]})
    return result


def cmd_block_update(api: NotionAPI, args) -> dict:
    block = api.get_block(args.block_id)
    kind = block.get("type")
    if kind in ("child_page", "child_database", "divider", "table_of_contents", "breadcrumb"):
        raise UsageError(f"block type {kind} has no editable text")
    payload = {"rich_text": [{"type": "text", "text": {"content": args.text}}]}
    updated = api.patch(f"/blocks/{normalize_id(args.block_id)}", {kind: payload})
    result = {"id": updated.get("id"), "short_id": short_id(updated["id"]),
              "type": updated.get("type"), "text": block_text(updated)}
    audit("block.update", dry_run=False, payload={"block_id": result["id"]})
    return result


def cmd_block_delete(api: NotionAPI, args) -> dict:
    target = fetch_block_target(api, args.block_id)
    dry_run = not args.yes
    delete_guard(yes=args.yes, confirm=args.confirm, targets=[target], purge=args.purge,
                 kind="block")
    if dry_run:
        result = {"dry_run": True, "action": "trash", "target": target,
                  "hint": "nothing was deleted. Re-run with --yes --confirm "
                          f"{target['short_id']} to proceed.",
                  "warning": "deleting a parent block removes all of its children"}
        audit("block.delete", dry_run=True, payload={"block_id": target["id"]})
        return result
    if args.purge:
        raise UsageError("the Notion API has no permanent block delete; run without --purge")
    deleted = api.trash("block", target["id"])
    result = {"dry_run": False, "action": "trashed", "block_id": deleted.get("id"),
              "type": deleted.get("type"), "text": block_text(deleted)}
    audit("block.delete", dry_run=False, payload={"block_id": deleted.get("id")})
    return result


def cmd_db_get(api: NotionAPI, args) -> dict:
    info = api.resolve_container(args.database_id)
    obj = info["object"]
    return {"kind": info["kind"], "id": obj.get("id"),
            "short_id": short_id(obj["id"]) if obj.get("id") else None,
            "title": object_title(obj),
            "data_sources": [{"id": s.get("id"), "name": s.get("name")} for s in info["sources"]],
            "properties": sorted((obj.get("properties") or {}).keys()),
            "url": obj.get("url"),
            "raw_object": obj.get("object")}


def cmd_db_sources(api: NotionAPI, args) -> dict:
    info = api.resolve_container(args.database_id)
    sources = info["sources"]
    if not sources and info["kind"] == "data_source":
        sources = [info["object"]]
    return {"database_id": normalize_id(args.database_id), "kind": info["kind"],
            "count": len(sources),
            "data_sources": [{"id": s.get("id"), "name": s.get("name"),
                              "short_id": short_id(s["id"]) if s.get("id") else None}
                             for s in sources]}


def _query(api: NotionAPI, args, container_id: str) -> dict:
    filter_obj = load_optional_json(args.filter)
    sorts = parse_sorts(args.sort) or None
    limit = max(1, min(args.limit, 100))
    source_id, rows = api.query_container(container_id, filter=filter_obj, sorts=sorts,
                                          limit=limit, page_size=max(1, min(args.page_size, 100)))
    pages = [page_summary(row) for row in rows]
    result = {"source_id": source_id, "source": short_id(source_id), "count": len(pages),
              "has_more": len(rows) >= limit, "results": pages}
    used_datasource = normalize_id(container_id) != normalize_id(source_id)
    if used_datasource:
        result["note"] = ("queried through data source "
                          f"{short_id(source_id)} (the 2025-09-03 database model)")
    return result


def cmd_db_query(api: NotionAPI, args) -> dict:
    return _query(api, args, args.database_id)


def cmd_ds_query(api: NotionAPI, args) -> dict:
    return _query(api, args, args.data_source_id)


def cmd_db_create(api: NotionAPI, args) -> dict:
    schema = load_json_arg(args.properties_json)
    if not isinstance(schema, dict):
        raise UsageError("--properties-json must be a JSON object of property schemas")
    parent = {"type": "page_id", "page_id": normalize_id(args.parent_page_id)}
    title = [{"type": "text", "text": {"content": args.title}}]
    if api.version >= "2025-09-03":
        body = {"parent": parent,
                "title": title,
                "initial_data_source": {"properties": schema}}
    else:
        body = {"parent": parent, "title": title, "properties": schema}
    created = api.post("/databases", body)
    result = {"id": created.get("id"), "short_id": short_id(created["id"]) if created.get("id") else None,
              "title": object_title(created), "url": created.get("url"),
              "data_sources": [{"id": s.get("id"), "name": s.get("name")}
                               for s in created.get("data_sources") or []]}
    audit("db.create", dry_run=False, payload={"database_id": result["id"], "title": result["title"]})
    return result


def cmd_ds_get(api: NotionAPI, args) -> dict:
    ds = api.get(f"/data_sources/{normalize_id(args.data_source_id)}")
    return {"id": ds.get("id"), "short_id": short_id(ds["id"]) if ds.get("id") else None,
            "title": object_title(ds), "parent": ds.get("parent"),
            "properties": sorted((ds.get("properties") or {}).keys()),
            "database_parent": ds.get("database_parent")}


def cmd_ds_create_page(api: NotionAPI, args) -> dict:
    properties = build_properties(args)
    if not properties:
        raise UsageError("a new row needs at least a title: pass --title or --prop")
    title_name = parent_title_property(api, "data_source", args.data_source_id)
    properties = resolve_title_property(properties, title_name)
    children = build_children(args)
    page = api.create_page("data_source", args.data_source_id, properties,
                           children=children or None)
    result = page_summary(page)
    audit("ds.create_page", dry_run=False, payload={
        "page_id": page.get("id"), "data_source_id": normalize_id(args.data_source_id),
        "title": result["title"]})
    return result


def cmd_comment_list(api: NotionAPI, args) -> dict:
    comments = api.paginate("/comments", method="GET", limit=max(1, args.limit),
                            body={"block_id": normalize_id(args.target_id)})
    return {"target": normalize_id(args.target_id), "count": len(comments), "results": [
        {"id": c.get("id"), "created_time": c.get("created_time"),
         "created_by": (c.get("created_by") or {}).get("id"),
         "text": rich_text_plain(c.get("rich_text"))} for c in comments]}


def cmd_comment_add(api: NotionAPI, args) -> dict:
    created = api.post("/comments", {
        "parent": {"page_id": normalize_id(args.page_id)},
        "rich_text": [{"type": "text", "text": {"content": args.text}}],
    })
    result = {"id": created.get("id"), "page_id": normalize_id(args.page_id),
              "text": rich_text_plain(created.get("rich_text"))}
    audit("comment.add", dry_run=False, payload={"page_id": result["page_id"], "comment_id": result["id"]})
    return result


def cmd_ref(api: NotionAPI, args) -> dict:
    raw = normalize_id(args.value)
    return {"input": args.value, "id": raw, "dashed": dashed(raw), "short_id": short_id(raw),
            "url": f"https://www.notion.so/{raw}"}


HANDLERS = {
    ("whoami",): cmd_whoami,
    ("search",): cmd_search,
    ("users",): cmd_users,
    ("page", "get"): cmd_page_get,
    ("page", "children"): cmd_page_children,
    ("page", "create"): cmd_page_create,
    ("page", "update"): cmd_page_update,
    ("page", "append"): cmd_page_append,
    ("page", "delete"): cmd_page_delete,
    ("block", "get"): cmd_block_get,
    ("block", "children"): cmd_block_children,
    ("block", "append"): cmd_block_append,
    ("block", "update"): cmd_block_update,
    ("block", "delete"): cmd_block_delete,
    ("db", "get"): cmd_db_get,
    ("db", "sources"): cmd_db_sources,
    ("db", "query"): cmd_db_query,
    ("db", "create"): cmd_db_create,
    ("ds", "get"): cmd_ds_get,
    ("ds", "query"): cmd_ds_query,
    ("ds", "create-page"): cmd_ds_create_page,
    ("ds", "delete-page"): cmd_ds_delete_page,
    ("comment", "list"): cmd_comment_list,
    ("comment", "add"): cmd_comment_add,
    ("ref",): cmd_ref,
}


def dispatch(args) -> dict:
    if args.command == "ref":  # purely local, no token needed
        return cmd_ref(None, args)
    key = (args.command,) if args.command in ("whoami", "search", "users") else \
        (args.command, getattr(args, f"{args.command}_command", None))
    handler = HANDLERS.get(key)
    if handler is None:
        raise UsageError(f"unknown command {' '.join(str(part) for part in key)}")
    api = build_api(args)
    return handler(api, args)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        result = dispatch(args)
    except UsageError as exc:
        return fail(str(exc), code=EXIT_USAGE)
    except NotionError as exc:
        detail = ""
        if exc.code:
            detail = f" [{exc.code}]"
        hint = ""
        if exc.status == 401:
            hint = " (token invalid or revoked; re-run notion_auth.py set-token)"
        elif exc.status == 403:
            hint = " (the integration lacks access; share the page or database with it)"
        elif exc.status == 404:
            hint = " (object not found, or not shared with the integration)"
        return fail(f"{exc}{detail}{hint}")
    except BrokenPipeError:
        return EXIT_OK
    fmt = option(args, "format", "json")
    if args.command == "search" or (args.command in ("db", "ds") and
                                    getattr(args, f"{args.command}_command", None) == "query"):
        print_rows(result, fmt)
    else:
        emit(result, fmt=fmt)
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
