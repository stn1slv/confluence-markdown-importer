"""Tests for the cmi CLI."""

from unittest.mock import MagicMock

import pytest
from typer.testing import CliRunner

from confluence_markdown_importer import importer
from confluence_markdown_importer.main import app
from confluence_markdown_importer.state import STATE_FILE_NAME, ImportState

runner = CliRunner()


@pytest.fixture
def confluence(monkeypatch):
    client = MagicMock()
    client.get_page_by_id.return_value = {"version": {"number": 5}}
    client.update_page.return_value = {"version": {"number": 6}}
    client.get_attachments_from_content.return_value = {"results": []}
    monkeypatch.setattr(importer, "_default_client_factory", lambda _url: client)
    return client


class TestBaseline:
    def test_creates_state_file_and_reports_count(self, export_root):
        result = runner.invoke(app, ["baseline", str(export_root)])

        assert result.exit_code == 0
        assert (export_root / STATE_FILE_NAME).exists()
        assert "2" in result.output

    def test_fails_without_lockfile(self, tmp_path):
        result = runner.invoke(app, ["baseline", str(tmp_path)])

        assert result.exit_code != 0
        assert "confluence-lock.json" in result.output


class TestImport:
    def test_dry_run_reports_but_does_not_push(self, export_root, confluence):
        runner.invoke(app, ["baseline", str(export_root)])
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited.\n", encoding="utf-8")

        result = runner.invoke(app, ["import", str(export_root), "--dry-run"])

        assert result.exit_code == 0
        assert "Systems.md" in result.output
        confluence.update_page.assert_not_called()

    def test_import_pushes_and_persists_state(self, export_root, confluence):
        runner.invoke(app, ["baseline", str(export_root)])
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited.\n", encoding="utf-8")

        result = runner.invoke(app, ["import", str(export_root)])

        assert result.exit_code == 0
        confluence.update_page.assert_called_once()
        state = ImportState.load(export_root / STATE_FILE_NAME)
        assert state.get_page("42").version == 6

    def test_conflict_is_reported(self, export_root, confluence):
        runner.invoke(app, ["baseline", str(export_root)])
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited.\n", encoding="utf-8")
        confluence.get_page_by_id.return_value = {"version": {"number": 9}}

        result = runner.invoke(app, ["import", str(export_root)])

        assert result.exit_code == 0
        assert "conflict" in result.output.lower()
        confluence.update_page.assert_not_called()

    def test_missing_baseline_prints_hint(self, export_root, confluence):
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited.\n", encoding="utf-8")

        result = runner.invoke(app, ["import", str(export_root)])

        assert result.exit_code == 0
        assert "cmi baseline" in result.output
        confluence.update_page.assert_not_called()

    def test_nothing_to_do_reports_clean(self, export_root, confluence):
        runner.invoke(app, ["baseline", str(export_root)])

        result = runner.invoke(app, ["import", str(export_root)])

        assert result.exit_code == 0
        confluence.update_page.assert_not_called()

    @pytest.mark.parametrize("env_key", ["CME_EXPORT__PAGE_HREF", "CME_EXPORT__ATTACHMENT_HREF"])
    def test_non_relative_href_config_refuses_import(self, export_root, confluence, monkeypatch, env_key):
        monkeypatch.setenv(env_key, "absolute")
        runner.invoke(app, ["baseline", str(export_root)])
        (export_root / "Space/Home/Systems.md").write_text("# Systems\n\nEdited.\n", encoding="utf-8")

        result = runner.invoke(app, ["import", str(export_root)])

        assert result.exit_code != 0
        assert "relative" in result.output
        confluence.update_page.assert_not_called()
