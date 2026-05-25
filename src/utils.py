import posixpath
import re


def sanitize_path(path: str) -> str:
    """Normalize and validate a WebDAV path.

    Rejects path traversal attempts and normalizes slashes.
    Returns a path starting with / and without trailing slash (except root).
    """
    if ".." in path.split("/"):
        raise ValueError("Path traversal ('..') is not allowed")
    if path.startswith("~"):
        raise ValueError("Paths starting with '~' are not allowed")

    # Normalize double slashes and ensure leading slash
    path = "/" + path.lstrip("/")
    path = posixpath.normpath(path)
    return path


def format_error(operation: str, detail: str) -> str:
    """Format a user-friendly error message."""
    return f"Error: {operation}: {detail}"


def matches_query(name: str, query: str) -> bool:
    """Check if a filename matches a search query.

    Supports case-insensitive substring or glob matching.
    If query contains * or ?, uses glob-style matching.
    If query contains spaces (and no glob chars), all terms must match (AND).
    """
    name_lower = name.lower()
    query_lower = query.lower()

    if "*" in query or "?" in query:
        pattern = re.escape(query_lower).replace(r"\*", ".*").replace(r"\?", ".")
        return bool(re.fullmatch(pattern, name_lower))

    return all(term in name_lower for term in query_lower.split())


def matches_terms(text: str, query: str) -> bool:
    """Check if text contains all query terms (AND logic, case-insensitive)."""
    text_lower = text.lower()
    return all(term in text_lower for term in query.lower().split())
