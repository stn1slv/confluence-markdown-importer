"""Import service: push locally edited pages to Confluence with conflict protection."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Protocol

from pydantic import BaseModel, Field

from confluence_markdown_importer.converter import PageTarget, convert_markdown
from confluence_markdown_importer.planner import hash_bytes
from confluence_markdown_importer.state import ImportState, PageState

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from confluence_markdown_exporter.utils.lockfile import ConfluenceLock

    from confluence_markdown_importer.planner import ChangePlan, PageUpdate

logger = logging.getLogger(__name__)

VERSION_COMMENT = "Updated by confluence-markdown-importer"


class ImporterError(Exception):
    """Base error for import failures."""


class ConfluenceClient(Protocol):
    """The subset of the atlassian.Confluence API the importer uses."""

    def get_page_by_id(self, page_id: str, expand: str | None = None) -> dict[str, Any]: ...
    def update_page(self, **kwargs: object) -> dict[str, Any]: ...
    def get_attachments_from_content(self, page_id: str, **kwargs: object) -> dict[str, Any]: ...


if TYPE_CHECKING:
    ClientFactory = Callable[[str], ConfluenceClient]


class Conflict(BaseModel):
    """A page skipped because it changed in Confluence since the baseline."""

    export_path: str
    baseline_version: int
    remote_version: int


class Failure(BaseModel):
    """A page that could not be imported."""

    export_path: str
    error: str


class ImportOutcome(BaseModel):
    """Result summary of an import run."""

    updated: list[str] = Field(default_factory=list)
    conflicts: list[Conflict] = Field(default_factory=list)
    failed: list[Failure] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _default_client_factory(url: str) -> ConfluenceClient:
    from confluence_markdown_exporter.api_clients import get_confluence_instance

    return get_confluence_instance(url)  # type: ignore[no-any-return]


class _AttachmentDirectory:
    """Resolves local attachment paths to remote attachment filenames via the lockfile and API."""

    def __init__(self, lock: ConfluenceLock, client_factory: ClientFactory) -> None:
        self._client_factory = client_factory
        self._titles_cache: dict[str, dict[str, str]] = {}
        # local path -> (org_url, owner page id, attachment id)
        self._by_path: dict[str, tuple[str, str, str]] = {
            entry.path: (org_url, page_id, attachment_id)
            for org_url, org in lock.orgs.items()
            for space in org.spaces.values()
            for page_id, page in space.pages.items()
            for attachment_id, entry in page.attachments.items()
        }

    def resolve(self, path: str) -> str | None:
        located = self._by_path.get(path)
        if located is None:
            return None
        org_url, owner_page_id, attachment_id = located
        return self._attachment_titles(org_url, owner_page_id).get(attachment_id)

    def _attachment_titles(self, org_url: str, page_id: str) -> dict[str, str]:
        if page_id not in self._titles_cache:
            client = self._client_factory(org_url)
            response = client.get_attachments_from_content(page_id, limit=250)
            results = response.get("results", []) if isinstance(response, dict) else []
            self._titles_cache[page_id] = {att["id"]: att["title"] for att in results if "id" in att}
        return self._titles_cache[page_id]


def run_import(
    root: Path,
    lock: ConfluenceLock,
    state: ImportState,
    plan: ChangePlan,
    *,
    dry_run: bool = False,
    force: bool = False,
    strip_title: bool = True,
    strip_breadcrumbs: bool = True,
    state_path: Path | None = None,
    client_factory: ClientFactory | None = None,
) -> ImportOutcome:
    """Push every update candidate in *plan* to Confluence.

    Conflicts (remote version differs from the baseline) are skipped unless *force*.
    In *dry_run* mode pages are converted and checked but nothing is written,
    neither remotely nor to the state file.
    """
    if client_factory is None:
        client_factory = _default_client_factory
    outcome = ImportOutcome()
    page_targets = {
        entry.export_path: PageTarget(entry.title, space_key)
        for org in lock.orgs.values()
        for space_key, space in org.spaces.items()
        for entry in space.pages.values()
    }
    attachments = _AttachmentDirectory(lock, client_factory)

    for candidate in plan.updates:
        try:
            updated = _import_page(
                root,
                candidate,
                state,
                outcome,
                page_targets=page_targets,
                attachments=attachments,
                client_factory=client_factory,
                dry_run=dry_run,
                force=force,
                strip_title=strip_title,
                strip_breadcrumbs=strip_breadcrumbs,
            )
        except Exception as e:
            logger.warning("Import failed for %s: %s", candidate.export_path, e)
            outcome.failed.append(Failure(export_path=candidate.export_path, error=str(e)))
            continue
        if updated and not dry_run and state_path is not None:
            state.save(state_path)
    return outcome


def _import_page(
    root: Path,
    candidate: PageUpdate,
    state: ImportState,
    outcome: ImportOutcome,
    *,
    page_targets: dict[str, PageTarget],
    attachments: _AttachmentDirectory,
    client_factory: ClientFactory,
    dry_run: bool,
    force: bool,
    strip_title: bool,
    strip_breadcrumbs: bool,
) -> bool:
    """Import one page. Returns True when the page and state were actually updated."""
    client = client_factory(candidate.org_url)
    remote = client.get_page_by_id(candidate.page_id, expand="version")
    remote_version = int(remote["version"]["number"])

    if remote_version != candidate.baseline_version and not force:
        outcome.conflicts.append(
            Conflict(
                export_path=candidate.export_path,
                baseline_version=candidate.baseline_version,
                remote_version=remote_version,
            )
        )
        return False

    file_path = root / candidate.export_path
    content_bytes = file_path.read_bytes()
    result = convert_markdown(
        content_bytes.decode("utf-8"),
        export_path=candidate.export_path,
        resolve_page=page_targets.get,
        resolve_attachment=attachments.resolve,
        strip_title=strip_title,
        strip_breadcrumbs=strip_breadcrumbs,
    )
    outcome.warnings.extend(f"{candidate.export_path}: {w}" for w in result.warnings)
    # Title changes are not synced in v1: exported H1 text can be a lossy rendering of
    # the real title, so renaming from it could corrupt titles. Keep the lockfile title.
    title = candidate.title
    if result.title is not None and result.title != candidate.title:
        outcome.warnings.append(
            f"{candidate.export_path}: H1 '{result.title}' differs from Confluence title "
            f"'{candidate.title}' — title updates are not supported, keeping the Confluence title"
        )

    if dry_run:
        outcome.updated.append(candidate.export_path)
        return False

    response = client.update_page(
        page_id=candidate.page_id,
        title=title,
        body=result.storage,
        representation="storage",
        always_update=True,
        version_comment=VERSION_COMMENT,
    )
    new_version = _version_from(response)
    if new_version is None:
        # The SDK can bail out and return None without performing the PUT; treating that as
        # success would record a version Confluence never reached and desync the baseline.
        raise ImporterError(
            f"update_page returned no usable result for page {candidate.page_id} "
            f"({candidate.export_path}); the page may not have been updated"
        )

    state.set_page(
        candidate.org_url,
        candidate.space_key,
        candidate.page_id,
        PageState(
            title=title,
            version=new_version,
            export_path=candidate.export_path,
            content_hash=hash_bytes(content_bytes),
        ),
    )
    outcome.updated.append(candidate.export_path)
    logger.info("Updated page %s (%s) to version %s", candidate.page_id, title, new_version)
    return True


def _version_from(response: object) -> int | None:
    if isinstance(response, dict):
        try:
            return int(response["version"]["number"])
        except (KeyError, TypeError, ValueError):
            return None
    return None
