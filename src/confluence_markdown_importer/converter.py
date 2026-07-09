"""Markdown → Confluence storage format (XHTML) conversion.

Inverts the transformations of confluence-markdown-exporter for the constructs it
emits: YAML frontmatter, breadcrumbs, H1 title, GitHub alerts, fenced code blocks,
relative page links, attachment images, and ``<mark>``/``<font>`` inline spans.
"""

from __future__ import annotations

import logging
import posixpath
import re
from collections.abc import Callable
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

import lxml.html
from lxml import etree
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field


class PageTarget(NamedTuple):
    """A Confluence page a local .md path resolves to."""

    title: str
    space_key: str | None = None


logger = logging.getLogger(__name__)

AC_NS = "http://www.atlassian.com/schema/confluence/4/ac/"
RI_NS = "http://www.atlassian.com/schema/confluence/4/ri/"
NSMAP = {"ac": AC_NS, "ri": RI_NS}

# Inverse of the exporter's alert_type_map (Confluence macro -> GitHub alert).
ALERT_TO_MACRO = {
    "IMPORTANT": "info",
    "NOTE": "panel",
    "TIP": "tip",
    "WARNING": "note",
    "CAUTION": "warning",
}

_FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)
_BREADCRUMB_RE = re.compile(r"^\[[^\]]*\]\([^)]*\)(?:\s*>\s*\[[^\]]*\]\([^)]*\))*\s*$")
_H1_RE = re.compile(r"^#\s+(.+?)\s*$")
_ALERT_MARKER_RE = re.compile(r"\A\[!(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\]\s*")

PageResolver = Callable[[str], PageTarget | None]
AttachmentResolver = Callable[[str], str | None]


class ConversionError(Exception):
    """Raised when the produced storage body is not well-formed."""


class ConversionResult(BaseModel):
    """Outcome of converting one markdown file."""

    title: str | None = None
    storage: str
    warnings: list[str] = Field(default_factory=list)


def _ac(name: str) -> etree.QName:
    return etree.QName(AC_NS, name)


def _ri(name: str) -> etree.QName:
    return etree.QName(RI_NS, name)


def _markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": True}).enable(["table", "strikethrough"])


def convert_markdown(
    text: str,
    *,
    export_path: str,
    resolve_page: PageResolver,
    resolve_attachment: AttachmentResolver,
    strip_title: bool = True,
    strip_breadcrumbs: bool = True,
) -> ConversionResult:
    """Convert exported markdown back to Confluence storage format."""
    warnings: list[str] = []
    body_md, title = _strip_preamble(text, strip_title=strip_title, strip_breadcrumbs=strip_breadcrumbs)

    html = _markdown_parser().render(body_md)
    root = _to_xml_tree(html)

    _convert_inline_spans(root)
    _convert_alerts(root)
    _convert_code_blocks(root)
    # Images first, so an image wrapped in a page link survives as the link body.
    _convert_images(root, export_path, resolve_attachment, warnings)
    _convert_links(root, export_path, resolve_page, warnings)

    storage = _serialize(root)
    _validate(storage)
    return ConversionResult(title=title, storage=storage, warnings=warnings)


def _strip_preamble(text: str, *, strip_title: bool, strip_breadcrumbs: bool) -> tuple[str, str | None]:
    """Remove frontmatter, the breadcrumb line, and the H1 title line."""
    text = _FRONTMATTER_RE.sub("", text, count=1)
    lines = text.split("\n")

    def next_content(start: int) -> int:
        i = start
        while i < len(lines) and not lines[i].strip():
            i += 1
        return i

    i = next_content(0)
    if strip_breadcrumbs and i < len(lines) and ".md)" in lines[i] and _BREADCRUMB_RE.match(lines[i]):
        # A first line made only of links can also be genuine content. When the H1 title is
        # exported too, a real breadcrumb line is always directly followed by it — require
        # that, so standalone content links are never stripped by mistake.
        follower = next_content(i + 1)
        is_breadcrumb = not strip_title or (follower < len(lines) and _H1_RE.match(lines[follower]) is not None)
        if is_breadcrumb:
            del lines[i]

    title: str | None = None
    i = next_content(i)
    if strip_title and i < len(lines):
        match = _H1_RE.match(lines[i])
        if match:
            # The exporter escapes markdown special characters in the H1 title.
            title = re.sub(r"\\(.)", r"\1", match.group(1))
            del lines[i]

    return "\n".join(lines), title


def _to_xml_tree(html: str) -> etree._Element:
    """Parse rendered HTML leniently, then re-parse as strict XML for transformation."""
    fragment = lxml.html.fragment_fromstring(html or "<p></p>", create_parent="div")
    xhtml = etree.tostring(fragment, method="xml", encoding="unicode")
    return etree.fromstring(xhtml)


def _text_content(el: etree._Element) -> str:
    """Return the concatenated text of *el* and its descendants."""
    return "".join(t for t in el.itertext() if isinstance(t, str))


def _convert_inline_spans(root: etree._Element) -> None:
    """Invert the exporter's <mark>/<font> output back to Confluence style spans."""
    for el in root.iter("mark"):
        el.tag = "span"
        style = el.get("style", "")
        el.set("style", style.replace("background:", "background-color:"))
    for el in root.iter("font"):
        el.tag = "span"
        color = el.get("color")
        if color is not None:
            del el.attrib["color"]
        if color and not el.get("style"):
            el.set("style", f"color: {color};")


def _convert_alerts(root: etree._Element) -> None:
    """Turn GitHub-style alert blockquotes into Confluence panel macros."""
    for blockquote in list(root.iter("blockquote")):
        first_p = next((child for child in blockquote if child.tag == "p"), None)
        if first_p is None or not first_p.text:
            continue
        match = _ALERT_MARKER_RE.match(first_p.text)
        if match is None:
            continue

        macro = etree.Element(_ac("structured-macro"), nsmap=NSMAP)
        macro.set(_ac("name"), ALERT_TO_MACRO[match.group(1)])
        macro.set(_ac("schema-version"), "1")
        body = etree.SubElement(macro, _ac("rich-text-body"))

        first_p.text = first_p.text[match.end() :]
        for child in list(blockquote):
            if child is first_p and not first_p.text and len(first_p) == 0:
                continue
            body.append(child)
        _replace(blockquote, macro)


def _convert_code_blocks(root: etree._Element) -> None:
    """Turn <pre><code class="language-x"> blocks into Confluence code macros."""
    for pre in list(root.iter("pre")):
        code = pre.find("code")
        if code is None:
            continue
        language = next(
            (cls.removeprefix("language-") for cls in (code.get("class") or "").split() if cls.startswith("language-")),
            None,
        )
        code_text = _text_content(code).removesuffix("\n")

        macro = etree.Element(_ac("structured-macro"), nsmap=NSMAP)
        macro.set(_ac("name"), "code")
        macro.set(_ac("schema-version"), "1")
        if language:
            parameter = etree.SubElement(macro, _ac("parameter"))
            parameter.set(_ac("name"), "language")
            parameter.text = language
        body = etree.SubElement(macro, _ac("plain-text-body"))
        body.text = etree.CDATA(code_text)  # lxml splits the section if code contains "]]>"
        _replace(pre, macro)


def _convert_links(root: etree._Element, export_path: str, resolve_page: PageResolver, warnings: list[str]) -> None:
    """Turn relative links to exported .md files into Confluence page links."""
    for anchor in list(root.iter("a")):
        href = anchor.get("href") or ""
        parsed = urlsplit(href)
        # Absolute, site-relative (/wiki/...), and same-page anchors are kept as plain <a>.
        if parsed.scheme or href.startswith(("#", "/")):
            continue

        target = _resolve_relative(export_path, parsed.path)
        if not target.endswith(".md"):
            warnings.append(f"Kept link to non-page local file as text: {href}")
            _drop_link(anchor)
            continue

        page_target = resolve_page(target)
        if page_target is None:
            warnings.append(f"Could not resolve page link target '{target}' — kept as plain text")
            _drop_link(anchor)
            continue

        link = etree.Element(_ac("link"), nsmap=NSMAP)
        if parsed.fragment:
            link.set(_ac("anchor"), unquote(parsed.fragment))
        page = etree.SubElement(link, _ri("page"))
        page.set(_ri("content-title"), page_target.title)
        if page_target.space_key:
            page.set(_ri("space-key"), page_target.space_key)
        if len(anchor) > 0:
            # The link wraps markup (e.g. an already-converted ac:image): keep it as a rich body.
            body = etree.SubElement(link, _ac("link-body"))
            body.text = anchor.text
            for child in list(anchor):
                body.append(child)
        else:
            body = etree.SubElement(link, _ac("plain-text-link-body"))
            body.text = etree.CDATA(_text_content(anchor) or href)
        _replace(anchor, link)


def _convert_images(
    root: etree._Element, export_path: str, resolve_attachment: AttachmentResolver, warnings: list[str]
) -> None:
    """Turn images into ac:image elements (attachment or external URL)."""
    for img in list(root.iter("img")):
        src = img.get("src") or ""
        alt = img.get("alt") or ""
        image = etree.Element(_ac("image"), nsmap=NSMAP)
        if alt:
            image.set(_ac("alt"), alt)

        if urlsplit(src).scheme or src.startswith("/"):
            resource = etree.SubElement(image, _ri("url"))
            resource.set(_ri("value"), src)
        else:
            target = _resolve_relative(export_path, urlsplit(src).path)
            filename = resolve_attachment(target)
            if filename is None:
                filename = posixpath.basename(target)
                warnings.append(f"Could not resolve attachment '{target}' — using filename '{filename}'")
            resource = etree.SubElement(image, _ri("attachment"))
            resource.set(_ri("filename"), filename)
        _replace(img, image)


def _resolve_relative(export_path: str, href_path: str) -> str:
    """Resolve *href_path* (URL-encoded, relative to the page file) to a path from the export root."""
    return posixpath.normpath(posixpath.join(posixpath.dirname(export_path), unquote(href_path)))


def _replace(old: etree._Element, new: etree._Element) -> None:
    """Replace *old* with *new* in the tree, keeping the tail text."""
    new.tail = old.tail
    parent = old.getparent()
    if parent is not None:
        parent.replace(old, new)


def _drop_link(anchor: etree._Element) -> None:
    """Remove an unconvertible link, preserving its content (text or wrapped markup)."""
    if len(anchor) > 0:
        _unwrap(anchor)
    else:
        _replace_with_text(anchor, _text_content(anchor) or (anchor.get("href") or ""))


def _unwrap(el: etree._Element) -> None:
    """Replace *el* with its own content, splicing children and text into the parent."""
    parent = el.getparent()
    if parent is None:
        return
    if el.text:
        previous = el.getprevious()
        if previous is not None:
            previous.tail = (previous.tail or "") + el.text
        else:
            parent.text = (parent.text or "") + el.text
    for child in list(el):
        el.addprevious(child)
    _replace_with_text(el, "")


def _replace_with_text(el: etree._Element, text: str) -> None:
    """Replace *el* with plain text, merging it into the surrounding text nodes."""
    parent = el.getparent()
    if parent is None:
        return
    text = text + (el.tail or "")
    previous = el.getprevious()
    if previous is not None:
        previous.tail = (previous.tail or "") + text
    else:
        parent.text = (parent.text or "") + text
    parent.remove(el)


def _serialize(root: etree._Element) -> str:
    """Serialize the children of the wrapper div, dropping namespace declarations.

    Confluence storage bodies conventionally use the ac:/ri: prefixes without
    declaring them, so the declarations added by lxml are stripped.
    """
    parts = [root.text or ""]
    parts.extend(etree.tostring(child, encoding="unicode") for child in root)
    storage = "".join(parts)
    return storage.replace(f' xmlns:ac="{AC_NS}"', "").replace(f' xmlns:ri="{RI_NS}"', "")


def _validate(storage: str) -> None:
    """Boundary check: the storage body must be well-formed XML."""
    wrapped = f'<root xmlns:ac="{AC_NS}" xmlns:ri="{RI_NS}">{storage}</root>'
    try:
        etree.fromstring(wrapped)
    except etree.XMLSyntaxError as e:
        raise ConversionError(f"Generated storage body is not well-formed XML: {e}") from e
