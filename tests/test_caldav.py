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
    get_todos,
    list_calendars,
    update_event,
    update_todo,
)


def _make_event_data(summary="Test Event", uid="test-uid-123"):
    cal = vobject.iCalendar()
    vevent = cal.add("vevent")
    vevent.add("summary").value = summary
    vevent.add("uid").value = uid
    vevent.add("dtstart").value = datetime(2026, 3, 5, 10, 0, tzinfo=utc)
    vevent.add("dtend").value = datetime(2026, 3, 5, 11, 0, tzinfo=utc)
    return cal.serialize()


def _make_todo_data(summary="Test Todo", uid="todo-uid-123"):
    cal = vobject.iCalendar()
    vtodo = cal.add("vtodo")
    vtodo.add("summary").value = summary
    vtodo.add("uid").value = uid
    vtodo.add("status").value = "NEEDS-ACTION"
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


class TestUpdateEvent:
    def test_updates_summary(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        mock_event.data = _make_event_data()
        calendar.event_by_uid.return_value = mock_event

        result = update_event("Personal", "test-uid-123", {"summary": "Updated"})
        assert "Updated event" in result
        mock_event.save.assert_called_once()


class TestDeleteEvent:
    def test_deletes_event(self, mock_principal):
        _, calendar = mock_principal
        mock_event = MagicMock()
        calendar.event_by_uid.return_value = mock_event

        result = delete_event("Personal", "test-uid-123")
        assert "Deleted event" in result
        mock_event.delete.assert_called_once()


class TestGetTodos:
    def test_returns_todos(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todos.return_value = [mock_todo]

        result = get_todos("Personal")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["summary"] == "Test Todo"


class TestCreateTodo:
    def test_creates_todo(self, mock_principal):
        _, calendar = mock_principal

        result = create_todo("Personal", "Buy groceries")
        assert isinstance(result, dict)
        assert "uid" in result
        calendar.save_todo.assert_called_once()


class TestUpdateTodo:
    def test_updates_summary(self, mock_principal):
        _, calendar = mock_principal
        mock_todo = MagicMock()
        mock_todo.data = _make_todo_data()
        calendar.todo_by_uid.return_value = mock_todo

        result = update_todo("Personal", "todo-uid-123", {"summary": "Updated Todo"})
        assert "Updated todo" in result
        mock_todo.save.assert_called_once()


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
