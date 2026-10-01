---
name: notion-craft
description: Read and write Notion content through the Notion API — find pages and databases, read page bodies and database rows, create and update pages, append or edit blocks, query data sources. Use when a task involves Notion pages, databases, or Notion links. Deletion runs as a dry run and requires explicit user confirmation plus --yes and --confirm.
---

# Notion Craft

Use this skill whenever a task reads from or writes to Notion. Everything goes through one
standard-library Python CLI; do not add dependencies, do not reach for an MCP server, and do
not drive the Notion web app.

`<skill-dir>` below is this loaded skill's resource base; the CLI lives in its `scripts`
directory. Prefer that reported base over any hard-coded path, then run the CLI with the Python
that is on PATH: `python <skill-dir>/scripts/notion.py ...` (on Windows, `py -3` also works).
Standard library only: Python 3.9 or newer is enough. The CLI keeps the short filename
`notion.py` on purpose — the tool is Notion, and the skill is `notion-craft`.

## Delete red lines (read before any write)

1. **Never delete, trash, or archive anything unless the user asked for that specific deletion
   in this conversation.** "Clean this up", "reorganize", "make it nicer", or an unrelated
   request is not permission to delete. When in doubt, do not delete and say what you left
   alone.
2. Deletion commands are **dry runs by default**. They report the exact target and change
   nothing. Run the dry run first, show the user what would be removed, and only then, if they
   still want it, re-run with `--yes --confirm <short-id-or-exact-title>`.
3. Deletes move objects to the **Notion trash** (`in_trash: true`), which a human can restore.
   There is no permanent page delete through the API for integrations: `--purge` is rejected on
   purpose.
4. Batch deletion needs an explicit `--max <n>`, is refused above 25 objects per call, and is
   refused entirely if the number of matches exceeds `--max`.
5. Never delete a parent block or page to "tidy up" child content. Children disappear with it.
6. Every write, dry run included, is appended to `<skill-dir>/.audit.log`. Never edit or delete
   that log to hide an operation.

## Credentials

Resolution order: `--token`, then `NOTION_TOKEN` / `NOTION_API_KEY` /
`NOTION_INTEGRATION_TOKEN`, then `~/.dsh/notion-credentials.json`.

```bash
python <skill-dir>/scripts/notion_auth.py where          # which source is in use
python <skill-dir>/scripts/notion_auth.py check          # verify the token
python <skill-dir>/scripts/notion_auth.py set-token ntn_xxx
```

If `check` reports 401, the token is wrong or revoked; ask the user for a new integration
token. Never print a full token, never write one into a file in the workspace, and never paste
one into a Notion page. A 403 or 404 on a specific object almost always means the page or
database has not been shared with the integration in Notion — the fix is on the Notion side
(`...` menu → Connections), so report it instead of retrying.

## Core workflows

Every command prints JSON by default; add `--format md` for the Markdown view where it exists.
IDs may be a bare 32-hex string, a dashed UUID, or a full Notion URL — all three work anywhere
an id is expected. `ref` converts between them.

**Find something**

```bash
python <skill-dir>/scripts/notion.py whoami
python <skill-dir>/scripts/notion.py search "quarterly plan" --limit 10
python <skill-dir>/scripts/notion.py search --type page          # everything shared
```

`search` only returns pages and databases that were shared with the integration.

**Read a page**

```bash
python <skill-dir>/scripts/notion.py page get <page-id-or-url>
python <skill-dir>/scripts/notion.py page children <page-id> --depth 2 --format md
```

`page get` returns properties in plain form (title, select, date, relation, rollup, formula …).
`page children` returns a flattened outline plus Markdown. Always read before you write so the
update is based on the current content, not on an assumption.

**Append content**

```bash
python <skill-dir>/scripts/notion.py page append <page-id> --markdown "## Notes
- first point
- [ ] follow up"
```

`--markdown` understands `#`/`##`/`###`, `-`, `1.`, `- [ ]`, `>`, `---` and fenced code;
`--text` makes one block per non-empty line; `--blocks-json` takes a raw Notion block array
(inline JSON, `@file.json`, or `-` for stdin). Appending is additive and safe — prefer it over
rewriting existing content.

**Create a page**

```bash
# under another page: the title property is simply "title"
python <skill-dir>/scripts/notion.py page create --parent-type page --parent-id <page-id> \
  --title "Meeting notes 2026-02-11" --markdown "- attendee: A"

# as a row of a database or data source: --title finds the real title property name
python <skill-dir>/scripts/notion.py ds create-page <data-source-id> --title "New task" \
  --prop "Status:select=Todo" --prop "Due:date=2026-03-01"
```

Use `--parent-type database` (or `data_source`) when the new page must be a row. The CLI reads
the parent schema and maps `--title` to the actual title property, which is workspace specific.

**Update a page**

```bash
python <skill-dir>/scripts/notion.py page update <page-id> --prop "Status:select=Done"
python <skill-dir>/scripts/notion.py page update <page-id> --title "Renamed" --icon "📌"
```

Only send the properties that changed. `--prop` accepts `Name=Value` (rich text by default) or
`Name:type=Value` with `title`, `rich_text`, `number`, `checkbox`, `select`, `multi_select`,
`date`, `url`, `email`, `relation`, `people`. A raw JSON value also works:
`--prop 'Tags=[{"name":"api"}]'`.

**Databases and data sources**

```bash
python <skill-dir>/scripts/notion.py db get <database-id>
python <skill-dir>/scripts/notion.py db sources <database-id>
python <skill-dir>/scripts/notion.py db query <database-id> --limit 20 \
  --filter '{"property":"Status","select":{"equals":"Todo"}}' \
  --sort '{"property":"Due","direction":"ascending"}'
python <skill-dir>/scripts/notion.py ds query <data-source-id> --limit 20
```

Since the `2025-09-03` API version a database is a *container* that owns one or more *data
sources*; only data sources can be queried. `db query` resolves that automatically (and the
CLI retries through `/v1/data_sources` if an older version hits a multi-source database), so
prefer `db query` when the user gave you a database link. Read `references/notion-api.md` before
building filters, creating a database, or debugging an unfamiliar error code.

**Blocks and comments**

```bash
python <skill-dir>/scripts/notion.py block children <block-id> --format md
python <skill-dir>/scripts/notion.py block append <block-id> --text "one line"
python <skill-dir>/scripts/notion.py block update <block-id> --text "corrected text"
python <skill-dir>/scripts/notion.py comment list <page-id>
python <skill-dir>/scripts/notion.py comment add <page-id> --text "should this be split?"
```

Prefer a comment over an unrequested content edit when you want to flag something.

**Delete (dry run first, always)**

```bash
# 1) show what would happen; nothing is removed
python <skill-dir>/scripts/notion.py page delete <page-id>
# 2) only after the user confirms that exact object
python <skill-dir>/scripts/notion.py page delete <page-id> --yes --confirm <short-id-or-exact-title>
```

`ds delete-page` and `block delete` behave the same way. If `--confirm` does not match a target
id or title, the command refuses and lists the candidates.

## Operating notes

* Rate limit is about three requests per second; the CLI paces itself and retries on 429/5xx.
  Keep `--limit` small when exploring instead of pulling whole databases.
* `--limit` is capped at 100 rows per query; paginate by narrowing the filter rather than
  raising it.
* Field names matter: the Notion API's `in_trash` replaced `archived`, `parent.database_id`
  became `parent.data_source_id`, and search filters use `data_source` on the new version. The
  CLI hides most of this, but raw JSON you pass in must match the version in use.
* Set `--api-version 2022-06-28` only if the workspace predates data sources and a call fails
  with a version error.
* Report honestly: if a write is rejected, show the API error and what you did not change.
  Never claim an update succeeded because the command exited 0 — the CLI prints the object the
  API returned, so quote that.
* Keep scratch files out of the user's workspace and out of the skill directory; the skill
  directory holds only the skill, its audit log, and nothing else.

## Maintaining this skill

The CLI and its tests are standard library only, so no environment setup is needed:

```bash
python <skill-dir>/scripts/test_notion_skill.py        # offline: no network, no token
python <skill-dir>/scripts/check_no_secrets.py         # fail if a credential leaked in
```

`test_notion_skill.py` stubs the HTTP layer, so it is safe in CI and must stay green: it covers
the delete guard, id parsing, pagination, the legacy data-source fallback and the argument
parser. `check_no_secrets.py` prints file names and match counts only — never the secret — so
its output is safe to log; run it before publishing the skill anywhere. The Notion token itself
always lives outside this directory (default `~/.dsh/notion-credentials.json`, which
`.gitignore` also ignores as a second line of defence).
