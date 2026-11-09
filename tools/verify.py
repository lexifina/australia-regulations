"""Check the corpus against the official Word documents.

The comparison is deliberately not run through the build's own parser.
This module re-reads the .docx with a second, minimal extractor written
a different way -- regular expressions over the raw document XML rather
than an element tree -- so that a bug in the main extractor shows up as
a mismatch instead of being reproduced identically on both sides.

What is compared, for every compilation of every instrument: the word
sequence of each provision, keyed by provision number, read back out of
git at that compilation's tag. That catches a missing provision, an
extra one, a renumbering, and any difference in the words themselves,
without relying on either side's file-naming rules.

Release bar: zero mismatches.
"""

import os
import re
import subprocess
import sys
import zipfile
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- the second extractor ----------------------------------------------
# Regex over raw XML. Intentionally plain: it knows about paragraphs,
# styles, text runs, no-break hyphens and the endnote boundary, and
# nothing else.

P_RE = re.compile(r"<w:p(?:\s[^>]*)?>.*?</w:p>|<w:p(?:\s[^>]*)?/>", re.S)
# An mc:AlternateContent element carries the same content twice, once under
# mc:Choice for a reader that understands the newer markup and once under
# mc:Fallback for one that does not. A reader must render exactly one. The
# prescribed forms in Schedule 1 of the National Consumer Credit Protection
# Regulations 2010 are textboxes written this way, so a scan that reads both
# counts each banner twice. Dropped by string surgery rather than by walking
# a tree, to keep this reader independent of the builder's.
ALTERNATE_RE = re.compile(r"<mc:AlternateContent\b.*?</mc:AlternateContent>", re.S)
FALLBACK_RE = re.compile(r"<mc:Fallback\b.*?</mc:Fallback>", re.S)


def one_branch(xml):
    """Keep mc:Choice and drop its mc:Fallback twin, wherever both exist."""
    def pick(match):
        block = match.group(0)
        if "<mc:Choice" not in block:
            return block               # Fallback alone is the only content
        return FALLBACK_RE.sub("", block)
    return ALTERNATE_RE.sub(pick, xml)
STYLE_RE = re.compile(r'<w:pStyle\s+w:val="([^"]+)"')
TEXT_RE = re.compile(
    r"<w:t(?:\s[^>]*)?>(?P<text>[^<]*)</w:t>"
    r"|(?P<hyphen><w:noBreakHyphen\s*/>)"
    r"|(?P<space><w:(?:tab|br|cr)\s*/>)")
SKIP_RE = re.compile(
    r"^(TOC\d*|Header|Footer|LI-Contentsheading|Contents(Head|Page|SectionBreak)"
    r"|PageBreak|SigningPageBreak|SchedSectionBreak|TextWOutChapSectionBreak)")
# The three drafting vocabularies, worked out here independently of parse.py:
# the Office of Parliamentary Counsel's ActHead1-5, the LI- family ASIC uses,
# and the Statutory Rules styles of the Corporations Regulations.
CONTAINERS = {"ActHead1": 0, "ActHead2": 1, "ActHead3": 2, "ActHead4": 3,
              "LI-Heading1": 0,
              "HC": 0, "HP": 1, "HD": 2, "HS": 3,
              "Scheduletitle": 0, "NewSchPt": 1}
PROVISIONS = ("ActHead5", "LI-Heading2", "HR", "NewSchHd")
# The endnotes are opened by a heading that names them, and that heading
# ends the law text. These three name nothing, and EndNotes is not the
# endnotes in every instrument -- Word also sets a prescribed form's own
# numbered footnotes with it -- so they end the law text only where no law
# follows them.
UNHEADED_ENDNOTES = ("ENoteNo", "EndNotes", "TableEnotesHeading")
SCHEDULE_RE = re.compile(r"^Schedule\s+([0-9A-Za-z.]+)\b")


def unit_path(number, schedule):
    """The file a provision belongs in, worked out from its number the same
    way the corpus names it -- but written here from the layout, not shared
    with the builder. Units are keyed by file rather than by number because
    clause 1 of Schedule 1 and clause 1 of Schedule 2 are different law."""
    digits = re.match(r"^(\d+)(.*)$", number)
    def pad(width):
        return (digits.group(1).zfill(width) + digits.group(2)) if digits else number
    if schedule is not None:
        return "sch-%s/cl-%s" % (schedule.lower(),
                                 number if "." in number else pad(2))
    if "." in number:
        return "r-%s" % number
    if "-" in number:
        return "s-%s" % number
    return "s-%s" % pad(3)
# Law text can sit under a container with no provision heading beneath it
# (the reproduced forms are whole Divisions). Such text is its own unit,
# named by the open containers, and this works the naming out again rather
# than trusting the corpus's file names.
KINDS = {"chapter": "ch", "part": "part", "division": "div",
         "subdivision": "subdiv", "schedule": "sch"}
HEAD_NUM_RE = re.compile(r"^([A-Za-z]+)\s+([0-9A-Za-z.\-]+)")


def container_key(stack):
    out = []
    for _level, text in stack:
        m = HEAD_NUM_RE.match(text)
        kind = KINDS.get(m.group(1).lower()) if m else None
        if kind:
            out.append("%s-%s" % (kind, m.group(2).lower()))
    return "/".join(out) if out else None
DROP = dict.fromkeys([0x00AD, 0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF], None)
DROP.update({0x2011: "-", 0x00A0: " ", 0x2009: " ", 0x202F: " "})


def _unescape(text):
    return (text.replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"')
                .replace("&apos;", "'"))


def law_text_end(read):
    """How many of the (style, text) pairs in `read` are law text.

    A heading that names the endnotes ends the law where it stands. A style
    from UNHEADED_ENDNOTES ends it only until a container or provision
    heading appears after it, which proves the law had not ended. An endnote
    amendment table reprints provision numbers as table text rather than as
    headings, so the real endnotes still end the law here.
    """
    unheaded = None
    for index, (style, text) in enumerate(read):
        if style and style.startswith(("ENotes", "Endnote", "LI-Endnote")):
            return index
        if style == "LI-Heading1" and text == "Endnotes":
            return index               # the endnote boundary, LI- spelling
        if style == "NoteHeading" and text.startswith("Notes to the"):
            return index               # the same boundary, Statutory Rules
        if style in UNHEADED_ENDNOTES:
            if unheaded is None:
                unheaded = index
        elif style in CONTAINERS or style in PROVISIONS:
            unheaded = None
    return len(read) if unheaded is None else unheaded


def minimal_paragraphs(docx_bytes):
    """Yield (style, text) for each body paragraph, endnotes excluded."""
    with zipfile.ZipFile(io.BytesIO(docx_bytes)) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    body = one_branch(xml.split("<w:body>", 1)[-1])
    read = []
    for match in P_RE.finditer(body):
        chunk = match.group(0)
        style_match = STYLE_RE.search(chunk)
        style = style_match.group(1) if style_match else None
        pieces = []
        for text_match in TEXT_RE.finditer(chunk):
            if text_match.group("hyphen") is not None:
                pieces.append("-")
            elif text_match.group("space") is not None:
                pieces.append(" ")
            else:
                pieces.append(_unescape(text_match.group("text")))
        read.append((style, re.sub(r"\s+", " ",
                                   "".join(pieces).translate(DROP)).strip()))
    for style, text in read[:law_text_end(read)]:
        if style and SKIP_RE.match(style):
            continue
        if text:
            yield style, text


def words(text):
    """Comparison tokens: words, with markdown furniture removed."""
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.M)
    text = text.replace("|", " ")
    return re.sub(r"\s+", " ", text).strip().split(" ") if text.strip() else []


HEADING_RE = re.compile(r"^\s*([0-9]+[0-9A-Za-z.\-]*)(?:\t| {2,})\s*(.*)$")


def docx_units(docx_bytes):
    """{provision number: words} plus ('intro', words), from the .docx."""
    units = {}
    key = "intro"
    buffer = []
    stack = []
    held = []
    schedule = None
    empty = None

    def close_empty():
        # A container heading that closed with nothing under it is a unit of
        # its own, the same way the corpus files it: a repealed Schedule is
        # published as a heading and nothing else.
        if empty is None:
            return
        path, headings = empty
        for heading in headings[:-1]:
            units.setdefault("_pending", []).extend(words(heading))
        if path:
            units.setdefault(path, []).extend(words(headings[-1]))

    for style, text in minimal_paragraphs(docx_bytes):
        if style in PROVISIONS:
            if buffer:
                units.setdefault(key, []).extend(words(" ".join(buffer)))
            # The containers opened above this provision belong to it.
            for heading in held:
                units.setdefault("_pending", []).extend(words(heading))
            held = []
            empty = None
            match = HEADING_RE.match(text)
            number = match.group(1) if match else text.split(" ")[0]
            key = unit_path(number, schedule)
            buffer = [text]
            continue
        if style in CONTAINERS:
            if buffer:
                units.setdefault(key, []).extend(words(" ".join(buffer)))
                buffer = []
            level = CONTAINERS[style]
            if stack and stack[-1][0] >= level:
                close_empty()
                held = []              # close_empty has accounted for them all
            empty = None
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            found = SCHEDULE_RE.match(text)
            if found:
                schedule = found.group(1)
            elif level == 0:
                schedule = None
            # Held, not filed: whether this heading belongs to the provision
            # it opens or to a unit of its own is not known until the next
            # paragraph says which arrives first.
            held.append(text)
            empty = (container_key(stack), list(held))
            continue
        if text == "Contents" and key == "intro":
            continue
        if held:
            # Text, not a provision: the container holds it directly.
            for heading in held[:-1]:
                units.setdefault("_pending", []).extend(words(heading))
            key = container_key(stack) or key
            buffer = [held[-1]]
            held = []
            empty = None
        buffer.append(text)
    if buffer:
        units.setdefault(key, []).extend(words(" ".join(buffer)))
    close_empty()
    return units


def git(repo, *args):
    result = subprocess.run(["git", "-C", repo] + list(args),
                            capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("git %s: %s" % (" ".join(args), result.stderr))
    return result.stdout


def read_tree(repo, tag, prefix):
    """{path: text} for every file under `prefix` at `tag`, in one pass.

    One `git show` per file costs a process each: the Corporations
    Regulations run to about 900 files a compilation, so a release would
    spawn six figures of them. `cat-file --batch` streams the lot instead.
    """
    listing = git(repo, "ls-tree", "-r", tag, prefix).splitlines()
    wanted = []
    for line in listing:
        if not line.strip():
            continue
        meta, _, path = line.partition("\t")
        parts = meta.split()
        if len(parts) >= 3 and parts[1] == "blob" and path.endswith(".md"):
            wanted.append((parts[2], path))
    if not wanted:
        return {}
    result = subprocess.run(
        ["git", "-C", repo, "cat-file", "--batch"],
        input="".join(sha + "\n" for sha, _ in wanted).encode("ascii"),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode != 0:
        raise RuntimeError("git cat-file: %s" % result.stderr.decode("utf-8", "replace"))
    out = {}
    stream = result.stdout
    at = 0
    for _sha, path in wanted:
        end = stream.index(b"\n", at)
        header = stream[at:end].split()
        size = int(header[2])
        start = end + 1
        out[path] = stream[start:start + size].decode("utf-8")
        at = start + size + 1          # the newline cat-file adds after each
    return out


def corpus_units(repo, tag, folder):
    """{unit path: words} read back out of git at `tag`."""
    units = {}
    for path, blob in sorted(read_tree(repo, tag, "corpus/%s/" % folder).items()):
        if os.path.basename(path) == "intro.md":
            units["intro"] = words(blob)
            continue
        inner = path.split("corpus/%s/" % folder, 1)[-1]
        if not os.path.basename(inner).startswith(("s-", "r-", "cl-")):
            # A container unit: its file name is its key, and its heading
            # line is part of the text rather than a container above it.
            containers, rest = [], []
            for line in blob.split("\n"):
                (containers if line.startswith("## ") else rest).append(line)
            units.setdefault("_pending", []).extend(
                words("\n".join(containers)))
            units[inner[:-3]] = words("\n".join(rest).replace("# ", "", 1))
            continue
        containers, heading, body = [], None, []
        for line in blob.split("\n"):
            if line.startswith("## "):
                containers.extend(words(line))
            elif line.startswith("# ") and heading is None:
                heading = line[2:]
            else:
                body.append(line)
        units.setdefault("_pending", []).extend(containers)
        units.setdefault(inner[:-3], []).extend(
            words(heading or "") + words("\n".join(body)))
    return units


def compare(expected, actual):
    """Differences between two {unit: words} maps.

    Container headings are pooled under "_pending" because the corpus
    stores each one in the file for the provision it opens, so their
    order depends on how files are listed rather than on the document.
    Their content is still checked, as a multiset.
    """
    problems = []
    for key in sorted(set(expected) | set(actual)):
        want, got = expected.get(key), actual.get(key)
        if want is None:
            problems.append((key, "present in corpus, absent from .docx", None))
            continue
        if got is None:
            problems.append((key, "present in .docx, absent from corpus", None))
            continue
        if key == "_pending":
            if sorted(want) != sorted(got):
                missing = [w for w in want if w not in got][:6]
                problems.append((key, "heading words differ", missing))
            continue
        if want != got:
            at = next((i for i, (a, b) in enumerate(zip(want, got)) if a != b),
                      min(len(want), len(got)))
            problems.append((key, "word %d of %d: .docx %r vs corpus %r"
                             % (at + 1, len(want),
                                " ".join(want[at:at + 6]),
                                " ".join(got[at:at + 6])), None))
    return problems


def verify_instrument(repo, title_id, *, today, log=print):
    """Check every compilation of one instrument. Returns a result dict."""
    import fetch
    import parse as parse_mod
    import register
    fetch.OFFLINE = True

    title = register.title(title_id)
    folder = parse_mod.folder_for(title["name"])
    documents = register.word_documents(title_id)
    checked, mismatches, skipped, out_of_scope = 0, [], [], []

    for version in register.versions(title_id):
        start = (version.get("start") or "")[:10]
        if start > today:
            continue
        tag = "%s/%s" % (folder, start)
        try:
            git(repo, "rev-parse", "--verify", "-q", tag + "^{tag}")
        except RuntimeError:
            try:
                git(repo, "rev-parse", "--verify", "-q", tag)
            except RuntimeError:
                skipped.append((start, "no tag"))
                continue
        readable, total = register.volume_coverage(title_id, version)
        if total and readable < total:
            out_of_scope.append((start, "%d of %d volumes are legacy .doc/.rtf"
                                 % (total - readable, total)))
            continue
        volumes = sorted((d for d in documents if d.get("start") == version.get("start")),
                         key=lambda d: d.get("volumeNumber") or 0)
        if not volumes:
            out_of_scope.append((start, "no Office Open XML document"))
            continue
        expected = {}
        try:
            as_made = str(version.get("compilationNumber")) == "0"
            for document in volumes:
                body = register.document_bytes(document, as_made=as_made)
                for key, value in docx_units(body).items():
                    if key == "intro" and "intro" in expected:
                        continue       # volume 1 carries the front matter
                    expected.setdefault(key, []).extend(value)
        except Exception as error:                   # noqa: BLE001
            skipped.append((start, str(error)))
            continue
        actual = corpus_units(repo, tag, folder)
        problems = compare(expected, actual)
        checked += 1
        if problems:
            mismatches.append({"tag": tag, "start": start,
                               "problems": problems[:12],
                               "count": len(problems)})
            log("  MISMATCH %s: %d" % (tag, len(problems)))
            for key, why, extra in problems[:3]:
                log("      %-10s %s %s" % (key, why, extra or ""))
        else:
            log("  ok %s  (%d provisions)" % (tag, len(actual)))
    return {"title_id": title_id, "folder": folder, "checked": checked,
            "mismatches": mismatches, "skipped": skipped,
            "out_of_scope": out_of_scope}


def main(argv=None):
    import argparse
    import json
    parser = argparse.ArgumentParser()
    parser.add_argument("titles", nargs="*")
    parser.add_argument("--today", required=True)
    parser.add_argument("--repo",
                        default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    parser.add_argument("--report")
    args = parser.parse_args(argv)

    import build as build_mod
    results = []
    for title_id in (args.titles or build_mod.TIER1):
        print(title_id, flush=True)
        results.append(verify_instrument(args.repo, title_id, today=args.today))
    total = sum(r["checked"] for r in results)
    bad = sum(len(r["mismatches"]) for r in results)
    skipped = sum(len(r["skipped"]) for r in results)
    outside = sum(len(r["out_of_scope"]) for r in results)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({"today": args.today, "compilations_checked": total,
                       "compilations_with_mismatches": bad,
                       "skipped": skipped, "out_of_scope": outside,
                       "results": results},
                      handle, indent=1, sort_keys=True, default=str)
    print("\n%d compilations checked, %d with mismatches, %d skipped, "
          "%d outside the readable-format scope" % (total, bad, skipped, outside))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
