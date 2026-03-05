"""CardDAV tools for OpenCloud contact management. 7 tools."""

import re
import uuid
from typing import Annotated

import caldav
import httpx
import vobject

from fastmcp import FastMCP

from src.config import settings
from src.utils import format_error

carddav_server = FastMCP(name="CardDAV")

_client: caldav.DAVClient | None = None
_principal: caldav.Principal | None = None

_ADDRESSBOOK_TYPE = "{urn:ietf:params:xml:ns:carddav}addressbook"

_ADDRESSBOOK_QUERY = """<?xml version="1.0" encoding="UTF-8"?>
<C:addressbook-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:carddav">
  <D:prop>
    <D:getetag/>
    <C:address-data/>
  </D:prop>
</C:addressbook-query>"""


def _get_principal() -> caldav.Principal:
    global _client, _principal
    if _principal is None:
        _client = caldav.DAVClient(
            url=settings.carddav_url,
            username=settings.opencloud_username,
            password=settings.opencloud_password,
        )
        _principal = _client.principal()
    return _principal


def _list_addressbooks() -> list[tuple[str, str, caldav.Calendar]]:
    """Return (display_name, url, Calendar) for each address book collection.

    Uses principal.children() and filters for carddav:addressbook resource type.
    Falls back to principal.calendars() for standalone Radicale.
    """
    principal = _get_principal()
    results = []

    try:
        children = principal.children()
        for url, resource_types, display_name in children:
            if _ADDRESSBOOK_TYPE in resource_types:
                book = caldav.Calendar(client=_client, url=str(url))
                results.append((display_name or "", str(url), book))
    except Exception:
        pass

    # Fallback for standalone Radicale
    if not results:
        try:
            for col in principal.calendars():
                col_name = col.get_display_name() if hasattr(col, "get_display_name") else ""
                results.append((col_name, str(col.url), col))
        except Exception:
            pass

    return results


def _resolve_addressbook(name: str) -> caldav.Calendar:
    """Resolve an address book by display name or path."""
    books = _list_addressbooks()

    for display_name, url, book in books:
        if display_name and display_name.lower() == name.lower():
            return book
    for display_name, url, book in books:
        if name in url or name.rstrip("/") == url.rstrip("/"):
            return book

    available = ", ".join(dn or url for dn, url, _ in books)
    raise ValueError(f"Address book '{name}' not found. Available: {available}")


def _fetch_vcards(book: caldav.Calendar) -> list[tuple[str, str]]:
    """Fetch all vCards from an address book via REPORT.

    Returns [(href, vcard_data), ...].  Uses a single addressbook-query
    REPORT request which is far more efficient than loading each object
    individually.
    """
    resp = httpx.request(
        "REPORT",
        str(book.url),
        auth=(settings.opencloud_username, settings.opencloud_password),
        headers={"Depth": "1", "Content-Type": "application/xml"},
        content=_ADDRESSBOOK_QUERY,
        follow_redirects=True,
        timeout=30,
    )
    if resp.status_code != 207:
        raise RuntimeError(f"REPORT failed: {resp.status_code}")

    # Parse hrefs and vCard data from the multistatus XML
    results = []
    text = resp.text
    # Extract each <response> block
    for block in re.findall(r"<(?:\w+:)?response>(.*?)</(?:\w+:)?response>", text, re.DOTALL):
        href_match = re.search(r"<(?:\w+:)?href>(.*?)</(?:\w+:)?href>", block)
        href = href_match.group(1) if href_match else ""
        vcard_match = re.search(r"(BEGIN:VCARD.*?END:VCARD)", block, re.DOTALL)
        if vcard_match:
            results.append((href, vcard_match.group(1)))
    return results


def _strip_photo(vcard_data: str) -> str:
    """Remove PHOTO property from vCard data to avoid parse errors on large base64 blobs."""
    return re.sub(
        r"PHOTO[;:][^\r\n]*(?:\r?\n[ \t][^\r\n]*)*\r?\n",
        "",
        vcard_data,
    )


def _vcard_to_summary(vcard_data: str) -> dict:
    """Extract summary fields from a vCard."""
    try:
        card = vobject.readOne(_strip_photo(vcard_data))
        result = {
            "name": str(card.fn.value) if hasattr(card, "fn") else "",
            "uid": str(card.uid.value) if hasattr(card, "uid") else "",
        }
        if hasattr(card, "email"):
            result["email"] = str(card.email.value)
        if hasattr(card, "tel"):
            result["phone"] = str(card.tel.value)
        if hasattr(card, "org"):
            org_val = card.org.value
            result["org"] = org_val[0] if isinstance(org_val, list) else str(org_val)
        return result
    except Exception:
        return {"name": "(parse error)", "uid": ""}


def _vcard_to_full(vcard_data: str) -> dict:
    """Extract all structured fields from a vCard."""
    try:
        card = vobject.readOne(_strip_photo(vcard_data))
        result: dict = {
            "name": str(card.fn.value) if hasattr(card, "fn") else "",
            "uid": str(card.uid.value) if hasattr(card, "uid") else "",
        }

        emails = []
        for child in card.getChildren():
            if child.name.upper() == "EMAIL":
                entry = {"value": str(child.value)}
                if hasattr(child, "type_param"):
                    entry["type"] = child.type_param
                emails.append(entry)
        if emails:
            result["emails"] = emails

        phones = []
        for child in card.getChildren():
            if child.name.upper() == "TEL":
                entry = {"value": str(child.value)}
                if hasattr(child, "type_param"):
                    entry["type"] = child.type_param
                phones.append(entry)
        if phones:
            result["phones"] = phones

        if hasattr(card, "org"):
            org_val = card.org.value
            result["org"] = org_val[0] if isinstance(org_val, list) else str(org_val)
        if hasattr(card, "note"):
            result["notes"] = str(card.note.value)
        if hasattr(card, "adr"):
            result["address"] = str(card.adr.value)
        if hasattr(card, "bday"):
            result["birthday"] = str(card.bday.value)

        return result
    except Exception:
        return {"name": "(parse error)", "uid": "", "raw": vcard_data[:200]}


# --- Tools ---


@carddav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def list_addressbooks() -> list[dict] | str:
    """List all available address books."""
    try:
        books = _list_addressbooks()
        return [
            {"name": display_name, "url": url}
            for display_name, url, _ in books
        ]
    except Exception as e:
        return format_error("list_addressbooks", str(e))


@carddav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def get_contacts(
    addressbook: Annotated[str, "Address book name or path"],
    limit: Annotated[int, "Max contacts to return (default 50, max 200)"] = 50,
) -> list[dict] | str:
    """Get contacts from an address book (summary view). Sorted alphabetically."""
    try:
        limit = min(max(limit, 1), 200)
        book = _resolve_addressbook(addressbook)
        vcards = _fetch_vcards(book)
        results = [_vcard_to_summary(data) for _, data in vcards]
        results.sort(key=lambda c: c.get("name", ""))
        return results[:limit]
    except ValueError as e:
        return format_error("get_contacts", str(e))
    except Exception as e:
        return format_error("get_contacts", str(e))


@carddav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def search_contacts(
    query: Annotated[str, "Search text to match against name, email, phone, and org"],
    addressbook: Annotated[str, "Address book name or path (searches all if omitted)"] = "",
) -> list[dict] | str:
    """Search contacts by text. Case-insensitive. Max 30 results."""
    try:
        if addressbook:
            books = [_resolve_addressbook(addressbook)]
        else:
            books = [book for _, _, book in _list_addressbooks()]

        results = []
        query_lower = query.lower()

        for book in books:
            try:
                vcards = _fetch_vcards(book)
            except Exception:
                continue
            for _, data in vcards:
                if len(results) >= 30:
                    break
                summary = _vcard_to_summary(data)
                searchable = " ".join(
                    str(v) for v in summary.values() if isinstance(v, str)
                ).lower()
                if query_lower in searchable:
                    results.append(summary)

        return results
    except ValueError as e:
        return format_error("search_contacts", str(e))
    except Exception as e:
        return format_error("search_contacts", str(e))


@carddav_server.tool(
    annotations={
        "readOnlyHint": True,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def get_contact(
    addressbook: Annotated[str, "Address book name or path"],
    uid: Annotated[str, "Contact UID"],
) -> dict | str:
    """Get full vCard details for a single contact including multi-value fields."""
    try:
        book = _resolve_addressbook(addressbook)
        for _, data in _fetch_vcards(book):
            try:
                card = vobject.readOne(_strip_photo(data))
                if hasattr(card, "uid") and str(card.uid.value) == uid:
                    return _vcard_to_full(data)
            except Exception:
                continue
        return format_error("get_contact", f"Contact with UID '{uid}' not found")
    except ValueError as e:
        return format_error("get_contact", str(e))
    except Exception as e:
        return format_error("get_contact", str(e))


@carddav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "openWorldHint": True,
    }
)
def create_contact(
    addressbook: Annotated[str, "Address book name or path"],
    name: Annotated[str, "Full name"],
    email: Annotated[str, "Email address"] = "",
    phone: Annotated[str, "Phone number"] = "",
    org: Annotated[str, "Organization"] = "",
    notes: Annotated[str, "Notes"] = "",
) -> dict | str:
    """Create a new contact."""
    try:
        book = _resolve_addressbook(addressbook)

        card = vobject.vCard()
        card.add("fn").value = name
        parts = name.rsplit(" ", 1)
        n = card.add("n")
        if len(parts) == 2:
            n.value = vobject.vcard.Name(family=parts[1], given=parts[0])
        else:
            n.value = vobject.vcard.Name(family=name, given="")

        uid_val = str(uuid.uuid4())
        card.add("uid").value = uid_val

        if email:
            card.add("email").value = email
        if phone:
            card.add("tel").value = phone
        if org:
            card.add("org").value = [org]
        if notes:
            card.add("note").value = notes

        vcard_str = card.serialize()
        book.save_event(vcard_str)
        return {"uid": uid_val}
    except ValueError as e:
        return format_error("create_contact", str(e))
    except Exception as e:
        return format_error("create_contact", str(e))


@carddav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def update_contact(
    addressbook: Annotated[str, "Address book name or path"],
    uid: Annotated[str, "Contact UID to update"],
    updates: Annotated[dict, "Fields to update: name, email, phone, org, notes. Multi-value: emails, phones as lists"],
) -> str:
    """Update a contact. Preserves PHOTO and other fields not being changed."""
    try:
        book = _resolve_addressbook(addressbook)
        target_href = None
        target_data = None

        for href, data in _fetch_vcards(book):
            try:
                card = vobject.readOne(_strip_photo(data))
                if hasattr(card, "uid") and str(card.uid.value) == uid:
                    target_href = href
                    target_data = data
                    break
            except Exception:
                continue

        if target_href is None:
            return format_error("update_contact", f"Contact with UID '{uid}' not found")

        # Extract PHOTO block to preserve it, then parse without it
        photo_match = re.search(
            r"(PHOTO[;:][^\r\n]*(?:\r?\n[ \t][^\r\n]*)*\r?\n)",
            target_data,
        )
        photo_block = photo_match.group(1) if photo_match else None
        card = vobject.readOne(_strip_photo(target_data))

        if "name" in updates:
            card.fn.value = updates["name"]
            parts = updates["name"].rsplit(" ", 1)
            if len(parts) == 2:
                card.n.value = vobject.vcard.Name(family=parts[1], given=parts[0])
            else:
                card.n.value = vobject.vcard.Name(family=updates["name"], given="")

        if "email" in updates:
            card.contents["email"] = []
            card.add("email").value = updates["email"]

        if "emails" in updates:
            card.contents["email"] = []
            for entry in updates["emails"]:
                e = card.add("email")
                if isinstance(entry, dict):
                    e.value = entry["value"]
                    if "type" in entry:
                        e.type_param = entry["type"]
                else:
                    e.value = str(entry)

        if "phone" in updates:
            card.contents["tel"] = []
            card.add("tel").value = updates["phone"]

        if "phones" in updates:
            card.contents["tel"] = []
            for entry in updates["phones"]:
                t = card.add("tel")
                if isinstance(entry, dict):
                    t.value = entry["value"]
                    if "type" in entry:
                        t.type_param = entry["type"]
                else:
                    t.value = str(entry)

        if "org" in updates:
            if hasattr(card, "org"):
                card.org.value = [updates["org"]]
            else:
                card.add("org").value = [updates["org"]]

        if "notes" in updates:
            if hasattr(card, "note"):
                card.note.value = updates["notes"]
            else:
                card.add("note").value = updates["notes"]

        # Re-insert PHOTO block if present, then save
        serialized = card.serialize()
        if photo_block:
            serialized = serialized.replace(
                "END:VCARD", photo_block + "END:VCARD"
            )
        obj = caldav.CalendarObjectResource(
            client=_client, url=target_href, data=serialized,
        )
        obj.save()
        return f"Updated contact {uid}"
    except ValueError as e:
        return format_error("update_contact", str(e))
    except Exception as e:
        return format_error("update_contact", str(e))


@carddav_server.tool(
    annotations={
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    }
)
def delete_contact(
    addressbook: Annotated[str, "Address book name or path"],
    uid: Annotated[str, "Contact UID to delete"],
) -> str:
    """Delete a contact by UID."""
    try:
        book = _resolve_addressbook(addressbook)
        for href, data in _fetch_vcards(book):
            try:
                card = vobject.readOne(_strip_photo(data))
                if hasattr(card, "uid") and str(card.uid.value) == uid:
                    obj = caldav.CalendarObjectResource(client=_client, url=href)
                    obj.delete()
                    return f"Deleted contact {uid}"
            except Exception:
                continue
        return format_error("delete_contact", f"Contact with UID '{uid}' not found")
    except ValueError as e:
        return format_error("delete_contact", str(e))
    except Exception as e:
        return format_error("delete_contact", str(e))
