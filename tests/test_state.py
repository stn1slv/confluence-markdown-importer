"""Tests for the import state file models and persistence."""

import json

from confluence_markdown_importer.state import ImportState, OrgState, PageState, SpaceState


def make_state() -> ImportState:
    page = PageState(
        title="Systems",
        version=5,
        export_path="Space/Home/Systems.md",
        content_hash="abc123",
    )
    return ImportState(orgs={"https://example.atlassian.net": OrgState(spaces={"CRI": SpaceState(pages={"42": page})})})


class TestRoundTrip:
    def test_save_and_load_round_trip(self, tmp_path):
        state = make_state()
        path = tmp_path / "confluence-import-state.json"

        state.save(path)
        loaded = ImportState.load(path)

        assert loaded.orgs == state.orgs
        assert loaded.last_baseline != ""

    def test_saved_file_is_valid_json_with_version(self, tmp_path):
        path = tmp_path / "confluence-import-state.json"
        make_state().save(path)

        data = json.loads(path.read_text(encoding="utf-8"))

        assert data["state_version"] == 1


class TestLoad:
    def test_load_missing_file_returns_empty_state(self, tmp_path):
        loaded = ImportState.load(tmp_path / "does-not-exist.json")

        assert loaded.orgs == {}

    def test_load_corrupt_file_returns_empty_state(self, tmp_path):
        path = tmp_path / "confluence-import-state.json"
        path.write_text("{not json", encoding="utf-8")

        loaded = ImportState.load(path)

        assert loaded.orgs == {}


class TestLookup:
    def test_get_page_finds_entry_across_orgs_and_spaces(self):
        state = make_state()

        entry = state.get_page("42")

        assert entry is not None
        assert entry.title == "Systems"

    def test_get_page_returns_none_for_unknown_id(self):
        assert make_state().get_page("999") is None

    def test_iter_pages_yields_full_coordinates(self):
        state = make_state()

        items = list(state.iter_pages())

        assert items == [("https://example.atlassian.net", "CRI", "42", state.get_page("42"))]

    def test_set_page_creates_nested_entries(self):
        state = ImportState()
        page = PageState(title="T", version=1, export_path="a.md", content_hash=None)

        state.set_page("https://example.atlassian.net", "CRI", "7", page)

        assert state.get_page("7") == page
