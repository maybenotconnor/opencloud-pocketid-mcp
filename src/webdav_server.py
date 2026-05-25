"""WebDAV tools for OpenCloud file management. 10 tools."""

import base64
import os
import posixpath
import re
import tempfile
from datetime import datetime, timezone
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
        if c in ("*", "?"):
            idx = i
            break
    prefix = pattern[:idx]
    last_slash = prefix.rfind("/")
    if last_slash <= 0:
        return "/"
    return prefix[:last_slash] or "/"


def _glob_match(item_path: str, pattern: str) -> bool:
    """Match a full item path against a glob pattern with ** support (case-insensitive).

    * matches any characters except /
    ** matches zero or more path segments (including their separators)
    Patterns without / are matched against the basename only.
    """
    item = item_path.lower().rstrip("/")
    pat = pattern.lower()

    if "/" not in pat:
        import fnmatch
        return fnmatch.fnmatch(posixpath.basename(item), pat)

    if not pat.startswith("/"):
        pat = "/" + pat

    # Build regex token by token
    regex_parts = ["^"]
    i = 0
    while i < len(pat):
        if pat[i:i + 3] == "**/":
            # Zero or more path segments followed by /
            regex_parts.append("(?:.+/)?")
            i += 3
        elif pat[i:i + 2] == "**":
            regex_parts.append(".*")
            i += 2
        elif pat[i] == "*":
            regex_parts.append("[^/]*")
            i += 1
        elif pat[i] == "?":
            regex_parts.append("[^/]")
            i += 1
        else:
            regex_parts.append(re.escape(pat[i]))
            i += 1
    regex_parts.append("$")
    return bool(re.match("".join(regex_parts), item))


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
    """Find files by path pattern. Pattern uses glob syntax — ** matches any depth. Examples: /Documents/**/*.pdf, *.txt, **/*report*. Use grep for full-text content search."""
    try:
        base_path = sanitize_path(_glob_base(pattern))
        client = _get_client()
        limit = min(max(limit, 1), 500)

        cutoff = None
        if modified_after:
            cutoff = datetime.fromisoformat(modified_after)
            if cutoff.tzinfo is None:
                cutoff = cutoff.replace(tzinfo=timezone.utc)

        results: list[dict] = []

        def _walk(dir_path: str, current_depth: int) -> None:
            if len(results) >= limit:
                return
            if depth > 0 and current_depth > depth:
                return
            try:
                items = client.ls(dir_path, detail=True)
            except Exception:
                return
            for item in items:
                if len(results) >= limit:
                    return
                item_path = item.get("name", "")
                if item_path.rstrip("/") == dir_path.rstrip("/"):
                    continue
                name = posixpath.basename(item_path.rstrip("/"))
                is_dir = item.get("type") == "directory"
                item_type = "directory" if is_dir else "file"

                if file_type != "all" and item_type != file_type:
                    if is_dir:
                        _walk(item_path, current_depth + 1)
                    continue

                if not _glob_match(item_path.rstrip("/"), pattern):
                    if is_dir:
                        _walk(item_path, current_depth + 1)
                    continue

                if cutoff:
                    mod_str = item.get("modified", "")
                    if mod_str:
                        try:
                            mod_dt = datetime.fromisoformat(str(mod_str))
                            if mod_dt.tzinfo is None:
                                mod_dt = mod_dt.replace(tzinfo=timezone.utc)
                            if mod_dt < cutoff:
                                if is_dir:
                                    _walk(item_path, current_depth + 1)
                                continue
                        except (ValueError, TypeError):
                            pass

                results.append({
                    "name": name,
                    "path": item_path,
                    "size": item.get("content_length", 0),
                    "modified": item.get("modified", ""),
                    "type": item_type,
                })

                if is_dir:
                    _walk(item_path, current_depth + 1)

        _walk(base_path, 1)
        results.sort(key=lambda r: r["path"])

        if len(results) >= limit:
            results.append({"note": f"Results truncated at {limit} matches"})

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
    old_string: Annotated[str, "Exact string to find — must appear exactly once in the file"],
    new_string: Annotated[str, "Replacement string"],
) -> str:
    """Make a targeted edit to a text file by replacing an exact string. Fails if old_string is not found or appears more than once (max 1MB)."""
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

            count = content.count(old_string)
            if count == 0:
                return format_error("edit_file", "old_string not found in file")
            if count > 1:
                return format_error(
                    "edit_file",
                    f"old_string found {count} times — provide more context to make it unique",
                )

            new_content = content.replace(old_string, new_string, 1)
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
    content: str,
    name: str,
    mediatype: str,
    modified_after: str,
    modified_before: str,
) -> str:
    """Build a KQL query string from structured parameters."""
    parts: list[str] = []
    if content:
        terms = content.split()
        if len(terms) == 1:
            parts.append(f"content:{terms[0]}")
        else:
            parts.append(" AND ".join(f"content:{t}" for t in terms))
    if name:
        parts.append(f"name:{name}")
    if mediatype:
        parts.append(f"mediatype:{mediatype}")
    if modified_after:
        parts.append(f"mtime>={modified_after}")
    if modified_before:
        parts.append(f"mtime<={modified_before}")
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


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def grep(
    content: Annotated[str, "Full-text content search (Tika) — multiple words are AND'd, e.g. 'quarterly budget'"] = "",
    name: Annotated[str, "Filename pattern, e.g. '*.pdf', 'report*', 'README'"] = "",
    mediatype: Annotated[str, "Filter: document, spreadsheet, presentation, pdf, image, video, audio, folder, archive"] = "",
    modified_after: Annotated[str, "Only files modified on or after this date (ISO 8601), e.g. '2026-01-01'"] = "",
    modified_before: Annotated[str, "Only files modified on or before this date (ISO 8601), e.g. '2026-12-31'"] = "",
    limit: Annotated[int, "Max results (default 50, max 200)"] = 50,
    offset: Annotated[int, "Pagination offset — skip first N results"] = 0,
) -> list[dict] | str:
    """Search files using OpenCloud's server-side search index (Tika). Content words are AND'd for precise results. Use glob for pattern-based file discovery. At least one search param required."""
    try:
        if not any([content, name, mediatype, modified_after, modified_before]):
            return format_error(
                "grep",
                "At least one search parameter (content, name, mediatype, modified_after, modified_before) is required.",
            )

        kql = _build_kql(content, name, mediatype, modified_after, modified_before)
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
            return format_error("grep", "Authentication failed. Check credentials.")
        if resp.status_code == 501:
            return format_error(
                "grep",
                "Server-side search is not available. The search index may not be configured.",
            )
        if resp.status_code != 207:
            return format_error("grep", f"Unexpected response: HTTP {resp.status_code}")

        results = _parse_search_response(resp.text)
        results.sort(key=lambda r: r.get("score", 0), reverse=True)

        return results
    except Exception as e:
        return format_error("grep", str(e))
