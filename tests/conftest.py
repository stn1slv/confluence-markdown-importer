"""Shared fixtures: a fake export tree with a lockfile, mirroring cme output."""

import os
import tempfile
from pathlib import Path

# Isolate tests from the developer's real cme configuration. Must be set before
# any confluence_markdown_exporter module resolves its config path at import time.
os.environ.setdefault("CME_CONFIG_PATH", str(Path(tempfile.mkdtemp()) / "app_data.json"))

import pytest
from confluence_markdown_exporter.utils.lockfile import ConfluenceLock, OrgEntry, PageEntry, SpaceEntry

ORG_URL = "https://example.atlassian.net"
SPACE_KEY = "CRI"


@pytest.fixture
def export_root(tmp_path) -> Path:
    """A directory that looks like a cme export root with two pages and a lockfile."""
    (tmp_path / "Space/Home").mkdir(parents=True)
    (tmp_path / "Space/Home/Systems.md").write_text("# Systems\n\nBody one.\n", encoding="utf-8")
    (tmp_path / "Space/Home/Topics.md").write_text("# Topics\n\nBody two.\n", encoding="utf-8")

    lock = ConfluenceLock(
        orgs={
            ORG_URL: OrgEntry(
                spaces={
                    SPACE_KEY: SpaceEntry(
                        pages={
                            "42": PageEntry(title="Systems", version=5, export_path="Space/Home/Systems.md"),
                            "43": PageEntry(title="Topics", version=2, export_path="Space/Home/Topics.md"),
                        }
                    )
                }
            )
        }
    )
    lock.save(tmp_path / "confluence-lock.json")
    return tmp_path


def make_lock(root: Path) -> ConfluenceLock:
    return ConfluenceLock.load(root / "confluence-lock.json")
