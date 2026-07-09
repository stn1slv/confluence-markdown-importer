"""Import state file: the hash baseline the importer compares local files against.

The file (``confluence-import-state.json``) mirrors the org/space/page nesting of the
exporter's ``confluence-lock.json`` but is owned exclusively by the importer.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)

STATE_VERSION = 1
STATE_FILE_NAME = "confluence-import-state.json"


class PageState(BaseModel):
    """Baseline entry for a single page."""

    title: str
    version: int
    export_path: str
    content_hash: str | None = None


class SpaceState(BaseModel):
    """Baseline entries for a Confluence space."""

    pages: dict[str, PageState] = Field(default_factory=dict)


class OrgState(BaseModel):
    """Baseline entries for a Confluence organisation (base URL)."""

    spaces: dict[str, SpaceState] = Field(default_factory=dict)


class ImportState(BaseModel):
    """Baseline state tracking the last known clean content of exported pages."""

    state_version: int = Field(default=STATE_VERSION)
    last_baseline: str = Field(default="")
    orgs: dict[str, OrgState] = Field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> ImportState:
        """Load the state file from disk, returning an empty state if missing or invalid."""
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                return cls.model_validate(data)
            except (ValidationError, json.JSONDecodeError):
                logger.warning("Failed to parse import state file %s. Starting fresh.", path)
        return cls()

    def save(self, path: Path) -> None:
        """Save the state to disk atomically (write to temp file, then replace)."""
        path.parent.mkdir(parents=True, exist_ok=True)
        for org in self.orgs.values():
            for space in org.spaces.values():
                space.pages = dict(sorted(space.pages.items()))
            org.spaces = dict(sorted(org.spaces.items()))
        self.orgs = dict(sorted(self.orgs.items()))
        self.last_baseline = datetime.now(tz=UTC).isoformat()

        json_str = json.dumps(self.model_dump(), indent=2, ensure_ascii=False)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
            ) as fd:
                tmp_path = Path(fd.name)
                fd.write(json_str)
            tmp_path.replace(path)
        except BaseException:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            raise

    def get_page(self, page_id: str) -> PageState | None:
        """Return the PageState for *page_id*, searching all orgs and spaces."""
        for org in self.orgs.values():
            for space in org.spaces.values():
                if page_id in space.pages:
                    return space.pages[page_id]
        return None

    def set_page(self, org_url: str, space_key: str, page_id: str, page: PageState) -> None:
        """Add or replace the entry for *page_id* under its org and space."""
        org = self.orgs.setdefault(org_url, OrgState())
        space = org.spaces.setdefault(space_key, SpaceState())
        space.pages[page_id] = page

    def iter_pages(self) -> Iterator[tuple[str, str, str, PageState]]:
        """Yield (org_url, space_key, page_id, page_state) for every tracked page."""
        for org_url, org in self.orgs.items():
            for space_key, space in org.spaces.items():
                for page_id, page in space.pages.items():
                    yield org_url, space_key, page_id, page
