"""Tests for the markdown → Confluence storage format converter."""

import pytest
from lxml import etree

from confluence_markdown_importer.converter import AC_NS, RI_NS, PageTarget, convert_markdown


def convert(text: str, **kwargs):
    defaults = {
        "export_path": "Space/Home/Page.md",
        "resolve_page": lambda _path: None,
        "resolve_attachment": lambda _path: None,
    }
    defaults.update(kwargs)
    return convert_markdown(text, **defaults)


def assert_well_formed(storage: str) -> None:
    etree.fromstring(f'<root xmlns:ac="{AC_NS}" xmlns:ri="{RI_NS}">{storage}</root>')


class TestStripping:
    def test_frontmatter_is_stripped(self):
        result = convert("---\nconfluence_tinyui_url: https://x/wiki/x/abc\n---\n\nHello.\n")

        assert "tinyui" not in result.storage
        assert "<p>Hello.</p>" in result.storage

    def test_breadcrumb_line_is_stripped(self):
        text = "[Parent](../Parent%20Page.md) > [Other](Other.md)\n\n# Title\n\nBody.\n"

        result = convert(text)

        assert "Parent" not in result.storage
        assert "<p>Body.</p>" in result.storage

    def test_h1_becomes_title_and_is_removed(self):
        result = convert("# My Page\n\nBody.\n")

        assert result.title == "My Page"
        assert "My Page" not in result.storage

    def test_no_h1_leaves_title_none(self):
        result = convert("Body only.\n")

        assert result.title is None

    def test_markdown_escapes_in_h1_are_unescaped(self):
        result = convert("# \\[prc-vouchers\\] consumers\n\nBody.\n")

        assert result.title == "[prc-vouchers] consumers"

    def test_breadcrumbs_kept_when_disabled(self):
        text = "[Parent](Parent.md)\n\nBody.\n"

        result = convert(text, strip_breadcrumbs=False)

        assert "Parent" in result.storage

    def test_first_line_link_not_stripped_when_no_h1_follows(self):
        text = "[See the index](Index.md)\n\nBody.\n"

        result = convert(text)

        assert "See the index" in result.storage
        assert "<p>Body.</p>" in result.storage

    def test_breadcrumb_kept_with_warning_when_title_stripping_disabled(self):
        text = "[Parent](../Parent.md)\n\nBody.\n"

        result = convert(text, strip_title=False)

        assert "Parent" in result.storage
        assert "<p>Body.</p>" in result.storage
        assert any("breadcrumb" in w for w in result.warnings)


class TestBasicBlocks:
    def test_paragraph_with_emphasis(self):
        result = convert("Some **bold** and *italic* text.\n")

        assert "<p>Some <strong>bold</strong> and <em>italic</em> text.</p>" in result.storage

    def test_table_renders_as_html_table(self):
        text = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"

        result = convert(text)

        assert "<table>" in result.storage
        assert "<th>A</th>" in result.storage
        assert "<td>1</td>" in result.storage
        assert_well_formed(result.storage)

    def test_code_fence_becomes_code_macro_with_language_and_cdata(self):
        text = '```java\nSystem.out.println("a & b");\n```\n'

        result = convert(text)

        assert '<ac:structured-macro ac:name="code"' in result.storage
        assert '<ac:parameter ac:name="language">java</ac:parameter>' in result.storage
        assert '<ac:plain-text-body><![CDATA[System.out.println("a & b");]]></ac:plain-text-body>' in result.storage
        assert_well_formed(result.storage)

    def test_code_fence_with_cdata_terminator_stays_well_formed(self):
        text = "```text\na ]]> b\n```\n"

        result = convert(text)

        assert_well_formed(result.storage)
        root = etree.fromstring(f'<root xmlns:ac="{AC_NS}" xmlns:ri="{RI_NS}">{result.storage}</root>')
        assert root.findtext(f".//{{{AC_NS}}}plain-text-body") == "a ]]> b"


class TestAlerts:
    @pytest.mark.parametrize(
        ("alert", "macro"),
        [
            ("IMPORTANT", "info"),
            ("NOTE", "panel"),
            ("TIP", "tip"),
            ("WARNING", "note"),
            ("CAUTION", "warning"),
        ],
    )
    def test_github_alert_maps_back_to_confluence_macro(self, alert, macro):
        text = f"> [!{alert}]\n> Alert body text.\n"

        result = convert(text)

        assert f'<ac:structured-macro ac:name="{macro}"' in result.storage
        assert "<ac:rich-text-body>" in result.storage
        assert "Alert body text." in result.storage
        assert f"[!{alert}]" not in result.storage
        assert_well_formed(result.storage)

    def test_plain_blockquote_stays_blockquote(self):
        result = convert("> Just a quote.\n")

        assert "<blockquote>" in result.storage


class TestLinks:
    def test_relative_md_link_becomes_page_link_with_space_key(self):
        resolver = {"Space/Home/Systems/S.001.md": PageTarget("S.001 - GK OmniPOS", "CRI")}

        result = convert(
            "See [the system](Systems/S.001.md) for details.\n",
            resolve_page=resolver.get,
        )

        assert '<ri:page ri:content-title="S.001 - GK OmniPOS"' in result.storage
        assert 'ri:space-key="CRI"' in result.storage
        assert "<ac:plain-text-link-body><![CDATA[the system]]></ac:plain-text-link-body>" in result.storage
        assert_well_formed(result.storage)

    def test_url_encoded_relative_link_resolves(self):
        resolver = {"Space/Integration Catalog.md": PageTarget("Integration Catalog")}

        result = convert(
            "[Catalog](../Integration%20Catalog.md)\n",
            resolve_page=resolver.get,
            strip_breadcrumbs=False,
        )

        assert '<ri:page ri:content-title="Integration Catalog"' in result.storage
        assert "ri:space-key" not in result.storage

    def test_link_fragment_becomes_ac_anchor(self):
        resolver = {"Space/Home/Other.md": PageTarget("Other", "CRI")}

        result = convert(
            "[section](Other.md#My%20Section)\n",
            resolve_page=resolver.get,
            strip_breadcrumbs=False,
        )

        assert '<ac:link ac:anchor="My Section"' in result.storage
        assert_well_formed(result.storage)

    def test_image_inside_resolved_link_is_kept_in_link_body(self):
        page_resolver = {"Space/Home/Other.md": PageTarget("Other", "CRI")}
        att_resolver = {"Space/attachments/a.png": "diagram.png"}

        result = convert(
            "[![](../attachments/a.png)](Other.md)\n",
            resolve_page=page_resolver.get,
            resolve_attachment=att_resolver.get,
            strip_breadcrumbs=False,
        )

        assert "<ac:link-body>" in result.storage
        assert '<ri:attachment ri:filename="diagram.png"' in result.storage
        assert_well_formed(result.storage)

    def test_image_inside_unresolved_link_is_unwrapped_not_dropped(self):
        att_resolver = {"Space/attachments/a.png": "diagram.png"}

        result = convert(
            "[![](../attachments/a.png)](Missing.md)\n",
            resolve_attachment=att_resolver.get,
            strip_breadcrumbs=False,
        )

        assert '<ri:attachment ri:filename="diagram.png"' in result.storage
        assert "<ac:link>" not in result.storage
        assert any("Missing.md" in w for w in result.warnings)
        assert_well_formed(result.storage)

    def test_unresolvable_md_link_falls_back_to_text_with_warning(self):
        result = convert("[gone](Missing.md)\n", strip_breadcrumbs=False)

        assert "<ac:link>" not in result.storage
        assert "gone" in result.storage
        assert any("Missing.md" in w for w in result.warnings)

    def test_external_link_is_kept(self):
        result = convert("[site](https://example.com/x)\n")

        assert '<a href="https://example.com/x">site</a>' in result.storage

    def test_anchor_without_href_is_left_untouched(self):
        result = convert('Jump target: <a name="section-1"></a> here.\n')

        assert '<a name="section-1"' in result.storage
        assert result.warnings == []

    def test_site_relative_link_is_kept_as_anchor(self):
        result = convert("[John Doe](/wiki/display/~6171624a860f78006bd5629c)\n", strip_breadcrumbs=False)

        assert '<a href="/wiki/display/~6171624a860f78006bd5629c">John Doe</a>' in result.storage
        assert result.warnings == []


class TestImages:
    def test_attachment_image_becomes_ri_attachment(self):
        resolver = {"Space/attachments/abc123.png": "diagram.png"}

        result = convert(
            "![alt text](../attachments/abc123.png)\n",
            resolve_attachment=resolver.get,
        )

        assert '<ri:attachment ri:filename="diagram.png"' in result.storage
        assert 'ac:alt="alt text"' in result.storage
        assert_well_formed(result.storage)

    def test_unresolvable_attachment_falls_back_to_basename_with_warning(self):
        result = convert("![](../attachments/abc123.png)\n")

        assert '<ri:attachment ri:filename="abc123.png"' in result.storage
        assert any("abc123.png" in w for w in result.warnings)

    def test_external_image_becomes_ri_url(self):
        result = convert("![](https://example.com/pic.png)\n")

        assert '<ri:url ri:value="https://example.com/pic.png"' in result.storage
        assert_well_formed(result.storage)

    def test_site_relative_image_becomes_ri_url(self):
        result = convert("![](/wiki/s/123/_/images/icons/wait.gif)\n")

        assert '<ri:url ri:value="/wiki/s/123/_/images/icons/wait.gif"' in result.storage
        assert "<ri:attachment" not in result.storage


class TestInlineHtml:
    def test_mark_becomes_highlight_span(self):
        result = convert('Status: <mark style="background: #dfe1e6;">DRAFT</mark>\n')

        assert '<span style="background-color: #dfe1e6;">DRAFT</span>' in result.storage
        assert "<mark" not in result.storage
        assert_well_formed(result.storage)

    def test_font_becomes_color_span(self):
        result = convert('<font style="color: #ff0000;">red</font>\n')

        assert '<span style="color: #ff0000;">red</span>' in result.storage
        assert "<font" not in result.storage

    def test_nbsp_entity_survives_as_numeric_reference(self):
        result = convert("a&nbsp;b\n")

        assert "&#160;" in result.storage or "\xa0" in result.storage
        assert_well_formed(result.storage)
