# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

Development uses `uv` and a Makefile (single entry point for all tasks):

```bash
make setup              # create venv and install deps (uv sync)
make test               # uv run pytest
make lint               # ruff check + ruff format --check + mypy (strict)
make format             # ruff format + ruff check --fix
make build              # uv build

uv run pytest tests/test_converter.py                       # one file
uv run pytest tests/test_converter.py::TestAlerts -k NOTE   # one class / match
uv run pytest --cov                                         # coverage (floor: 80%)
uv run cmi --help                                           # run the CLI locally
```

## What this tool is

`cmi` pushes locally edited markdown back to Confluence. It is the reverse of
`confluence-markdown-exporter` (`cme`), which is a **pip dependency, not vendored** — auth
config (`get_settings()`, honors `CME_CONFIG_PATH`/`CME_*` env), API clients
(`get_confluence_instance`), and the `confluence-lock.json` models (`ConfluenceLock`) are all
imported from `confluence_markdown_exporter.*`. Never reimplement those, and never write to
`confluence-lock.json` — it is owned by the exporter.

## Architecture

Strict presentation/service split (`src/confluence_markdown_importer/`):

- `main.py` — typer CLI (`cmi baseline`, `cmi import`). Printing and exit codes only; no logic.
- `state.py` — `confluence-import-state.json`: the importer-owned hash baseline, mirroring the
  lockfile's orgs → spaces → pages nesting. Atomic writes (temp file + replace).
- `planner.py` — pure functions comparing three sources of truth: lockfile (file ↔ page mapping),
  state file (baseline hashes/versions), and files on disk. Produces a `ChangePlan`
  (updates / unchanged / no_baseline / deleted_locally / untracked). All paths are POSIX strings
  relative to the export root, matching lockfile `export_path` values.
- `converter.py` — markdown → Confluence storage XHTML. Pipeline: strip preamble (frontmatter,
  breadcrumb line, H1) → markdown-it render → lenient lxml.html parse re-serialized as strict XML →
  element transforms (alerts, code macros, images **before** links so linked images survive as
  link bodies, mark/font spans) → serialize with `ac:`/`ri:` prefixes, xmlns declarations stripped →
  well-formedness validation (raises `ConversionError`). Link/attachment resolution is injected via
  callables (`PageResolver` returns `PageTarget(title, space_key)`), keeping the converter offline
  and unit-testable.
- `importer.py` — orchestrates one import run: per candidate page, fetch remote version → conflict
  check against the baseline version (skip-and-report unless `--force`) → convert → `update_page`
  (representation `storage`) → update the state entry with the new version and the hash of the
  bytes that were actually read. State is saved only after a real update. The Confluence client is
  a `Protocol` with an injectable `client_factory` (tests pass mocks; no patching needed).

## Invariants that are easy to break

- The converter's mappings are **exact inverses of the exporter's output** (alert map:
  IMPORTANT→info, NOTE→panel, TIP→tip, WARNING→note, CAUTION→warning; `<mark
  style="background: …">` ↔ highlight span; H1 titles arrive markdown-escaped). When changing a
  mapping, check the corresponding exporter code first.
- Title renames are deliberately unsupported: exported H1s can be lossy renderings of the real
  title (verified on real data), so the importer always keeps the lockfile title and only warns.
- The breadcrumb line is stripped only when directly followed by the H1 — a first line made of
  links can be genuine content.
- `AuthNotConfiguredError` from the exporter inherits `BaseException` on purpose so per-page
  `except Exception` handlers cannot swallow it; it must reach the `main()` boundary.
- `tests/conftest.py` sets `CME_CONFIG_PATH` to a temp path **before** any
  `confluence_markdown_exporter` import so tests never read the developer's real cme config.
  Keep that ordering.

## Verification beyond unit tests

`~/src/epam/ad-docs` is a real 949-page export used as smoke data. Copy it to a scratch dir
(never modify it in place), then `uv run cmi baseline <copy>` and, after editing a file,
`uv run cmi import <copy> --dry-run` (read-only, but hits the real Confluence API using the
developer's cme credentials). A converter change should also be mass-checked against all pages of
that export for XML validation failures before it ships.
