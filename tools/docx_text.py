"""Read the text of an Office Open XML document, in reading order.

This is the build's primary extractor. It works on the document XML
directly rather than through python-docx, which keeps the dependency
list empty and makes the normalisation rules explicit in one place.

Two details in the Register's files are easy to miss and both change the
text:

  * Hyphens in words like Governor-General and self-contained are not
    characters at all. They are <w:noBreakHyphen/> elements between two
    runs, so an extractor that reads only <w:t> silently produces
    "GovernorGeneral".
  * The table of contents is built from field codes. Its instruction
    text lives in <w:instrText>, which must never be emitted, and its
    visible entries carry TOC styles, which the parser drops.
"""

import re
import zipfile
import io

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"

# Characters the brief requires removed outright.
ZERO_WIDTH = dict.fromkeys(
    [0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF], None)
# Non-breaking hyphen becomes an ordinary hyphen; non-breaking and thin
# spaces become ordinary spaces before whitespace is collapsed.
TRANSLATE = dict(ZERO_WIDTH)
TRANSLATE.update({0x2011: "-", 0x00A0: " ", 0x2009: " ", 0x202F: " "})

import xml.etree.ElementTree as ET
import collections

#: kind is "p" or "table"; text is a string or, for a table, a list of
#: rows; spaced is the paragraph text with tabs and space runs intact.
Block = collections.namedtuple("Block", "kind style text spaced")


def normalise(text):
    """Apply the corpus text rules to one string, collapsing whitespace."""
    return re.sub(r"\s+", " ", substitute(text)).strip()


def substitute(text):
    """Character-level rules only: no whitespace collapsing.

    Headings separate the provision number from its heading with a tab
    or a run of spaces, so the splitter needs the text before runs are
    collapsed.
    """
    return text.translate(TRANSLATE)


def _subtree_text(node, out):
    """Collect a paragraph subtree's visible text, in reading order.

    One walk covers the whole subtree rather than taking each <w:r> in
    turn, because two shapes in the Register's files put text where a
    run-by-run walk reads it wrongly.

      * A textbox anchors whole paragraphs inside the paragraph that
        carries it, and their runs are descendants of that paragraph's
        own run. A run-by-run walk joins those paragraphs to each other
        with nothing between them, so Form 11A of the National Consumer
        Credit Protection Regulations 2010 read "IMPORTANTWe have not
        received a payment".
      * An mc:AlternateContent element holds the same content twice --
        mc:Choice for a reader that understands the newer markup, and
        mc:Fallback for one that does not. A reader must render exactly
        one of them. Reading both printed that form's banner twice over.

    Emitting only <w:t> keeps out field instruction text in <w:instrText>
    and the <w:delText> of a tracked deletion, as before.
    """
    for child in node:
        tag = child.tag
        if tag == MC + "AlternateContent":
            branch = child.find(MC + "Choice")
            if branch is None:
                branch = child.find(MC + "Fallback")
            if branch is not None:
                _subtree_text(branch, out)
        elif tag == W + "t":
            out.append(child.text or "")
        elif tag == W + "noBreakHyphen":
            out.append("\u2011")
        elif tag == W + "softHyphen":
            out.append("\u00ad")
        elif tag == W + "tab":
            out.append("\t")
        elif tag in (W + "br", W + "cr"):
            out.append(" ")
        elif tag == W + "p":
            # A nested paragraph is its own line. The separator is a
            # newline and not a space because the heading splitter reads
            # a tab or two spaces as the break between a provision number
            # and its heading, and must not see one here.
            out.append("\n")
            _subtree_text(child, out)
            out.append("\n")
        else:
            _subtree_text(child, out)


def paragraph_text(paragraph, *, collapse=True):
    """Visible text of one <w:p>, excluding field instruction text."""
    out = []
    _subtree_text(paragraph, out)
    raw = "".join(out)
    return normalise(raw) if collapse else substitute(raw)


def paragraph_style(paragraph):
    style = paragraph.find(W + "pPr/" + W + "pStyle")
    return style.get(W + "val") if style is not None else None


def table_rows(table):
    """Rows of a <w:tbl> as lists of normalised cell strings."""
    rows = []
    for tr in table.findall(W + "tr"):
        cells = []
        for tc in tr.findall(W + "tc"):
            parts = [paragraph_text(p) for p in tc.iter(W + "p")]
            cells.append(normalise(" ".join(part for part in parts if part)))
        rows.append(cells)
    return rows


def blocks(docx_bytes):
    """Yield ('p', style, text) and ('table', None, rows) in reading order.

    Nested tables are reached through the outer table's cells, so only
    top-level body children are walked here.
    """
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        xml = archive.read("word/document.xml")
    body = ET.fromstring(xml).find(W + "body")
    if body is None:
        return
    for child in body:
        if child.tag == W + "p":
            yield Block("p", paragraph_style(child),
                        paragraph_text(child),
                        paragraph_text(child, collapse=False))
        elif child.tag == W + "tbl":
            yield Block("table", None, table_rows(child), None)
