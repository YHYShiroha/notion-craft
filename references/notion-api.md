# Notion API notes for the `notion` skill

Source of truth is the official docs: <https://developers.notion.com/reference/intro>.
This file records only what the CLI depends on, so future edits do not have to re-derive it.

## Authentication

* Header `Authorization: Bearer <token>` on every request.
* Header `Notion-Version: <version>` is mandatory. The CLI defaults to `2025-09-03`;
  `2022-06-28` is the previous generation and still works for single-source databases.
* Tokens start with `ntn_` (new) or `secret_` (legacy). An *internal integration token* is what
  this skill uses. Public OAuth is out of scope.
* An integration only sees objects a user explicitly shared with it. `403`/`404` on a known
  object is a sharing problem, not a code problem.

## API versions and data sources

| | `2022-06-28` | `2025-09-03` (default) |
|---|---|---|
| Database | the thing that holds rows | a **container** that owns data sources |
| Rows live in | `/v1/databases/{id}/query` | `/v1/data_sources/{id}/query` |
| Page parent for a row | `{"type":"database_id","database_id":...}` | `{"type":"data_source_id","data_source_id":...}` |
| Search filter object value | `database` | `data_source` |
| Database object | `properties` map | `data_sources: [{id, name}]`, no direct properties |
| Create database | `properties` at the top level | `initial_data_source.properties` |
| Archive field | `archived` | `in_trash` |

The CLI papers over the difference: `resolve_container` describes an id, `query_container`
queries it, and a legacy `validation_error` with `additional_data.error_type ==
"multiple_data_sources_for_database"` triggers a retry through `/v1/data_sources`. `page create`
and `ds create-page` read the parent schema to find the real title property name.

## Endpoints used

| Purpose | Request |
|---|---|
| Identity | `GET /v1/users/me` |
| Users | `GET /v1/users` |
| Search | `POST /v1/search` with `{query, filter, sort, page_size, start_cursor}` |
| Page read | `GET /v1/pages/{page_id}` |
| Page create | `POST /v1/pages` with `{parent, properties, children?, icon?, cover?}` |
| Page update | `PATCH /v1/pages/{page_id}` with `{properties?, icon?, cover?, in_trash?}` |
| Block read | `GET /v1/blocks/{block_id}` |
| Block children | `GET /v1/blocks/{block_id}/children` |
| Append blocks | `PATCH /v1/blocks/{block_id}/children` with `{children: [...]}` |
| Update block | `PATCH /v1/blocks/{block_id}` with `{<type>: {...}}` |
| Move to trash | `PATCH /v1/pages/{id}` or `PATCH /v1/blocks/{id}` with `{"in_trash": true}` |
| Database read | `GET /v1/databases/{database_id}` |
| Database create | `POST /v1/databases` |
| Data source read | `GET /v1/data_sources/{data_source_id}` |
| Data source query | `POST /v1/data_sources/{data_source_id}/query` |
| Database query (legacy) | `POST /v1/databases/{database_id}/query` |
| Comments | `GET /v1/comments?block_id=...`, `POST /v1/comments` (page parents only) |

`GET` list endpoints take `page_size` (max 100) and `start_cursor`; responses carry
`has_more` and `next_cursor`. The CLI paginates for you up to `--limit`.

## Property payload shapes

| Type | Value |
|---|---|
| title / rich_text | `[{"type":"text","text":{"content":"..."}}]` |
| number | `123.4` |
| checkbox | `true` |
| select | `{"name":"Todo"}` |
| multi_select | `[{"name":"api"}]` |
| status | `{"name":"Done"}` |
| date | `{"start":"2026-03-01","end":null}` |
| url / email / phone_number | `"..."` |
| relation | `[{"id":"<page-id>"}]` |
| people | `[{"id":"<user-id>"}]` |

The **name of the title property is different in every database** ("Name", "title", "名称"…).
That is why `--title` resolves against the parent schema instead of hard-coding `title`.

## Filter and sort shapes

```json
{"property": "Status", "select": {"equals": "Todo"}}
{"and": [{"property": "Done", "checkbox": {"equals": false}},
         {"property": "Due", "date": {"on_or_before": "2026-03-01"}}]}
{"property": "Name", "title": {"contains": "report"}}
```

Sort: `{"property": "Due", "direction": "ascending"}` or `{"timestamp": "last_edited_time",
"direction": "descending"}`. `--sort` accepts one or more JSON objects.

## Errors

| Status | Meaning and action |
|---|---|
| 400 `validation_error` | Payload shape or property name is wrong. Check the property exists and that the parent type matches the version. |
| 400 `multiple_data_sources_for_database` | Legacy version querying a multi-source database. Retry through `/v1/data_sources/{id}` (the CLI does this automatically). |
| 401 `unauthorized` | Token missing, wrong, or revoked. Re-run `notion_auth.py set-token`. |
| 403 `restricted_resource` | Integration lacks the capability (needs "Update content" or "Insert content" enabled). |
| 404 `object_not_found` | Object deleted, or never shared with the integration. |
| 409 `conflict_error` | Concurrent edit; re-read the object and retry once. |
| 429 `rate_limited` | ~3 requests/second per integration. Honour `Retry-After`. |
| 502/503 | Notion-side hiccup; retry with backoff. |

## Deletion reality check

* Pages and blocks are **moved to the trash** with `in_trash: true`; the object stays
  restorable from the Notion UI. The CLI therefore never claims a permanent delete.
* There is no supported endpoint to permanently purge a page or block through an integration
  token, so `--purge` is rejected rather than faked.
* Deleting a parent cascades to its children. That is why the guard demands an exact
  `--confirm` and a `--max` for batches.

## Limits worth remembering

* 100 blocks per append request (the CLI chunks larger payloads).
* 100 rows per query page; `--limit` is capped at 100.
* 2 000 blocks per page, 100 blocks per request, ~500 KB per request body.
* Rich text content per block is capped (2 000 characters), so split long text.

## Official references

* Introduction: <https://developers.notion.com/reference/intro>
* Upgrade FAQ for `2025-09-03`: <https://developers.notion.com/guides/get-started/upgrade-faqs-2025-09-03>
* Query a data source: <https://developers.notion.com/reference/query-a-data-source>
* Block object: <https://developers.notion.com/reference/block>
* Request limits: <https://developers.notion.com/reference/request-limits>
* Status codes: <https://developers.notion.com/reference/status-codes>
