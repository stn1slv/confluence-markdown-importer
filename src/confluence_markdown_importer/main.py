"""cmi CLI: push locally edited markdown (exported with cme) back to Confluence.

Presentation layer only — all logic lives in the service modules. Authentication
and connection settings are read from the confluence-markdown-exporter (cme)
config file, so no separate credential setup is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from confluence_markdown_exporter.utils.app_data_store import get_settings
from confluence_markdown_exporter.utils.lockfile import ConfluenceLock

from confluence_markdown_importer.importer import run_import
from confluence_markdown_importer.planner import ChangePlan, build_baseline, plan_changes
from confluence_markdown_importer.state import STATE_FILE_NAME, ImportState

logger = logging.getLogger(__name__)

app = typer.Typer(
    help=(
        "Import locally edited markdown back to Confluence.\n\n"
        "Works on a directory exported with confluence-markdown-exporter (cme): the "
        "exporter's confluence-lock.json maps files to pages, and credentials are read "
        "from the cme configuration (see `cme config path`).\n\n"
        "Typical workflow: `cme space <URL>` → `cmi baseline` → edit markdown → "
        "`cmi import --dry-run` → `cmi import`."
    ),
    no_args_is_help=True,
)

DirectoryArg = Annotated[
    Path,
    typer.Argument(
        help="Export root directory (where confluence-lock.json lives).",
        exists=True,
        file_okay=False,
    ),
]


def _setup_logging() -> None:
    level = get_settings().export.log_level
    logging.basicConfig(level=level, format="%(levelname)s %(name)s: %(message)s")


def _load_lock(directory: Path) -> ConfluenceLock:
    lockfile_name = get_settings().export.lockfile_name
    lockfile_path = directory / lockfile_name
    if not lockfile_path.exists():
        typer.echo(f"Error: no {lockfile_name} found in {directory}. Run a cme export first.", err=True)
        raise typer.Exit(code=1)
    return ConfluenceLock.load(lockfile_path)


@app.command()
def baseline(directory: DirectoryArg = Path()) -> None:
    """Snapshot the current files as the clean baseline (run right after each export)."""
    _setup_logging()
    lock = _load_lock(directory)
    state, report = build_baseline(directory, lock)
    state.save(directory / STATE_FILE_NAME)
    typer.echo(f"Baseline recorded for {report.recorded} page(s) in {directory / STATE_FILE_NAME}")
    for path in report.missing:
        typer.echo(f"  missing on disk (no hash recorded): {path}")


@app.command(name="import")
def import_(
    directory: DirectoryArg = Path(),
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Convert and check pages, but do not write anything.")
    ] = False,
    force: Annotated[
        bool, typer.Option("--force", help="Push even when the page changed in Confluence since the baseline.")
    ] = False,
) -> None:
    """Push locally edited pages back to Confluence (update existing pages only)."""
    _setup_logging()
    settings = get_settings()
    if settings.export.page_href != "relative" or settings.export.attachment_href != "relative":
        typer.echo(
            "Error: cmi only supports exports created with export.page_href=relative and "
            f"export.attachment_href=relative (current: page_href={settings.export.page_href}, "
            f"attachment_href={settings.export.attachment_href}). Links and images exported in "
            "other styles cannot be converted back and would be silently corrupted.",
            err=True,
        )
        raise typer.Exit(code=1)
    lock = _load_lock(directory)
    state = ImportState.load(directory / STATE_FILE_NAME)

    plan = plan_changes(directory, lock, state)
    _echo_plan(plan)
    if not plan.updates:
        typer.echo("Nothing to import.")
        return

    outcome = run_import(
        directory,
        lock,
        state,
        plan,
        dry_run=dry_run,
        force=force,
        strip_title=settings.export.include_document_title,
        strip_breadcrumbs=settings.export.page_breadcrumbs,
        state_path=directory / STATE_FILE_NAME,
    )

    for warning in outcome.warnings:
        typer.echo(f"  warning: {warning}")
    verb = "Would update" if dry_run else "Updated"
    for path in outcome.updated:
        typer.echo(f"{verb}: {path}")
    for conflict in outcome.conflicts:
        typer.echo(
            f"Conflict (skipped): {conflict.export_path} — Confluence is at version "
            f"{conflict.remote_version}, baseline is {conflict.baseline_version}. "
            "Re-export and re-apply your change, or use --force."
        )
    for failure in outcome.failed:
        typer.echo(f"Failed: {failure.export_path} — {failure.error}", err=True)

    typer.echo(
        f"Done: {len(outcome.updated)} {'candidate(s)' if dry_run else 'updated'}, "
        f"{len(outcome.conflicts)} conflict(s), {len(outcome.failed)} failed."
    )
    if outcome.failed:
        raise typer.Exit(code=1)


def _echo_plan(plan: ChangePlan) -> None:
    typer.echo(f"Unchanged: {plan.unchanged} page(s)")
    for path in plan.no_baseline:
        typer.echo(f"No baseline (run `cmi baseline` after export): {path}")
    for path in plan.deleted_locally:
        typer.echo(f"Deleted locally (deletion sync not supported): {path}")
    for path in plan.untracked:
        typer.echo(f"New local file (page creation not supported): {path}")


def main() -> None:  # pragma: no cover
    """Entry point wrapper that reports auth errors cleanly."""
    from confluence_markdown_exporter.api_clients import AuthNotConfiguredError

    try:
        app()
    except AuthNotConfiguredError as e:
        typer.echo(f"Error: {e}. Configure credentials with `cme config edit auth.confluence`.", err=True)
        raise SystemExit(1) from e


if __name__ == "__main__":  # pragma: no cover
    main()
