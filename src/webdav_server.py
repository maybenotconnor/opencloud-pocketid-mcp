"""WebDAV tools for OpenCloud file management. 10 tools."""

import base64
import posixpath
import re
import tempfile
from datetime import datetime, timezone
from typing import Annotated
from urllib.parse import unquote

import httpx
from fastmcp import FastMCP
from webdav4.client import Client as WebDAVClient, ResourceAlreadyExists

from src.config import settings
from src.utils import format_error, matches_query, sanitize_path

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


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def find_files(
    path: Annotated[str, "Root directory to search, e.g. '/' or '/Documents'"] = "/",
    query: Annotated[str, "Name filter — substring or glob pattern (*, ?). Empty = all files"] = "",
    modified_after: Annotated[str, "ISO 8601 datetime — only files modified after this, e.g. '2026-03-04'"] = "",
    file_type: Annotated[str, "Filter by type: 'file', 'directory', or 'all'"] = "all",
    depth: Annotated[int, "Max recursion depth. 0 = unlimited, 1 = single directory only"] = 0,
    limit: Annotated[int, "Max results to return (default 100, max 500)"] = 100,
) -> list[dict] | str:
    """Find files and directories recursively with optional filters. Use depth=1 for single-directory listing."""
    try:
        path = sanitize_path(path)
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

                # Apply filters
                if file_type != "all" and item_type != file_type:
                    if is_dir:
                        _walk(item_path, current_depth + 1)
                    continue
                if query and not matches_query(name, query):
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

        _walk(path, 1)
        results.sort(key=lambda r: r["path"])

        if len(results) >= limit:
            results.append({"note": f"Results truncated at {limit} matches"})

        return results
    except ValueError as e:
        return format_error("find_files", str(e))
    except Exception as e:
        return format_error("find_files", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def read_file(
    path: Annotated[str, "Path to the file to read, e.g. '/Documents/notes.txt'"],
) -> str:
    """Read a text file's content (max 1MB). For binary files use read_binary."""
    try:
        path = sanitize_path(path)
        client = _get_client()

        info = client.info(path)
        size = info.get("content_length", 0)
        if size and size > 1_048_576:
            return format_error(
                "read_file",
                f"File is {size / 1_048_576:.1f}MB, exceeds 1MB limit. "
                "Use read_binary for large files.",
            )

        with tempfile.NamedTemporaryFile() as tmp:
            client.download_file(path, tmp.name)
            # Check for binary content
            with open(tmp.name, "rb") as f:
                chunk = f.read(8192)
                if b"\x00" in chunk:
                    return format_error(
                        "read_file",
                        "File appears to be binary. Use read_binary instead.",
                    )
            with open(tmp.name, "r", encoding="utf-8") as f:
                return f.read()
    except ValueError as e:
        return format_error("read_file", str(e))
    except UnicodeDecodeError:
        return format_error(
            "read_file",
            "File is not valid UTF-8 text. Use read_binary instead.",
        )
    except Exception as e:
        return format_error("read_file", str(e))


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def read_binary(
    path: Annotated[str, "Path to the file to read as base64"],
) -> str:
    """Read a binary file as base64 (max 5MB). Warning: base64 output is large and consumes context window."""
    try:
        path = sanitize_path(path)
        client = _get_client()

        info = client.info(path)
        size = info.get("content_length", 0)
        if size and size > 5_242_880:
            return format_error(
                "read_binary",
                f"File is {size / 1_048_576:.1f}MB, exceeds 5MB limit.",
            )

        with tempfile.NamedTemporaryFile() as tmp:
            client.download_file(path, tmp.name)
            with open(tmp.name, "rb") as f:
                data = f.read()
            return base64.b64encode(data).decode("ascii")
    except ValueError as e:
        return format_error("read_binary", str(e))
    except Exception as e:
        return format_error("read_binary", str(e))


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

def _build_kql(content: str, name: str, mediatype: str, mtime: str) -> str:
    """Build a KQL query string from structured parameters."""
    parts: list[str] = []
    if content:
        parts.append(f"content:{content}")
    if name:
        parts.append(f"name:{name}")
    if mediatype:
        parts.append(f"mediatype:{mediatype}")
    if mtime:
        parts.append(f"mtime:{mtime}")
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

        # Name
        m = re.search(r"<oc:name>(.*?)</oc:name>", block)
        entry["name"] = m.group(1) if m else ""

        # Href → cleaned path
        m = re.search(r"<(?:\w+:)?href>(.*?)</(?:\w+:)?href>", block)
        if m:
            raw_path = unquote(m.group(1))
            # Strip /remote.php/dav/spaces/{uuid}/ prefix
            cleaned = re.sub(r"^/remote\.php/dav/spaces/[^/]+", "", raw_path)
            entry["path"] = cleaned or "/"
        else:
            entry["path"] = ""

        # Type: directory if <d:collection/> present
        if re.search(r"<(?:\w+:)?collection\s*/?>", block):
            entry["type"] = "directory"
        else:
            entry["type"] = "file"

        # Size
        m = re.search(r"<(?:\w+:)?getcontentlength>(.*?)</(?:\w+:)?getcontentlength>", block)
        entry["size"] = int(m.group(1)) if m else 0

        # Modified
        m = re.search(r"<(?:\w+:)?getlastmodified>(.*?)</(?:\w+:)?getlastmodified>", block)
        entry["modified"] = m.group(1) if m else ""

        # Content type
        m = re.search(r"<(?:\w+:)?getcontenttype>(.*?)</(?:\w+:)?getcontenttype>", block)
        entry["content_type"] = m.group(1) if m else ""

        # Score
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
def search_files(
    content: Annotated[str, "Full-text content search inside files (Tika), e.g. 'quarterly report'"] = "",
    name: Annotated[str, "Filename pattern, e.g. '*.pdf', 'report*', 'README'"] = "",
    mediatype: Annotated[str, "Filter: document, spreadsheet, presentation, pdf, image, video, audio, folder, archive"] = "",
    mtime: Annotated[str, "Modified: 'today', 'last 7 days', 'last 30 days', 'this year', 'last year'"] = "",
    limit: Annotated[int, "Max results (default 50, max 200)"] = 50,
    offset: Annotated[int, "Pagination offset — skip first N results"] = 0,
) -> list[dict] | str:
    """Search files using OpenCloud's server-side search index. Supports full-text content search (Tika).
    Faster than find_files for large file trees. All params combine with AND logic.
    At least one search param (content, name, mediatype, mtime) is required."""
    try:
        # Validate at least one search param
        if not any([content, name, mediatype, mtime]):
            return format_error(
                "search_files",
                "At least one search parameter (content, name, mediatype, mtime) is required.",
            )

        kql = _build_kql(content, name, mediatype, mtime)
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
            return format_error("search_files", "Authentication failed. Check credentials.")
        if resp.status_code == 501:
            return format_error(
                "search_files",
                "Server-side search is not available. The search index may not be configured.",
            )
        if resp.status_code != 207:
            return format_error("search_files", f"Unexpected response: HTTP {resp.status_code}")

        results = _parse_search_response(resp.text)
        # Sort by relevance score descending
        results.sort(key=lambda r: r.get("score", 0), reverse=True)

        return results
    except Exception as e:
        return format_error("search_files", str(e))


