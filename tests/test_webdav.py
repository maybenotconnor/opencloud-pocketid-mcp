"""Tests for WebDAV tools — uses mocks for the webdav4 client."""

from unittest.mock import MagicMock, patch

import pytest

from src.webdav_server import (
    copy,
    delete,
    find_files,
    get_file_info,
    mkdir,
    move,
    read_binary,
    read_file,
    write_file,
)


@pytest.fixture(autouse=True)
def mock_client():
    """Provide a mock webdav4 client for all tests."""
    client = MagicMock()
    with patch("src.webdav_server._get_client", return_value=client):
        yield client


class TestFindFiles:
    def test_lists_single_directory(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "/", "type": "directory", "content_length": 0, "modified": ""},
            {"name": "/file.txt", "type": "file", "content_length": 100, "modified": "2026-01-01"},
            {"name": "/subdir", "type": "directory", "content_length": 0, "modified": "2026-01-02"},
        ]
        result = find_files("/", depth=1)
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "file.txt"
        assert result[1]["name"] == "subdir"
        assert result[1]["type"] == "directory"

    def test_rejects_path_traversal(self):
        result = find_files("/../etc")
        assert "Error" in result

    def test_filters_by_query(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "/", "type": "directory", "content_length": 0, "modified": ""},
            {"name": "/readme.md", "type": "file", "content_length": 50, "modified": "2026-01-01"},
            {"name": "/photo.jpg", "type": "file", "content_length": 200, "modified": "2026-01-02"},
        ]
        result = find_files("/", query="*.md", depth=1)
        assert len(result) == 1
        assert result[0]["name"] == "readme.md"

    def test_filters_by_file_type(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "/", "type": "directory", "content_length": 0, "modified": ""},
            {"name": "/file.txt", "type": "file", "content_length": 100, "modified": "2026-01-01"},
            {"name": "/subdir", "type": "directory", "content_length": 0, "modified": "2026-01-02"},
        ]
        result = find_files("/", file_type="directory", depth=1)
        assert len(result) == 1
        assert result[0]["type"] == "directory"

    def test_filters_by_modified_after(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "/", "type": "directory", "content_length": 0, "modified": ""},
            {"name": "/old.txt", "type": "file", "content_length": 50, "modified": "2026-01-01T00:00:00+00:00"},
            {"name": "/new.txt", "type": "file", "content_length": 50, "modified": "2026-03-05T12:00:00+00:00"},
        ]
        result = find_files("/", modified_after="2026-03-01", depth=1)
        assert len(result) == 1
        assert result[0]["name"] == "new.txt"

    def test_respects_limit(self, mock_client):
        items = [{"name": "/", "type": "directory", "content_length": 0, "modified": ""}]
        for i in range(20):
            items.append({"name": f"/file{i}.txt", "type": "file", "content_length": 10, "modified": "2026-01-01"})
        mock_client.ls.return_value = items
        result = find_files("/", depth=1, limit=5)
        # 5 results + 1 truncation note
        assert len(result) == 6
        assert result[-1].get("note") is not None


class TestReadFile:
    def test_rejects_large_files(self, mock_client):
        mock_client.info.return_value = {"content_length": 2_000_000}
        result = read_file("/large.bin")
        assert "1MB limit" in result

    def test_rejects_path_traversal(self):
        result = read_file("/../etc/passwd")
        assert "Error" in result


class TestReadBinary:
    def test_rejects_large_files(self, mock_client):
        mock_client.info.return_value = {"content_length": 10_000_000}
        result = read_binary("/huge.bin")
        assert "5MB limit" in result


class TestWriteFile:
    def test_creates_parent_dirs(self, mock_client):
        result = write_file("/deep/nested/file.txt", "hello")
        mock_client.mkdir.assert_called_once()
        assert "Successfully wrote" in result

    def test_uploads_with_overwrite(self, mock_client):
        write_file("/file.txt", "content")
        _, kwargs = mock_client.upload_file.call_args
        assert kwargs["overwrite"] is True


class TestMkdir:
    def test_creates_directory(self, mock_client):
        result = mkdir("/new-dir")
        mock_client.mkdir.assert_called_once_with("/new-dir")
        assert "Directory created" in result

    def test_idempotent_when_exists(self, mock_client):
        from webdav4.client import ResourceAlreadyExists
        mock_client.mkdir.side_effect = ResourceAlreadyExists("/existing-dir")
        result = mkdir("/existing-dir")
        assert "already exists" in result


class TestDelete:
    def test_deletes_file(self, mock_client):
        result = delete("/file.txt")
        mock_client.remove.assert_called_once_with("/file.txt")
        assert "Deleted" in result


class TestMove:
    def test_moves_file(self, mock_client):
        result = move("/a.txt", "/b.txt")
        mock_client.move.assert_called_once_with("/a.txt", "/b.txt")
        assert "Moved" in result

    def test_rejects_traversal_in_source(self):
        result = move("/../etc/passwd", "/safe.txt")
        assert "Error" in result


class TestCopy:
    def test_copies_file(self, mock_client):
        result = copy("/a.txt", "/b.txt")
        mock_client.copy.assert_called_once_with("/a.txt", "/b.txt")
        assert "Copied" in result


class TestGetFileInfo:
    def test_returns_metadata(self, mock_client):
        mock_client.info.return_value = {
            "content_length": 512,
            "modified": "2026-03-01",
            "etag": '"abc123"',
            "content_type": "text/plain",
            "type": "file",
        }
        result = get_file_info("/notes.txt")
        assert isinstance(result, dict)
        assert result["size"] == 512
        assert result["content_type"] == "text/plain"


