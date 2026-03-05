"""CalDAV tools for Radicale calendar management. 10 tools."""

import uuid
from datetime import datetime
from typing import Annotated

import caldav
import vobject
from vobject.icalendar import utc
from zoneinfo import ZoneInfo

from fastmcp import FastMCP

from src.config import settings
from src.utils import format_error

caldav_server = FastMCP(name="CalDAV")

_client: caldav.DAVClient | None = None
_principal: caldav.Principal | None = None


def _get_principal() -> caldav.Principal:
    global _client, _principal
    if _principal is None:
        _client = caldav.DAVClient(
            url=settings.caldav_url,
            username=settings.opencloud_username,
            password=settings.opencloud_password,
        )
        _principal = _client.principal()
    return _principal


def _get_tz() -> ZoneInfo:
    return ZoneInfo(settings.default_timezone)


def _parse_dt(value: str) -> datetime:
    """Parse an ISO 8601 datetime string. Naive strings get the default timezone.

    Normalizes stdlib UTC to vobject's utc to avoid serialization errors.
    """
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_get_tz())
    elif dt.utcoffset() is not None and dt.utcoffset().total_seconds() == 0:
        # vobject can't serialize datetime.timezone.utc — use its own utc
        dt = dt.replace(tzinfo=utc)
    return dt


def _dt_to_str(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if hasattr(dt, "isoformat"):
        return dt.isoformat()
    return str(dt)


def _resolve_calendar(name: str) -> caldav.Calendar:
    """Resolve a calendar by display name or path."""
    principal = _get_principal()
    calendars = principal.calendars()
    # Try exact match on name
    for cal in calendars:
        if cal.get_display_name() and cal.get_display_name().lower() == name.lower():
            return cal
    # Try path match
    for cal in calendars:
        cal_url = str(cal.url)
        if name in cal_url or name.rstrip("/") == cal_url.rstrip("/"):
            return cal
    available = ", ".join(c.get_display_name() or str(c.url) for c in calendars)
    raise ValueError(f"Calendar '{name}' not found. Available: {available}")


def _event_to_dict(event: caldav.Event) -> dict:
    """Extract key fields from a calendar event."""
    try:
        cal = vobject.readOne(event.data)
        vevent = cal.vevent
        result = {
            "summary": str(vevent.summary.value) if hasattr(vevent, "summary") else "",
            "uid": str(vevent.uid.value) if hasattr(vevent, "uid") else "",
            "dtstart": _dt_to_str(vevent.dtstart.value) if hasattr(vevent, "dtstart") else None,
            "dtend": _dt_to_str(vevent.dtend.value) if hasattr(vevent, "dtend") else None,
            "location": str(vevent.location.value) if hasattr(vevent, "location") else "",
            "description": str(vevent.description.value) if hasattr(vevent, "description") else "",
            "is_recurring": hasattr(vevent, "rrule"),
        }
        return result
    except Exception:
        return {"uid": "", "summary": "(parse error)", "raw": event.data[:200]}


def _todo_to_dict(todo: caldav.Todo) -> dict:
    """Extract key fields from a VTODO."""
    try:
        cal = vobject.readOne(todo.data)
        vtodo = cal.vtodo
        return {
            "summary": str(vtodo.summary.value) if hasattr(vtodo, "summary") else "",
            "uid": str(vtodo.uid.value) if hasattr(vtodo, "uid") else "",
            "due": _dt_to_str(vtodo.due.value) if hasattr(vtodo, "due") else None,
            "status": str(vtodo.status.value) if hasattr(vtodo, "status") else "",
            "priority": int(vtodo.priority.value) if hasattr(vtodo, "priority") else None,
            "description": str(vtodo.description.value) if hasattr(vtodo, "description") else "",
        }
    except Exception:
        return {"uid": "", "summary": "(parse error)"}


# --- Tools ---


@caldav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def list_calendars() -> list[dict] | str:
    """List all available calendars."""
    try:
        principal = _get_principal()
        calendars = principal.calendars()
        return [
            {
                "name": cal.get_display_name() or "",
                "url": str(cal.url),
            }
            for cal in calendars
        ]
    except Exception as e:
        return format_error("list_calendars", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def get_events(
    calendar: Annotated[str, "Calendar name or path"],
    start: Annotated[str, "Start date/time (ISO 8601), e.g. '2026-03-01' or '2026-03-01T09:00'"],
    end: Annotated[str, "End date/time (ISO 8601)"],
) -> list[dict] | str:
    """Get events in a date range. Expands recurring events."""
    try:
        cal = _resolve_calendar(calendar)
        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)
        events = cal.search(start=start_dt, end=end_dt, expand=True)
        return [_event_to_dict(e) for e in events]
    except ValueError as e:
        return format_error("get_events", str(e))
    except Exception as e:
        return format_error("get_events", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def create_event(
    calendar: Annotated[str, "Calendar name or path"],
    summary: Annotated[str, "Event title"],
    start: Annotated[str, "Start date/time (ISO 8601)"],
    end: Annotated[str, "End date/time (ISO 8601)"],
    location: Annotated[str, "Event location"] = "",
    description: Annotated[str, "Event description"] = "",
) -> dict | str:
    """Create a new calendar event."""
    try:
        cal = _resolve_calendar(calendar)
        start_dt = _parse_dt(start)
        end_dt = _parse_dt(end)

        vcal = vobject.iCalendar()
        vevent = vcal.add("vevent")
        vevent.add("summary").value = summary
        vevent.add("dtstart").value = start_dt
        vevent.add("dtend").value = end_dt
        if location:
            vevent.add("location").value = location
        if description:
            vevent.add("description").value = description

        uid = str(uuid.uuid4())
        vevent.add("uid").value = uid

        event = cal.save_event(vcal.serialize())
        return {"uid": uid, "url": str(event.url)}
    except ValueError as e:
        return format_error("create_event", str(e))
    except Exception as e:
        return format_error("create_event", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def update_event(
    calendar: Annotated[str, "Calendar name or path"],
    uid: Annotated[str, "Event UID to update"],
    updates: Annotated[dict, "Fields to update: summary, start, end, location, description"],
) -> str:
    """Update an existing event. Only modifies the fields provided in updates."""
    try:
        cal = _resolve_calendar(calendar)
        event = cal.event_by_uid(uid)
        vcal = vobject.readOne(event.data)
        vevent = vcal.vevent

        if "summary" in updates:
            vevent.summary.value = updates["summary"]
        if "start" in updates:
            vevent.dtstart.value = _parse_dt(updates["start"])
        if "end" in updates:
            vevent.dtend.value = _parse_dt(updates["end"])
        if "location" in updates:
            if hasattr(vevent, "location"):
                vevent.location.value = updates["location"]
            else:
                vevent.add("location").value = updates["location"]
        if "description" in updates:
            if hasattr(vevent, "description"):
                vevent.description.value = updates["description"]
            else:
                vevent.add("description").value = updates["description"]

        event.data = vcal.serialize()
        event.save()
        return f"Updated event {uid}"
    except ValueError as e:
        return format_error("update_event", str(e))
    except Exception as e:
        return format_error("update_event", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def delete_event(
    calendar: Annotated[str, "Calendar name or path"],
    uid: Annotated[str, "Event UID to delete"],
) -> str:
    """Delete a calendar event by UID."""
    try:
        cal = _resolve_calendar(calendar)
        event = cal.event_by_uid(uid)
        event.delete()
        return f"Deleted event {uid}"
    except ValueError as e:
        return format_error("delete_event", str(e))
    except Exception as e:
        return format_error("delete_event", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def search_events(
    query: Annotated[str, "Search text to match against event summary and description"],
    calendar: Annotated[str, "Calendar name or path (searches all if omitted)"] = "",
) -> list[dict] | str:
    """Search events by text. Matches against summary and description. Max 30 results."""
    try:
        principal = _get_principal()
        calendars = (
            [_resolve_calendar(calendar)] if calendar else principal.calendars()
        )

        results = []
        query_lower = query.lower()

        for cal in calendars:
            try:
                events = cal.events()
            except Exception:
                continue
            for event in events:
                if len(results) >= 30:
                    break
                d = _event_to_dict(event)
                summary = d.get("summary", "").lower()
                desc = d.get("description", "").lower()
                if query_lower in summary or query_lower in desc:
                    results.append(d)

        return results
    except ValueError as e:
        return format_error("search_events", str(e))
    except Exception as e:
        return format_error("search_events", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def get_todos(
    calendar: Annotated[str, "Calendar name or path"],
    include_completed: Annotated[bool, "Include completed todos"] = False,
) -> list[dict] | str:
    """Get todos/tasks from a calendar."""
    try:
        cal = _resolve_calendar(calendar)
        todos = cal.todos(include_completed=include_completed)
        return [_todo_to_dict(t) for t in todos]
    except ValueError as e:
        return format_error("get_todos", str(e))
    except Exception as e:
        return format_error("get_todos", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def create_todo(
    calendar: Annotated[str, "Calendar name or path"],
    summary: Annotated[str, "Todo title"],
    due: Annotated[str, "Due date/time (ISO 8601)"] = "",
    priority: Annotated[int, "Priority (1=highest, 9=lowest)"] = 0,
    description: Annotated[str, "Todo description"] = "",
) -> dict | str:
    """Create a new todo/task."""
    try:
        cal = _resolve_calendar(calendar)

        vcal = vobject.iCalendar()
        vtodo = vcal.add("vtodo")
        vtodo.add("summary").value = summary

        uid = str(uuid.uuid4())
        vtodo.add("uid").value = uid

        if due:
            vtodo.add("due").value = _parse_dt(due)
        if priority:
            vtodo.add("priority").value = str(priority)
        if description:
            vtodo.add("description").value = description

        vtodo.add("status").value = "NEEDS-ACTION"

        cal.save_todo(vcal.serialize())
        return {"uid": uid}
    except ValueError as e:
        return format_error("create_todo", str(e))
    except Exception as e:
        return format_error("create_todo", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def update_todo(
    calendar: Annotated[str, "Calendar name or path"],
    uid: Annotated[str, "Todo UID to update"],
    updates: Annotated[dict, "Fields to update: summary, due, priority, description"],
) -> str:
    """Update an existing todo. Only modifies the fields provided."""
    try:
        cal = _resolve_calendar(calendar)
        todo = cal.todo_by_uid(uid)
        vcal = vobject.readOne(todo.data)
        vtodo = vcal.vtodo

        if "summary" in updates:
            vtodo.summary.value = updates["summary"]
        if "due" in updates:
            if hasattr(vtodo, "due"):
                vtodo.due.value = _parse_dt(updates["due"])
            else:
                vtodo.add("due").value = _parse_dt(updates["due"])
        if "priority" in updates:
            if hasattr(vtodo, "priority"):
                vtodo.priority.value = str(updates["priority"])
            else:
                vtodo.add("priority").value = str(updates["priority"])
        if "description" in updates:
            if hasattr(vtodo, "description"):
                vtodo.description.value = updates["description"]
            else:
                vtodo.add("description").value = updates["description"]

        todo.data = vcal.serialize()
        todo.save()
        return f"Updated todo {uid}"
    except ValueError as e:
        return format_error("update_todo", str(e))
    except Exception as e:
        return format_error("update_todo", str(e))


@caldav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def complete_todo(
    calendar: Annotated[str, "Calendar name or path"],
    uid: Annotated[str, "Todo UID to mark complete"],
) -> str:
    """Mark a todo as completed with a timestamp."""
    try:
        cal = _resolve_calendar(calendar)
        todo = cal.todo_by_uid(uid)
        vcal = vobject.readOne(todo.data)
        vtodo = vcal.vtodo

        if hasattr(vtodo, "status"):
            vtodo.status.value = "COMPLETED"
        else:
            vtodo.add("status").value = "COMPLETED"

        if hasattr(vtodo, "completed"):
            vtodo.completed.value = datetime.now(tz=utc)
        else:
            vtodo.add("completed").value = datetime.now(tz=utc)

        todo.data = vcal.serialize()
        todo.save()
        return f"Completed todo {uid}"
    except ValueError as e:
        return format_error("complete_todo", str(e))
    except Exception as e:
        return format_error("complete_todo", str(e))
