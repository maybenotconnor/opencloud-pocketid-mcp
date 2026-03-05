"""Tests for CardDAV tools — uses mocks for the caldav/CardDAV client."""

from unittest.mock import MagicMock, patch

import pytest
import vobject

from src.carddav_server import (
    create_contact,
    delete_contact,
    get_contact,
    get_contacts,
    list_addressbooks,
    search_contacts,
    update_contact,
)


def _make_vcard(name="John Doe", uid="contact-uid-123", email="john@example.com"):
    card = vobject.vCard()
    card.add("fn").value = name
    card.add("n").value = vobject.vcard.Name(family="Doe", given="John")
    card.add("uid").value = uid
    card.add("email").value = email
    card.add("tel").value = "+1-555-0100"
    card.add("org").value = ["Acme Corp"]
    return card.serialize()


@pytest.fixture
def mock_principal():
    principal = MagicMock()
    book = MagicMock()
    book.name = "Contacts"
    book.url = "https://dav.example.com/user/contacts/"

    # children() returns (url, resource_types, display_name) tuples
    addressbook_type = "{urn:ietf:params:xml:ns:carddav}addressbook"
    principal.children.return_value = [
        (book.url, [addressbook_type, "{DAV:}collection"], "Contacts"),
    ]

    with patch("src.carddav_server._get_principal", return_value=principal), \
         patch("src.carddav_server._client", new=MagicMock()), \
         patch("caldav.Calendar", return_value=book):
        yield principal, book


@pytest.fixture
def mock_vcards():
    """Provide mock _fetch_vcards returning test vCard data."""
    vcard_data = _make_vcard()
    mock_results = [("/contacts/contact-uid-123.vcf", vcard_data)]

    with patch("src.carddav_server._fetch_vcards", return_value=mock_results):
        yield mock_results


class TestListAddressbooks:
    def test_returns_books(self, mock_principal):
        result = list_addressbooks()
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "Contacts"


class TestGetContacts:
    def test_returns_contacts(self, mock_principal, mock_vcards):
        result = get_contacts("Contacts")
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["name"] == "John Doe"
        assert result[0]["email"] == "john@example.com"

    def test_respects_limit(self, mock_principal):
        vcards = [
            (f"/contacts/uid-{i}.vcf", _make_vcard(name=f"Person {i}", uid=f"uid-{i}"))
            for i in range(10)
        ]
        with patch("src.carddav_server._fetch_vcards", return_value=vcards):
            result = get_contacts("Contacts", limit=3)
            assert len(result) == 3


class TestSearchContacts:
    def test_finds_by_name(self, mock_principal, mock_vcards):
        result = search_contacts("john")
        assert isinstance(result, list)
        assert len(result) == 1

    def test_case_insensitive(self, mock_principal, mock_vcards):
        result = search_contacts("JOHN")
        assert len(result) == 1

    def test_no_match(self, mock_principal, mock_vcards):
        result = search_contacts("zzzzz")
        assert len(result) == 0


class TestGetContact:
    def test_finds_by_uid(self, mock_principal, mock_vcards):
        result = get_contact("Contacts", "contact-uid-123")
        assert isinstance(result, dict)
        assert result["name"] == "John Doe"

    def test_not_found(self, mock_principal, mock_vcards):
        result = get_contact("Contacts", "nonexistent")
        assert "Error" in result


class TestCreateContact:
    def test_creates_contact(self, mock_principal):
        _, book = mock_principal
        result = create_contact("Contacts", "Jane Smith", email="jane@example.com")
        assert isinstance(result, dict)
        assert "uid" in result
        book.save_event.assert_called_once()


class TestUpdateContact:
    def test_updates_name(self, mock_principal, mock_vcards):
        mock_obj = MagicMock()
        with patch("caldav.CalendarObjectResource", return_value=mock_obj):
            result = update_contact("Contacts", "contact-uid-123", {"name": "Jane Doe"})
            assert "Updated contact" in result
            mock_obj.save.assert_called_once()

    def test_not_found(self, mock_principal, mock_vcards):
        result = update_contact("Contacts", "nonexistent", {"name": "X"})
        assert "Error" in result


class TestDeleteContact:
    def test_deletes_contact(self, mock_principal, mock_vcards):
        mock_obj = MagicMock()
        with patch("caldav.CalendarObjectResource", return_value=mock_obj):
            result = delete_contact("Contacts", "contact-uid-123")
            assert "Deleted contact" in result
            mock_obj.delete.assert_called_once()

    def test_not_found(self, mock_principal, mock_vcards):
        with patch("caldav.CalendarObjectResource", return_value=MagicMock()):
            result = delete_contact("Contacts", "nonexistent")
            assert "Error" in result
