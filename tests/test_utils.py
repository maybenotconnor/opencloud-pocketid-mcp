"""Tests for utils.py — path sanitization and query matching."""

import pytest

from src.utils import format_error, matches_query, sanitize_path


class TestSanitizePath:
    def test_normalizes_basic_path(self):
        assert sanitize_path("/Documents") == "/Documents"

    def test_adds_leading_slash(self):
        assert sanitize_path("Documents") == "/Documents"

    def test_removes_trailing_slash(self):
        assert sanitize_path("/Documents/") == "/Documents"

    def test_normalizes_double_slashes(self):
        assert sanitize_path("/Documents//Notes") == "/Documents/Notes"

    def test_root_path(self):
        assert sanitize_path("/") == "/"

    def test_rejects_path_traversal(self):
        with pytest.raises(ValueError, match="Path traversal"):
            sanitize_path("/Documents/../etc/passwd")

    def test_rejects_tilde(self):
        with pytest.raises(ValueError, match="~"):
            sanitize_path("~/Documents")

    def test_allows_dots_in_filename(self):
        assert sanitize_path("/file.txt") == "/file.txt"

    def test_allows_dotfile(self):
        assert sanitize_path("/.env") == "/.env"


class TestMatchesQuery:
    def test_substring_match(self):
        assert matches_query("document.txt", "doc")

    def test_case_insensitive(self):
        assert matches_query("Document.txt", "document")

    def test_no_match(self):
        assert not matches_query("photo.jpg", "document")

    def test_glob_star(self):
        assert matches_query("report.pdf", "*.pdf")

    def test_glob_question(self):
        assert matches_query("file1.txt", "file?.txt")

    def test_glob_no_match(self):
        assert not matches_query("report.pdf", "*.txt")


class TestFormatError:
    def test_format(self):
        assert format_error("read_file", "not found") == "Error: read_file: not found"
