"""Tests for WebDAV tools — uses mocks for the webdav4 client."""

from unittest.mock import MagicMock, patch

import pytest

from src.webdav_server import (
    _build_kql,
    _parse_search_response,
    copy,
    delete,
    find_files,
    get_file_info,
    mkdir,
    move,
    read_binary,
    read_file,
    search_files,
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


# --- Server-side search tests ---

_SAMPLE_207 = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/spaces/abc-123-def/Documents/report.pdf</d:href>
    <d:propstat>
      <d:prop>
        <oc:name>report.pdf</oc:name>
        <d:resourcetype/>
        <d:getcontentlength>204800</d:getcontentlength>
        <d:getlastmodified>Mon, 10 Mar 2026 12:00:00 GMT</d:getlastmodified>
        <d:getcontenttype>application/pdf</d:getcontenttype>
        <oc:score>0.85</oc:score>
      </d:prop>
    </d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/spaces/abc-123-def/Budget/2026.xlsx</d:href>
    <d:propstat>
      <d:prop>
        <oc:name>2026.xlsx</oc:name>
        <d:resourcetype/>
        <d:getcontentlength>51200</d:getcontentlength>
        <d:getlastmodified>Sun, 09 Mar 2026 08:00:00 GMT</d:getlastmodified>
        <d:getcontenttype>application/vnd.openxmlformats-officedocument.spreadsheetml.sheet</d:getcontenttype>
        <oc:score>0.42</oc:score>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>"""

_SAMPLE_DIR_207 = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/spaces/abc-123-def/Projects</d:href>
    <d:propstat>
      <d:prop>
        <oc:name>Projects</oc:name>
        <d:resourcetype><d:collection/></d:resourcetype>
        <d:getlastmodified>Mon, 10 Mar 2026 12:00:00 GMT</d:getlastmodified>
        <oc:score>0.5</oc:score>
      </d:prop>
    </d:propstat>
  </d:response>
</d:multistatus>"""


class TestSearchFiles:
    @pytest.fixture(autouse=True)
    def mock_httpx(self):
        """Mock httpx.request for search tests (search uses httpx directly, not webdav4)."""
        with patch("src.webdav_server.httpx.request") as mock_req:
            self.mock_request = mock_req
            yield mock_req

    def _mock_207(self, xml=_SAMPLE_207):
        resp = MagicMock()
        resp.status_code = 207
        resp.text = xml
        self.mock_request.return_value = resp

    def test_content_search(self):
        self._mock_207()
        result = search_files(content="budget")
        assert isinstance(result, list)
        assert len(result) == 2
        # Should be sorted by score descending
        assert result[0]["name"] == "report.pdf"
        assert result[0]["score"] == 0.85
        assert result[1]["name"] == "2026.xlsx"
        # Verify KQL built correctly
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "content:budget" in body

    def test_name_search(self):
        self._mock_207()
        result = search_files(name="*.pdf")
        assert isinstance(result, list)
        call_kwargs = self.mock_request.call_args
        body = call_kwargs.kwargs.get("content", "")
        assert "name:*.pdf" in body

    def test_combined_params(self):
        self._mock_207()
        search_files(content="report", mediatype="pdf", mtime="today")
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "content:report" in body
        assert "mediatype:pdf" in body
        assert "mtime:today" in body

    def test_cleans_space_href(self):
        self._mock_207()
        result = search_files(content="budget")
        # Path should have spaces prefix stripped
        assert result[0]["path"] == "/Documents/report.pdf"
        assert result[1]["path"] == "/Budget/2026.xlsx"

    def test_empty_params_returns_error(self):
        result = search_files()
        assert isinstance(result, str)
        assert "Error" in result
        assert "At least one" in result

    def test_handles_non_207(self):
        resp = MagicMock()
        resp.status_code = 500
        self.mock_request.return_value = resp
        result = search_files(content="test")
        assert isinstance(result, str)
        assert "Error" in result
        assert "500" in result

    def test_handles_auth_error(self):
        resp = MagicMock()
        resp.status_code = 401
        self.mock_request.return_value = resp
        result = search_files(content="test")
        assert isinstance(result, str)
        assert "Authentication" in result

    def test_handles_501(self):
        resp = MagicMock()
        resp.status_code = 501
        self.mock_request.return_value = resp
        result = search_files(content="test")
        assert isinstance(result, str)
        assert "not available" in result

    def test_parses_directories(self):
        self._mock_207(xml=_SAMPLE_DIR_207)
        result = search_files(name="Projects")
        assert len(result) == 1
        assert result[0]["type"] == "directory"
        assert result[0]["size"] == 0

    def test_respects_limit(self):
        self._mock_207()
        search_files(content="budget", limit=300)
        body = self.mock_request.call_args.kwargs.get("content", "")
        # Should be clamped to 200
        assert "<oc:limit>200</oc:limit>" in body

    def test_respects_limit_minimum(self):
        self._mock_207()
        search_files(content="budget", limit=0)
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "<oc:limit>1</oc:limit>" in body


class TestBuildKql:
    def test_single_content(self):
        assert _build_kql("budget", "", "", "") == "content:budget"

    def test_single_name(self):
        assert _build_kql("", "*.pdf", "", "") == "name:*.pdf"

    def test_combined(self):
        result = _build_kql("report", "*.pdf", "document", "today")
        assert result == "content:report name:*.pdf mediatype:document mtime:today"

    def test_empty(self):
        assert _build_kql("", "", "", "") == ""


class TestParseSearchResponse:
    def test_parses_files(self):
        results = _parse_search_response(_SAMPLE_207)
        assert len(results) == 2
        assert results[0]["name"] == "report.pdf"
        assert results[0]["size"] == 204800
        assert results[0]["content_type"] == "application/pdf"
        assert results[0]["type"] == "file"

    def test_parses_directory(self):
        results = _parse_search_response(_SAMPLE_DIR_207)
        assert len(results) == 1
        assert results[0]["type"] == "directory"

    def test_empty_response(self):
        results = _parse_search_response('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>')
        assert results == []


