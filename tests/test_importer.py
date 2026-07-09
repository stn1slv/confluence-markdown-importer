"""Tests for the import service (conflict handling and page updates)."""

from unittest.mock import MagicMock

import pytest
from conftest import make_lock

from confluence_markdown_importer.importer import run_import
from confluence_markdown_importer.planner import build_baseline, plan_changes


@pytest.fixture
def confluence():
    client = MagicMock()
    client.get_page_by_id.return_value = {"version": {"number": 5}}
    client.update_page.return_value = {"version": {"number": 6}}
    client.get_attachments_from_content.return_value = {"results": []}
    return client


def prepare(export_root, new_body: str):
    """Baseline the fixture tree, then edit Systems.md (page 42, version 5)."""
    state, _ = build_baseline(export_root, make_lock(export_root))
    (export_root / "Space/Home/Systems.md").write_text(new_body, encoding="utf-8")
    return state


def do_import(export_root, state, confluence, **kwargs):
    lock = make_lock(export_root)
    plan = plan_changes(export_root, lock, state)
    return run_import(
        export_root,
        lock,
        state,
        plan,
        client_factory=lambda _url: confluence,
        **kwargs,
    )


class TestUpdate:
    def test_updates_page_and_state_when_remote_matches_baseline(self, export_root, confluence):
        state = prepare(export_root, "# Systems\n\nEdited body.\n")

        outcome = do_import(export_root, state, confluence)

        assert outcome.updated == ["Space/Home/Systems.md"]
        confluence.update_page.assert_called_once()
        kwargs = confluence.update_page.call_args.kwargs
        assert kwargs["page_id"] == "42"
        assert kwargs["title"] == "Systems"
        assert "<p>Edited body.</p>" in kwargs["body"]
        assert kwargs["representation"] == "storage"
        entry = state.get_page("42")
        assert entry.version == 6
        assert plan_changes(export_root, make_lock(export_root), state).updates == []

    def test_changed_h1_keeps_confluence_title_and_warns(self, export_root, confluence):
        state = prepare(export_root, "# Renamed Systems\n\nBody.\n")

        outcome = do_import(export_root, state, confluence)

        assert confluence.update_page.call_args.kwargs["title"] == "Systems"
        assert state.get_page("42").title == "Systems"
        assert any("Renamed Systems" in w for w in outcome.warnings)

    def test_relative_links_resolve_against_lockfile_titles(self, export_root, confluence):
        state = prepare(export_root, "# Systems\n\nSee [topics](Topics.md).\n")

        do_import(export_root, state, confluence)

        body = confluence.update_page.call_args.kwargs["body"]
        assert '<ri:page ri:content-title="Topics"' in body
        assert 'ri:space-key="CRI"' in body


class TestConflicts:
    def test_remote_version_mismatch_skips_and_reports(self, export_root, confluence):
        state = prepare(export_root, "# Systems\n\nEdited.\n")
        confluence.get_page_by_id.return_value = {"version": {"number": 7}}

        outcome = do_import(export_root, state, confluence)

        assert outcome.updated == []
        assert len(outcome.conflicts) == 1
        assert outcome.conflicts[0].export_path == "Space/Home/Systems.md"
        assert outcome.conflicts[0].baseline_version == 5
        assert outcome.conflicts[0].remote_version == 7
        confluence.update_page.assert_not_called()

    def test_conflict_only_run_does_not_write_state_file(self, export_root, confluence):
        state = prepare(export_root, "# Systems\n\nEdited.\n")
        confluence.get_page_by_id.return_value = {"version": {"number": 7}}
        state_path = export_root / "confluence-import-state.json"

        do_import(export_root, state, confluence, state_path=state_path)

        assert not state_path.exists()

    def test_force_overrides_conflict(self, export_root, confluence):
        state = prepare(export_root, "# Systems\n\nEdited.\n")
        confluence.get_page_by_id.return_value = {"version": {"number": 7}}
        confluence.update_page.return_value = {"version": {"number": 8}}

        outcome = do_import(export_root, state, confluence, force=True)

        assert outcome.updated == ["Space/Home/Systems.md"]
        assert state.get_page("42").version == 8


class TestDryRun:
    def test_dry_run_converts_but_does_not_update(self, export_root, confluence):
        state = prepare(export_root, "# Systems\n\nEdited.\n")
        before = state.get_page("42").model_copy()

        outcome = do_import(export_root, state, confluence, dry_run=True)

        assert outcome.updated == ["Space/Home/Systems.md"]
        confluence.update_page.assert_not_called()
        assert state.get_page("42") == before


class TestFailures:
    def test_api_error_on_one_page_does_not_block_others(self, export_root, confluence):
        state, _ = build_baseline(export_root, make_lock(export_root))
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited A.\n", encoding="utf-8")
        (export_root / "Space/Home/Topics.md").write_text("# Topics\n\nEdited B.\n", encoding="utf-8")
        confluence.get_page_by_id.side_effect = [
            {"version": {"number": 5}},
            {"version": {"number": 2}},
        ]
        confluence.update_page.side_effect = [RuntimeError("boom"), {"version": {"number": 3}}]

        outcome = do_import(export_root, state, confluence)

        assert len(outcome.failed) == 1
        assert outcome.failed[0].export_path == "Space/Home/Systems.md"
        assert outcome.updated == ["Space/Home/Topics.md"]
