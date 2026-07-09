"""Tests for the baseline builder and the change planner."""

import hashlib

from conftest import make_lock

from confluence_markdown_importer.planner import build_baseline, plan_changes
from confluence_markdown_importer.state import ImportState


class TestBuildBaseline:
    def test_records_hash_version_and_title_for_each_tracked_page(self, export_root):
        state, report = build_baseline(export_root, make_lock(export_root))

        entry = state.get_page("42")
        expected = hashlib.sha256((export_root / "Space/Home/Systems.md").read_bytes()).hexdigest()
        assert entry is not None
        assert entry.content_hash == expected
        assert entry.version == 5
        assert entry.title == "Systems"
        assert entry.export_path == "Space/Home/Systems.md"
        assert report.recorded == 2
        assert report.missing == []

    def test_missing_file_recorded_with_none_hash_and_reported(self, export_root):
        (export_root / "Space/Home/Topics.md").unlink()

        state, report = build_baseline(export_root, make_lock(export_root))

        entry = state.get_page("43")
        assert entry is not None
        assert entry.content_hash is None
        assert report.missing == ["Space/Home/Topics.md"]


class TestPlanChanges:
    def baseline(self, export_root) -> ImportState:
        state, _ = build_baseline(export_root, make_lock(export_root))
        return state

    def test_unchanged_files_are_not_candidates(self, export_root):
        state = self.baseline(export_root)

        plan = plan_changes(export_root, make_lock(export_root), state)

        assert plan.updates == []
        assert plan.unchanged == 2

    def test_edited_file_becomes_update_candidate(self, export_root):
        state = self.baseline(export_root)
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited.\n", encoding="utf-8")

        plan = plan_changes(export_root, make_lock(export_root), state)

        assert [c.page_id for c in plan.updates] == ["42"]
        candidate = plan.updates[0]
        assert candidate.org_url == "https://example.atlassian.net"
        assert candidate.space_key == "CRI"
        assert candidate.title == "Systems"
        assert candidate.baseline_version == 5
        assert candidate.export_path == "Space/Home/Systems.md"

    def test_page_without_baseline_entry_is_reported(self, export_root):
        state = ImportState()  # baseline never run
        (export_root / "Space/Home/Systems.md").write_text("edited", encoding="utf-8")

        plan = plan_changes(export_root, make_lock(export_root), state)

        assert plan.updates == []
        assert sorted(plan.no_baseline) == ["Space/Home/Systems.md", "Space/Home/Topics.md"]

    def test_locally_deleted_file_is_reported(self, export_root):
        state = self.baseline(export_root)
        (export_root / "Space/Home/Systems.md").unlink()

        plan = plan_changes(export_root, make_lock(export_root), state)

        assert plan.deleted_locally == ["Space/Home/Systems.md"]
        assert plan.updates == []

    def test_untracked_markdown_file_is_reported_but_sidecars_ignored(self, export_root):
        state = self.baseline(export_root)
        (export_root / "Space/Home/New Page.md").write_text("# New\n", encoding="utf-8")
        (export_root / "Space/Home/Systems.comments.md").write_text("comments", encoding="utf-8")

        plan = plan_changes(export_root, make_lock(export_root), state)

        assert plan.untracked == ["Space/Home/New Page.md"]
