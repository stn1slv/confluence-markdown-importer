"""Baseline building and change planning services.

The planner compares three sources of truth:
- the exporter lockfile (``confluence-lock.json``): which files map to which pages,
- the import state file: the hash baseline recorded by ``cmi baseline``,
- the files on disk.
"""

from __future__ import annotations

import hashlib
import logging
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from confluence_markdown_importer.state import ImportState, PageState

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from confluence_markdown_exporter.utils.lockfile import ConfluenceLock, PageEntry

logger = logging.getLogger(__name__)

COMMENTS_SIDECAR_SUFFIX = ".comments.md"


class BaselineReport(BaseModel):
    """Result summary of a baseline run."""

    recorded: int = 0
    missing: list[str] = Field(default_factory=list)


class PageUpdate(BaseModel):
    """A locally edited page that is a candidate for import."""

    org_url: str
    space_key: str
    page_id: str
    title: str
    baseline_version: int
    export_path: str


class ChangePlan(BaseModel):
    """The full change set computed for an import run."""

    updates: list[PageUpdate] = Field(default_factory=list)
    unchanged: int = 0
    no_baseline: list[str] = Field(default_factory=list)
    deleted_locally: list[str] = Field(default_factory=list)
    untracked: list[str] = Field(default_factory=list)


def hash_bytes(content: bytes) -> str:
    """Return the sha256 hex digest of *content*."""
    return hashlib.sha256(content).hexdigest()


def hash_file(path: Path) -> str:
    """Return the sha256 hex digest of the file's raw bytes."""
    return hash_bytes(path.read_bytes())


def _iter_lock_pages(lock: ConfluenceLock) -> Iterator[tuple[str, str, str, PageEntry]]:
    for org_url, org in lock.orgs.items():
        for space_key, space in org.spaces.items():
            for page_id, entry in space.pages.items():
                yield org_url, space_key, page_id, entry


def build_baseline(root: Path, lock: ConfluenceLock) -> tuple[ImportState, BaselineReport]:
    """Snapshot the current content of every lockfile-tracked file as the clean baseline.

    Builds a fresh state (entries for pages no longer in the lockfile are dropped).
    Files missing on disk are recorded with ``content_hash=None`` and reported.
    """
    state = ImportState()
    report = BaselineReport()
    for org_url, space_key, page_id, entry in _iter_lock_pages(lock):
        file_path = root / entry.export_path
        content_hash = hash_file(file_path) if file_path.exists() else None
        if content_hash is None:
            report.missing.append(entry.export_path)
        else:
            report.recorded += 1
        state.set_page(
            org_url,
            space_key,
            page_id,
            PageState(
                title=entry.title,
                version=entry.version,
                export_path=entry.export_path,
                content_hash=content_hash,
            ),
        )
    return state, report


def plan_changes(root: Path, lock: ConfluenceLock, state: ImportState) -> ChangePlan:
    """Compute the change set for an import run."""
    plan = ChangePlan()
    tracked_paths: set[str] = set()

    for org_url, space_key, page_id, entry in _iter_lock_pages(lock):
        tracked_paths.add(entry.export_path)
        file_path = root / entry.export_path
        if not file_path.exists():
            plan.deleted_locally.append(entry.export_path)
            continue

        baseline = state.get_page(page_id)
        if baseline is None or baseline.content_hash is None:
            plan.no_baseline.append(entry.export_path)
            continue

        if hash_file(file_path) == baseline.content_hash:
            plan.unchanged += 1
            continue

        plan.updates.append(_to_update(org_url, space_key, page_id, entry, baseline))

    plan.untracked = sorted(
        p.relative_to(root).as_posix()
        for p in root.rglob("*.md")
        if not p.name.endswith(COMMENTS_SIDECAR_SUFFIX)
        and not any(part.startswith(".") for part in p.relative_to(root).parts)
        and p.relative_to(root).as_posix() not in tracked_paths
    )
    plan.no_baseline.sort()
    plan.deleted_locally.sort()
    return plan


def _to_update(org_url: str, space_key: str, page_id: str, entry: PageEntry, baseline: PageState) -> PageUpdate:
    return PageUpdate(
        org_url=org_url,
        space_key=space_key,
        page_id=page_id,
        title=entry.title,
        baseline_version=baseline.version,
        export_path=entry.export_path,
    )
