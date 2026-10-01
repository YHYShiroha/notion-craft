"""Shared Notion API client and helpers for the `notion` DSH skill.

Standard library only. No third-party dependencies, by design: this skill has to
work in a bare DSH runtime.

Safety model (see SKILL.md for the operating rules):
  * every destructive call goes through :func:`resolve_delete_guard`, which
    refuses unless the caller passed both `--yes` and a matching `--confirm`;
  * deletes send `in_trash: true` (recoverable from the Notion trash) unless
    `--purge` is passed;
  * every write, including dry runs, is appended to the skill's audit log.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

API_BASE = "https://api.notion.com/v1"

# 2025-09-03 introduced data sources: a database is now a container that owns one
# or more data sources. Older versions (<= 2022-06-28) only know databases and
# break on databases that have more than one data source.
DEFAULT_VERSION = "2025-09-03"
LEGACY_VERSION = "2022-06-28"

SKILL_DIR = Path(__file__).resolve().parent.parent
# The audit log is the record of what really happened, so it must never be
# polluted by tests: point NOTION_AUDIT_LOG at a scratch path (or os.devnull) to
# redirect it. test_notion_skill.py relies on this.
AUDIT_LOG = Path(os.environ.get("NOTION_AUDIT_LOG") or (SKILL_DIR / ".audit.log"))
CREDENTIALS_PATH = Path(
    os.environ.get("NOTION_CREDENTIALS_FILE")
    or (Path(os.environ.get("DSH_HOME", Path.home() / ".dsh")) / "notion-credentials.json")
)

USER_AGENT = "dsh-notion-skill/1.0 (+https://developers.notion.com)"

# Notion allows ~3 requests/second. Stay under it and back off on 429 / 5xx.
MIN_REQUEST_INTERVAL = 0.34
MAX_RETRIES = 4
TIMEOUT_SECONDS = 45.0

_LAST_REQUEST_AT = 0.0


class NotionError(RuntimeError):
    """An error returned by the Notion API, or a transport failure."""

    def __init__(self, message: str, *, status: int | None = None, code: str | None = None,
                 body: Any = None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.body = body

    @property
    def is_data_source_mismatch(self) -> bool:
        """True when a legacy-version call hit a multi-data-source database."""
        if self.status != 400:
            return False
        if isinstance(self.body, dict):
            data = self.body.get("additional_data") or {}
            if data.get("error_type") == "multiple_data_sources_for_database":
                return True
            if data.get("child_data_source_ids"):
                return True
        text = str(self)
        return "multiple data sources" in text.lower()


class UsageError(RuntimeError):
    """The caller asked for something invalid or unsafe."""


# --------------------------------------------------------------------------- #
# ids
# --------------------------------------------------------------------------- #

def normalize_id(value: str) -> str:
    """Return a bare 32-hex Notion id, accepting urls, dashes and bare hex."""
    if not value:
        raise UsageError("empty id")
    text = value.strip()
    if text.startswith("http://") or text.startswith("https://"):
        text = urllib.parse.unquote(urllib.parse.urlparse(text).path)
    else:
        text = urllib.parse.unquote(text)
    # dashed uuids are unambiguous; a bare 32-hex run is the second best signal.
    # Do not strip dashes before matching, or hex characters from a url slug can
    # glue themselves onto the id and shift the window.
    match = re.search(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                      r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", text)
    if not match:
        match = re.search(r"[0-9a-fA-F]{32}", text)
    if not match:
        raise UsageError(f"cannot read a Notion id out of {value!r}")
    return match.group(0).replace("-", "").lower()


def dashed(value: str) -> str:
    raw = normalize_id(value).replace("-", "")
    return f"{raw[0:8]}-{raw[8:12]}-{raw[12:16]}-{raw[16:20]}-{raw[20:32]}"


def short_id(value: str) -> str:
    return normalize_id(value)[:8]


# --------------------------------------------------------------------------- #
# credentials
# --------------------------------------------------------------------------- #

def resolve_token(explicit: str | None = None) -> str:
    """Find a token: explicit argument, then env, then the credentials file."""
    if explicit:
        return explicit.strip()
    for name in ("NOTION_TOKEN", "NOTION_API_KEY", "NOTION_INTEGRATION_TOKEN"):
        candidate = os.environ.get(name)
        if candidate and candidate.strip():
            return candidate.strip()
    if CREDENTIALS_PATH.is_file():
        try:
            payload = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise UsageError(
                f"credentials file {CREDENTIALS_PATH} is not readable JSON: {exc}"
            ) from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if token and str(token).strip():
            return str(token).strip()
    raise UsageError(
        "no Notion token found. Provide one with --token, or set NOTION_TOKEN, or run "
        "`python notion_auth.py set-token <token>`."
    )


def save_token(token: str, *, note: str | None = None) -> Path:
    token = token.strip()
    if not token:
        raise UsageError("refusing to store an empty token")
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {"token": token}
    if note:
        payload["note"] = note
    payload["saved_at"] = datetime.now(timezone.utc).isoformat()
    CREDENTIALS_PATH.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    try:  # best effort on Windows; POSIX honours this properly
        os.chmod(CREDENTIALS_PATH, 0o600)
    except OSError:
        pass
    return CREDENTIALS_PATH


# --------------------------------------------------------------------------- #
# api client
# --------------------------------------------------------------------------- #

class NotionAPI:
    def __init__(self, token: str, *, version: str = DEFAULT_VERSION,
                 verbose: bool = False):
        self.token = token
        self.version = version
        self.verbose = verbose

    # -- transport ---------------------------------------------------------- #

    def request(self, method: str, path: str, *, body: dict | None = None,
                query: dict | None = None, version: str | None = None) -> dict:
        global _LAST_REQUEST_AT
        url = path if path.startswith("http") else f"{API_BASE}{path}"
        if query:
            clean = {k: v for k, v in query.items() if v is not None}
            if clean:
                url = f"{url}?{urllib.parse.urlencode(clean)}"
        data = None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Notion-Version": version or self.version,
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        }
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            wait = MIN_REQUEST_INTERVAL - (time.monotonic() - _LAST_REQUEST_AT)
            if wait > 0:
                time.sleep(wait)
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            if self.verbose:
                print(f"[notion] {method} {url}", file=sys.stderr)
            try:
                _LAST_REQUEST_AT = time.monotonic()
                with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
                    raw = response.read().decode("utf-8")
                return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw)
                except json.JSONDecodeError:
                    parsed = {"raw": raw}
                code = parsed.get("code") if isinstance(parsed, dict) else None
                message = parsed.get("message") if isinstance(parsed, dict) else raw
                if exc.code == 429 or 500 <= exc.code < 600:
                    delay = _retry_after(exc.headers, attempt)
                    if attempt < MAX_RETRIES:
                        if self.verbose:
                            print(f"[notion] retrying after HTTP {exc.code} in {delay:.1f}s",
                                  file=sys.stderr)
                        time.sleep(delay)
                        last_error = NotionError(f"HTTP {exc.code}: {message}",
                                                 status=exc.code, code=code, body=parsed)
                        continue
                raise NotionError(f"HTTP {exc.code}: {message}", status=exc.code,
                                  code=code, body=parsed) from None
            except urllib.error.URLError as exc:
                last_error = NotionError(f"network failure: {exc.reason}")
                if attempt < MAX_RETRIES:
                    time.sleep(_backoff(attempt))
                    continue
                raise last_error from None
            except TimeoutError:
                last_error = NotionError("request timed out")
                if attempt < MAX_RETRIES:
                    time.sleep(_backoff(attempt))
                    continue
                raise last_error from None
        raise last_error or NotionError("request failed")

    def get(self, path: str, **query) -> dict:
        return self.request("GET", path, query=query or None)

    def post(self, path: str, body: dict | None = None) -> dict:
        return self.request("POST", path, body=body or {})

    def patch(self, path: str, body: dict) -> dict:
        return self.request("PATCH", path, body=body)

    def delete(self, path: str, body: dict | None = None) -> dict:
        return self.request("DELETE", path, body=body)

    # -- convenience -------------------------------------------------------- #

    def paginate(self, path: str, *, body: dict | None = None, method: str = "POST",
                 limit: int = 100, page_size: int = 100) -> list[dict]:
        """Collect up to `limit` results, following next_cursor."""
        results: list[dict] = []
        cursor: str | None = None
        page_size = max(1, min(page_size, 100))
        while True:
            payload = dict(body or {})
            payload["page_size"] = min(page_size, max(1, limit - len(results)))
            if cursor:
                payload["start_cursor"] = cursor
            page = self.request(method, path, body=payload if method == "POST" else None,
                                query=payload if method == "GET" else None)
            results.extend(page.get("results") or [])
            cursor = page.get("next_cursor")
            if not page.get("has_more") or not cursor or len(results) >= limit:
                break
        return results[:limit]

    def users_me(self) -> dict:
        return self.get("/users/me")

    def get_page(self, page_id: str) -> dict:
        return self.get(f"/pages/{normalize_id(page_id)}")

    def get_block(self, block_id: str) -> dict:
        return self.get(f"/blocks/{normalize_id(block_id)}")

    def block_children(self, block_id: str, *, limit: int = 100) -> list[dict]:
        return self.paginate(f"/blocks/{normalize_id(block_id)}/children",
                             method="GET", limit=limit)

    def resolve_container(self, database_id: str,
                          *, version: str | None = None) -> dict:
        """Describe a database/datasource id: which object it is and its sources."""
        raw = normalize_id(database_id)
        api_version = version or self.version
        if api_version >= "2025-09-03":
            first_error: NotionError | None = None
            try:
                db = self.get(f"/databases/{raw}")
                return {"kind": "database", "object": db,
                        "sources": db.get("data_sources") or []}
            except NotionError as exc:
                first_error = exc
                if exc.status not in (400, 404):
                    raise
            try:
                ds = self.get(f"/data_sources/{raw}")
            except NotionError:
                if first_error is not None:
                    raise first_error
                raise
            return {"kind": "data_source", "object": ds, "sources": [ds]}
        # legacy versions
        try:
            db = self.get(f"/databases/{raw}")
            return {"kind": "database", "object": db, "sources": []}
        except NotionError as exc:
            if exc.status not in (400, 404):
                raise
            ds = self.get(f"/data_sources/{raw}")
            return {"kind": "data_source", "object": ds, "sources": [ds]}

    def query_container(self, container_id: str, *, filter: dict | None = None,
                        sorts: list | None = None, limit: int = 100,
                        page_size: int = 100) -> tuple[str, list[dict]]:
        """Query rows of a database or data source; returns (used_source_id, rows).

        Handles the 2025-09-03 split automatically: a plain database id is
        resolved to its (first) data source, and a legacy-version error telling us
        the database has several data sources triggers a retry through
        /v1/data_sources.
        """
        raw = normalize_id(container_id)
        body: dict[str, Any] = {}
        if filter:
            body["filter"] = filter
        if sorts:
            body["sorts"] = sorts

        if self.version >= "2025-09-03":
            info = self.resolve_container(raw)
            if info["kind"] == "data_source":
                return raw, self.paginate(f"/data_sources/{raw}/query", body=body,
                                          limit=limit, page_size=page_size)
            sources = info["sources"]
            if not sources:
                raise UsageError(
                    f"database {short_id(raw)} has no data source visible to this "
                    "integration; share the database with the integration first"
                )
            source_id = normalize_id(sources[0]["id"])
            return source_id, self.paginate(f"/data_sources/{source_id}/query", body=body,
                                            limit=limit, page_size=page_size)

        try:
            rows = self.paginate(f"/databases/{raw}/query", body=body, limit=limit,
                                 page_size=page_size)
            return raw, rows
        except NotionError as exc:
            if not exc.is_data_source_mismatch:
                raise
            ids = ((exc.body or {}).get("additional_data") or {}).get("child_data_source_ids") or []
            source_id = normalize_id(ids[0])
            rows = self.paginate(f"/data_sources/{source_id}/query", body=body, limit=limit,
                                 page_size=page_size)
            return source_id, rows

    def create_page(self, parent_type: str, parent_id: str, properties: dict,
                    *, children: list | None = None, icon: str | None = None,
                    cover: str | None = None) -> dict:
        parent_key = {
            "page": "page_id",
            "database": "database_id",
            "data_source": "data_source_id",
            "workspace": None,
        }.get(parent_type)
        if parent_type == "workspace":
            parent: dict[str, Any] = {"type": "workspace", "workspace": True}
        elif parent_key:
            parent = {"type": parent_key, parent_key: normalize_id(parent_id)}
        else:
            raise UsageError(f"unknown parent type {parent_type!r}")
        body: dict[str, Any] = {"parent": parent, "properties": properties}
        if children:
            body["children"] = children
        if icon:
            body["icon"] = {"type": "emoji", "emoji": icon} if _is_emoji(icon) else {
                "type": "external", "external": {"url": icon}}
        if cover:
            body["cover"] = {"type": "external", "external": {"url": cover}}
        return self.post("/pages", body)

    def trash(self, object_kind: str, object_id: str) -> dict:
        """Move a page or block to the Notion trash (recoverable)."""
        raw = normalize_id(object_id)
        path = f"/{object_kind}s/{raw}"
        try:
            return self.patch(path, {"in_trash": True})
        except NotionError as exc:
            # older API versions only understand `archived`
            if exc.status == 400 and isinstance(exc.body, dict) and \
                    "in_trash" in json.dumps(exc.body):
                return self.patch(path, {"archived": True})
            raise


def _is_emoji(value: str) -> bool:
    return not value.startswith("http") and len(value) <= 8


def _backoff(attempt: int) -> float:
    return min(8.0, 0.5 * (2 ** attempt))


def _retry_after(headers, attempt: int) -> float:
    try:
        raw = headers.get("Retry-After") if headers else None
        if raw:
            return min(30.0, max(0.5, float(raw)))
    except (TypeError, ValueError):
        pass
    return _backoff(attempt)


# --------------------------------------------------------------------------- #
# delete guard + audit
# --------------------------------------------------------------------------- #

def delete_guard(*, yes: bool, confirm: str | None, targets: list[dict],
                 purge: bool = False, max_items: int | None = None,
                 kind: str = "object") -> None:
    """Enforce the skill's deletion red lines.

    Rules, in order:
      1. a dry run (no --yes) is always allowed, it just reports;
      2. --yes alone is not enough: --confirm must match a target id or title;
      3. batch deletes must declare --max and must not exceed it;
      4. --purge needs --confirm as well.
    """
    if not yes:
        return
    if not confirm:
        raise UsageError(
            f"refusing to delete {len(targets)} {kind}(s): --yes requires --confirm "
            "<exact id or title>, so the wrong row cannot be removed by accident"
        )
    needle = confirm.strip().lower()
    if len(needle) < 4:
        raise UsageError("--confirm must be at least 4 characters, e.g. an id prefix or "
                         "the exact title")
    matched = [t for t in targets if _target_matches(t, needle)]
    if not matched:
        preview = ", ".join(describe_target(t) for t in targets[:3])
        raise UsageError(
            f"--confirm {confirm!r} does not match any target. Candidates: {preview or '(none)'}"
        )
    if max_items is not None and len(targets) > max_items:
        raise UsageError(
            f"refusing to delete {len(targets)} {kind}(s): --max is {max_items}. "
            "Raise --max deliberately if that is really intended."
        )
    if len(targets) > 1 and max_items is None:
        raise UsageError(
            f"refusing to delete {len(targets)} {kind}(s) in one call: batch deletes need "
            "--max <n> so the blast radius is explicit"
        )
    if len(targets) > 25:
        raise UsageError(f"refusing to delete {len(targets)} {kind}(s) in one call "
                         "(hard ceiling is 25); split the work into reviewed batches")


def _target_matches(target: dict, needle: str) -> bool:
    candidates = {
        str(target.get("id") or ""),
        str(target.get("short_id") or ""),
        str(target.get("title") or ""),
        str(target.get("text") or ""),
    }
    for candidate in candidates:
        if not candidate:
            continue
        low = candidate.strip().lower()
        if low == needle or low.startswith(needle) or needle in low:
            return True
    return False


def describe_target(target: dict) -> str:
    label = target.get("title") or target.get("text") or "(untitled)"
    label = " ".join(str(label).split())[:60]
    return f"{target.get('short_id') or '?'} {target.get('type') or '?'} {label!r}"


def audit(event: str, *, dry_run: bool, payload: dict) -> None:
    """Append one JSON line to the skill audit log. Never raises."""
    if not AUDIT_LOG.name:  # e.g. NOTION_AUDIT_LOG pointed at a directory-like path
        return
    record = {
        "at": datetime.now(timezone.utc).isoformat(),
        "event": event,
        "dry_run": dry_run,
        **payload,
    }
    try:
        AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
        with AUDIT_LOG.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:  # audit must not break the operation, but say so
        print(f"[notion] warning: could not write audit log: {exc}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# object rendering helpers
# --------------------------------------------------------------------------- #

PLAIN = {
    "title": lambda prop: rich_text_plain(prop.get("title")),
    "rich_text": lambda prop: rich_text_plain(prop.get("rich_text")),
    "number": lambda prop: prop.get("number"),
    "select": lambda prop: (prop.get("select") or {}).get("name"),
    "multi_select": lambda prop: ", ".join(o.get("name", "") for o in prop.get("multi_select") or []),
    "status": lambda prop: (prop.get("status") or {}).get("name"),
    "date": lambda prop: _date_plain(prop.get("date")),
    "people": lambda prop: ", ".join(p.get("name") or p.get("id", "") for p in prop.get("people") or []),
    "files": lambda prop: ", ".join(f.get("name") or f.get("external", {}).get("url", "")
                                    for f in prop.get("files") or []),
    "checkbox": lambda prop: prop.get("checkbox"),
    "url": lambda prop: prop.get("url"),
    "email": lambda prop: prop.get("email"),
    "phone_number": lambda prop: prop.get("phone_number"),
    "formula": lambda prop: _formula_plain(prop.get("formula")),
    "relation": lambda prop: ", ".join(r.get("id", "") for r in prop.get("relation") or []),
    "rollup": lambda prop: _rollup_plain(prop.get("rollup")),
    "created_time": lambda prop: prop.get("created_time"),
    "last_edited_time": lambda prop: prop.get("last_edited_time"),
    "created_by": lambda prop: (prop.get("created_by") or {}).get("id"),
    "last_edited_by": lambda prop: (prop.get("last_edited_by") or {}).get("id"),
    "unique_id": lambda prop: _unique_id_plain(prop.get("unique_id")),
    "verification": lambda prop: (prop.get("verification") or {}).get("state"),
}


def rich_text_plain(items: Iterable[dict] | None) -> str:
    if not items:
        return ""
    return "".join(item.get("plain_text", "") for item in items)


def _date_plain(value: dict | None) -> str | None:
    if not value:
        return None
    start, end = value.get("start"), value.get("end")
    if start and end:
        return f"{start} -> {end}"
    return start or end


def _formula_plain(value: dict | None):
    if not value:
        return None
    return value.get(value.get("type", ""), None)


def _rollup_plain(value: dict | None):
    if not value:
        return None
    kind = value.get("type")
    if kind == "array":
        return [_property_plain(item) for item in value.get("array") or []]
    return value.get(kind) if kind else None


def _unique_id_plain(value: dict | None) -> str | None:
    if not value:
        return None
    prefix, number = value.get("prefix"), value.get("number")
    return f"{prefix}-{number}" if prefix else (str(number) if number is not None else None)


def _property_plain(prop: dict) -> Any:
    if not isinstance(prop, dict):
        return prop
    kind = prop.get("type")
    handler = PLAIN.get(kind)
    if handler:
        try:
            return handler(prop)
        except (KeyError, TypeError, AttributeError):
            return None
    return prop.get(kind)


def property_plain(prop: dict) -> Any:
    return _property_plain(prop)


def object_title(obj: dict) -> str:
    """Best-effort title for a page, database or data source."""
    if obj.get("object") == "page":
        for prop in (obj.get("properties") or {}).values():
            if isinstance(prop, dict) and prop.get("type") == "title":
                text = rich_text_plain(prop.get("title"))
                if text:
                    return text
        return "(untitled page)"
    if obj.get("object") in ("database", "data_source"):
        text = rich_text_plain(obj.get("title"))
        return text or obj.get("name") or "(untitled)"
    if obj.get("object") == "block":
        return block_text(obj) or obj.get("type", "block")
    return obj.get("id", "(unknown)")


def page_summary(page: dict, *, include_props: bool = True) -> dict:
    props = {}
    if include_props:
        for name, prop in (page.get("properties") or {}).items():
            props[name] = property_plain(prop)
    return {
        "id": page.get("id"),
        "short_id": short_id(page["id"]) if page.get("id") else None,
        "title": object_title(page),
        "url": page.get("url"),
        "parent": page.get("parent"),
        "created_time": page.get("created_time"),
        "last_edited_time": page.get("last_edited_time"),
        "in_trash": page.get("in_trash"),
        "properties": props,
    }


# --------------------------------------------------------------------------- #
# block rendering
# --------------------------------------------------------------------------- #

_SUPPORTED_BLOCKS = {
    "paragraph", "heading_1", "heading_2", "heading_3", "bulleted_list_item",
    "numbered_list_item", "to_do", "toggle", "quote", "callout", "code", "divider",
    "bookmark", "image", "video", "file", "pdf", "child_page", "child_database",
    "equation", "table_row", "column_list", "column", "synced_block", "link_preview",
    "template", "breadcrumb", "table_of_contents", "unsupported", "embed",
}


def block_text(block: dict) -> str:
    """Extract plain text from any block type, tolerating unknown shapes."""
    kind = block.get("type")
    if kind == "child_page":
        return (block.get("child_page") or {}).get("title", "")
    if kind == "child_database":
        return (block.get("child_database") or {}).get("title", "")
    payload = block.get(kind) if kind else None
    if not isinstance(payload, dict):
        return ""
    if "rich_text" in payload:
        return rich_text_plain(payload.get("rich_text"))
    if "caption" in payload:
        return rich_text_plain(payload.get("caption"))
    if kind == "equation":
        return payload.get("expression", "")
    if kind in ("image", "video", "file", "pdf"):
        return payload.get("external", {}).get("url") or payload.get("file", {}).get("url", "")
    return ""


def block_to_markdown(block: dict, *, indent: int = 0) -> str:
    kind = block.get("type", "unknown")
    pad = "  " * indent
    text = block_text(block)
    payload = block.get(kind) if isinstance(block.get(kind), dict) else {}
    if kind == "heading_1":
        return f"{pad}# {text}"
    if kind == "heading_2":
        return f"{pad}## {text}"
    if kind == "heading_3":
        return f"{pad}### {text}"
    if kind == "bulleted_list_item":
        return f"{pad}- {text}"
    if kind == "numbered_list_item":
        return f"{pad}1. {text}"
    if kind == "to_do":
        mark = "x" if payload.get("checked") else " "
        return f"{pad}- [{mark}] {text}"
    if kind == "toggle":
        return f"{pad}- {text}"
    if kind == "quote":
        return f"{pad}> {text}"
    if kind == "callout":
        icon = (payload.get("icon") or {}).get("emoji", "")
        return f"{pad}> {icon} {text}".rstrip()
    if kind == "code":
        language = payload.get("language") or ""
        return f"{pad}```{language}\n{text}\n{pad}```"
    if kind == "divider":
        return f"{pad}---"
    if kind == "equation":
        return f"{pad}$$\n{text}\n$$"
    if kind == "bookmark":
        return f"{pad}{text}"
    if kind in ("image", "video", "file", "pdf"):
        return f"{pad}![]({text})" if text else ""
    if kind == "child_page":
        return f"{pad}## {text} (child page)"
    if kind == "child_database":
        return f"{pad}## {text} (child database)"
    if kind == "table_row":
        cells = payload.get("cells") or []
        return f"{pad}| " + " | ".join(rich_text_plain(cell) for cell in cells) + " |"
    if kind in ("unsupported",):
        return f"{pad}<!-- unsupported block -->"
    if kind in ("column_list", "column", "synced_block"):
        return ""
    return f"{pad}{text}" if text else ""


def flatten_blocks(blocks: list[dict], children_of: dict[str, list[dict]] | None = None,
                   depth: int = 0, max_depth: int = 3) -> list[dict]:
    """Flatten a block tree into a readable outline."""
    children_of = children_of or {}
    rows: list[dict] = []
    for block in blocks:
        rows.append({
            "id": block.get("id"),
            "short_id": short_id(block["id"]) if block.get("id") else None,
            "type": block.get("type"),
            "text": block_text(block),
            "has_children": block.get("has_children"),
            "depth": depth,
            "markdown": block_to_markdown(block, indent=depth),
        })
        kids = children_of.get(block.get("id") or "", [])
        if kids and depth < max_depth:
            rows.extend(flatten_blocks(kids, children_of, depth + 1, max_depth))
    return rows


# --------------------------------------------------------------------------- #
# input parsing helpers
# --------------------------------------------------------------------------- #

def load_json_arg(value: str) -> Any:
    """Accept inline JSON, @file, or - for stdin."""
    text = value
    if value == "-":
        text = sys.stdin.read()
    elif value.startswith("@"):
        try:
            text = Path(value[1:]).read_text(encoding="utf-8")
        except OSError as exc:
            raise UsageError(f"cannot read JSON from {value[1:]}: {exc}") from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise UsageError(f"invalid JSON: {exc}") from exc


def parse_properties(pairs: list[str] | None) -> dict:
    """Turn `--prop Name=Value` / `--prop 'Name:type=Value'` into Notion properties.

    Without an explicit type we pick by value shape: a JSON object/array is sent
    as-is, `true`/`false` become checkboxes, numbers become numbers, and anything
    else becomes rich_text or the title (for the title property).
    """
    result: dict[str, Any] = {}
    for pair in pairs or []:
        if "=" not in pair:
            raise UsageError(f"--prop expects Name=Value, got {pair!r}")
        left, value = pair.split("=", 1)
        if ":" in left:
            name, forced = left.rsplit(":", 1)
        else:
            name, forced = left, None
        name = name.strip()
        if not name:
            raise UsageError(f"--prop has an empty name: {pair!r}")
        result[name] = _property_value(value, forced)
    return result


def _property_value(value: str, forced: str | None) -> dict:
    stripped = value.strip()
    if forced is None and stripped[:1] in "{[[":
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass
    if forced in ("title", "rich_text", "text"):
        key = "title" if forced == "title" else "rich_text"
        return {key: [{"text": {"content": value}}]}
    if forced == "number":
        return {"number": float(value)}
    if forced == "checkbox":
        return {"checkbox": _truthy(value)}
    if forced == "select":
        return {"select": {"name": value}}
    if forced == "multi_select":
        return {"multi_select": [{"name": item.strip()} for item in value.split(",") if item.strip()]}
    if forced == "date":
        return {"date": {"start": value}}
    if forced == "url":
        return {"url": value}
    if forced == "email":
        return {"email": value}
    if forced in ("relation", "people"):
        ids = [item.strip() for item in value.split(",") if item.strip()]
        key = "relation" if forced == "relation" else "people"
        return {key: [{"id": item} for item in ids]}
    if forced:
        raise UsageError(f"unsupported --prop type {forced!r}")
    if stripped.lower() in ("true", "false"):
        return {"rich_text": [{"text": {"content": value}}]}
    return {"rich_text": [{"text": {"content": value}}]}


def _truthy(value: str) -> bool:
    return value.strip().lower() in ("1", "true", "yes", "y", "on")


def text_to_blocks(text: str, block_type: str = "paragraph") -> list[dict]:
    """One block per non-empty line; blank lines are dropped."""
    blocks: list[dict] = []
    for line in text.splitlines():
        content = line.rstrip()
        if not content.strip():
            continue
        blocks.append({
            "object": "block",
            "type": block_type,
            block_type: {"rich_text": [{"type": "text", "text": {"content": content}}]},
        })
    return blocks


def markdown_to_blocks(text: str) -> list[dict]:
    """Very small Markdown subset -> Notion blocks (#, ##, ###, -, 1., >, ---, ```)."""
    blocks: list[dict] = []
    in_code = False
    code_lines: list[str] = []
    code_language = "plain text"
    for line in text.splitlines():
        if line.strip().startswith("```"):
            if in_code:
                blocks.append({"object": "block", "type": "code", "code": {
                    "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
                    "language": code_language,
                }})
                in_code, code_lines = False, []
            else:
                in_code = True
                code_language = line.strip().strip("`").strip() or "plain text"
            continue
        if in_code:
            code_lines.append(line)
            continue
        stripped = line.strip()
        if not stripped:
            continue
        if stripped in ("---", "***", "___"):
            blocks.append({"object": "block", "type": "divider", "divider": {}})
            continue
        heading = re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if heading:
            level = len(heading.group(1))
            blocks.append({"object": "block", "type": f"heading_{level}", f"heading_{level}": {
                "rich_text": [{"type": "text", "text": {"content": heading.group(2)}}]}})
            continue
        todo = re.match(r"^[-*]\s+\[( |x|X)\]\s+(.*)$", stripped)
        if todo:
            blocks.append({"object": "block", "type": "to_do", "to_do": {
                "rich_text": [{"type": "text", "text": {"content": todo.group(2)}}],
                "checked": todo.group(1).lower() == "x"}})
            continue
        bullet = re.match(r"^[-*]\s+(.*)$", stripped)
        if bullet:
            blocks.append({"object": "block", "type": "bulleted_list_item",
                           "bulleted_list_item": {"rich_text": [
                               {"type": "text", "text": {"content": bullet.group(1)}}]}})
            continue
        numbered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if numbered:
            blocks.append({"object": "block", "type": "numbered_list_item",
                           "numbered_list_item": {"rich_text": [
                               {"type": "text", "text": {"content": numbered.group(1)}}]}})
            continue
        quote = re.match(r"^>\s?(.*)$", stripped)
        if quote:
            blocks.append({"object": "block", "type": "quote", "quote": {"rich_text": [
                {"type": "text", "text": {"content": quote.group(1)}}]}})
            continue
        blocks.append({"object": "block", "type": "paragraph", "paragraph": {"rich_text": [
            {"type": "text", "text": {"content": stripped}}]}})
    if in_code and code_lines:
        blocks.append({"object": "block", "type": "code", "code": {
            "rich_text": [{"type": "text", "text": {"content": "\n".join(code_lines)}}],
            "language": code_language}})
    return blocks


def chunked(items: list, size: int = 100) -> Iterable[list]:
    for index in range(0, len(items), size):
        yield items[index:index + size]


def emit(payload: Any, *, fmt: str = "json") -> None:
    """Print a result: json (default) or markdown/text."""
    if fmt == "json":
        print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))
        return
    if isinstance(payload, dict) and "markdown" in payload and fmt in ("md", "markdown"):
        print(payload["markdown"])
        return
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


def fail(message: str, *, code: int = 1) -> int:
    print(f"error: {message}", file=sys.stderr)
    return code
