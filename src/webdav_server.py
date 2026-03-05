"""WebDAV tools for OpenCloud file management. 10 tools."""

import base64
import posixpath
import tempfile
from typing import Annotated

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
def list_files(
    path: Annotated[str, "Directory path to list, e.g. '/' or '/Documents'"] = "/",
) -> list[dict] | str:
    """List files and directories at the given path."""
    try:
        path = sanitize_path(path)
        client = _get_client()
        items = client.ls(path, detail=True)
        results = []
        for item in items:
            # Skip the directory itself (webdav4 includes it)
            item_path = item.get("name", "")
            if item_path.rstrip("/") == path.rstrip("/"):
                continue
            results.append({
                "name": posixpath.basename(item_path.rstrip("/")),
                "path": item_path,
                "size": item.get("content_length", 0),
                "modified": item.get("modified", ""),
                "type": "directory" if item.get("type") == "directory" else "file",
            })
        return results
    except ValueError as e:
        return format_error("list_files", str(e))
    except Exception as e:
        return format_error("list_files", str(e))


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


@webdav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def search_files(
    query: Annotated[str, "Search query — substring match or glob pattern (*, ?)"],
    path: Annotated[str, "Directory to search in"] = "/",
) -> list[dict] | str:
    """Search for files by name. Case-insensitive substring or glob match. Max 50 results."""
    try:
        path = sanitize_path(path)
        client = _get_client()
        results = []

        def _walk(dir_path: str) -> None:
            if len(results) >= 50:
                return
            try:
                items = client.ls(dir_path, detail=True)
            except Exception:
                return
            for item in items:
                if len(results) >= 50:
                    return
                item_path = item.get("name", "")
                if item_path.rstrip("/") == dir_path.rstrip("/"):
                    continue
                name = posixpath.basename(item_path.rstrip("/"))
                is_dir = item.get("type") == "directory"
                if matches_query(name, query):
                    results.append({
                        "name": name,
                        "path": item_path,
                        "size": item.get("content_length", 0),
                        "type": "directory" if is_dir else "file",
                    })
                if is_dir:
                    _walk(item_path)

        _walk(path)
        results.sort(key=lambda r: r["path"])

        if len(results) >= 50:
            results.append({"note": "Results truncated at 50 matches"})

        return results
    except ValueError as e:
        return format_error("search_files", str(e))
    except Exception as e:
        return format_error("search_files", str(e))
