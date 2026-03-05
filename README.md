# OpenCloud MCP Server

A self-hosted [Model Context Protocol](https://modelcontextprotocol.io) server that connects Claude to your OpenCloud files, Radicale calendars, and Radicale contacts. Authenticated via [PocketID](https://pocket-id.org) passkeys. Runs in a single Docker container.

**27 tools** across 3 namespaced servers:

- `webdav_` (10 tools) -- File management via OpenCloud WebDAV
- `caldav_` (10 tools) -- Calendar and todo management via Radicale CalDAV
- `carddav_` (7 tools) -- Contact management via Radicale CardDAV

Built with [FastMCP](https://gofastmcp.com) v3, Python 3.11, and [uv](https://docs.astral.sh/uv/).

---

## Prerequisites

You need these services already running and accessible:

| Service | Purpose |
|---------|---------|
| **OpenCloud** | File storage with WebDAV enabled |
| **Radicale** | CalDAV/CardDAV server for calendars and contacts |
| **PocketID** | OIDC identity provider (passkey authentication) |
| **Reverse proxy** | TLS termination (Caddy, Traefik, nginx, etc.) |
| **Docker + Docker Compose** | On the host machine |

You also need a DNS record pointing to your server (e.g., `mcp.example.com`).

---

## Setup

### 1. Register a PocketID OIDC Client

1. Go to **PocketID Admin > Settings > OIDC Clients**
2. Create a new **confidential** client
3. Set the callback URL to: `https://mcp.example.com/auth/callback`
4. Enable **PKCE** with S256 challenge method
5. Request scopes: `openid profile email`
6. Save the **Client ID** and **Client Secret**

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` with your real values:

```bash
# --- PocketID OIDC Auth ---
POCKETID_CONFIG_URL=https://id.example.com/.well-known/openid-configuration
POCKETID_CLIENT_ID=<your-client-id>
POCKETID_CLIENT_SECRET=<your-client-secret>

# --- MCP Server ---
MCP_BASE_URL=https://mcp.example.com
MCP_PORT=8000

# --- Security ---
# Generate a random key:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
JWT_SIGNING_KEY=<random-secret>

# --- OpenCloud (WebDAV) ---
OPENCLOUD_WEBDAV_URL=https://cloud.example.com/remote.php/webdav
OPENCLOUD_USERNAME=<service-account-username>
OPENCLOUD_PASSWORD=<service-account-password>

# --- Radicale (CalDAV/CardDAV) ---
RADICALE_URL=https://dav.example.com
RADICALE_USERNAME=<service-account-username>
RADICALE_PASSWORD=<service-account-password>

# --- Optional ---
# DEFAULT_TIMEZONE=America/New_York
```

> **Note:** The Radicale service account must have access to all calendars and address books you want to expose. If using htpasswd auth, configure Radicale's rights file accordingly.

### 3. Configure Reverse Proxy

Route `mcp.example.com` to the container on port 8000. Example for **Caddy**:

```
mcp.example.com {
    reverse_proxy opencloud-mcp:8000
}
```

The container joins the `caddy_net` Docker network by default. Adjust `docker-compose.yml` if your proxy network has a different name.

### 4. Build and Run

```bash
docker compose up -d
```

Verify the health check:

```bash
curl https://mcp.example.com/health
# OK
```

### 5. Connect Claude

1. In [Claude.ai](https://claude.ai), go to **Settings > MCP Connectors**
2. Add a new connector with URL: `https://mcp.example.com/mcp`
3. Complete the PocketID passkey login when prompted
4. All 27 tools are now available in your conversations

This also works with **Claude Code** -- it supports remote MCP servers with OAuth natively.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `POCKETID_CONFIG_URL` | Yes | | OIDC discovery URL |
| `POCKETID_CLIENT_ID` | Yes | | From PocketID client registration |
| `POCKETID_CLIENT_SECRET` | Yes | | From PocketID client registration |
| `MCP_BASE_URL` | Yes | | Public URL of this server |
| `JWT_SIGNING_KEY` | Yes | | Secret for signing session JWTs |
| `OPENCLOUD_WEBDAV_URL` | Yes | | OpenCloud WebDAV endpoint |
| `OPENCLOUD_USERNAME` | Yes | | WebDAV service account username |
| `OPENCLOUD_PASSWORD` | Yes | | WebDAV service account password |
| `RADICALE_URL` | Yes | | Radicale server URL |
| `RADICALE_USERNAME` | Yes | | Radicale service account username |
| `RADICALE_PASSWORD` | Yes | | Radicale service account password |
| `MCP_PORT` | No | `8000` | Internal server port |
| `DEFAULT_TIMEZONE` | No | `America/New_York` | Timezone for naive datetime inputs |

---

## Tool Reference

### WebDAV -- File Management (10 tools)

| Tool | Description |
|------|-------------|
| `webdav_list_files` | List files and directories at a path |
| `webdav_read_file` | Read a text file (max 1MB, rejects binary) |
| `webdav_read_binary` | Read any file as base64 (max 5MB) |
| `webdav_write_file` | Write text to a file (auto-creates parent dirs) |
| `webdav_mkdir` | Create a directory (idempotent) |
| `webdav_delete` | Delete a file or directory |
| `webdav_move` | Move or rename a file/directory |
| `webdav_copy` | Copy a file or directory |
| `webdav_get_file_info` | Get metadata (size, modified, etag, content type) |
| `webdav_search_files` | Search filenames by substring or glob (max 50 results) |

### CalDAV -- Calendar and Todos (10 tools)

| Tool | Description |
|------|-------------|
| `caldav_list_calendars` | List all calendars |
| `caldav_get_events` | Get events in a date range (expands recurring) |
| `caldav_create_event` | Create a calendar event |
| `caldav_update_event` | Partial update of an event by UID |
| `caldav_delete_event` | Delete an event by UID |
| `caldav_search_events` | Search events by text (max 30 results) |
| `caldav_get_todos` | Get todos from a calendar |
| `caldav_create_todo` | Create a new todo/task |
| `caldav_update_todo` | Update a todo by UID |
| `caldav_complete_todo` | Mark a todo as completed with timestamp |

### CardDAV -- Contacts (7 tools)

| Tool | Description |
|------|-------------|
| `carddav_list_addressbooks` | List all address books |
| `carddav_get_contacts` | Get contacts (summary view, max 200) |
| `carddav_search_contacts` | Search by name, email, phone, org (max 30 results) |
| `carddav_get_contact` | Get full vCard details for a contact |
| `carddav_create_contact` | Create a new contact |
| `carddav_update_contact` | Update a contact (preserves PHOTO and other fields) |
| `carddav_delete_contact` | Delete a contact by UID |

---

## Project Structure

```
opencloud-mcp/
├── pyproject.toml          # Dependencies (uv/hatch)
├── uv.lock                 # Reproducible lockfile
├── .env.example            # Environment template
├── Dockerfile              # Multi-stage build (uv + Python 3.11)
├── docker-compose.yml      # Production deployment
├── src/
│   ├── main.py             # ASGI entrypoint, server composition
│   ├── auth.py             # OIDCProxy configuration for PocketID
│   ├── config.py           # Settings from environment variables
│   ├── webdav_server.py    # 10 WebDAV tools
│   ├── caldav_server.py    # 10 CalDAV tools
│   ├── carddav_server.py   # 7 CardDAV tools
│   └── utils.py            # Path sanitization, error formatting
└── tests/
    ├── test_utils.py
    ├── test_webdav.py
    ├── test_caldav.py
    └── test_carddav.py
```

---

## Development

### Install dependencies

```bash
uv sync --dev
```

### Run tests

```bash
uv run python -m pytest tests/ -v
```

### Run locally (without Docker)

Create a `.env` file with your credentials, then:

```bash
uv run uvicorn src.main:app --host 0.0.0.0 --port 8000
```

---

## Auth Flow

```
Claude.ai                    FastMCP OIDCProxy             PocketID
   |                              |                           |
   |--- GET /mcp --------------->|                           |
   |<-- 401 Unauthorized --------|                           |
   |                              |                           |
   |--- POST /register --------->|  (accepts any DCR)       |
   |<-- {client_id} -------------|                           |
   |                              |                           |
   |--- GET /authorize --------->|--- GET /authorize ------->|
   |   (+ PKCE challenge)        |   (+ new PKCE challenge)  |
   |                              |                           |
   |              [User authenticates with passkey]           |
   |                              |                           |
   |                              |<-- code + callback -------|
   |                              |--- POST /token ---------->|
   |                              |<-- PocketID tokens -------|
   |<-- redirect + new code ------|  (encrypts + stores)      |
   |                              |                           |
   |--- POST /token ------------>|  (validates PKCE)         |
   |<-- FastMCP JWT -------------|  (issues own JWT)         |
   |                              |                           |
   |=== MCP tool calls with Bearer JWT =====================>|
```

PocketID lacks Dynamic Client Registration (DCR). The OIDCProxy bridges this by accepting DCR requests from Claude.ai while using pre-registered credentials to authenticate with PocketID upstream.

---

## Security Notes

- The container runs as a **non-root user** (uid 1000)
- All file paths are **sanitized** -- `..` traversal and `~` expansion are rejected
- **No FUSE mounts** -- pure HTTP client, no SYS_ADMIN capabilities needed
- Backend credentials are service-account level -- **single-user deployment**
- JWT signing key should be stable across restarts (set in `.env`)

---

## License

Private -- Decanlys LLC
