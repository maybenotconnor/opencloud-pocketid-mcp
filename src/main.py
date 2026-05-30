"""OpenCloud MCP Server — ASGI entrypoint.

Composes WebDAV, CalDAV, and CardDAV sub-servers under a single
FastMCP root with PocketID OIDC authentication.
"""

from starlette.requests import Request
from starlette.responses import PlainTextResponse

from fastmcp import FastMCP

from src.auth import create_auth
from src.caldav_server import caldav_server
from src.carddav_server import carddav_server
from src.webdav_server import webdav_server

# Root server with OIDC auth
mcp = FastMCP(
    name="OpenCloud MCP",
    auth=create_auth(),
    instructions=(
        "OpenCloud MCP provides tools for managing files (WebDAV), "
        "calendars/todos (CalDAV), and contacts (CardDAV). "
        "Tools are prefixed: webdav_, caldav_, carddav_. "
        "Use webdav_glob for pattern-based file discovery (e.g. '/Documents/**/*.pdf'). "
        "Use webdav_search for full-text/keyword content search (Tika, relevance-ranked, NOT regex) with optional date filters. "
        "Use webdav_read_file to read files; images are returned as image content automatically. "
        "Use webdav_edit_file for targeted text edits; webdav_write_file for full rewrites. "
        "Use caldav_find_events and caldav_find_todos for calendar/task search. "
        "Use carddav_find_contacts for contact search. All text queries use AND logic across terms."
    ),
)

# Mount sub-servers with namespace prefixes
mcp.mount(webdav_server, namespace="webdav")
mcp.mount(caldav_server, namespace="caldav")
mcp.mount(carddav_server, namespace="carddav")


# Health check endpoint
@mcp.custom_route("/health", methods=["GET"])
async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


# Create ASGI app for uvicorn
app = mcp.http_app(path="/mcp")
