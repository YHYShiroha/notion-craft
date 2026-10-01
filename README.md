# notion-craft

A [DSH](https://github.com/deepseek-ai) skill that lets an agent read and write Notion content
through the official Notion API — find pages, read page bodies, query databases and data
sources, create and update pages, append and edit blocks, and comment. Deletion is a **dry run
by default** and needs two explicit confirmations before it touches anything.

Standard library only. No dependencies, no MCP server, no browser automation.

```
                                 ┌─────────────────────────┐
   your request  ──►  agent  ──►  │  scripts/notion.py      │  ──►  api.notion.com
                                 │  (+ delete guard,       │
                                 │   + audit log)          │
                                 └─────────────────────────┘
```

---

## Table of contents

- [Why this skill exists](#why-this-skill-exists)
- [Requirements](#requirements)
- [Install](#install)
- [Get a Notion token and store it](#get-a-notion-token-and-store-it)
  - [Step 1 — create an internal integration](#step-1--create-an-internal-integration)
  - [Step 2 — set its capabilities](#step-2--set-its-capabilities)
  - [Step 3 — copy the secret](#step-3--copy-the-secret)
  - [Step 4 — share the pages with the integration](#step-4--share-the-pages-with-the-integration)
  - [Step 5 — put the token where the skill can find it](#step-5--put-the-token-where-the-skill-can-find-it)
- [Verify the setup](#verify-the-setup)
- [Usage examples](#usage-examples)
- [Command reference](#command-reference)
- [The delete guard](#the-delete-guard)
- [API versions and data sources](#api-versions-and-data-sources)
- [Troubleshooting](#troubleshooting)
- [Development](#development)
- [Security](#security)
- [License](#license)

---

## Why this skill exists

Most "Notion automation" breaks in one of three places, so this skill is built around them:

1. **Accidental deletion.** An agent that can write can also destroy. Here, `page delete`,
   `block delete` and `ds delete-page` only *report* what they would remove unless you pass both
   `--yes` and `--confirm <exact id or title>`. Deletes move objects to the Notion trash, which
   you can restore. There is no permanent-delete code path, because the Notion API does not
   offer one for integrations — and this skill does not pretend otherwise.
2. **The 2025 data-source split.** Since API version `2025-09-03` a database is a *container*
   that owns one or more *data sources*, and only data sources can be queried. A naive
   integration breaks the moment a database has two sources. This one resolves the data source
   automatically and falls back for older API versions.
3. **Workspace-specific property names.** The title property is called `Name` in one database,
   `title` in another, `标题` in a third. `--title` reads the parent schema and maps itself to
   whatever the real name is, instead of hard-coding one.

Everything the skill writes — dry runs included — is appended to an audit log next to the
skill, so there is always a record of what happened.

---

## Requirements

| | |
|---|---|
| Python | 3.9 or newer (`python` or `py -3` on Windows). Standard library only. |
| Network | HTTPS access to `api.notion.com` |
| Notion | A workspace where you can create an integration |
| DSH | Any recent version; the skill is discovered from a filesystem skills directory |

---

## Install

Copy (or clone) this directory into a skills directory that DSH scans. The user-level directory
works for every session:

```bash
# Linux / macOS
cp -r notion-craft ~/.dsh/skills/notion-craft

# Windows (PowerShell)
Copy-Item -Recurse notion-craft "$env:USERPROFILE\.dsh\skills\notion-craft"
```

Scanned roots, highest priority first:

| Rank | Source | Path |
|---|---|---|
| 100 | project | `<project>/.dsh/skills` |
| 200 | project | `<project>/.agents/skills` |
| 400 | user | `~/.dsh/skills` |
| 500 | user | `~/.agents/skills` |

DSH watches these directories, so the skill appears without restarting the app. Confirm it by
asking the agent to use `notion-craft`.

---

## Get a Notion token and store it

The skill authenticates with a **Notion internal integration token**. You create it yourself in
Notion; it takes about two minutes.

### Step 1 — create an internal integration

1. Sign in to Notion and open <https://www.notion.so/my-integrations>.
2. Click **New integration**.
3. Fill in:
   - **Name** — anything, e.g. `notion-craft` (this is what you will see in Notion's
     "Connections" list later, so pick something recognisable).
   - **Associated workspace** — **the workspace you want to operate on**. If you pick the wrong
     one, every page will look missing later.
   - **Type** — **Internal**. Public/OAuth integrations are not supported by this skill.
4. Click **Save**.

### Step 2 — set its capabilities

On the integration page open **Capabilities** and choose:

| Capability | Needed for | Notes |
|---|---|---|
| **Read content** | everything | required |
| **Update content** | `page update`, `block update` | required for edits |
| **Insert content** | `page create`, `page append`, `ds create-page`, comments | required for new content |
| **Delete content** | `page delete`, `block delete`, `ds delete-page` | optional — **leave it off if you want a hard stop on deletion** |

With **Delete content** disabled the delete commands fail with `403`, which is a second,
server-side guard on top of the CLI's own confirmation flow.

### Step 3 — copy the secret

Back on the integration's **Configuration** page, find **Internal Integration Secret**, click
**Show**, then **Copy**. It looks like `ntn_…` (newer) or `secret_…` (older).

> The value is shown **once**. If you lose it, generate a new one. Never commit it, never paste
> it into a Notion page, never put it in a URL.

### Step 4 — share the pages with the integration

**A token alone grants access to nothing.** Notion integrations start with an empty scope, so
you must connect the integration to the content you want it to see:

1. Open the page or database **in Notion**.
2. Click **`...`** (top right) → **Connections** → **Connect to** → pick your integration.
3. For a database, open **the database itself** and connect it there too — sharing the parent
   page does **not** automatically cover a database's rows.

Notion calls this file visible only to the pages you connected. Symptom of skipping this step:
`whoami` succeeds, but `search` returns nothing and specific pages return `404`.

### Step 5 — put the token where the skill can find it

The CLI resolves credentials in this order, first hit wins:

1. `--token <value>` on any command
2. environment variable — `NOTION_TOKEN`, `NOTION_API_KEY`, or `NOTION_INTEGRATION_TOKEN`
3. the credentials file: **`~/.dsh/notion-credentials.json`**

**Recommended (file):**

```bash
python <skill-dir>/scripts/notion_auth.py set-token ntn_your_token_here
```

That writes `~/.dsh/notion-credentials.json`, restricts it to your user where the OS supports
it, and immediately verifies the token. On Windows the file lands at:

```
C:\Users\<you>\.dsh\notion-credentials.json
```

The file is plain JSON — this is its entire shape:

```json
{
  "token": "ntn_your_token_here",
  "saved_at": "2026-01-01T00:00:00+00:00"
}
```

> The credentials file lives **outside** this repository on purpose. This repo's `.gitignore`
> also ignores `*credentials*.json`, `.env`, `*.token` and the audit log as a second line of
> defence, and `scripts/check_no_secrets.py` will fail loudly if a secret ever lands in the tree.

**Alternative (environment variable):**

```bash
# Linux / macOS
export NOTION_TOKEN=ntn_your_token_here

# Windows PowerShell, current session only
$env:NOTION_TOKEN = 'ntn_your_token_here'

# Windows, persist for your user
setx NOTION_TOKEN "ntn_your_token_here"
```

To see which source wins, or to remove a stored token:

```bash
python <skill-dir>/scripts/notion_auth.py where       # which credential source is in use
python <skill-dir>/scripts/notion_auth.py clear       # dry run
python <skill-dir>/scripts/notion_auth.py clear --yes # actually delete the file
```

---

## Verify the setup

```bash
python <skill-dir>/scripts/notion_auth.py check     # token valid?
python <skill-dir>/scripts/notion.py whoami         # bot name + workspace
python <skill-dir>/scripts/notion.py search --limit 5
```

Expected: `check` prints your integration and workspace; `search` lists the objects you shared.
If `search` returns `count: 0`, go back to [Step 4](#step-4--share-the-pages-with-the-integration).

---

## Usage examples

IDs may be a bare 32-hex string, a dashed UUID, or a full Notion URL — all three work anywhere
an id is expected. Every command prints JSON; add `--format md` where a Markdown view exists.

**Find things**

```bash
python <skill-dir>/scripts/notion.py search "quarterly plan" --limit 10
python <skill-dir>/scripts/notion.py search --type page
```

**Read**

```bash
python <skill-dir>/scripts/notion.py page get <page-id-or-url>
python <skill-dir>/scripts/notion.py page children <page-id> --depth 2 --format md
```

**Create a page**

```bash
python <skill-dir>/scripts/notion.py page create \
  --parent-type page --parent-id <page-id> \
  --title "Meeting notes" --markdown "## Agenda
- item one
- [ ] follow up"
```

**Create a row in a database**

```bash
python <skill-dir>/scripts/notion.py ds create-page <data-source-id> \
  --title "New task" --prop "Status:select=Todo" --prop "Due:date=2026-03-01"
```

**Update properties**

```bash
python <skill-dir>/scripts/notion.py page update <page-id> --prop "Status:select=Done"
```

**Query a database**

```bash
python <skill-dir>/scripts/notion.py db query <database-id> --limit 20 \
  --filter '{"property":"Status","select":{"equals":"Todo"}}' \
  --sort '{"property":"Due","direction":"ascending"}'
```

**Append and edit blocks**

```bash
python <skill-dir>/scripts/notion.py page append <page-id> --text "one line per block"
python <skill-dir>/scripts/notion.py block update <block-id> --text "corrected text"
```

**Delete — dry run first, always**

```bash
python <skill-dir>/scripts/notion.py page delete <page-id>          # reports only
python <skill-dir>/scripts/notion.py page delete <page-id> --yes --confirm <id-or-title>
```

---

## Command reference

| Command | What it does |
|---|---|
| `whoami` | validate the token, print the integration identity |
| `search [query] [--type page\|data_source\|database] [--limit N]` | find shared pages and data sources |
| `users [--limit N]` | list workspace users visible to the integration |
| `page get <id> [--no-properties]` | page properties in plain form |
| `page children <id> [--depth N] [--limit N] [--format md]` | page body as blocks + Markdown |
| `page create --parent-type page\|database\|data_source\|workspace --parent-id <id> --title T [--prop P=V] [--markdown M\|--text T\|--blocks-json J] [--icon E] [--cover URL]` | create a page or row |
| `page update <id> [--prop P=V] [--title T] [--icon E] [--cover URL] [--trash --yes --confirm C]` | update properties (optionally trash) |
| `page append <id> [--markdown M\|--text T\|--blocks-json J]` | append blocks |
| `page delete <id> [--yes --confirm C] [--purge]` | trash a page (dry run by default) |
| `block get <id>` | one block |
| `block children <id> [--limit N]` | a block's children |
| `block append <id> [...]` | append children to a block |
| `block update <id> --text T` | replace a block's text |
| `block delete <id> [--yes --confirm C]` | remove a block (dry run by default) |
| `db get <id>` / `db sources <id>` | database container and its data sources |
| `db query <id> [--filter J] [--sort J] [--limit N] [--page-size N]` | query rows, resolving the data source |
| `db create <parent-page-id> --title T --properties-json J` | create a database |
| `ds get <id>` / `ds query <id> [...]` | data source operations |
| `ds create-page <id> --title T [--prop P=V]` | create a row in a data source |
| `ds delete-page <id> [--yes --confirm C]` | trash a row (dry run by default) |
| `comment list <id>` / `comment add <page-id> --text T` | read and add comments |
| `ref <url-or-id>` | convert between Notion URLs and ids (offline, no token needed) |

Global flags: `--token`, `--api-version`, `--format json|md`, `--verbose`. They work before or
after the subcommand.

`--prop` accepts `Name=Value` (rich text by default) or `Name:type=Value` with `title`,
`rich_text`, `number`, `checkbox`, `select`, `multi_select`, `date`, `url`, `email`, `relation`
or `people`. A raw JSON value also works: `--prop 'Tags=[{"name":"api"}]'`.
`--filter`, `--sort`, `--blocks-json` accept inline JSON, `@file.json`, or `-` for stdin.

---

## The delete guard

Deletion is the only operation that can lose data, so it is fenced five ways:

1. **Dry run by default.** Without `--yes` the command prints the resolved target (real title +
   id, fetched from Notion) and exits without sending a write. It reports
   `"dry_run": true` and nothing else happens.
2. **`--yes` alone is refused.** You also need `--confirm <exact id, id prefix, or exact title>`,
   at least 4 characters. A mismatch refuses and lists the candidates, so the wrong row cannot be
   removed by a typo.
3. **Trash, not purge.** Deletes send `in_trash: true`; you can restore from Notion's trash.
   `--purge` is rejected outright, because the Notion API offers no permanent page/block delete
   for integrations.
4. **Batches are gated.** More than one target requires `--max <n>`, the actual match count must
   not exceed it, and there is a hard ceiling of 25 objects per call.
5. **Everything is logged.** Each write, dry runs included, appends one JSON line to
   `.audit.log` beside the skill. The log is `.gitignore`d, and tests can never pollute it
   (they redirect it to the null device).

Worked example:

```console
$ python scripts/notion.py page delete 3ecee4421d4b81ad964ad52d95cdb9aa
{
  "dry_run": true,
  "action": "trash",
  "target": { "short_id": "3ecee442", "title": "Old draft", "type": "page" },
  "hint": "nothing was deleted. Re-run with --yes --confirm 3ecee442 to proceed."
}

$ python scripts/notion.py page delete 3ecee4421d4b81ad964ad52d95cdb9aa --yes --confirm 3ecee442
{ "dry_run": false, "action": "trashed", "in_trash": true, ... }
```

The skill's `SKILL.md` additionally instructs the agent never to delete anything unless the
user asked for that specific deletion in the current conversation.

---

## API versions and data sources

| | `2022-06-28` | `2025-09-03` (default) |
|---|---|---|
| Database | holds rows directly | a **container** owning one or more data sources |
| Rows queried via | `/v1/databases/{id}/query` | `/v1/data_sources/{id}/query` |
| Row's page parent | `database_id` | `data_source_id` |
| Search filter value | `database` | `data_source` |
| Archive field | `archived` | `in_trash` |

`db query` handles this: it resolves a database id to its first data source, and if an older
version returns `multiple_data_sources_for_database`, it retries through `/v1/data_sources`.
Set `--api-version 2022-06-28` only for a workspace that predates data sources. More detail in
[`references/notion-api.md`](references/notion-api.md).

---

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `no Notion token found` | No token in any source. Run `notion_auth.py set-token`, or set `NOTION_TOKEN`. |
| `401 unauthorized` | Token wrong, revoked, or from a deleted integration. Generate a new secret. |
| `403 restricted_resource` | A capability is off (e.g. **Delete content**), or the object is not shared. |
| `404 object_not_found` | The page/database was never connected to the integration — see Step 4. |
| `search` returns `count: 0` | Nothing is shared with the integration yet. |
| `429 rate_limited` | ~3 requests/second. The CLI paces and retries; lower `--limit`. |
| `validation_error` on a property | Wrong property name or type. Check `db get`/`ds get` for the schema; remember the title property name varies per database. |
| `Databases with multiple data sources are not supported` | You forced `--api-version 2022-06-28` on a multi-source database. Drop the flag. |
| Skill does not appear | The directory must be directly under a scanned root and contain `SKILL.md`. Check the name matches `name:` in the frontmatter. |

---

## Development

No dependencies, no virtualenv:

```bash
python scripts/test_notion_skill.py     # 41 offline tests: no network, no token
python scripts/check_no_secrets.py      # fails if a credential leaked into the tree
```

`test_notion_skill.py` stubs the HTTP layer, so it is safe in CI and covers the delete guard, id
parsing, pagination, the legacy data-source fallback, property/Markdown parsing, error mapping
and the argument parser. Keep it green.

`check_no_secrets.py` reports file names and match counts only — never the secret itself — so
its output is safe to paste into a log.

Layout:

```
notion-craft/
├── SKILL.md                    # agent instructions: delete red lines, workflows, notes
├── references/notion-api.md    # endpoints, payload shapes, version differences, error codes
└── scripts/
    ├── notion.py               # CLI: 25 subcommands, argparse
    ├── notion_client.py        # API client, pagination, delete guard, audit log
    ├── notion_auth.py          # credential set/check/where/clear
    ├── test_notion_skill.py    # offline test suite
    └── check_no_secrets.py     # credential leak scanner
```

---

## Security

- The token is read from `--token`, an environment variable, or
  `~/.dsh/notion-credentials.json` — never from this repository.
- Nothing in this repo prints a full token. `notion_auth.py` masks values
  (`ntn_…hmn5`), and the leak scanner never echoes matches.
- Run `scripts/check_no_secrets.py` before publishing or committing.
- Rotate immediately if a secret ever reaches a commit: Notion integrations let you regenerate
  the secret, and GitHub's history rewrite is not a substitute for rotation.
- Scope is decided in Notion: an integration can only touch pages a user explicitly connected,
  and you can leave **Delete content** disabled to make deletion impossible from the API side.

---

## License

MIT — see [LICENSE](LICENSE).
