# OpenCloud MCP — Agent Skill

Use this as connector instructions to teach Claude how to use the OpenCloud MCP tools effectively.

---

## Overview

OpenCloud MCP provides 24 tools across three services: **WebDAV** (files), **CalDAV** (calendars/todos), and **CardDAV** (contacts). All tools are prefixed by their service namespace: `webdav_`, `caldav_`, `carddav_`.

## Discovery Tools — the `find_*` pattern

Three flexible discovery tools share a consistent design: all parameters are optional, combine any filters you need.

### webdav_find_files
Find files and directories with optional filters. All params optional.

| Use case | Parameters |
|---|---|
| List a single directory | `find_files(path="/Documents", depth=1)` |
| Full recursive file tree | `find_files(path="/")` |
| Find by name pattern | `find_files(query="*.pdf")` |
| Find by glob | `find_files(query="report*")` |
| Files modified recently | `find_files(modified_after="2026-03-04")` |
| Only directories | `find_files(file_type="directory")` |
| Combine: recent Python files | `find_files(path="/Projects", query="*.py", modified_after="2026-03-01")` |
| Shallow listing with filter | `find_files(path="/Photos", query="*.jpg", depth=1)` |

### caldav_find_events
Find calendar events by date range and/or text search. Use `start`+`end` for date filtering, `query` for text, or both.

| Use case | Parameters |
|---|---|
| Events this week | `find_events(calendar="Personal", start="2026-03-03", end="2026-03-09")` |
| Search by text | `find_events(query="standup")` |
| Search within date range | `find_events(calendar="Work", start="2026-03-01", end="2026-03-31", query="review")` |
| All events across calendars | `find_events(start="2026-03-01", end="2026-04-01")` |

### carddav_find_contacts
Find contacts with optional text search. Omit `query` to list all, provide it to search.

| Use case | Parameters |
|---|---|
| All contacts | `find_contacts(addressbook="Contacts")` |
| Search by name/email/org | `find_contacts(query="john")` |
| Search across all books | `find_contacts(query="acme")` |

## File Operations

- **Read text**: `webdav_read_file(path="/file.txt")` — max 1MB, UTF-8 only
- **Read binary**: `webdav_read_binary(path="/image.png")` — returns base64, max 5MB
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

- **Get todos**: `caldav_get_todos(calendar="Personal")` — add `include_completed=True` for all
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

- Always use `find_*` tools for discovery before CRUD operations — get UIDs from results
- Calendar and addressbook names are case-insensitive (e.g., "personal" matches "Personal")
- Date parameters accept ISO 8601 format: `"2026-03-05"` or `"2026-03-05T14:00"`
- The `updates` parameter on update tools is a dict — only include fields you want to change
- Events return timestamp fields: `created`, `last_modified`, `dtstamp` (ISO 8601 or null)
- Todos return timestamp fields: `created`, `last_modified`, `dtstamp`, `completed` (ISO 8601 or null)
