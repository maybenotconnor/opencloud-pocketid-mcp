"""WebDAV tools for OpenCloud file management. 10 tools."""

import base64
import fnmatch
import os
import posixpath
import re
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Annotated
from urllib.parse import unquote

import httpx
from fastmcp import FastMCP
from mcp.types import ImageContent
from webdav4.client import Client as WebDAVClient, ResourceAlreadyExists

from src.config import settings
from src.utils import format_error, sanitize_path

webdav_server = FastMCP(name="WebDAV")

# Module-level client, initialized at import time.
# The server runs in a single container with stable env vars,
# so eager init is fine here.
_client: WebDAVClient | None = None


def _get_client() -> WebDAVClient:
    global _client
    if _client is None:
        _client = WebDAVClient(
            base_url=settings.webdav_url,
            auth=(settings.opencloud_username, settings.opencloud_password),
        )
    return _client


# --- Glob helpers ---

def _glob_base(pattern: str) -> str:
    """Extract the non-wildcard prefix directory from a glob pattern."""
    idx = len(pattern)
    for i, c in enumerate(pattern):
        if c in ("*", "?", "[", "{"):
            idx = i
            break
    prefix = pattern[:idx]
    last_slash = prefix.rfind("/")
    if last_slash <= 0:
        return "/"
    return prefix[:last_slash] or "/"


_MAX_BRACE_EXPANSIONS = 64


def _split_top_commas(s: str) -> list[str]:
    """Split on commas that are not nested inside a deeper brace group."""
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in s:
        if c == "{":
            depth += 1
            cur.append(c)
        elif c == "}":
            depth -= 1
            cur.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur))
    return parts


def _expand_braces(pattern: str, _depth: int = 0) -> list[str]:
    """Expand brace alternations: 'a{b,c}d' -> ['abd', 'acd'].

    Handles nested and multiple groups; a comma-less group (e.g. '{x}') is left
    literal. Expansion is capped to avoid combinatorial blowup.
    """
    if "{" not in pattern or _depth > 10:
        return [pattern]
    i = 0
    n = len(pattern)
    while i < n:
        if pattern[i] == "{":
            depth = 1
            j = i + 1
            has_comma = False
            while j < n and depth > 0:
                c = pattern[j]
                if c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                elif c == "," and depth == 1:
                    has_comma = True
                j += 1
            if depth == 0 and has_comma:
                close = j - 1
                prefix, suffix = pattern[:i], pattern[close + 1:]
                out: list[str] = []
                for opt in _split_top_commas(pattern[i + 1:close]):
                    out.extend(_expand_braces(prefix + opt + suffix, _depth + 1))
                    if len(out) >= _MAX_BRACE_EXPANSIONS:
                        break
                return out[:_MAX_BRACE_EXPANSIONS]
            # Unbalanced or comma-less '{': treat literally, keep scanning.
        i += 1
    return [pattern]


def _pattern_segments(pattern: str) -> list[str] | None:
    """Split an absolute glob pattern into lowercase path segments.

    Returns None for basename-only patterns (no '/'), which match at any depth.
    A '**' segment matches zero or more path segments.
    """
    pat = pattern.lower()
    if "/" not in pat:
        return None
    if not pat.startswith("/"):
        pat = "/" + pat
    return [seg for seg in pat.split("/") if seg != ""]


def _reachable_states(path_segs: list[str], pat_segs: list[str]) -> set[int]:
    """Run a small segment NFA and return the pattern positions reachable
    after consuming all of ``path_segs``.

    Position ``len(pat_segs)`` means the whole pattern was consumed (a full
    match). A '**' segment may consume zero or more path segments.
    """
    def closure(states: set[int]) -> set[int]:
        result = set(states)
        stack = list(states)
        while stack:
            s = stack.pop()
            # '**' may match zero segments — skip past it.
            if s < len(pat_segs) and pat_segs[s] == "**" and s + 1 not in result:
                result.add(s + 1)
                stack.append(s + 1)
        return result

    states = closure({0})
    for seg in path_segs:
        nxt: set[int] = set()
        for s in states:
            if s >= len(pat_segs):
                continue
            tok = pat_segs[s]
            if tok == "**":
                nxt.add(s)  # '**' consumes this segment and stays
            elif fnmatch.fnmatch(seg, tok):
                nxt.add(s + 1)
        states = closure(nxt)
    return states


def _glob_match(item_path: str, pattern: str) -> bool:
    """Match a full item path against a glob pattern (case-insensitive).

    Supports *, ?, [..] char classes, {a,b} alternation, and ** (zero or more
    path segments). Patterns without / are matched against the basename only.
    """
    return any(_glob_match_one(item_path, p) for p in _expand_braces(pattern))


def _glob_match_one(item_path: str, pattern: str) -> bool:
    """Match a single brace-free glob pattern against a full item path."""
    item = item_path.lower().rstrip("/")
    pat_segs = _pattern_segments(pattern)
    if pat_segs is None:
        return fnmatch.fnmatch(posixpath.basename(item), pattern.lower())
    path_segs = [seg for seg in item.split("/") if seg != ""]
    return len(pat_segs) in _reachable_states(path_segs, pat_segs)


def _glob_can_descend(dir_path: str, pattern: str) -> bool:
    """Return True if some item *beneath* ``dir_path`` could still match the
    pattern (for any brace expansion).

    Used to prune the walk: a directory is only entered when the pattern can
    match something deeper. Conservative — when in doubt it returns True, so a
    real match is never pruned.
    """
    return any(_can_descend_one(dir_path, p) for p in _expand_braces(pattern))


def _can_descend_one(dir_path: str, pattern: str) -> bool:
    pat_segs = _pattern_segments(pattern)
    if pat_segs is None:
        # Basename-only patterns can match at any depth.
        return True
    path_segs = [seg for seg in dir_path.lower().rstrip("/").split("/") if seg != ""]
    states = _reachable_states(path_segs, pat_segs)
    # Descending is worthwhile only if the pattern still has tokens left to
    # match further segments from at least one reachable state.
    return any(s < len(pat_segs) for s in states)


def _is_deep_pattern(pattern: str) -> bool:
    """True when the pattern can match at arbitrary depth ('**' or basename-only).

    Combined with a root ('/') base, such patterns would crawl the whole drive
    one PROPFIND at a time, so they are served by the search index instead.
    Folder-scoped deep patterns still walk their subtree (see glob()).
    """
    return "**" in pattern or "/" not in pattern


def _glob_search_name(pattern: str) -> str:
    """Derive a KQL ``name:`` filter that is a *superset* of the pattern's
    basename match, so post-filtering with ``_glob_match`` stays correct.

    The basename token's wildcards/char-classes are widened to ``*`` (e.g.
    ``*[Hh]obb*`` -> ``*obb*``); literal runs are preserved to keep the
    server-side query selective.
    """
    token = pattern.rsplit("/", 1)[-1]
    out: list[str] = []
    i = 0
    while i < len(token):
        c = token[i]
        if c in "*?":
            out.append("*")
            i += 1
        elif c == "[":
            j = token.find("]", i + 1)
            out.append("*")
            i = j + 1 if j != -1 else i + 1
        else:
            out.append(c)
            i += 1
    return re.sub(r"\*+", "*", "".join(out))


# The client-side walk lists each level's directories concurrently and stops
# after a budget of directories, so a scoped pattern over a very large subtree
# returns (partial) results instead of hanging.
_WALK_CONCURRENCY = 12
_WALK_DIR_BUDGET = 800


def _to_dt(value: str) -> datetime:
    """Parse an ISO 8601 or RFC 1123 (HTTP) date into an aware datetime.

    Returns the epoch-min on failure so unparseable entries sort last under a
    most-recent-first ordering.
    """
    if value:
        for parse in (datetime.fromisoformat, parsedate_to_datetime):
            try:
                dt = parse(value)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt
            except (ValueError, TypeError):
                continue
    return datetime.min.replace(tzinfo=timezone.utc)


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def glob(
    pattern: Annotated[str, "Glob pattern — embeds path and name, e.g. '/Documents/**/*.pdf', '*.txt', '**/*report*'"] = "**/*",
    modified_after: Annotated[str, "ISO 8601 datetime — only items modified after this, e.g. '2026-03-04'"] = "",
    file_type: Annotated[str, "Filter by type: 'file', 'directory', or 'all'"] = "all",
    depth: Annotated[int, "Max recursion depth. 0 = unlimited, 1 = single directory only"] = 0,
    limit: Annotated[int, "Max results to return (default 100, max 500)"] = 100,
) -> list[dict] | str:
    """Find files by path pattern. Pattern uses glob syntax — ** matches any depth, [..] char classes supported. Examples: /Documents/**/*.pdf, *.txt, **/*report*. Results are sorted most-recently-modified first. Drive-wide patterns (rooted at '/' with ** or basename-only) use the server search index; scoped patterns walk that subtree. Use search for full-text content search."""
    try:
        base_path = sanitize_path(_glob_base(pattern))
        client = _get_client()
        limit = min(max(limit, 1), 500)

        cutoff = None
        if modified_after:
            cutoff = datetime.fromisoformat(modified_after)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)

        # Drive-wide unbounded-depth patterns (rooted at '/', containing ** or
        # basename-only) would force a full client-side crawl. Serve them from
        # the server-side search index — a few requests — falling back to the
        # walk only when the index is unavailable. Scoped patterns (rooted at a
        # folder) use the walk so results can't be silently truncated by the
        # index's global result cap.
        if depth == 0 and base_path == "/" and _is_deep_pattern(pattern):
            searched = _glob_via_search(pattern, cutoff, file_type, limit)
            if searched is not None:
                searched.sort(key=lambda r: _to_dt(r.get("modified", "")), reverse=True)
                if len(searched) >= limit:
                    searched = searched[:limit]
                    searched.append({"note": f"Results truncated at {limit} matches"})
                return searched

        results: list[dict] = []
        dirs_visited = 0
        budget_hit = False
        frontier = [base_path]
        current_depth = 1

        def _list_dir(dir_path: str) -> list[dict]:
            try:
                return client.ls(dir_path, detail=True)
            except Exception:
                return []

        # Breadth-first walk, listing each level's directories concurrently so
        # the per-directory PROPFIND round-trips overlap instead of serializing.
        with ThreadPoolExecutor(max_workers=_WALK_CONCURRENCY) as pool:
            while frontier and len(results) < limit and not budget_hit:
                if depth > 0 and current_depth > depth:
                    break
                batch = frontier
                if dirs_visited + len(batch) > _WALK_DIR_BUDGET:
                    batch = batch[: max(0, _WALK_DIR_BUDGET - dirs_visited)]
                    budget_hit = True
                dirs_visited += len(batch)

                next_frontier: list[str] = []
                for dir_path, items in zip(batch, pool.map(_list_dir, batch)):
                    for item in items:
                        item_path = item.get("name", "")
                        # webdav4 returns paths relative to the base URL.
                        if not item_path.startswith("/"):
                            item_path = "/" + item_path
                        if item_path.rstrip("/") == dir_path.rstrip("/"):
                            continue
                        is_dir = item.get("type") == "directory"
                        item_type = "directory" if is_dir else "file"

                        # Queue subdirs only when the pattern can still match
                        # something deeper, pruning branches that can't match.
                        if is_dir and _glob_can_descend(item_path, pattern):
                            next_frontier.append(item_path)

                        if file_type != "all" and item_type != file_type:
                            continue
                        if not _glob_match(item_path.rstrip("/"), pattern):
                            continue
                        if cutoff:
                            mod_str = item.get("modified", "")
                            if mod_str:
                                try:
                                    mod_dt = datetime.fromisoformat(str(mod_str))
                                    if mod_dt.tzinfo is None:
                                        mod_dt = mod_dt.replace(tzinfo=timezone.utc)
                                    if mod_dt < cutoff:
                                        continue
                                except (ValueError, TypeError):
                                    pass

                        results.append({
                            "name": posixpath.basename(item_path.rstrip("/")),
                            "path": item_path,
                            "size": item.get("content_length", 0),
                            "modified": item.get("modified", ""),
                            "type": item_type,
                        })

                frontier = next_frontier
                current_depth += 1

        results.sort(key=lambda r: _to_dt(r.get("modified", "")), reverse=True)

        if len(results) >= limit:
            results = results[:limit]
            results.append({"note": f"Results truncated at {limit} matches"})
        elif budget_hit:
            results.append({
                "note": f"Traversal stopped after {_WALK_DIR_BUDGET} directories — "
                        "results may be incomplete. Narrow the path or use search."
            })

        return results
    except ValueError as e:
        return format_error("glob", str(e))
    except Exception as e:
        return format_error("glob", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def read_file(
    path: Annotated[str, "Path to the file to read, e.g. '/Documents/notes.txt'"],
    binary: Annotated[bool, "Return base64-encoded content for non-image binary files"] = False,
) -> str | ImageContent:
    """Read a file's content. Text files returned as string (max 1MB). Images returned as image content automatically. Other binary files require binary=True for base64 (max 5MB)."""
    try:
        path = sanitize_path(path)
        client = _get_client()

        info = client.info(path)
        size = info.get("content_length", 0)
        mime = (info.get("content_type") or "").lower()

        # Images: return as multimodal image content
        if mime.startswith("image/"):
            if size and size > 5_242_880:
                return format_error(
                    "read_file",
                    f"Image is {size / 1_048_576:.1f}MB, exceeds 5MB limit.",
                )
            with tempfile.NamedTemporaryFile() as tmp:
                client.download_file(path, tmp.name)
                with open(tmp.name, "rb") as f:
                    data = f.read()
            return ImageContent(
                type="image",
                data=base64.b64encode(data).decode("ascii"),
                mimeType=mime,
            )

        # Binary mode: return base64
        if binary:
            if size and size > 5_242_880:
                return format_error(
                    "read_file",
                    f"File is {size / 1_048_576:.1f}MB, exceeds 5MB limit.",
                )
            with tempfile.NamedTemporaryFile() as tmp:
                client.download_file(path, tmp.name)
                with open(tmp.name, "rb") as f:
                    data = f.read()
            return base64.b64encode(data).decode("ascii")

        # Text mode
        if size and size > 1_048_576:
            return format_error(
                "read_file",
                f"File is {size / 1_048_576:.1f}MB, exceeds 1MB limit. "
                "Use binary=True for large binary files.",
            )

        with tempfile.NamedTemporaryFile() as tmp:
            client.download_file(path, tmp.name)
            with open(tmp.name, "rb") as f:
                chunk = f.read(8192)
                if b"\x00" in chunk:
                    return format_error(
                        "read_file",
                        f"File appears to be binary (detected type: {mime or 'unknown'}). "
                        "Images are returned automatically; use binary=True for other binary files.",
                    )
            with open(tmp.name, "r", encoding="utf-8") as f:
                return f.read()
    except ValueError as e:
        return format_error("read_file", str(e))
    except UnicodeDecodeError:
        return format_error(
            "read_file",
            "File is not valid UTF-8 text. Use binary=True for base64.",
        )
    except Exception as e:
        return format_error("read_file", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def write_file(
    path: Annotated[str, "Destination path, e.g. '/Documents/notes.txt'"],
    content: Annotated[str, "Text content to write"],
) -> str:
    """Write text content to a file. Creates parent directories if needed. Overwrites existing files."""
    try:
        path = sanitize_path(path)
        client = _get_client()

        # Auto-create parent directories
        parent = posixpath.dirname(path)
        if parent and parent != "/":
            try:
                client.mkdir(parent)
            except ResourceAlreadyExists:
                pass

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt") as tmp:
            tmp.write(content)
            tmp.flush()
            client.upload_file(tmp.name, path, overwrite=True)

        return f"Successfully wrote {len(content)} characters to {path}"
    except ValueError as e:
        return format_error("write_file", str(e))
    except Exception as e:
        return format_error("write_file", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def edit_file(
    path: Annotated[str, "Path to the file to edit, e.g. '/Documents/notes.txt'"],
    old_str: Annotated[str, "Exact string to find — must appear exactly once in the file"],
    new_str: Annotated[str, "Replacement string"],
) -> str:
    """Make a targeted edit to a text file by replacing an exact string. Fails if old_str is not found or appears more than once (max 1MB)."""
    try:
        path = sanitize_path(path)
        client = _get_client()

        info = client.info(path)
        size = info.get("content_length", 0)
        if size and size > 1_048_576:
            return format_error(
                "edit_file",
                f"File is {size / 1_048_576:.1f}MB, exceeds 1MB limit.",
            )

        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as tmp:
            tmp_path = tmp.name

        try:
            client.download_file(path, tmp_path)
            with open(tmp_path, "r", encoding="utf-8") as f:
                content = f.read()

            count = content.count(old_str)
            if count == 0:
                return format_error("edit_file", "old_str not found in file")
            if count > 1:
                return format_error(
                    "edit_file",
                    f"old_str found {count} times — provide more context to make it unique",
                )

            new_content = content.replace(old_str, new_str, 1)
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            client.upload_file(tmp_path, path, overwrite=True)

            return f"Edited {path}"
        finally:
            os.unlink(tmp_path)
    except ValueError as e:
        return format_error("edit_file", str(e))
    except UnicodeDecodeError:
        return format_error("edit_file", "File is not valid UTF-8 text")
    except Exception as e:
        return format_error("edit_file", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def mkdir(
    path: Annotated[str, "Directory path to create, e.g. '/Documents/Projects'"],
) -> str:
    """Create a directory. Idempotent — succeeds if directory already exists."""
    try:
        path = sanitize_path(path)
        client = _get_client()
        try:
            client.mkdir(path)
        except ResourceAlreadyExists:
            return f"Directory already exists: {path}"
        return f"Directory created: {path}"
    except ValueError as e:
        return format_error("mkdir", str(e))
    except Exception as e:
        return format_error("mkdir", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def delete(
    path: Annotated[str, "Path to the file or directory to delete"],
) -> str:
    """Delete a file or directory (including contents)."""
    try:
        path = sanitize_path(path)
        client = _get_client()
        client.remove(path)
        return f"Deleted: {path}"
    except ValueError as e:
        return format_error("delete", str(e))
    except Exception as e:
        return format_error("delete", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "openWorldHint": True,
    }
)
def move(
    source: Annotated[str, "Source path"],
    dest: Annotated[str, "Destination path"],
) -> str:
    """Move or rename a file or directory."""
    try:
        source = sanitize_path(source)
        dest = sanitize_path(dest)
        client = _get_client()
        client.move(source, dest)
        return f"Moved {source} → {dest}"
    except ValueError as e:
        return format_error("move", str(e))
    except Exception as e:
        return format_error("move", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def copy(
    source: Annotated[str, "Source path"],
    dest: Annotated[str, "Destination path"],
) -> str:
    """Copy a file or directory."""
    try:
        source = sanitize_path(source)
        dest = sanitize_path(dest)
        client = _get_client()
        client.copy(source, dest)
        return f"Copied {source} → {dest}"
    except ValueError as e:
        return format_error("copy", str(e))
    except Exception as e:
        return format_error("copy", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def get_file_info(
    path: Annotated[str, "Path to the file or directory"],
) -> dict | str:
    """Get detailed metadata for a file or directory."""
    try:
        path = sanitize_path(path)
        client = _get_client()
        info = client.info(path)
        return {
            "path": path,
            "size": info.get("content_length", 0),
            "modified": info.get("modified", ""),
            "etag": info.get("etag", ""),
            "content_type": info.get("content_type", ""),
            "type": "directory" if info.get("type") == "directory" else "file",
        }
    except ValueError as e:
        return format_error("get_file_info", str(e))
    except Exception as e:
        return format_error("get_file_info", str(e))


# --- Server-side search helpers ---

def _build_kql(
    pattern: str,
    filename: str,
    mediatype: str,
    modified_after: str,
    modified_before: str,
) -> str:
    """Build a KQL query string from structured parameters.

    Each value is stripped of KQL operator characters before embedding
    to prevent injection of unintended query clauses.
    """
    parts: list[str] = []
    if pattern:
        # Keep word chars, hyphens, apostrophes; strip KQL operators
        terms = [re.sub(r"[^\w\-']", "", t) for t in pattern.split()]
        terms = [t for t in terms if t]
        # Web-search style: each word matches the file name (substring) OR the
        # extracted content. Name is always indexed; content needs the server's
        # Tika extractor. Words are OR'd so over-listing keywords doesn't collapse
        # to zero results — relevance ranking floats all-term matches to the top.
        clauses = [f"(name:*{t}* OR content:{t})" for t in terms]
        if clauses:
            group = " OR ".join(clauses)
            if len(clauses) > 1:
                group = f"({group})"
            parts.append(group)
    if filename:
        # Allow glob chars (* ?) and common filename characters
        safe = re.sub(r"[^\w.*?\-/ ]", "", filename)
        if safe:
            parts.append(f"name:{safe}")
    if mediatype:
        safe = re.sub(r"\W", "", mediatype)
        if safe:
            parts.append(f"mediatype:{safe}")
    if modified_after:
        # ISO 8601 dates only contain digits, dashes, T, colon, Z, +
        safe = re.sub(r"[^\d\-T:Z+]", "", modified_after)
        if safe:
            parts.append(f"mtime>={safe}")
    if modified_before:
        safe = re.sub(r"[^\d\-T:Z+]", "", modified_before)
        if safe:
            parts.append(f"mtime<={safe}")
    return " ".join(parts)


def _xml_escape(text: str) -> str:
    """Escape special characters for safe XML embedding."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _get_search_url() -> str:
    """Return the REPORT endpoint for server-side search."""
    base = settings.opencloud_url.rstrip("/")
    return f"{base}/remote.php/dav/files/{settings.opencloud_username}"


def _parse_search_response(xml_text: str) -> list[dict]:
    """Parse a 207 multistatus XML response from oc:search-files."""
    results: list[dict] = []

    for block in re.findall(
        r"<(?:\w+:)?response>(.*?)</(?:\w+:)?response>", xml_text, re.DOTALL
    ):
        entry: dict = {}

        m = re.search(r"<oc:name>(.*?)</oc:name>", block)
        entry["name"] = m.group(1) if m else ""

        m = re.search(r"<(?:\w+:)?href>(.*?)</(?:\w+:)?href>", block)
        if m:
            raw_path = unquote(m.group(1))
            cleaned = re.sub(r"^/remote\.php/dav/spaces/[^/]+", "", raw_path)
            entry["path"] = cleaned or "/"
        else:
            entry["path"] = ""

        if re.search(r"<(?:\w+:)?collection\s*/?>", block):
            entry["type"] = "directory"
        else:
            entry["type"] = "file"

        m = re.search(r"<(?:\w+:)?getcontentlength>(.*?)</(?:\w+:)?getcontentlength>", block)
        entry["size"] = int(m.group(1)) if m else 0

        m = re.search(r"<(?:\w+:)?getlastmodified>(.*?)</(?:\w+:)?getlastmodified>", block)
        entry["modified"] = m.group(1) if m else ""

        m = re.search(r"<(?:\w+:)?getcontenttype>(.*?)</(?:\w+:)?getcontenttype>", block)
        entry["content_type"] = m.group(1) if m else ""

        m = re.search(r"<oc:score>(.*?)</oc:score>", block)
        entry["score"] = float(m.group(1)) if m else 0.0

        results.append(entry)

    return results


_SEARCH_XML_TEMPLATE = """\
<?xml version="1.0" encoding="UTF-8"?>
<oc:search-files xmlns:a="DAV:" xmlns:oc="http://owncloud.org/ns">
  <oc:search>
    <oc:pattern>{query}</oc:pattern>
    <oc:limit>{limit}</oc:limit>
    <oc:offset>{offset}</oc:offset>
  </oc:search>
</oc:search-files>"""


_SEARCH_PAGE_SIZE = 200
_SEARCH_MAX_PAGES = 5


def _search_index(kql: str, want: int) -> list[dict] | None:
    """Run paginated REPORT search requests for a KQL query.

    Returns parsed raw results (stopping once ``want`` are collected, the
    server is exhausted, or the page budget is reached). Returns None — so the
    caller can fall back — only when the *first* request fails or the index is
    unavailable; a later-page failure yields what was collected so far.
    """
    if not kql:
        return None

    collected: list[dict] = []
    for page in range(_SEARCH_MAX_PAGES):
        body = _SEARCH_XML_TEMPLATE.format(
            query=_xml_escape(kql),
            limit=_SEARCH_PAGE_SIZE,
            offset=page * _SEARCH_PAGE_SIZE,
        )
        try:
            resp = httpx.request(
                "REPORT",
                _get_search_url(),
                auth=(settings.opencloud_username, settings.opencloud_password),
                headers={"Content-Type": "application/xml"},
                content=body,
                follow_redirects=True,
                timeout=30,
            )
        except Exception:
            return collected or None
        if resp.status_code != 207:
            return collected or None

        batch = _parse_search_response(resp.text)
        collected.extend(batch)
        if len(batch) < _SEARCH_PAGE_SIZE or len(collected) >= want:
            break
    return collected


def _glob_via_search(
    pattern: str,
    cutoff: datetime | None,
    file_type: str,
    limit: int,
) -> list[dict] | None:
    """Resolve a glob via the server-side search index, post-filtering for exact
    glob semantics. Returns None (so the caller falls back to the walk) when the
    index is unavailable or the request fails.
    """
    # A braced pattern expands to several name filters; query each, dedupe by
    # path, then post-filter with the full (braced) matcher.
    name_filters = {_glob_search_name(p) for p in _expand_braces(pattern)}
    raw: list[dict] = []
    seen: set[str] = set()
    index_ok = False
    for nf in name_filters:
        batch = _search_index(_build_kql("", nf, "", "", ""), want=limit)
        if batch is None:
            continue
        index_ok = True
        for r in batch:
            path = r.get("path", "")
            if path not in seen:
                seen.add(path)
                raw.append(r)
    if not index_ok:
        return None

    results: list[dict] = []
    for r in raw:
        item_path = r.get("path", "")
        item_type = r.get("type", "file")
        if file_type != "all" and item_type != file_type:
            continue
        if not _glob_match(item_path.rstrip("/"), pattern):
            continue
        if cutoff:
            mod_str = r.get("modified", "")
            if mod_str:
                try:
                    mod_dt = parsedate_to_datetime(mod_str)
                    if mod_dt.tzinfo is None:
                        mod_dt = mod_dt.replace(tzinfo=timezone.utc)
                    if mod_dt < cutoff:
                        continue
                except (ValueError, TypeError):
                    pass
        results.append({
            "name": r.get("name", "") or posixpath.basename(item_path.rstrip("/")),
            "path": item_path,
            "size": r.get("size", 0),
            "modified": r.get("modified", ""),
            "type": item_type,
        })
    return results


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def search(
    pattern: Annotated[str, "Keywords to find files by name or content, like a web search (NOT regex). Each word matches the filename or the file's text; words are OR'd and results are ranked by relevance (files matching more words rank higher), e.g. 'quarterly budget'"] = "",
    filename: Annotated[str, "Hard filename filter (glob syntax), e.g. '*.pdf', 'report*', 'README' — restricts results to names matching this, then ranks by pattern"] = "",
    path: Annotated[str, "Optional path prefix to scope results, e.g. '/Documents' — client-side filter"] = "",
    mediatype: Annotated[str, "Filter: document, spreadsheet, presentation, pdf, image, video, audio, folder, archive"] = "",
    modified_after: Annotated[str, "Only files modified on or after this date (ISO 8601), e.g. '2026-01-01'"] = "",
    modified_before: Annotated[str, "Only files modified on or before this date (ISO 8601), e.g. '2026-12-31'"] = "",
    limit: Annotated[int, "Max results (default 50, max 200)"] = 50,
    offset: Annotated[int, "Pagination offset — skip first N results"] = 0,
) -> list[dict] | str:
    """Search files via OpenCloud's server-side index, like a web search box: each keyword matches the file name or its text content, and results are ranked by relevance (files matching more keywords rank higher). Keywords are OR'd, so listing extra words broadens rather than empties the results. This is keyword/full-text search, NOT line-by-line regex like a code grep — use the glob tool for exact path/filename pattern discovery. Content matching requires the server's Tika extractor; name matching always works. At least one search param required (path alone is not sufficient)."""
    try:
        if not any([pattern, filename, mediatype, modified_after, modified_before]):
            return format_error(
                "search",
                "At least one search parameter (pattern, filename, mediatype, modified_after, modified_before) is required.",
            )

        kql = _build_kql(pattern, filename, mediatype, modified_after, modified_before)
        limit = min(max(limit, 1), 200)
        offset = max(offset, 0)

        body = _SEARCH_XML_TEMPLATE.format(
            query=_xml_escape(kql),
            limit=limit,
            offset=offset,
        )

        resp = httpx.request(
            "REPORT",
            _get_search_url(),
            auth=(settings.opencloud_username, settings.opencloud_password),
            headers={"Content-Type": "application/xml"},
            content=body,
            follow_redirects=True,
            timeout=30,
        )

        if resp.status_code == 401:
            return format_error("search", "Authentication failed. Check credentials.")
        if resp.status_code == 501:
            return format_error(
                "search",
                "Server-side search is not available. The search index may not be configured.",
            )
        if resp.status_code != 207:
            return format_error("search", f"Unexpected response: HTTP {resp.status_code}")

        results = _parse_search_response(resp.text)
        if path:
            results = [r for r in results if r.get("path", "").startswith(path)]
        results.sort(key=lambda r: r.get("score", 0), reverse=True)

        return results
    except Exception as e:
        return format_error("search", str(e))
