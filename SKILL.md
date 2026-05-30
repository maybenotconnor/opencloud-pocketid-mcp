# OpenCloud MCP — Agent Skill

Use this as connector instructions to teach Claude how to use the OpenCloud MCP tools effectively.

---

## Overview

OpenCloud MCP provides 25 tools across three services: **WebDAV** (files), **CalDAV** (calendars/todos), and **CardDAV** (contacts). All tools are prefixed by their service namespace: `webdav_`, `caldav_`, `carddav_`.

## Discovery Tools

Four flexible discovery tools share a consistent design: all parameters are optional, combine any filters you need. Additionally, `search` provides fast server-side indexed search with full-text content support.

### webdav_glob
Find files and directories by glob pattern. The pattern embeds both path and filename — use `**` for any depth.

| Use case | Parameters |
|---|---|
| List a single directory | `glob(pattern="/Documents/*", depth=1)` |
| Full recursive file tree | `glob(pattern="**/*")` |
| PDFs in a subtree | `glob(pattern="/Documents/**/*.pdf")` |
| Any report file | `glob(pattern="**/*report*")` |
| Files modified recently | `glob(pattern="**/*", modified_after="2026-03-04")` |
| Only directories | `glob(pattern="**/*", file_type="directory")` |
| Recent Python files | `glob(pattern="/Projects/**/*.py", modified_after="2026-03-01")` |
| Shallow listing | `glob(pattern="/Photos/*.jpg", depth=1)` |

### webdav_search
Server-side indexed search using OpenCloud's search engine, like a web search box. Each `pattern` keyword matches the file name or its text content (content requires Tika); keywords are OR'd and results are relevance-ranked, so files matching more keywords float to the top and over-listing words broadens rather than empties the results. Faster than the `glob` tool for large file trees. At least one param required (`path` alone is not sufficient). Note: this is keyword/full-text search, **not** a line-by-line regex like the built-in Grep tool. Use `filename` for a hard name filter (e.g. `*.pdf`), the `glob` tool for exact path-pattern discovery, and `path` to scope results to a directory.

| Use case | Parameters |
|---|---|
| Keyword search (single term) | `search(pattern="report")` |
| Keyword search (multi-word, ranked) | `search(pattern="quarterly budget")` |
| Restrict to a filename pattern | `search(filename="*.pdf")` |
| Scoped to a directory | `search(pattern="budget", path="/Finance")` |
| Find by media type | `search(mediatype="image")` |
| Documents modified after date | `search(mediatype="document", modified_after="2026-03-01")` |
| Combined filters | `search(pattern="budget", mediatype="spreadsheet")` |
| Date range | `search(pattern="report", modified_after="2026-01-01", modified_before="2026-12-31")` |
| Paginated results | `search(filename="*.txt", limit=20, offset=40)` |

> **When to use `search` vs `glob`:**
> - `search` — fast server-side indexed search, supports content search inside files, best for large trees
> - `glob` — client-side recursive walk, supports exact date filters and depth control, works without search index

### caldav_find_events
Find calendar events by date range and/or text search. Use `start`+`end` for date filtering, `query` for text, or both.

| Use case | Parameters |
|---|---|
| Events this week | `find_events(calendar="Personal", start="2026-03-03", end="2026-03-09")` |
| Search by text | `find_events(query="standup")` |
| Search within date range | `find_events(calendar="Work", start="2026-03-01", end="2026-03-31", query="review")` |
| All events across calendars | `find_events(start="2026-03-01", end="2026-04-01")` |

### caldav_find_todos
Find todos by date range, text search, and/or status. All params optional. Date range uses `start`/`end` (same as `find_events`).

| Use case | Parameters |
|---|---|
| All todos from a calendar | `find_todos(calendar="Personal")` |
| Include completed | `find_todos(calendar="Personal", include_completed=True)` |
| Search by text | `find_todos(query="groceries")` |
| Todos due this week | `find_todos(start="2026-03-03", end="2026-03-09")` |
| Overdue todos | `find_todos(end="2026-03-10")` |
| Combine: text + date range | `find_todos(calendar="Work", query="report", start="2026-03-01", end="2026-03-31")` |
| All todos across calendars | `find_todos()` |

### carddav_find_contacts
Find contacts with optional text search. Omit `query` to list all, provide it to search.

| Use case | Parameters |
|---|---|
| All contacts | `find_contacts(addressbook="Contacts")` |
| Search by name/email/org | `find_contacts(query="john")` |
| Search across all books | `find_contacts(query="acme")` |

## File Operations

- **Read text**: `webdav_read_file(path="/file.txt")` — max 1MB, UTF-8 text
- **Read image**: `webdav_read_file(path="/photo.png")` — images auto-detected, returned as image content
- **Read binary**: `webdav_read_file(path="/archive.zip", binary=True)` — returns base64, max 5MB
- **Edit text file**: `webdav_edit_file(path="/file.txt", old_str="...", new_str="...")` — exact-match replace; fails if not found or matches >1 time
- **Write file**: `webdav_write_file(path="/file.txt", content="...")` — creates parent dirs, overwrites
- **Create directory**: `webdav_mkdir(path="/new-folder")`
- **Get metadata**: `webdav_get_file_info(path="/file.txt")` — size, modified date, content type
- **Copy**: `webdav_copy(source="/a.txt", dest="/b.txt")`
- **Move/rename**: `webdav_move(source="/old.txt", dest="/new.txt")`
- **Delete**: `webdav_delete(path="/file.txt")` — works on files and directories

## Calendar Operations

- **List calendars**: `caldav_list_calendars()`
- **Create event**: `caldav_create_event(calendar="Personal", summary="Meeting", start="2026-03-10T14:00", end="2026-03-10T15:00")`
- **Update event**: `caldav_update_event(calendar="Personal", uid="...", updates={"summary": "New Title"})`
- **Delete event**: `caldav_delete_event(calendar="Personal", uid="...")`

## Todo Operations

- **Find todos**: `caldav_find_todos(calendar="Personal")` — add `include_completed=True` for all, supports date/text filters
- **Create todo**: `caldav_create_todo(calendar="Personal", summary="Buy groceries")`
- **Update todo**: `caldav_update_todo(calendar="Personal", uid="...", updates={"summary": "Updated"})`
- **Complete todo**: `caldav_complete_todo(calendar="Personal", uid="...")`

## Contact Operations

- **List address books**: `carddav_list_addressbooks()`
- **Get full contact detail**: `carddav_get_contact(addressbook="Contacts", uid="...")` — returns all fields
- **Create contact**: `carddav_create_contact(addressbook="Contacts", name="Jane Smith", email="jane@example.com")`
- **Update contact**: `carddav_update_contact(addressbook="Contacts", uid="...", updates={"email": "new@example.com"})`
- **Delete contact**: `carddav_delete_contact(addressbook="Contacts", uid="...")`

## Tips

- Always use discovery tools (`glob`, `search`, `find_events`, `find_todos`, `find_contacts`) before CRUD operations — get UIDs from results
- Calendar and addressbook names are case-insensitive (e.g., "personal" matches "Personal")
- Date parameters accept ISO 8601 format: `"2026-03-05"` or `"2026-03-05T14:00"`
- The `updates` parameter on update tools is a dict — only include fields you want to change
- Events return timestamp fields: `created`, `last_modified`, `dtstamp` (ISO 8601 or null)
- Todos return timestamp fields: `created`, `last_modified`, `dtstamp`, `completed` (ISO 8601 or null)
