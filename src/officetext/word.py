"""Word documents (.docx) as a Document: the body in order, header and footer, notes and changes.

A plain python-docx walk loses what this keeps:

- automatic numbers of headings and lists ("1.2", "(a)", "第3条" live in numbering.xml, not in
  the text, so a plain walk prints "概要" where the document says "1.2 概要")
- the header and footer (a document number, a revision, "社外秘")
- footnotes and endnotes, numbered as the reader sees them
- text boxes (kept apart from the paragraph they float over)
- content controls (python-docx skips whatever a w:sdt wraps, sometimes whole sections and the
  answer cells of a form)
- tracked changes and comments, marked where they are
- the table of contents, left as one block instead of a page of stale page numbers
"""

from __future__ import annotations

import re
import zipfile
from pathlib import Path

from .model import Block, Change, Document
from .package import check_unpacked_size

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
}
_HALF_KANA = dict(
    zip(
        "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲンヰヱ",
        "ｱｲｳｴｵｶｷｸｹｺｻｼｽｾｿﾀﾁﾂﾃﾄﾅﾆﾇﾈﾉﾊﾋﾌﾍﾎﾏﾐﾑﾒﾓﾔﾕﾖﾗﾘﾙﾚﾛﾜｦﾝｲｴ",
        strict=True,
    )
)
_BOX_DEPTH = 3  # text boxes inside text boxes, as deep as is read


def _q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


W = NS["w"].join("{}")  # "{...main}" to prefix a local name


def _squash(text: str) -> str:
    """Runs of spaces, tabs and line breaks as one space; a full-width space is text and stays."""
    return re.sub(r"[ \t\r\n]+", " ", text)


def read_document(path: Path | str) -> Document:
    """Read a .docx. The file is opened for reading only; nothing is written anywhere."""
    path = Path(path)
    check_unpacked_size(path)
    return _Reader(path).read()


def _safe_xml(data: bytes):
    from lxml import etree

    parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False)
    return etree.fromstring(data, parser)


class _Reader:
    def __init__(self, path: Path) -> None:
        from docx import Document as open_docx

        self.path = path
        self.doc = open_docx(str(path))
        self.warnings: list[str] = []
        self._unwrap_table_controls()
        self._load_numbering()
        self.counters: dict[str, dict[int, int]] = {}
        self.notes_by_id = self._load_notes("word/footnotes.xml", "footnote") | self._load_notes(
            "word/endnotes.xml", "endnote"
        )
        self.note_label: dict[str, str] = {}
        self.note_count: dict[str, int] = {}
        self.changes: dict[str, Change] = {}
        self.comments = self._load_comments()

    # -- the whole document -----------------------------------------------------------------

    def read(self) -> Document:
        blocks = self._blocks()
        header, footer = self._header_footer()
        notes = {label: self.notes_by_id[key] for key, label in self.note_label.items() if key in self.notes_by_id}
        return Document(
            blocks=tuple(blocks),
            header=tuple(header),
            footer=tuple(footer),
            notes=notes,
            changes=tuple(self.changes.values()),
            warnings=tuple(self.warnings),
        )

    def _blocks(self) -> list[Block]:
        out: list[Block] = []
        toc = 0
        for kind, obj in self._iter_body():
            if kind == "toc":
                out.append(Block("toc", f"{obj}"))
                continue
            if kind == "t":
                if toc:
                    out.append(Block("toc", f"{toc}"))
                    toc = 0
                out.append(Block("table", rows=self._table_rows(obj)))
                continue
            if re.match(r"(?:toc|目次)\s*\d", self._style_name(obj), re.I):
                toc += 1  # a table of contents typed in TOC styles rather than as a field
                continue
            if toc:
                out.append(Block("toc", f"{toc}"))
                toc = 0
            text, boxes = self._para(obj._p)
            text = text.strip()
            sect_only = (
                not text and not boxes and obj._p.pPr is not None and obj._p.pPr.find(W + "sectPr") is not None
            )  # an empty paragraph carrying only a section break takes no number in Word
            label, bullet = ("", False) if sect_only else self._label(self._num_pr(obj))
            level = self._heading_level(obj)
            if text:
                if level:
                    out.append(Block("heading", _squash(text), level=level, label=label))
                elif bullet or label:
                    depth = (self._num_pr(obj) or ("", 0))[1]
                    out.append(Block("list", _squash(text), level=depth, label="•" if bullet else label))
                else:
                    out.append(Block("paragraph", text))
            out += [Block("textbox", b) for b in boxes]
        if toc:
            out.append(Block("toc", f"{toc}"))
        return out

    # -- body walk ----------------------------------------------------------------------------

    def _iter_body(self):
        """('p', paragraph) / ('t', table) / ('toc', entries) in document order, going into content
        controls instead of skipping what they wrap."""
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        def walk(container):
            for child in container.iterchildren():
                if child.tag == W + "p":
                    yield "p", Paragraph(child, self.doc)
                elif child.tag == W + "tbl":
                    yield "t", Table(child, self.doc)
                elif child.tag == W + "sdt":
                    gallery = child.find(".//" + W + "docPartGallery")
                    content = child.find(W + "sdtContent")
                    if gallery is not None and "Table of Contents" in (gallery.get(W + "val") or ""):
                        yield "toc", len(content.findall(".//" + W + "p")) if content is not None else 0
                    elif content is not None:
                        yield from walk(content)

        yield from walk(self.doc.element.body)

    def _unwrap_table_controls(self) -> None:
        """A content control around a whole cell or row hides it from python-docx, which reads only
        direct w:tc / w:tr children: a form's answer cells vanish. Its cells are moved up in its
        place, in memory only."""
        kids = {W + "tbl": (W + "tr",), W + "tr": (W + "tc",), W + "tc": (W + "p", W + "tbl")}
        for _ in range(3):  # nested controls, a few levels deep
            changed = False
            for sdt in list(self.doc.element.body.iter(W + "sdt")):
                parent = sdt.getparent()
                content = sdt.find(W + "sdtContent")
                if parent is None or parent.tag not in kids or content is None:
                    continue
                at = parent.index(sdt)
                for k, child in enumerate([c for c in content if c.tag in (*kids[parent.tag], W + "sdt")]):
                    parent.insert(at + k, child)
                parent.remove(sdt)
                changed = True
            if not changed:
                break

    def _table_rows(self, table, depth: int = 0) -> tuple[tuple[str, ...], ...]:
        rows = []
        for row in table.rows:
            cells: list[str] = []
            prev = None
            for cell in row.cells:
                if cell._tc is prev:  # python-docx repeats a cell merged across columns
                    continue
                prev = cell._tc
                cells.append("\n".join(self._cell_lines(cell, depth)))
            rows.append(tuple(cells))
        return tuple(rows)

    def _cell_lines(self, cell, depth: int) -> list[str]:
        """A cell's paragraphs and the tables nested in it (one line per row), in order."""
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        lines: list[str] = []
        for child in cell._tc.iterchildren():
            if child.tag == W + "p":
                p = Paragraph(child, cell)
                text, boxes = self._para(child)
                label, bullet = self._label(self._num_pr(p))
                text = text.strip()
                if text:
                    lines.append(("• " if bullet else f"{label} " if label else "") + text)
                lines += boxes
            elif child.tag == W + "tbl" and depth < 3:
                for row in self._table_rows(Table(child, cell), depth + 1):
                    joined = " | ".join(c.replace("\n", " / ") for c in row if c.strip())
                    if joined:
                        lines.append(joined)
        return lines

    # -- one paragraph ------------------------------------------------------------------------

    def _para(self, p) -> tuple[str, list[str]]:
        parts: list[str] = []
        boxes: list[str] = []
        self._collect(p, parts, boxes)
        # Word splits one edit or one struck phrase into many runs: join them back into one span
        text = "".join(parts).replace("++}{++", "").replace("--}{--", "").replace("~~~~", "")
        return text, boxes

    def _collect(self, node, parts: list[str], boxes: list[str], depth: int = 0) -> None:
        """Walk a paragraph's XML in order: text, tabs and breaks; text boxes to `boxes`;
        tracked changes, comments, note references and furigana as inline marks."""
        for child in node:
            tag = child.tag
            if not isinstance(tag, str):
                continue
            if tag == _q("mc", "AlternateContent"):
                choice = child.find(_q("mc", "Choice"))  # the VML fallback repeats the same box
                self._collect(choice if choice is not None else child, parts, boxes, depth)
            elif tag.endswith("}txbxContent"):
                text = self._box(child, depth)
                if text:
                    boxes.append(text)
            elif tag in (W + "instrText", W + "delText"):
                continue
            elif tag in (W + "ins", W + "moveTo", W + "del", W + "moveFrom"):
                deleted = tag in (W + "del", W + "moveFrom")
                if deleted:
                    text = "".join(x.text or "" for x in child.iter(W + "delText", W + "t"))
                else:
                    inner: list[str] = []
                    self._collect(child, inner, boxes, depth)
                    text = "".join(inner)
                self._note_change(child, "deleted" if deleted else "inserted", text)
                if text.strip():
                    clean = text.replace("{", "(").replace("}", ")")
                    parts.append(f"{{--{clean}--}}" if deleted else f"{{++{clean}++}}")
                elif not deleted:
                    parts.append(text)
            elif tag == W + "commentReference":
                c = self.comments.get(child.get(W + "id") or "")
                if c:
                    who = re.sub(r"[{}<>]", " ", c[0])
                    body = re.sub(r"[{}<>]", " ", _squash(c[1]).strip())[:500]
                    parts.append(f"{{>>{who}: {body}<<}}")
            elif tag in (W + "t", _q("m", "t")):
                parts.append(child.text or "")
            elif tag == W + "tab":
                parts.append("\t")
            elif tag in (W + "br", W + "cr"):
                parts.append("\n")
            elif tag == W + "ruby":
                base: list[str] = []
                reading: list[str] = []
                rb, rt = child.find(W + "rubyBase"), child.find(W + "rt")
                if rb is not None:
                    self._collect(rb, base, boxes, depth)
                if rt is not None:
                    self._collect(rt, reading, boxes, depth)
                b, r = "".join(base).strip(), "".join(reading).strip()
                parts.append(f"｜{b}《{r}》" if b and r else b)
            elif tag in (W + "footnoteReference", W + "endnoteReference"):
                # numbered in the order they are referenced, as printed, not by their id in the file
                prefix = "e" if tag == W + "endnoteReference" else ""
                key = prefix + (child.get(W + "id") or "")
                if key not in self.note_label:
                    self.note_count[prefix] = self.note_count.get(prefix, 0) + 1
                    self.note_label[key] = f"{prefix}{self.note_count[prefix]}"
                parts.append(f"[^{self.note_label[key]}]")
            elif tag in (W + "drawing", W + "pict", W + "object"):
                for box in child.iter(W + "txbxContent"):  # the outermost boxes; inner ones by their own walk
                    anc = box.getparent()
                    while anc is not None and anc is not child and anc.tag != W + "txbxContent":
                        anc = anc.getparent()
                    if anc is child:
                        text = self._box(box, depth)
                        if text:
                            boxes.append(text)
            elif tag == W + "r" and self._struck(child):
                inner = []
                self._collect(child, inner, boxes, depth)
                text = "".join(inner)
                parts.append(f"~~{text}~~" if text.strip() else text)
            else:
                self._collect(child, parts, boxes, depth)

    def _box(self, box, depth: int) -> str:
        """A text box's own paragraphs (and those in its tables); boxes inside it come from those
        paragraphs' walks, so nothing is read twice."""
        if depth > _BOX_DEPTH:
            return ""
        lines = []
        for p in box.findall(W + "p") + box.findall(W + "tbl//" + W + "p"):
            inner: list[str] = []
            inner_boxes: list[str] = []
            self._collect(p, inner, inner_boxes, depth + 1)
            text = "".join(inner).strip()
            if text:
                lines.append(text)
            lines += inner_boxes
        return "\n".join(lines)

    @staticmethod
    def _struck(run) -> bool:
        rpr = run.find(W + "rPr")
        if rpr is None:
            return False
        for name in ("strike", "dstrike"):
            el = rpr.find(W + name)
            if el is not None and (el.get(W + "val") or "true").lower() not in ("0", "false", "off"):
                return True
        return False

    def _note_change(self, el, kind: str, text: str) -> None:
        cid = el.get(W + "id") or str(len(self.changes))
        if cid in self.changes or not text.strip():
            return
        self.changes[cid] = Change(
            kind,
            _squash(text).strip()[:300],
            author=_squash(el.get(W + "author") or "").strip()[:80],
            date=(el.get(W + "date") or "")[:10],
        )

    # -- headings and numbers -----------------------------------------------------------------

    @staticmethod
    def _style_name(paragraph) -> str:
        try:
            return (paragraph.style.name or "") if paragraph.style else ""
        except Exception:  # noqa: BLE001 - a style the document names but does not define
            return ""

    @staticmethod
    def _heading_level(paragraph) -> int:
        try:
            style = paragraph.style
        except Exception:  # noqa: BLE001
            return 0
        seen = 0
        while style is not None and seen < 10:
            name = style.name or ""
            m = re.match(r"(?:Heading|見出し)\s*(\d)", name)
            if m:
                return int(m[1])
            if name in ("Title", "表題"):
                return 1
            style = style.base_style
            seen += 1
        return 0

    def _load_numbering(self) -> None:
        """numId -> {ilvl: {fmt, text, start}} from numbering.xml, with each num's overrides.

        Word counts every num naming one abstractNum as one list, and a num overriding a level's
        start restarts that level (and those below it) where it is first used.
        """
        self.num: dict[str, dict[int, dict]] = {}
        self.num_abs: dict[str, str] = {}
        self.num_restart: dict[str, set[int]] = {}
        self.num_used: set[str] = set()
        try:
            root = self.doc.part.numbering_part.element
        except Exception:  # noqa: BLE001 - no numbering part: nothing is numbered
            return

        def clamp(v, default: int = 1) -> int:
            try:
                return max(0, min(32767, int(v)))
            except (TypeError, ValueError):
                return default

        def level(lvl) -> dict:
            fmt, txt, st = lvl.find(W + "numFmt"), lvl.find(W + "lvlText"), lvl.find(W + "start")
            il = clamp(lvl.get(W + "ilvl"), 0)
            text = (txt.get(W + "val") if txt is not None else f"%{il + 1}.") or ""
            return {
                "fmt": fmt.get(W + "val") if fmt is not None else "decimal",
                "text": re.sub(r"[\x00-\x1f\x7f]", "", text)[:40],
                "start": clamp(st.get(W + "val")) if st is not None else 1,
            }

        abstract: dict[str, dict[int, dict]] = {}
        style_link: dict[str, str] = {}
        num_link: dict[str, str] = {}
        for an in root.findall(W + "abstractNum"):
            aid = an.get(W + "abstractNumId")
            abstract[aid] = {clamp(lv.get(W + "ilvl"), 0): level(lv) for lv in an.findall(W + "lvl")}
            el = an.find(W + "styleLink")
            if el is not None and el.get(W + "val"):
                style_link[el.get(W + "val")] = aid
            el = an.find(W + "numStyleLink")
            if el is not None and el.get(W + "val"):
                num_link[aid] = el.get(W + "val")
        for num in root.findall(W + "num"):
            ref = num.find(W + "abstractNumId")
            named = ref.get(W + "val") if ref is not None else ""
            aid = style_link.get(num_link.get(named, ""), named)  # a list style: its definition's levels
            levels = {k: dict(v) for k, v in abstract.get(aid, {}).items()}
            nid = num.get(W + "numId")
            if named:
                self.num_abs[nid] = "a" + named
            for ov in num.findall(W + "lvlOverride"):
                il = clamp(ov.get(W + "ilvl"), 0)
                so = ov.find(W + "startOverride")
                if so is not None and il in levels:
                    levels[il]["start"] = clamp(so.get(W + "val"))
                    self.num_restart.setdefault(nid, set()).add(il)
                lv = ov.find(W + "lvl")
                if lv is not None:
                    levels[il] = level(lv)
            self.num[nid] = levels

    @staticmethod
    def _num_pr(paragraph) -> tuple[str, int] | None:
        """(numId, ilvl) from the paragraph, else from its style chain (each from the nearest style
        that sets it: "Clause 4" may set only its level and take the list from the style below)."""
        ppr = paragraph._p.pPr
        if ppr is not None and ppr.numPr is not None:
            nid = ppr.numPr.numId.val if ppr.numPr.numId is not None else None
            il = ppr.numPr.ilvl.val if ppr.numPr.ilvl is not None else 0
            if nid is not None:
                return (str(nid), max(0, min(8, int(il)))) if str(nid) != "0" else None
        try:
            style = paragraph.style
        except Exception:  # noqa: BLE001
            return None
        seen, lvl = 0, None
        while style is not None and seen < 10:
            sppr = style.element.find(W + "pPr")
            np_ = sppr.find(W + "numPr") if sppr is not None else None
            if np_ is not None:
                nid, il = np_.find(W + "numId"), np_.find(W + "ilvl")
                if lvl is None and il is not None:
                    try:
                        lvl = max(0, min(8, int(il.get(W + "val"))))
                    except (TypeError, ValueError):
                        pass
                if nid is not None:
                    return None if nid.get(W + "val") == "0" else (nid.get(W + "val"), lvl or 0)
            style = style.base_style
            seen += 1
        return None

    def _label(self, num: tuple[str, int] | None) -> tuple[str, bool]:
        """(label, is_bullet). Advances the counters: call once per paragraph, in order."""
        if num is None:
            return "", False
        nid, il = num
        levels = self.num.get(nid)
        if not levels or il not in levels:
            return "", False
        cnt = self.counters.setdefault(self.num_abs.get(nid, nid), {})
        if nid not in self.num_used:
            self.num_used.add(nid)
            for k in self.num_restart.get(nid, ()):
                if k >= il:
                    cnt.pop(k, None)
        cnt[il] = cnt.get(il, levels[il]["start"] - 1) + 1
        for k in [k for k in cnt if k > il]:
            del cnt[k]
        if levels[il]["fmt"] == "bullet":
            return "", True
        text = levels[il]["text"]
        for k in range(8, -1, -1):
            if f"%{k + 1}" in text:
                lk = levels.get(k, {"fmt": "decimal", "start": 1})
                text = text.replace(f"%{k + 1}", format_number(cnt.get(k, lk["start"]), lk["fmt"]))
        return text.strip(), False

    # -- header, footer, notes, comments ------------------------------------------------------

    def _header_footer(self) -> tuple[list[str], list[str]]:
        header: list[str] = []
        footer: list[str] = []
        for section in self.doc.sections:
            for into, parts in (
                (header, ("header", "first_page_header", "even_page_header")),
                (footer, ("footer", "first_page_footer", "even_page_footer")),
            ):
                for name in parts:
                    part = getattr(section, name)
                    if name != parts[0] and part.is_linked_to_previous:
                        continue  # a first-page or even-page header that is not set
                    for line in self._part_lines(part):
                        if line not in into:
                            into.append(line)
        return header, footer

    def _part_lines(self, part) -> list[str]:
        """Each paragraph of a header or footer, also those in its tables and text boxes; a
        paragraph gives only its own text, not that of a box it holds (a paragraph of its own)."""
        try:
            el = part._element
        except (AttributeError, ValueError):
            return []
        lines = []
        for p in el.iter(W + "p"):
            own = []
            for t in p.iter(W + "t"):
                anc = t.getparent()
                while anc is not None and anc.tag != W + "p":
                    anc = anc.getparent()
                if anc is p:
                    own.append(t.text or "")
            text = _squash("".join(own)).strip()
            if text and text not in lines:
                lines.append(text)
        return lines

    def _zip_part(self, name: str):
        try:
            with zipfile.ZipFile(self.path) as z:
                return _safe_xml(z.read(name)) if name in z.namelist() else None
        except (zipfile.BadZipFile, OSError):
            return None

    def _load_notes(self, part: str, kind: str) -> dict[str, str]:
        root = self._zip_part(part)
        if root is None:
            return {}
        out = {}
        for note in root.findall(W + kind):
            if note.get(W + "type") in ("separator", "continuationSeparator"):
                continue
            nid = note.get(W + "id") or ""
            if re.fullmatch(r"-?\d{1,9}", nid):
                text = _squash("".join(t.text or "" for t in note.iter(W + "t"))).strip()
                out[("e" if kind == "endnote" else "") + nid] = text
        return out

    def _load_comments(self) -> dict[str, tuple[str, str]]:
        root = self._zip_part("word/comments.xml")
        if root is None:
            return {}
        return {
            c.get(W + "id") or "": (c.get(W + "author") or "", "".join(t.text or "" for t in c.iter(W + "t")))
            for c in root.findall(W + "comment")
        }


def format_number(n: int, fmt: str) -> str:
    """A list counter as Word prints it in the given numFmt."""
    if fmt in ("decimal", "decimalZero"):
        return str(n)
    if fmt == "decimalFullWidth":
        return str(n).translate(str.maketrans("0123456789", "０１２３４５６７８９"))
    if fmt in ("lowerLetter", "upperLetter"):
        s, k = "", n
        while k > 0:
            k, r = divmod(k - 1, 26)
            s = chr(97 + r) + s
        return s.upper() if fmt == "upperLetter" else s
    if n < 0 or n > 32767:
        return str(n)
    if fmt in ("lowerRoman", "upperRoman"):
        if not 1 <= n <= 3999:
            return str(n)
        vals = [(1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"),
                (40, "xl"), (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")]  # fmt: skip
        s, k = "", n
        for v, sym in vals:
            while k >= v:
                s += sym
                k -= v
        return s.upper() if fmt == "upperRoman" else s
    if fmt == "decimalEnclosedCircle":
        return chr(0x2460 + n - 1) if 1 <= n <= 20 else str(n)
    if fmt in ("japaneseCounting", "japaneseLegal", "japaneseDigitalTenThousand", "chineseCounting"):
        digits = "〇一二三四五六七八九"
        if n < 10:
            return digits[n]
        if n < 100:
            tens, ones = divmod(n, 10)
            return ("" if tens == 1 else digits[tens]) + "十" + (digits[ones] if ones else "")
        return str(n)
    if fmt in ("aiueo", "aiueoFullWidth", "iroha", "irohaFullWidth"):
        kana = (
            "アイウエオカキクケコサシスセソタチツテトナニヌネノハヒフヘホマミムメモヤユヨラリルレロワヲン"
            if fmt.startswith("aiueo")
            else "イロハニホヘトチリヌルヲワカヨタレソツネナラムウヰノオクヤマケフコエテアサキユメミシヱヒモセス"
        )
        if not 1 <= n <= len(kana):
            return str(n)
        return kana[n - 1] if fmt.endswith("FullWidth") else _HALF_KANA.get(kana[n - 1], kana[n - 1])
    if fmt == "none":
        return ""
    return str(n)
