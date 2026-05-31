"""Tests for WebDAV tools — uses mocks for the webdav4 client."""

import base64
from unittest.mock import MagicMock, patch

import pytest
from mcp.types import ImageContent, TextContent

from src.webdav_server import (
    _WALK_DIR_BUDGET,
    _build_kql,
    _expand_braces,
    _glob_base,
    _glob_can_descend,
    _glob_match,
    _glob_search_name,
    _is_deep_pattern,
    _parse_search_response,
    copy,
    delete,
    edit_file,
    get_file_info,
    glob,
    mkdir,
    move,
    read_file,
    search,
    write_file,
)


@pytest.fixture(autouse=True)
def mock_client():
    """Provide a mock webdav4 client for all tests."""
    client = MagicMock()
    with patch("src.webdav_server._get_client", return_value=client):
        yield client


class TestGlob:
    # webdav4 ls() returns paths without leading slash (relative to base URL)
    def test_lists_single_directory(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "file.txt", "type": "file", "content_length": 100, "modified": "2026-01-01"},
            {"name": "subdir", "type": "directory", "content_length": 0, "modified": "2026-01-02"},
        ]
        result = glob("**/*", depth=1)
        assert isinstance(result, list)
        assert len(result) == 2
        # Results are ordered most-recently-modified first (Claude Code parity):
        # subdir (2026-01-02) precedes file.txt (2026-01-01).
        assert result[0]["name"] == "subdir"
        assert result[0]["type"] == "directory"
        assert result[1]["name"] == "file.txt"

    def test_rejects_path_traversal(self):
        result = glob("/../etc/**/*")
        assert "Error" in result

    def test_filters_by_extension(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "readme.md", "type": "file", "content_length": 50, "modified": "2026-01-01"},
            {"name": "photo.jpg", "type": "file", "content_length": 200, "modified": "2026-01-02"},
        ]
        result = glob("*.md", depth=1)
        assert len(result) == 1
        assert result[0]["name"] == "readme.md"

    def test_filters_by_file_type(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "file.txt", "type": "file", "content_length": 100, "modified": "2026-01-01"},
            {"name": "subdir", "type": "directory", "content_length": 0, "modified": "2026-01-02"},
        ]
        result = glob("**/*", file_type="directory", depth=1)
        assert len(result) == 1
        assert result[0]["type"] == "directory"

    def test_filters_by_modified_after(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "old.txt", "type": "file", "content_length": 50, "modified": "2026-01-01T00:00:00+00:00"},
            {"name": "new.txt", "type": "file", "content_length": 50, "modified": "2026-03-05T12:00:00+00:00"},
        ]
        result = glob("**/*", modified_after="2026-03-01", depth=1)
        assert len(result) == 1
        assert result[0]["name"] == "new.txt"

    def test_respects_limit(self, mock_client):
        items = []
        for i in range(20):
            items.append({"name": f"file{i}.txt", "type": "file", "content_length": 10, "modified": "2026-01-01"})
        mock_client.ls.return_value = items
        result = glob("**/*", depth=1, limit=5)
        assert len(result) == 6
        assert result[-1].get("note") is not None

    def test_path_pattern_uses_base_dir(self, mock_client):
        mock_client.ls.return_value = []
        glob("/Documents/**/*.pdf")
        mock_client.ls.assert_called_with("/Documents", detail=True)

    def test_does_not_recurse_for_single_level_pattern(self, mock_client):
        # '/Docs/*.pdf' only matches direct children, so sibling subdirectories
        # must NOT be walked — this is the performance fix.
        def fake_ls(path, detail=True):
            if path.rstrip("/") == "/Docs":
                return [
                    {"name": "Docs/a.pdf", "type": "file", "content_length": 1, "modified": "2026-01-01"},
                    {"name": "Docs/Photos", "type": "directory", "content_length": 0, "modified": "2026-01-01"},
                ]
            raise AssertionError(f"glob recursed into {path!r}; should have been pruned")
        mock_client.ls.side_effect = fake_ls
        result = glob("/Docs/*.pdf")
        assert [r["name"] for r in result] == ["a.pdf"]
        mock_client.ls.assert_called_once_with("/Docs", detail=True)

    def test_prunes_sibling_branches_for_prefixed_pattern(self, mock_client):
        # '/Docs/Reports/**/*.pdf' must descend into Reports but skip Photos.
        calls = []

        def fake_ls(path, detail=True):
            calls.append(path.rstrip("/"))
            if path.rstrip("/") == "/Docs":
                return [
                    {"name": "Docs/Reports", "type": "directory", "content_length": 0, "modified": "2026-01-01"},
                    {"name": "Docs/Photos", "type": "directory", "content_length": 0, "modified": "2026-01-01"},
                ]
            if path.rstrip("/") == "/Docs/Reports":
                return [
                    {"name": "Docs/Reports/q1.pdf", "type": "file", "content_length": 1, "modified": "2026-01-01"},
                ]
            raise AssertionError(f"glob recursed into {path!r}; should have been pruned")
        mock_client.ls.side_effect = fake_ls
        result = glob("/Docs/Reports/**/*.pdf")
        assert [r["name"] for r in result] == ["q1.pdf"]
        assert "/Docs/Photos" not in calls

    def test_recurses_for_double_star_pattern(self, mock_client):
        # '**' patterns legitimately need to walk every subdirectory.
        def fake_ls(path, detail=True):
            if path.rstrip("/") == "/Docs":
                return [
                    {"name": "Docs/sub", "type": "directory", "content_length": 0, "modified": "2026-01-01"},
                ]
            if path.rstrip("/") == "/Docs/sub":
                return [
                    {"name": "Docs/sub/deep.pdf", "type": "file", "content_length": 1, "modified": "2026-01-01"},
                ]
            return []
        mock_client.ls.side_effect = fake_ls
        result = glob("/Docs/**/*.pdf")
        assert [r["name"] for r in result] == ["deep.pdf"]

    def test_absolute_pattern_matches_paths_with_spaces(self, mock_client):
        # webdav4 returns paths without leading slash; normalization must handle spaces
        mock_client.ls.return_value = [
            {"name": "Notes/3 Knowledge/AI Skills/opencloud.md", "type": "file",
             "content_length": 1024, "modified": "2026-05-01"},
            {"name": "Notes/3 Knowledge/AI Skills/other.txt", "type": "file",
             "content_length": 512, "modified": "2026-05-02"},
        ]
        result = glob("/Notes/3 Knowledge/**/*.md")
        assert len(result) == 1
        assert result[0]["name"] == "opencloud.md"

    def test_scoped_deep_pattern_walks_not_index(self, mock_client):
        # A folder-scoped '**' pattern must walk that subtree, NOT hit the
        # global index (whose result cap could silently drop in-folder matches).
        mock_client.ls.return_value = [
            {"name": "Notes/a.md", "type": "file", "content_length": 1, "modified": "2026-01-01"},
        ]
        with patch("src.webdav_server.httpx.request") as mock_req:
            result = glob("/Notes/**/*.md")
            mock_req.assert_not_called()
        mock_client.ls.assert_called()
        assert [r["name"] for r in result] == ["a.md"]

    def test_results_sorted_by_modified_desc(self, mock_client):
        mock_client.ls.return_value = [
            {"name": "Notes/old.md", "type": "file", "content_length": 1, "modified": "2026-01-01T00:00:00+00:00"},
            {"name": "Notes/new.md", "type": "file", "content_length": 1, "modified": "2026-05-01T00:00:00+00:00"},
            {"name": "Notes/mid.md", "type": "file", "content_length": 1, "modified": "2026-03-01T00:00:00+00:00"},
        ]
        result = glob("/Notes/*.md")
        assert [r["name"] for r in result] == ["new.md", "mid.md", "old.md"]

    def test_walk_budget_stops_and_notes(self, mock_client):
        # A pathologically deep subtree must terminate with a note rather than
        # crawl forever. Each directory contains one deeper subdirectory.
        def fake_ls(path, detail=True):
            depth = path.rstrip("/").count("/")
            return [{
                "name": f"{path.rstrip('/')}/d{depth}".lstrip("/"),
                "type": "directory", "content_length": 0, "modified": "2026-01-01",
            }]
        mock_client.ls.side_effect = fake_ls
        result = glob("/Docs/**/*.pdf")
        assert any(isinstance(r, dict) and "note" in r for r in result)
        # The walk visited at most the budget number of directories.
        assert mock_client.ls.call_count <= _WALK_DIR_BUDGET

    def test_brace_expansion_matches_each_alternative(self, mock_client):
        # '{md,txt}' must match both extensions; '.pdf' must be excluded.
        mock_client.ls.return_value = [
            {"name": "Docs/a.md", "type": "file", "content_length": 1, "modified": "2026-01-03"},
            {"name": "Docs/b.txt", "type": "file", "content_length": 1, "modified": "2026-01-02"},
            {"name": "Docs/c.pdf", "type": "file", "content_length": 1, "modified": "2026-01-01"},
        ]
        result = glob("/Docs/*.{md,txt}")
        names = {r["name"] for r in result if "path" in r}
        assert names == {"a.md", "b.txt"}


class TestExpandBraces:
    def test_no_braces_is_identity(self):
        assert _expand_braces("**/*.md") == ["**/*.md"]

    def test_simple_alternation(self):
        assert _expand_braces("/a/*.{ts,js}") == ["/a/*.ts", "/a/*.js"]

    def test_nested(self):
        assert _expand_braces("x{a,{b,c}}y") == ["xay", "xby", "xcy"]

    def test_multiple_groups(self):
        assert _expand_braces("{a,b}{1,2}") == ["a1", "a2", "b1", "b2"]

    def test_commaless_group_is_literal(self):
        assert _expand_braces("/a/{x}.md") == ["/a/{x}.md"]


_GLOB_SEARCH_207 = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/spaces/abc-123/Personal/Hobbies.md</d:href>
    <d:propstat><d:prop>
      <oc:name>Hobbies.md</oc:name>
      <d:resourcetype/>
      <d:getcontentlength>120</d:getcontentlength>
      <d:getlastmodified>Mon, 04 May 2026 12:00:00 GMT</d:getlastmodified>
    </d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/spaces/abc-123/Work/cobbler.txt</d:href>
    <d:propstat><d:prop>
      <oc:name>cobbler.txt</oc:name>
      <d:resourcetype/>
      <d:getcontentlength>50</d:getcontentlength>
      <d:getlastmodified>Mon, 04 May 2026 12:00:00 GMT</d:getlastmodified>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""


class TestGlobServerSearch:
    """Deep ('**'-rooted / basename-only) globs are served by the search index."""

    @pytest.fixture(autouse=True)
    def mock_httpx(self):
        with patch("src.webdav_server.httpx.request") as mock_req:
            self.mock_request = mock_req
            yield mock_req

    def _mock_207(self, xml=_GLOB_SEARCH_207):
        resp = MagicMock()
        resp.status_code = 207
        resp.text = xml
        self.mock_request.return_value = resp

    def test_deep_pattern_uses_index_not_walk(self, mock_client):
        self._mock_207()
        glob("**/*[Hh]obb*")
        # Server search index was queried...
        assert self.mock_request.called
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "name:*obb*" in body
        # ...and the walk was never invoked.
        mock_client.ls.assert_not_called()

    def test_char_class_post_filter_excludes_non_matches(self, mock_client):
        # 'cobbler' contains 'obb' (so the broadened server query returns it)
        # but does NOT match '*[Hh]obb*', so the exact post-filter drops it.
        self._mock_207()
        result = glob("**/*[Hh]obb*")
        names = [r["name"] for r in result]
        assert "Hobbies.md" in names
        assert "cobbler.txt" not in names

    def test_basename_only_pattern_uses_index(self, mock_client):
        self._mock_207()
        glob("*.pdf")
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "name:*.pdf" in body
        mock_client.ls.assert_not_called()

    def test_explicit_depth_uses_walk_not_index(self, mock_client):
        # A bounded depth means the user wants a scoped walk, not a drive search.
        mock_client.ls.return_value = []
        glob("**/*", depth=1)
        self.mock_request.assert_not_called()
        mock_client.ls.assert_called()

    def test_falls_back_to_walk_when_index_unavailable(self, mock_client):
        resp = MagicMock()
        resp.status_code = 501
        self.mock_request.return_value = resp
        mock_client.ls.return_value = [
            {"name": "Hobbies.md", "type": "file", "content_length": 1, "modified": "2026-05-04"},
        ]
        result = glob("**/*[Hh]obb*")
        # Index said "unavailable", so we fell back to the walk.
        mock_client.ls.assert_called()
        assert [r["name"] for r in result] == ["Hobbies.md"]

    def test_modified_after_filters_index_results(self, mock_client):
        xml = _GLOB_SEARCH_207.replace(
            "Mon, 04 May 2026 12:00:00 GMT", "Mon, 04 May 2020 12:00:00 GMT", 1
        )
        self._mock_207(xml=xml)
        result = glob("**/*obb*", modified_after="2026-01-01")
        # Hobbies.md is now dated 2020 and must be filtered out.
        assert all(r["name"] != "Hobbies.md" for r in result)

    @staticmethod
    def _page_xml(n, start=0, modified="Mon, 04 May 2026 12:00:00 GMT"):
        rows = "".join(
            f"<d:response><d:href>/remote.php/dav/spaces/x/f{i}.md</d:href>"
            f"<d:propstat><d:prop><oc:name>f{i}.md</oc:name><d:resourcetype/>"
            f"<d:getcontentlength>1</d:getcontentlength>"
            f"<d:getlastmodified>{modified}</d:getlastmodified>"
            f"</d:prop></d:propstat></d:response>"
            for i in range(start, start + n)
        )
        return (
            '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            f"{rows}</d:multistatus>"
        )

    def test_paginates_across_pages_until_limit(self, mock_client):
        # A full first page plus a partial second page: the index is queried
        # twice (with an advancing offset) to honor a limit above one page.
        page1 = MagicMock(status_code=207, text=self._page_xml(200, 0))
        page2 = MagicMock(status_code=207, text=self._page_xml(50, 200))
        self.mock_request.side_effect = [page1, page2]
        result = glob("**/*.md", limit=300)
        assert self.mock_request.call_count == 2
        second_body = self.mock_request.call_args_list[1].kwargs.get("content", "")
        assert "<oc:offset>200</oc:offset>" in second_body
        files = [r for r in result if "path" in r]
        assert len(files) == 250

    def test_brace_pattern_queries_each_alternative(self, mock_client):
        # '**/*.{ts,js}' must issue a name query per alternative.
        self._mock_207(xml=self._page_xml(1, 0))
        glob("**/*.{ts,js}")
        bodies = [c.kwargs.get("content", "") for c in self.mock_request.call_args_list]
        joined = "\n".join(bodies)
        assert "name:*.ts" in joined
        assert "name:*.js" in joined
        mock_client.ls.assert_not_called()

    def test_index_results_sorted_by_mtime_desc(self, mock_client):
        xml = (
            '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            "<d:response><d:href>/remote.php/dav/spaces/x/older.md</d:href>"
            "<d:propstat><d:prop><oc:name>older.md</oc:name><d:resourcetype/>"
            "<d:getlastmodified>Mon, 04 May 2020 12:00:00 GMT</d:getlastmodified>"
            "</d:prop></d:propstat></d:response>"
            "<d:response><d:href>/remote.php/dav/spaces/x/newer.md</d:href>"
            "<d:propstat><d:prop><oc:name>newer.md</oc:name><d:resourcetype/>"
            "<d:getlastmodified>Mon, 04 May 2026 12:00:00 GMT</d:getlastmodified>"
            "</d:prop></d:propstat></d:response>"
            "</d:multistatus>"
        )
        self._mock_207(xml=xml)
        result = glob("**/*.md")
        names = [r["name"] for r in result if "path" in r]
        assert names == ["newer.md", "older.md"]


class TestGlobHelpers:
    def test_glob_base_absolute_with_wildcard(self):
        assert _glob_base("/Documents/**/*.pdf") == "/Documents"

    def test_glob_base_relative(self):
        assert _glob_base("**/*.pdf") == "/"

    def test_glob_base_no_wildcard(self):
        assert _glob_base("/Documents/report.pdf") == "/Documents"

    def test_glob_base_root_wildcard(self):
        assert _glob_base("*.txt") == "/"

    def test_glob_match_double_star(self):
        assert _glob_match("/Documents/Projects/report.pdf", "/Documents/**/*.pdf")

    def test_glob_match_single_star(self):
        assert _glob_match("/Documents/report.pdf", "/Documents/*.pdf")
        assert not _glob_match("/Documents/sub/report.pdf", "/Documents/*.pdf")

    def test_glob_match_no_slash_basename(self):
        assert _glob_match("/Documents/report.pdf", "*.pdf")
        assert _glob_match("/any/depth/report.pdf", "*.pdf")

    def test_glob_match_question_mark(self):
        assert _glob_match("/file1.txt", "file?.txt")
        assert not _glob_match("/file10.txt", "file?.txt")

    def test_glob_match_case_insensitive(self):
        assert _glob_match("/Documents/Report.PDF", "/Documents/*.pdf")

    def test_can_descend_basename_pattern_always_true(self):
        assert _glob_can_descend("/anything/here", "*.pdf")

    def test_can_descend_single_level_pattern_prunes(self):
        # '/Documents/*.pdf' matches only direct children of /Documents
        assert _glob_can_descend("/Documents", "/Documents/*.pdf")
        assert not _glob_can_descend("/Documents/sub", "/Documents/*.pdf")
        assert not _glob_can_descend("/Other", "/Documents/*.pdf")

    def test_can_descend_double_star_descends_deep(self):
        assert _glob_can_descend("/Documents", "/Documents/**/*.pdf")
        assert _glob_can_descend("/Documents/a/b/c", "/Documents/**/*.pdf")
        assert not _glob_can_descend("/Other", "/Documents/**/*.pdf")

    def test_can_descend_prefixed_pattern_prunes_siblings(self):
        assert _glob_can_descend("/Docs/Reports", "/Docs/Reports/**/*.pdf")
        assert not _glob_can_descend("/Docs/Photos", "/Docs/Reports/**/*.pdf")

    def test_glob_match_char_class(self):
        assert _glob_match("/notes/Hobbies.md", "**/*[Hh]obb*")
        assert _glob_match("/notes/hobby.md", "**/*[Hh]obb*")
        assert not _glob_match("/notes/cobbler.txt", "**/*[Hh]obb*")

    def test_is_deep_pattern(self):
        assert _is_deep_pattern("**/*[Hh]obb*")
        assert _is_deep_pattern("**/*.pdf")
        assert _is_deep_pattern("*.pdf")          # basename-only -> any depth
        assert _is_deep_pattern("report")
        assert not _is_deep_pattern("/Documents/*.pdf")
        assert not _is_deep_pattern("/Documents/report.pdf")

    def test_glob_search_name_widens_specials(self):
        assert _glob_search_name("**/*[Hh]obb*") == "*obb*"
        assert _glob_search_name("*.pdf") == "*.pdf"
        assert _glob_search_name("**/*report*") == "*report*"
        assert _glob_search_name("file?.txt") == "file*.txt"


class TestReadFile:
    def test_rejects_large_text_files(self, mock_client):
        mock_client.info.return_value = {"content_length": 2_000_000, "content_type": "text/plain"}
        result = read_file("/large.txt")
        assert "1MB limit" in result

    def test_rejects_large_binary_files(self, mock_client):
        mock_client.info.return_value = {"content_length": 10_000_000, "content_type": "application/octet-stream"}
        result = read_file("/huge.bin", binary=True)
        assert "5MB limit" in result

    def test_rejects_path_traversal(self):
        result = read_file("/../etc/passwd")
        assert "Error" in result

    def test_image_returns_metadata_and_image_content(self, mock_client):
        mock_client.info.return_value = {
            "content_length": 1024,
            "content_type": "image/jpeg",
            "created": "2026-01-02T09:30:00Z",
            "modified": "Mon, 10 Mar 2026 12:00:00 GMT",
        }
        with patch("src.webdav_server.tempfile.NamedTemporaryFile") as mock_tmp, \
             patch("builtins.open", create=True) as mock_open:
            mock_tmp.return_value.__enter__ = lambda s: s
            mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value.name = "/tmp/fake"
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            mock_open.return_value.read.return_value = b"\xff\xd8\xff"
            result = read_file("/photo.jpg")
        assert isinstance(result, list)
        # First block is the metadata, second is the image.
        assert isinstance(result[0], TextContent)
        assert "Size: 1.0 KB" in result[0].text
        assert "Created: 2026-01-02T09:30:00+00:00" in result[0].text
        assert "Modified: 2026-03-10T12:00:00+00:00" in result[0].text
        assert isinstance(result[1], ImageContent)
        assert result[1].mimeType == "image/jpeg"

    def test_binary_flag_returns_metadata_and_base64(self, mock_client):
        mock_client.info.return_value = {"content_length": 100, "content_type": "application/zip"}
        with patch("src.webdav_server.tempfile.NamedTemporaryFile") as mock_tmp, \
             patch("builtins.open", create=True) as mock_open:
            mock_tmp.return_value.__enter__ = lambda s: s
            mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value.name = "/tmp/fake"
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            mock_open.return_value.read.return_value = b"\x00\x01\x02"
            result = read_file("/archive.zip", binary=True)
        assert isinstance(result, list)
        assert isinstance(result[0], TextContent)
        assert "Size: 100 B" in result[0].text
        assert isinstance(result[1], TextContent)
        assert result[1].text == base64.b64encode(b"\x00\x01\x02").decode("ascii")

    def test_text_returns_metadata_and_content(self, mock_client):
        mock_client.info.return_value = {
            "content_length": 11,
            "content_type": "text/plain",
            "modified": "Mon, 10 Mar 2026 12:00:00 GMT",
        }
        with patch("src.webdav_server.tempfile.NamedTemporaryFile") as mock_tmp, \
             patch("builtins.open", create=True) as mock_open:
            mock_tmp.return_value.__enter__ = lambda s: s
            mock_tmp.return_value.__exit__ = MagicMock(return_value=False)
            mock_tmp.return_value.name = "/tmp/fake"
            mock_open.return_value.__enter__ = lambda s: s
            mock_open.return_value.__exit__ = MagicMock(return_value=False)
            # First read() is the binary null-byte sniff, second is the text content.
            mock_open.return_value.read.side_effect = [b"hello world", "hello world"]
            result = read_file("/notes.txt")
        assert isinstance(result, list)
        assert isinstance(result[0], TextContent)
        assert "Created:" not in result[0].text  # no creationdate provided
        assert "Modified: 2026-03-10T12:00:00+00:00" in result[0].text
        assert result[1].text == "hello world"


class TestEditFile:
    def _make_download(self, content: str):
        """Return a side_effect for download_file that writes content to the given path."""
        def _write(src, dst):
            with open(dst, "w", encoding="utf-8") as f:
                f.write(content)
        return _write

    def test_success(self, mock_client):
        mock_client.info.return_value = {"content_length": 100}
        mock_client.download_file.side_effect = self._make_download(
            "Hello world\nThis is a test file."
        )
        result = edit_file("/notes.txt", old_str="Hello world", new_str="Hello OpenCloud")
        assert "Edited" in result
        mock_client.upload_file.assert_called_once()

    def test_not_found(self, mock_client):
        mock_client.info.return_value = {"content_length": 100}
        mock_client.download_file.side_effect = self._make_download("Hello world")
        result = edit_file("/notes.txt", old_str="missing string", new_str="replacement")
        assert "not found" in result

    def test_multiple_matches(self, mock_client):
        mock_client.info.return_value = {"content_length": 100}
        mock_client.download_file.side_effect = self._make_download("foo bar foo")
        result = edit_file("/notes.txt", old_str="foo", new_str="baz")
        assert "2" in result

    def test_rejects_large_files(self, mock_client):
        mock_client.info.return_value = {"content_length": 2_000_000}
        result = edit_file("/large.txt", "foo", "bar")
        assert "1MB limit" in result

    def test_rejects_path_traversal(self):
        result = edit_file("/../etc/passwd", "foo", "bar")
        assert "Error" in result


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
            "created": "2026-01-15",
            "modified": "2026-03-01",
            "etag": '"abc123"',
            "content_type": "text/plain",
            "type": "file",
        }
        result = get_file_info("/notes.txt")
        assert isinstance(result, dict)
        assert result["size"] == 512
        assert result["content_type"] == "text/plain"
        # Timestamps are normalized to ISO 8601.
        assert result["created"] == "2026-01-15T00:00:00+00:00"
        assert result["modified"] == "2026-03-01T00:00:00+00:00"

    def test_created_blank_when_absent(self, mock_client):
        mock_client.info.return_value = {"content_length": 1, "modified": "2026-03-01", "type": "file"}
        result = get_file_info("/notes.txt")
        assert result["created"] == ""


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
        <d:creationdate>2026-01-02T09:30:00Z</d:creationdate>
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


class TestSearch:
    @pytest.fixture(autouse=True)
    def mock_httpx(self):
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
        result = search(query="budget")
        assert isinstance(result, list)
        assert len(result) == 2
        assert result[0]["name"] == "report.pdf"
        assert result[0]["score"] == 0.85
        assert result[1]["name"] == "2026.xlsx"
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "(name:*budget* OR content:budget)" in body

    def test_multi_word_keywords_ored(self):
        self._mock_207()
        search(query="quarterly budget")
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "((name:*quarterly* OR content:quarterly) OR (name:*budget* OR content:budget))" in body

    def test_modified_after(self):
        self._mock_207()
        search(query="report", modified_after="2026-01-01")
        body = self.mock_request.call_args.kwargs.get("content", "")
        # >= is XML-escaped to &gt;= in the body
        assert "mtime&gt;=2026-01-01" in body

    def test_modified_before(self):
        self._mock_207()
        search(query="report", modified_before="2026-12-31")
        body = self.mock_request.call_args.kwargs.get("content", "")
        # <= is XML-escaped to &lt;= in the body
        assert "mtime&lt;=2026-12-31" in body

    def test_combined_params(self):
        self._mock_207()
        search(query="report", mediatype="pdf", modified_after="2026-01-01", modified_before="2026-06-30")
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "(name:*report* OR content:report)" in body
        assert "mediatype:pdf" in body
        assert "mtime&gt;=2026-01-01" in body
        assert "mtime&lt;=2026-06-30" in body

    def test_cleans_space_href(self):
        self._mock_207()
        result = search(query="budget")
        assert result[0]["path"] == "/Documents/report.pdf"
        assert result[1]["path"] == "/Budget/2026.xlsx"

    def test_path_filter(self):
        self._mock_207()
        result = search(query="budget", path="/Documents")
        assert len(result) == 1
        assert result[0]["path"] == "/Documents/report.pdf"

    def test_path_filter_uses_segment_boundary(self):
        # path="/Doc" must NOT match "/Documents/report.pdf" — only items
        # at /Doc or under /Doc/ count as in-scope. Naive startswith() fails this.
        xml = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/spaces/abc/Doc/note.md</d:href>
    <d:propstat><d:prop>
      <oc:name>note.md</oc:name><d:resourcetype/>
      <d:getcontentlength>10</d:getcontentlength>
      <d:getlastmodified>Mon, 10 Mar 2026 12:00:00 GMT</d:getlastmodified>
      <oc:score>0.9</oc:score>
    </d:prop></d:propstat>
  </d:response>
  <d:response>
    <d:href>/remote.php/dav/spaces/abc/Documents/report.pdf</d:href>
    <d:propstat><d:prop>
      <oc:name>report.pdf</oc:name><d:resourcetype/>
      <d:getcontentlength>20</d:getcontentlength>
      <d:getlastmodified>Mon, 10 Mar 2026 12:00:00 GMT</d:getlastmodified>
      <oc:score>0.8</oc:score>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""
        self._mock_207(xml=xml)
        result = search(query="note", path="/Doc")
        paths = [r["path"] for r in result]
        assert paths == ["/Doc/note.md"]

    def test_path_filter_matches_exact_folder(self):
        # path="/Doc" should also keep an item at exactly "/Doc" (the folder itself).
        xml = """\
<?xml version="1.0" encoding="utf-8"?>
<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
  <d:response>
    <d:href>/remote.php/dav/spaces/abc/Doc</d:href>
    <d:propstat><d:prop>
      <oc:name>Doc</oc:name>
      <d:resourcetype><d:collection/></d:resourcetype>
      <d:getlastmodified>Mon, 10 Mar 2026 12:00:00 GMT</d:getlastmodified>
      <oc:score>0.5</oc:score>
    </d:prop></d:propstat>
  </d:response>
</d:multistatus>"""
        self._mock_207(xml=xml)
        result = search(query="doc", path="/Doc")
        assert len(result) == 1
        assert result[0]["path"] == "/Doc"

    def test_path_filter_handles_trailing_slash(self):
        # path="/Documents/" must behave identically to path="/Documents".
        self._mock_207()
        result = search(query="budget", path="/Documents/")
        assert len(result) == 1
        assert result[0]["path"] == "/Documents/report.pdf"

    def test_empty_params_returns_error(self):
        result = search()
        assert isinstance(result, str)
        assert "Error" in result
        assert "At least one" in result

    def test_path_alone_not_sufficient(self):
        result = search(path="/Documents")
        assert "Error" in result
        assert "At least one" in result

    def test_handles_non_207(self):
        resp = MagicMock()
        resp.status_code = 500
        self.mock_request.return_value = resp
        result = search(query="test")
        assert "Error" in result
        assert "500" in result

    def test_handles_auth_error(self):
        resp = MagicMock()
        resp.status_code = 401
        self.mock_request.return_value = resp
        result = search(query="test")
        assert "Authentication" in result

    def test_handles_501(self):
        resp = MagicMock()
        resp.status_code = 501
        self.mock_request.return_value = resp
        result = search(query="test")
        assert "not available" in result

    def test_parses_directories(self):
        self._mock_207(xml=_SAMPLE_DIR_207)
        result = search(query="Projects")
        assert len(result) == 1
        assert result[0]["type"] == "directory"

    def test_respects_limit(self):
        self._mock_207()
        search(query="budget", limit=300)
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "<oc:limit>200</oc:limit>" in body

    def test_respects_limit_minimum(self):
        self._mock_207()
        search(query="budget", limit=0)
        body = self.mock_request.call_args.kwargs.get("content", "")
        assert "<oc:limit>1</oc:limit>" in body


class TestBuildKql:
    def test_single_term_matches_name_or_content(self):
        assert _build_kql("budget", "", "", "", "") == "(name:*budget* OR content:budget)"

    def test_multi_word_terms_ored_and_grouped(self):
        assert _build_kql("quarterly budget", "", "", "", "") == (
            "((name:*quarterly* OR content:quarterly) OR (name:*budget* OR content:budget))"
        )

    def test_three_word_terms_ored_and_grouped(self):
        result = _build_kql("q4 financial report", "", "", "", "")
        assert result == (
            "((name:*q4* OR content:q4) OR (name:*financial* OR content:financial) "
            "OR (name:*report* OR content:report))"
        )

    def test_single_name(self):
        assert _build_kql("", "*.pdf", "", "", "") == "name:*.pdf"

    def test_modified_after(self):
        assert _build_kql("", "", "", "2026-01-01", "") == "mtime>=2026-01-01"

    def test_modified_before(self):
        assert _build_kql("", "", "", "", "2026-12-31") == "mtime<=2026-12-31"

    def test_combined(self):
        result = _build_kql("report", "*.pdf", "document", "2026-01-01", "2026-12-31")
        assert result == (
            "(name:*report* OR content:report) name:*.pdf mediatype:document "
            "mtime>=2026-01-01 mtime<=2026-12-31"
        )

    def test_empty(self):
        assert _build_kql("", "", "", "", "") == ""


class TestParseSearchResponse:
    def test_parses_files(self):
        results = _parse_search_response(_SAMPLE_207)
        assert len(results) == 2
        assert results[0]["name"] == "report.pdf"
        assert results[0]["size"] == 204800
        assert results[0]["created"] == "2026-01-02T09:30:00Z"
        assert results[1]["created"] == ""
        assert results[0]["type"] == "file"

    def test_parses_directory(self):
        results = _parse_search_response(_SAMPLE_DIR_207)
        assert len(results) == 1
        assert results[0]["type"] == "directory"

    def test_empty_response(self):
        results = _parse_search_response('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"/>')
        assert results == []
