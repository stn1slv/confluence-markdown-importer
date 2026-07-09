# confluence-markdown-importer

Push locally edited markdown back to Confluence. The counterpart of
[confluence-markdown-exporter](https://github.com/Spenhouet/confluence-markdown-exporter) (`cme`):
you export a space to a git repository with `cme`, edit the markdown locally (for example with an
AI coding agent, reviewed in a git branch), and then update the Confluence pages with `cmi`.

## How it works

- **Shared configuration.** `cmi` reads the same config file as `cme` (including `CME_CONFIG_PATH`
  and `CME_*` environment overrides). If `cme` is already set up, no extra credential setup is
  needed. To configure credentials, run `cme config edit auth.confluence`.
- **Shared page mapping.** The exporter's `confluence-lock.json` maps local files to Confluence
  pages (page id, space, version). `cmi` never writes that file.
- **Own baseline state.** `cmi baseline` records a sha256 hash of every tracked file in
  `confluence-import-state.json` next to the lockfile. `cmi import` pushes only files whose hash
  changed since the baseline. Commit this file to git together with `confluence-lock.json` so
  the baseline travels with the repository and every collaborator compares against the same
  clean state.
- **Conflict protection.** Before updating a page, `cmi` compares the current Confluence version
  with the baseline version. If someone changed the page in Confluence since your export, the page
  is skipped and reported (override with `--force`).

## Workflow

```bash
# 1. Export (cme) and snapshot the baseline (cmi)
cme space https://company.atlassian.net/wiki/spaces/KEY
cmi baseline
git add -A && git commit -m "docs: sync from Confluence"

# 2. Edit markdown locally, review in a branch

# 3. Preview and push
cmi import --dry-run
cmi import

# 4. Re-export to normalize local files to Confluence's canonical rendering,
#    then snapshot the new baseline
cme space https://company.atlassian.net/wiki/spaces/KEY
cmi baseline
```

Both commands take the export root directory as an optional argument (default: current directory).

## Commands

| Command | Purpose |
| --- | --- |
| `cmi baseline [DIR]` | Record the current files as the clean baseline. Run right after each export. |
| `cmi import [DIR] --dry-run` | Show what would be pushed, including conversion warnings. Read-only. |
| `cmi import [DIR]` | Update changed pages in Confluence. |
| `cmi import [DIR] --force` | Also push pages that changed in Confluence since the baseline. |

## What gets converted

Exported markdown is converted back to Confluence storage format:

- Headings, paragraphs, emphasis, lists, tables, blockquotes.
- Fenced code blocks → Confluence code macro (with language).
- GitHub alerts (`> [!NOTE]` etc.) → Confluence panel macros (exact inverse of the exporter's mapping).
- Relative links to exported `.md` files → Confluence page links (resolved through the lockfile).
- Attachment images → `ri:attachment` references (filenames resolved through the Confluence API).
- `<mark>` / `<font>` spans → Confluence highlight / color spans.
- The YAML frontmatter, breadcrumb line, and H1 title added by the exporter are stripped
  (honoring the corresponding `cme` export settings).

Every generated page body is validated as well-formed XML before it is sent.

## Scope and limitations (v1)

- **Update existing pages only.** New local files, locally deleted files, and files without a
  baseline are reported but not synced.
- **No title renames.** The exported H1 can be a lossy rendering of the real page title, so `cmi`
  keeps the Confluence title and warns when the H1 differs.
- **Lossy for dynamic macros.** The exporter renders macros (include, page properties reports,
  TOC, Jira links) into static markdown. Importing such a page replaces the live macro with that
  static rendering. Pages with plain text, tables, code, alerts, and links round-trip cleanly.
- **Unresolvable links degrade to text.** Links to pages outside the exported space (not in the
  lockfile) and links to local attachment files are kept as plain text and reported as warnings.
- **Requires relative href exports.** `cmi import` refuses to run when the cme config uses
  `export.page_href` or `export.attachment_href` other than `relative` (the default), because
  absolute and wiki-style links cannot be converted back.
- No attachment upload and no comment sync.

## Development

```bash
make setup    # create the environment (uv)
make test     # pytest
make lint     # ruff + mypy
make format   # autoformat
make build    # build wheel/sdist
```

## License

MIT
