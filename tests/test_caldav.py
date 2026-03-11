"""Tests for CalDAV tools — uses mocks for the caldav client."""

from datetime import datetime
from unittest.mock import MagicMock, patch

import pytest
import vobject
from vobject.icalendar import utc

from src.caldav_server import (
    _parse_dt,
    complete_todo,
    create_event,
    create_todo,
    delete_event,
    find_events,
    find_todos,
    list_calendars,
    update_event,
    update_todo,
)


def _make_event_data(summary="Test Event", uid="test-uid-123", include_timestamps=True):
    cal = vobject.iCalendar()
    vevent = cal.add("vevent")
    vevent.add("summary").value = summary
    vevent.add("uid").value = uid
    vevent.add("dtstart").value = datetime(2026, 3, 5, 10, 0, tzinfo=utc)
    vevent.add("dtend").value = datetime(2026, 3, 5, 11, 0, tzinfo=utc)
    if include_timestamps:
        vevent.add("created").value = datetime(2026, 3, 4, 9, 0, tzinfo=utc)
        vevent.add("last-modified").value = datetime(2026, 3, 4, 10, 0, tzinfo=utc)
        vevent.add("dtstamp").value = datetime(2026, 3, 4, 9, 0, tzinfo=utc)
    return cal.serialize()


def _make_todo_data(summary="Test Todo", uid="todo-uid-123", include_timestamps=True, due=None, completed=None):
    cal = vobject.iCalendar()
    vtodo = cal.add("vtodo")
    vtodo.add("summary").value = summary
    vtodo.add("uid").value = uid
    vtodo.add("status").value = "COMPLETED" if completed else "NEEDS-ACTION"
    if due:
        vtodo.add("due").value = due
    if completed:
        vtodo.add("completed").value = completed
    if include_timestamps:
        vtodo.add("created").value = datetime(2026, 3, 4, 9, 0, tzinfo=utc)
        vtodo.add("last-modified").value = datetime(2026, 3, 4, 10, 0, tzinfo=utc)
        vtodo.add("dtstamp").value = datetime(2026, 3, 4, 9, 0, tzinfo=utc)
    return cal.serialize()


@pytest.fixture
def mock_principal():
    principal = MagicMock()
    calendar = MagicMock()
    calendar.get_display_name.return_value = "Personal"
    calendar.url = "https://dav.example.com/user/personal/"
    principal.calendars.return_value = [calendar]
    with patch("src.caldav_server._get_principal", return_value=principal):
        yield principal, calendar


class TestListCalendars:
    def test_returns_calendars(self, mock_principal):
        result = list_calendars()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "Personal"


class TestFindEvents:
    def test_returns_events_by_date_range(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data()
        calendar.search.return_value = [mock_event]

        result = find_events("Personal", start="2026-03-01", end="2026-03-31")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["summary"] == "Test Event"
        assert result[0]["uid"] == "test-uid-123"

    def test_calendar_not_found(self, mock_principal):
        result = find_events("Nonexistent", start="2026-03-01", end="2026-03-31")
        assert "Error" in result
        assert "not found" in result

    def test_searches_by_text(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data(summary="Team Standup")
        calendar.events.return_value = [mock_event]

        result = find_events(query="standup")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["summary"] == "Team Standup"

    def test_text_filter_excludes_non_matches(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data(summary="Team Standup")
        calendar.events.return_value = [mock_event]

        result = find_events(query="lunch")
        assert len(result) == 0

    def test_event_includes_timestamps(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data()
        calendar.search.return_value = [mock_event]

        result = find_events("Personal", start="2026-03-01", end="2026-03-31")
        assert result[0]["created"] is not None
        assert result[0]["last_modified"] is not None
        assert result[0]["dtstamp"] is not None
        assert "2026-03-04" in result[0]["created"]

    def test_event_without_timestamps_returns_none(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data(include_timestamps=False)
        calendar.search.return_value = [mock_event]

        result = find_events("Personal", start="2026-03-01", end="2026-03-31")
        assert result[0]["created"] is None
        assert result[0]["last_modified"] is None
        # dtstamp is auto-set by vobject (RFC 5545 requires it), so it's never None
        assert result[0]["dtstamp"] is not None


class TestCreateEvent:
    def test_creates_event(self, mock_principal):
        _, calendar = mock_principal
        mock_saved = MagicMock()
        mock_saved.url = "https://dav.example.com/event/123"
        calendar.save_event.return_value = mock_saved

        result = create_event(
            "Personal", "New Meeting", "2026-03-10T14:00", "2026-03-10T15:00"
        )
        assert isinstance(result, dict)
        assert "uid" in result
        assert "url" in result
        calendar.save_event.assert_called_once()

    def test_create_event_sets_timestamps(self, mock_principal):
        _, calendar = mock_principal
        mock_saved = MagicMock()
        mock_saved.url = "https://dav.example.com/event/123"
        calendar.save_event.return_value = mock_saved

        create_event("Personal", "New Meeting", "2026-03-10T14:00", "2026-03-10T15:00")

        ical_data = calendar.save_event.call_args[0][0]
        assert "CREATED:" in ical_data
        assert "DTSTAMP:" in ical_data


class TestUpdateEvent:
    def test_updates_summary(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data()
        calendar.event_by_uid.return_value = mock_event

        result = update_event("Personal", "test-uid-123", {"summary": "Updated"})
        assert "Updated event" in result
        mock_event.save.assert_called_once()

    def test_update_event_sets_last_modified(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data()
        calendar.event_by_uid.return_value = mock_event

        update_event("Personal", "test-uid-123", {"summary": "Updated"})

        saved_data = mock_event.data
        assert "LAST-MODIFIED:" in saved_data


class TestDeleteEvent:
    def test_deletes_event(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        calendar.event_by_uid.return_value = mock_event

        result = delete_event("Personal", "test-uid-123")
        assert "Deleted event" in result
        mock_event.delete.assert_called_once()


class TestFindTodos:
    def test_returns_todos(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["summary"] == "Test Todo"

    def test_todo_includes_timestamps(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal")
        assert result[0]["created"] is not None
        assert result[0]["last_modified"] is not None
        assert result[0]["dtstamp"] is not None
        assert result[0]["completed"] is None

    def test_searches_all_calendars_when_empty(self, mock_principal):
        principal, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todos.return_value = [mock_todo]

        result = find_todos()
        assert isinstance(result, list)
        assert len(result) == 1
        principal.calendars.assert_called()

    def test_filters_by_query_text(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data(summary="Buy groceries")
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal", query="groceries")
        assert len(result) == 1
        assert result[0]["summary"] == "Buy groceries"

    def test_query_excludes_non_matches(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data(summary="Buy groceries")
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal", query="laundry")
        assert len(result) == 0

    def test_filters_by_due_date_in_range(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data(due=datetime(2026, 3, 10, 12, 0, tzinfo=utc))
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal", after="2026-03-09", before="2026-03-11")
        assert len(result) == 1

    def test_filters_by_due_date_out_of_range(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data(due=datetime(2026, 3, 10, 12, 0, tzinfo=utc))
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal", after="2026-03-15", before="2026-03-20")
        assert len(result) == 0

    def test_matches_by_created_when_no_due(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        # include_timestamps=True sets created to 2026-03-04T09:00Z, no due
        mock_todo.data = _make_todo_data()
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal", after="2026-03-01", before="2026-03-05")
        assert len(result) == 1

    def test_excludes_todo_with_no_dates_when_date_filter_active(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data(include_timestamps=False)
        calendar.todos.return_value = [mock_todo]

        result = find_todos(calendar="Personal", after="2026-03-01", before="2026-03-31")
        assert len(result) == 0

    def test_respects_limit(self, mock_principal):
        _, calendar = mock_principal
        todos = []
        for i in range(5):
            t = MagicMock()
            t.data = _make_todo_data(summary=f"Todo {i}", uid=f"uid-{i}")
            todos.append(t)
        calendar.todos.return_value = todos

        result = find_todos(calendar="Personal", limit=3)
        assert len(result) == 3


class TestCreateTodo:
    def test_creates_todo(self, mock_principal):
        _, calendar = mock_principal

        result = create_todo("Personal", "Buy groceries")
        assert isinstance(result, dict)
        assert "uid" in result
        calendar.save_todo.assert_called_once()

    def test_create_todo_sets_timestamps(self, mock_principal):
        _, calendar = mock_principal

        create_todo("Personal", "Buy groceries")

        ical_data = calendar.save_todo.call_args[0][0]
        assert "CREATED:" in ical_data
        assert "DTSTAMP:" in ical_data


class TestUpdateTodo:
    def test_updates_summary(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todo_by_uid.return_value = mock_todo

        result = update_todo("Personal", "todo-uid-123", {"summary": "Updated Todo"})
        assert "Updated todo" in result
        mock_todo.save.assert_called_once()

    def test_update_todo_sets_last_modified(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todo_by_uid.return_value = mock_todo

        update_todo("Personal", "todo-uid-123", {"summary": "Updated Todo"})

        saved_data = mock_todo.data
        assert "LAST-MODIFIED:" in saved_data


class TestCompleteTodo:
    def test_completes_todo(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todo_by_uid.return_value = mock_todo

        result = complete_todo("Personal", "todo-uid-123")
        assert "Completed todo" in result
        mock_todo.save.assert_called_once()


class TestParseDt:
    def test_naive_gets_default_tz(self):
        dt = _parse_dt("2026-03-05T14:00")
        assert dt.tzinfo is not None

    def test_utc_offset_normalized_to_vobject_utc(self):
        dt = _parse_dt("2026-03-05T14:00:00+00:00")
        assert dt.tzinfo is utc

    def test_non_utc_offset_preserved(self):
        dt = _parse_dt("2026-03-05T14:00:00-05:00")
        assert dt.utcoffset().total_seconds() == -5 * 3600
