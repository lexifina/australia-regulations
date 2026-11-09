"""Split one compiled instrument into one file per provision.

Layout, per the build brief:

    <folder>/intro.md          name and preamble
    <folder>/s-052.md          sections, numbered plainly
    <folder>/s-1-10.md         sections in the modern compound style
    <folder>/r-6.10.01.md      regulations
    <folder>/sch-1/cl-01.md    schedule clauses

Inside a file: `##` lines for the part and division headings that open
at that provision, a `#` line carrying the number and heading, then one
line per paragraph, with table rows as a single line whose cells are
separated by " | ".

Scope boundary: a compilation ends with the Register's endnotes --
about the endnotes, abbreviation key, legislation history, amendment
history. Those are the Register's editorial apparatus rather than the
law, and the git history reconstructs the same information from the
version records, so they are not part of the corpus. The boundary is
the first ENotesHeading1 paragraph, and the verifier finds it
independently.
"""

import re

# Two drafting vocabularies appear in these instruments. The Office of
# Parliamentary Counsel uses ActHead1-5; ASIC and some other makers use an
# LI- family of their own. No document mixes them, so one set serves both.
#
# In an LI- document LI-SectionHeading is NOT a provision heading: it marks
# the text the instrument notionally inserts into the Act it modifies, which
# belongs to the body of the provision doing the modifying. The provisions
# are LI-Heading2 and the Parts above them are LI-Heading1.
#
# Headings above provision level, with the depth each one sits at. The depth
# is stated rather than taken from a list position because a Schedule and a
# Chapter are both outermost, and one list cannot hold two styles at a depth.
CONTAINER_LEVEL = {
    # Office of Parliamentary Counsel
    "ActHead1": 0, "ActHead2": 1, "ActHead3": 2, "ActHead4": 3,
    # ASIC and other makers of legislative instruments
    "LI-Heading1": 0,
    # Statutory Rules, the style the Corporations Regulations are drafted in.
    # TOC1-TOC5 mirror HC, HP, HD, HS and HR with matching counts, which is
    # how the depths below were settled.
    "HC": 0, "HP": 1, "HD": 2, "HS": 3,
    "Scheduletitle": 0, "NewSchPt": 1,
}
PROVISION_STYLES = {"ActHead5", "LI-Heading2", "HR", "NewSchHd"}
FRONT_STYLES = {
    "ShortT", "InstNo", "CompiledActNo", "MadeunderText", "CompiledMadeUnder",
    "SignCoverPageStart", "SignCoverPageEnd", "LongT", "Formula",
    "LI-Title", "LI-Fronttext", "LI-Fronttextheading1", "LI-Fronttextheading2",
    "Title", "CoverAct", "CoverMade", "CoverStatRule", "CoverUpdate",
    "UpdateDate", "RGHead", "RGPara",
}
# The endnotes begin the material about the law rather than the law. In every
# drafting style a heading opens them and names them, and that heading is the
# boundary. These three styles name nothing, and one of them is not the
# endnotes at all in some instruments: Word's EndNotes style also sets the
# numbered footnotes printed under a prescribed form. Schedule 1 of the
# National Consumer Credit Protection Regulations 2010 uses it that way a
# third of the way through the compilation, and treating it as the boundary
# there drops the rest of Schedule 1 and all of Schedules 2 to 10. So these
# end the law text only where no law follows them -- see _law_text_end.
UNHEADED_ENDNOTE_STYLES = {"ENoteNo", "EndNotes", "TableEnotesHeading"}
# Dropped: contents entries, running heads, endnote bodies, page furniture.
# "Endnote" must not also match EndNotes, which is a prescribed form's own
# footnote and is law text.
SKIP_STYLE = re.compile(
    r"^(TOC\d*|Header|Footer|ENotes|Endnote(?![sS])|LI-Endnote"
    r"|LI-Contentsheading|Contents(Head|Page|SectionBreak)|PageBreak"
    r"|SigningPageBreak|SchedSectionBreak|TextWOutChapSectionBreak)")

SCHEDULE_RE = re.compile(r"^Schedule\s+([0-9A-Za-z.]+)\b")
# A container heading often carries its own number ("Part 6", "Division 2").
# Some instruments put law text straight under one with no provision heading
# beneath it at all -- the reproduced ACCC forms are whole Divisions of the
# merger determinations -- so that text needs a file, and the open containers
# name it: part-6/div-2.md.
CONTAINER_KIND = {"chapter": "ch", "part": "part", "division": "div",
                  "subdivision": "subdiv", "schedule": "sch"}
CONTAINER_HEAD_RE = re.compile(r"^([A-Za-z]+)\s+([0-9A-Za-z.\-]+)")
# "1  Name" / "6.10.01  Interpretation" -- number, then a tab or a run of
# two or more spaces, then the heading.
HEADING_RE = re.compile(r"^\s*([0-9]+[0-9A-Za-z.\-]*)(?:\t| {2,})\s*(.*)$")


def _slug(name):
    name = name.lower()
    name = re.sub(r"[‐-―]", "-", name)
    name = re.sub(r"[^a-z0-9]+", "-", name)
    return name.strip("-")


def _provision_filename(number, *, schedule):
    """Map a provision number to its path within the instrument folder."""
    if schedule is not None:
        stem = number if "." in number else _pad(number, 2)
        return "sch-%s/cl-%s.md" % (schedule.lower(), stem)
    if "." in number:
        return "r-%s.md" % number
    # A compound section number ("1-10", the modern Commonwealth style)
    # is kept exactly as the instrument writes it, so the file name is
    # the citation. Only a plain integer is padded, which is what makes
    # s-052.md sort with its neighbours.
    if "-" in number:
        return "s-%s.md" % number
    return "s-%s.md" % _pad(number, 3)


def _pad(number, width):
    """Zero-pad the leading digits of a number, keeping any suffix."""
    match = re.match(r"^(\d+)(.*)$", number)
    if not match:
        return number
    return match.group(1).zfill(width) + match.group(2)


def _container_path(stack):
    """The file path for text held directly by the open containers."""
    parts = []
    for _level, text in stack:
        match = CONTAINER_HEAD_RE.match(text)
        kind = CONTAINER_KIND.get(match.group(1).lower()) if match else None
        if kind:
            parts.append("%s-%s" % (kind, match.group(2).lower()))
    return "/".join(parts) if parts else None


class Container(object):
    """Law text held by a Part, Division or Schedule that has no provision
    heading under it. Rendered like a provision, but headed by the container
    heading itself, because that heading is the only name the text has."""

    def __init__(self, stack, headings, lines):
        self.stack = list(stack)
        self.headings = list(headings)
        self.lines = list(lines)

    @property
    def path(self):
        stem = _container_path(self.stack)
        return (stem + ".md") if stem else None

    def render(self):
        out = ["## " + c for c in self.headings[:-1]]
        if out:
            out.append("")
        out.append("# " + (self.headings[-1] if self.headings else ""))
        if self.lines:
            out.append("")
            out.extend(self.lines)
        return "\n".join(out).rstrip() + "\n"


class Provision(object):
    def __init__(self, number, heading, containers, schedule):
        self.number = number
        self.heading = heading
        self.containers = list(containers)
        self.schedule = schedule
        self.lines = []

    @property
    def path(self):
        return _provision_filename(self.number, schedule=self.schedule)

    def render(self):
        out = ["## " + c for c in self.containers]
        if out:
            out.append("")
        out.append("# %s  %s" % (self.number, self.heading) if self.heading
                   else "# %s" % self.number)
        if self.lines:
            out.append("")
            out.extend(self.lines)
        return "\n".join(out).rstrip() + "\n"


def _law_text_end(blocks):
    """How many of `blocks` are law text, endnotes and all that follows cut off.

    A heading that names the endnotes is trusted where it stands. A style
    from UNHEADED_ENDNOTE_STYLES is trusted only until a container or
    provision heading appears after it, which proves the law had not ended.
    An endnote amendment table reprints provision numbers as table text, not
    as headings, so the real endnotes survive this test.
    """
    unheaded = None
    for index, block in enumerate(blocks):
        if block.kind == "table":
            continue
        style, text = block.style, (block.text or "")
        if style and style.startswith("ENotes"):
            return index               # endnotes: end of the law text
        if style == "LI-Heading1" and text == "Endnotes":
            return index               # the same boundary, LI- spelling
        if style == "NoteHeading" and text.startswith("Notes to the"):
            return index               # the heading that opens those endnotes
        if style in UNHEADED_ENDNOTE_STYLES:
            if unheaded is None:
                unheaded = index
        elif style in CONTAINER_LEVEL or style in PROVISION_STYLES:
            unheaded = None
    return len(blocks) if unheaded is None else unheaded


def parse(blocks, *, instrument_name, notes=None):
    """Return {relative path: file text} for one compilation.

    `blocks` is the sequence produced by docx_text.blocks. `notes`, if
    given, collects anomalies worth reporting -- a provision number the
    source uses twice, for one.
    """
    files = {}
    front = []
    current = None
    pending = []
    schedule = None
    started = False
    stack = []                         # open container headings, outermost first
    orphan = []                        # text held by a container, no provision yet
    empty = None                       # a container heading with nothing under it yet

    def flush_empty():
        # A container heading that closed with no provision and no text of
        # its own. A repealed Schedule is published this way -- "Schedule
        # 10BA-- [see Endnote 4]" and nothing else -- and the heading is
        # part of the compilation, so it keeps its own file.
        if empty is None:
            return
        path, headings = empty
        if not path:
            return
        unit = Container([], headings, [])
        target = path + ".md"
        if target in files:
            # Schedule 2's title is reprinted as its list of forms runs on,
            # so the same container closes empty more than once. Each
            # reprint joins the file in the order it is printed.
            files[target] = files[target].rstrip() + "\n\n" + unit.render()
        else:
            files[target] = unit.render()

    def flush_container():
        # Text that sits under a container with no provision heading of its
        # own. Dropping it would lose real law: the forms in the merger
        # determinations are whole Divisions written this way.
        if not orphan:
            return
        unit = Container(stack, pending, orphan)
        if unit.path is None:
            raise ValueError("law text under an unnumbered container: %r"
                             % (pending[-1] if pending else None))
        if unit.path in files:
            # A Schedule's text arrives in more than one run: Schedule 2 of
            # the Corporations Regulations is a long list of forms whose
            # title is reprinted along the way, and it spans volumes. The
            # runs belong to one Schedule, so they join in the order the
            # compilation prints them.
            files[unit.path] = (files[unit.path].rstrip() + "\n\n"
                                + unit.render())
            if notes is not None:
                notes.append({"kind": "container-continued", "path": unit.path})
        else:
            files[unit.path] = unit.render()
        del orphan[:]
        del pending[:]

    def flush():
        if current is None:
            return
        if current.path not in files:
            files[current.path] = current.render()
            return
        # The source itself numbers two provisions alike. The 2001
        # compilation of the Corporations Regulations heads both
        # "Controller's notice to owner or lessor of property" and
        # "Certified copies of reports" as regulation 5.2.01; later
        # compilations number the second 5.2.02. Renumbering it here would
        # alter the law, and dropping it would lose a regulation, so both
        # stay in the file their shared number names, in the order the
        # compilation prints them, and the anomaly is reported.
        files[current.path] = files[current.path].rstrip() + "\n\n" + current.render()
        if notes is not None:
            notes.append({"kind": "duplicate-number", "path": current.path,
                          "number": current.number})

    blocks = list(blocks)
    for block in blocks[:_law_text_end(blocks)]:
        if block.kind == "table":
            rows = [" | ".join(c for c in row) for row in block.text]
            rows = [r for r in rows if r.strip()]
            if current is not None:
                current.lines.extend(rows)
            elif started:
                orphan.extend(rows)
                empty = None           # the container holds a table, so it is not empty
            else:
                front.extend(rows)
            continue

        style, text, spaced = block.style, block.text, block.spaced
        if not text:
            continue
        if style and SKIP_STYLE.match(style):
            continue
        if not started and text == "Contents":
            continue

        if style in CONTAINER_LEVEL:
            started = True
            flush()
            flush_container()
            current = None
            # Keyed by level, not by position: a Part following a Division
            # must replace the Division and everything under it, and the
            # outer levels a document never uses must not shift the rest.
            level = CONTAINER_LEVEL[style]
            if stack and stack[-1][0] >= level:
                flush_empty()          # the heading above closed with nothing in it
                del pending[:]         # the file just written accounts for them
            empty = None
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, text))
            match = SCHEDULE_RE.match(text)
            if match:
                schedule = match.group(1)
                pending = [text]
            else:
                if level == 0:
                    # A Schedule's reach ends where the next outermost
                    # heading begins, or provisions printed after a Schedule
                    # would be filed as its clauses.
                    schedule = None
                # `pending` only ever holds headings opened since the last
                # provision, so each new one is simply appended: a file
                # shows the headings that open at it, and no others.
                pending.append(text)
            empty = (_container_path(stack), list(pending))
            continue

        if style in PROVISION_STYLES:
            started = True
            flush()
            flush_container()
            empty = None
            match = HEADING_RE.match(spaced or text)
            if match:
                number, heading = match.group(1), match.group(2).strip()
            else:
                number, heading = text.split(" ", 1) if " " in text else (text, "")
            current = Provision(number, heading, pending, schedule)
            pending = []
            continue

        if current is not None:
            current.lines.append(text)
        elif started:
            orphan.append(text)
            empty = None
        else:
            if style in FRONT_STYLES or style is None or style == "subsection":
                front.append(text)

    flush()
    flush_container()
    flush_empty()
    if front:
        # The first front-matter paragraph is the instrument's own short
        # title, so it becomes the heading rather than being repeated
        # under one. Verification compares word sequences, and a title
        # printed twice here would be a difference against the source.
        files["intro.md"] = "# %s\n\n%s\n" % (
            front[0], "\n".join(front[1:])) if len(front) > 1 \
            else "# %s\n" % front[0]
    return files


def folder_for(instrument_name):
    return _slug(instrument_name)
