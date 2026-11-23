"""Excel workbooks (.xlsx, .xlsm) as a Workbook: every cell that holds something, as stored and as
shown, with what a plain openpyxl read loses:

- a formula Excel never calculated (a file saved by a tool): openpyxl gives None, which looks like
  an empty cell; here it keeps its formula and is marked not cached
- text in shapes and text boxes (openpyxl ignores the drawing layer, where review notes often are)
- comments, struck-through cells and struck words inside a cell (in a revision, "deleted")
- hidden and very hidden sheets, hidden rows and columns, merged ranges
- the printed header and footer (a document number, a revision)
- values as Excel displays them with their number format (15%, 1,234, 2026/10/01)
"""

from __future__ import annotations

import datetime as dt
import posixpath
import re
import zipfile
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from .model import Cell, Shape, Sheet, Workbook
from .package import check_unpacked_size

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"
_OFFICE_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_XDR = "{http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing}"
_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_DRAWING_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing"


def read_workbook(path: Path | str, sheets: list[str] | None = None) -> Workbook:
    """Read a workbook. It is opened for reading only; nothing is written anywhere.

    sheets: only these (by name), in the workbook's order; None for all of them.
    """
    from openpyxl import load_workbook

    path = Path(path)
    check_unpacked_size(path)
    values = load_workbook(path, data_only=True, read_only=False)
    formulas = load_workbook(path, data_only=False, read_only=False)  # so a formula never reads as blank
    warnings: list[str] = []
    try:
        shapes = _shapes(path)
        struck_runs = _struck_runs(path)
        out = []
        for ws in values.worksheets:
            if sheets is not None and ws.title not in sheets:
                continue
            out.append(_sheet(ws, formulas[ws.title], shapes.get(ws.title, ()), struck_runs, warnings))
        return Workbook(tuple(out), tuple(warnings))
    finally:
        values.close()
        formulas.close()


def _sheet(ws, fws, shapes, struck_runs: dict[str, str], warnings: list[str]) -> Sheet:
    from openpyxl.utils import get_column_letter
    from openpyxl.utils.cell import column_index_from_string

    hidden_rows = frozenset(r for r, dim in ws.row_dimensions.items() if dim.hidden)
    hidden_cols: set[str] = set()
    for letter, dim in ws.column_dimensions.items():
        if dim.hidden:
            lo = column_index_from_string(letter)
            hi = dim.max if dim.max and dim.max >= lo else lo
            hidden_cols |= {get_column_letter(c) for c in range(lo, min(hi, lo + 200) + 1)}
    cells = []
    uncached = []
    for row in ws.iter_rows():
        for cell in row:
            raw = cell.value
            fval = fws.cell(row=cell.row, column=cell.column).value
            formula = fval if isinstance(fval, str) and fval.startswith("=") else None
            if raw in (None, "") and formula is None:
                continue
            struck = bool(raw not in (None, "") and cell.has_style and cell.font is not None and cell.font.strike)
            shown = "" if raw is None else display(raw, cell.number_format)
            if struck:
                shown = f"~~{shown}~~"
            elif isinstance(raw, str) and raw in struck_runs:
                shown = struck_runs[raw]
            if formula is not None and raw is None:
                uncached.append(cell.coordinate)
            letter = get_column_letter(cell.column)
            cells.append(
                Cell(
                    address=cell.coordinate,
                    row=cell.row,
                    col=cell.column,
                    value=raw,
                    shown=shown,
                    formula=formula,
                    cached=not (formula is not None and raw is None),
                    struck=struck,
                    comment=cell.comment.text if cell.comment else None,
                    hidden=cell.row in hidden_rows or letter in hidden_cols,
                )
            )
    if uncached:
        warnings.append(
            f"{ws.title}: 計算されていない式が {len(uncached)} 個あります（{', '.join(uncached[:5])}"
            + (" ほか" if len(uncached) > 5 else "")
            + "）。Excel で開いて保存するまで、結果の値はありません"
        )
    header, footer = _header_footer(ws)
    return Sheet(
        name=ws.title,
        state=ws.sheet_state,
        cells=tuple(cells),
        shapes=tuple(shapes),
        merged=tuple(str(r) for r in ws.merged_cells.ranges),
        header=header,
        footer=footer,
        hidden_rows=hidden_rows,
        hidden_cols=frozenset(hidden_cols),
    )


# --------------------------------------------------------------------------- header and footer

_HF_FIELDS = {"P": "[page]", "N": "[pages]", "D": "[date]", "T": "[time]", "F": "[file]", "A": "[sheet]",
              "Z": "[folder]", "G": "[picture]"}  # fmt: skip


def _hf_fields(t: str) -> str:
    """Header text with its fields named (&P page, &N pages, &F file, &A sheet) and its formatting
    codes (font, size, colour) dropped."""
    t = t.replace("&&", "\0")  # a literal &, kept apart first
    t = re.sub(r'&"[^"]*"', "", t)
    t = re.sub(r"&\d+", "", t)
    t = re.sub(r"&K[0-9A-Fa-f]{6}|&K\d{2}[+-]\d{3}", "", t)
    t = re.sub(r"&([A-Za-z])", lambda m: _HF_FIELDS.get(m[1].upper(), ""), t)
    return t.replace("\0", "&").strip()


def _header_footer(ws) -> tuple[tuple[str, ...], tuple[str, ...]]:
    out = []
    for hf in (getattr(ws, "oddHeader", None), getattr(ws, "oddFooter", None)):
        parts = []
        for pos in ("left", "center", "right"):
            t = getattr(getattr(hf, pos, None), "text", None)
            if t and t.strip():
                parts.append(_hf_fields(t))
        out.append(tuple(p for p in parts if p))
    return out[0], out[1]


# --------------------------------------------------------------------------- the package itself


def _safe_xml(data: bytes):
    from lxml import etree

    return etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False))


def _rels(z: zipfile.ZipFile, part: str) -> dict[str, tuple[str, str]]:
    """rId -> (type, target part name) of a part, targets resolved against the part's folder."""
    folder, name = posixpath.split(part)
    rels = posixpath.join(folder, "_rels", name + ".rels")
    if rels not in z.namelist():
        return {}
    out = {}
    for r in _safe_xml(z.read(rels)).iter(_REL + "Relationship"):
        if r.get("TargetMode") == "External":
            continue
        target = r.get("Target") or ""
        target = target.lstrip("/") if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
        out[r.get("Id") or ""] = (r.get("Type") or "", target)
    return out


def _sheet_parts(z: zipfile.ZipFile) -> dict[str, str]:
    """Sheet name -> its part name, from the workbook and its relationships."""
    if "xl/workbook.xml" not in z.namelist():
        return {}
    rels = _rels(z, "xl/workbook.xml")
    out = {}
    for s in _safe_xml(z.read("xl/workbook.xml")).iter(_MAIN + "sheet"):
        rid = s.get(_OFFICE_REL + "id") or ""
        if rid in rels:
            out[s.get("name") or ""] = rels[rid][1]
    return out


def _shapes(path: Path) -> dict[str, list[Shape]]:
    """Sheet name -> the text of its shapes and text boxes, each with the cell its corner is on."""
    from openpyxl.utils import get_column_letter

    out: dict[str, list[Shape]] = {}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            for title, part in _sheet_parts(z).items():
                for typ, target in _rels(z, part).values():
                    if typ != _DRAWING_REL or target not in names:
                        continue
                    for anchor in _safe_xml(z.read(target)):
                        frm = anchor.find(_XDR + "from")
                        at = ""
                        if frm is not None:
                            col, row = frm.findtext(_XDR + "col"), frm.findtext(_XDR + "row")
                            if col and row and col.isdigit() and row.isdigit():
                                at = f"{get_column_letter(int(col) + 1)}{int(row) + 1}"
                        for sp in anchor.iter(_XDR + "sp"):  # also those inside a group
                            text = _shape_text(sp)
                            if text:
                                box = sp.find(f"{_XDR}nvSpPr/{_XDR}cNvSpPr")
                                kind = "textbox" if box is not None and box.get("txBox") in ("1", "true") else "shape"
                                out.setdefault(title, []).append(Shape(text, at, kind))
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    return out


def _shape_text(sp) -> str:
    body = sp.find(_XDR + "txBody")
    if body is None:
        return ""
    paras = ["".join(t.text or "" for t in p.iter(_A + "t")).strip() for p in body.findall(_A + "p")]
    return "\n".join(p for p in paras if p)


def _struck_runs(path: Path) -> dict[str, str]:
    """Rich text with some words struck: plain text -> text with ~~marks~~ (openpyxl reads a cell's
    text without its runs, and the cell's own font is not struck when only some runs are). A text
    written both struck and not struck is left out: which cell holds which cannot be told."""
    texts = []
    try:
        with zipfile.ZipFile(path) as z:
            parts = {
                n: z.read(n)
                for n in z.namelist()
                if n == "xl/sharedStrings.xml" or re.fullmatch(r"xl/worksheets/[^/]+\.xml", n)
            }
        if any(b"strike" in raw for raw in parts.values()):
            for name, raw in parts.items():
                if name.endswith("sharedStrings.xml"):
                    texts += _safe_xml(raw).iter(_MAIN + "si")
                elif b"strike" in raw:
                    texts += _safe_xml(raw).iter(_MAIN + "is")
    except (zipfile.BadZipFile, OSError, ValueError):
        return {}
    out: dict[str, str] = {}
    clash: set[str] = set()
    plain_only: set[str] = set()
    for si in texts:
        runs = []
        for r in si.findall(_MAIN + "r"):
            text = "".join(t.text or "" for t in r.findall(_MAIN + "t"))
            st = r.find(f"{_MAIN}rPr/{_MAIN}strike")
            runs.append((text, st is not None and st.get("val", "1") not in ("0", "false")))
        if not any(s for t, s in runs if t.strip()):
            plain_only.add(
                "".join(t for t, _ in runs) if runs else "".join(t.text or "" for t in si.findall(_MAIN + "t"))
            )
            continue
        plain = "".join(t for t, _ in runs)
        marked = "".join(f"~~{t}~~" if s and t.strip() else t for t, s in runs).replace("~~~~", "")
        if out.get(plain, marked) != marked:
            clash.add(plain)
        out[plain] = marked
    return {k: v for k, v in out.items() if k not in clash and k not in plain_only}


# --------------------------------------------------------------------------- number formats

_DATE_TOKEN = re.compile(r"yyyy|yy|mmmm|mmm|mm|m|dddd|ddd|dd|d|hh|h|ss|s|am/pm|a/p|\[h\]|ge+|e")


def display(v: Any, fmt: str | None) -> str:
    """What Excel displays for a value with this number format, for the common formats; an unknown
    or exotic format falls back to the plain value rather than a guess."""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, int | float):
        shown = format_number(v, fmt or "General")
        return _plain(shown) if not isinstance(shown, str) else shown
    if isinstance(v, dt.datetime | dt.date | dt.time):
        return _date(v, fmt)
    return str(v)


def _plain(v: int | float) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _date(v, fmt: str | None) -> str:
    low = re.sub(r"\[[^\]]*\]|\"[^\"]*\"|\\.", "", (fmt or "General").split(";")[0]).strip().lower()
    if not _DATE_TOKEN.search(low) or "ge" in low or low.startswith("e"):
        # General, text, or a Japanese era: ISO is the least surprising
        if isinstance(v, dt.datetime):
            return v.strftime("%Y-%m-%d") if v.time() == dt.time() else v.strftime("%Y-%m-%d %H:%M:%S")
        return v.isoformat()
    d = v if isinstance(v, dt.datetime) else dt.datetime.combine(v, dt.time()) if isinstance(v, dt.date) else v
    if isinstance(d, dt.time):
        d = dt.datetime.combine(dt.date(1900, 1, 1), d)
    out, prev_h, i = [], False, 0
    toks = list(_DATE_TOKEN.finditer(low))
    for k, m in enumerate(toks):
        out.append(low[i : m.start()])
        t = m.group(0)
        nxt = toks[k + 1].group(0) if k + 1 < len(toks) else ""
        if t == "yyyy":
            out.append(f"{d.year:04d}")
        elif t == "yy":
            out.append(f"{d.year % 100:02d}")
        elif t == "mmmm":
            out.append(d.strftime("%B"))
        elif t == "mmm":
            out.append(d.strftime("%b"))
        elif t in ("mm", "m"):
            unit = d.minute if prev_h or nxt.startswith("s") else d.month  # mm after h or before s is minutes
            out.append(f"{unit:02d}" if t == "mm" else str(unit))
        elif t == "dddd":
            out.append(d.strftime("%A"))
        elif t == "ddd":
            out.append(d.strftime("%a"))
        elif t in ("dd", "d"):
            out.append(f"{d.day:02d}" if t == "dd" else str(d.day))
        elif t in ("hh", "h", "[h]"):
            hour = d.hour if "am/pm" not in low and "a/p" not in low else (d.hour % 12 or 12)
            out.append(f"{hour:02d}" if t == "hh" else str(hour))
        elif t in ("ss", "s"):
            out.append(f"{d.second:02d}" if t == "ss" else str(d.second))
        elif t in ("am/pm", "a/p"):
            out.append("AM" if d.hour < 12 else "PM")
        prev_h = t in ("hh", "h", "[h]")
        i = m.end()
    out.append(low[i:])
    return "".join(out)


def _format_tokens(sec: str) -> list[tuple[bool, str]]:
    """A number-format section as (is_literal, text) pieces. Code is 0 # ? . , % E+ @ and General;
    quoted text, escaped characters and [$¥-411] currency are literal; colours, conditions, _x
    padding and *x fill are dropped."""
    out: list[tuple[bool, str]] = []
    i = 0
    while i < len(sec):
        c = sec[i]
        if c == '"':
            j = sec.find('"', i + 1)
            j = len(sec) if j < 0 else j
            out.append((True, sec[i + 1 : j]))
            i = j + 1
        elif c == "\\" and i + 1 < len(sec):
            out.append((True, sec[i + 1]))
            i += 2
        elif c == "[":
            j = sec.find("]", i)
            j = len(sec) if j < 0 else j
            m = re.match(r"\[\$([^\-\]]*)", sec[i : j + 1])
            if m and m.group(1):
                out.append((True, m.group(1)))
            i = j + 1
        elif c in "_*":
            i += 2
        elif sec[i : i + 7].lower() == "general":
            out.append((False, "G"))
            i += 7
        elif c in "0#?.,%@":
            out.append((False, c))
            i += 1
        elif c in "Ee" and i + 1 < len(sec) and sec[i + 1] in "+-":
            out.append((False, "E"))
            i += 2
        else:
            out.append((True, c))
            i += 1
    return out


def format_number(v: int | float, fmt: str) -> int | float | str:
    """A number as Excel shows it: the section for negatives (#,##0;(#,##0) gives (1,200)) and zero,
    the text around the digits (0"件" gives 5件), percent, decimals, thousands separators and scaling
    by trailing commas. General stays a number."""
    secs, cur, quoted, i = [], "", False, 0  # split on ; outside quotes, keeping empty sections
    while i < len(fmt):
        c = fmt[i]
        if c == "\\" and not quoted and i + 1 < len(fmt):
            cur += fmt[i : i + 2]
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        if c == ";" and not quoted:
            secs.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    secs.append(cur)
    general = v if isinstance(v, int) else float(f"{v:.10g}") if abs(v) < 1e15 else v
    cond = [re.search(r"\[(<=|>=|<>|<|>|=)\s*(-?[\d.]+)\]", s) for s in secs]
    sign = ""
    if any(cond):
        ops = {"<": lambda a, b: a < b, ">": lambda a, b: a > b, "=": lambda a, b: a == b,
               "<=": lambda a, b: a <= b, ">=": lambda a, b: a >= b, "<>": lambda a, b: a != b}  # fmt: skip
        pick = next((k for k, m in enumerate(cond) if m and ops[m[1]](v, float(m[2]))), None)
        if pick is None:
            pick = next((k for k, m in enumerate(cond) if not m), len(secs) - 1)
        sec, x = secs[pick], abs(v)
        sign = "-" if v < 0 and not cond[pick] else ""
    elif v < 0 and len(secs) > 1:
        sec, x = secs[1], -v
    elif v == 0 and len(secs) > 2:
        sec, x = secs[2], v
    else:
        sec, x = secs[0], abs(v)
        sign = "-" if v < 0 else ""
    if not sec:  # an empty section hides the value
        return ""
    toks = _format_tokens(sec)
    code = "".join(t for lit, t in toks if not lit)
    if not code:
        if re.search(r"[A-Za-z]", re.sub(r'"[^"]*"|\\.|\[[^\]]*\]', "", sec)):
            return general  # a date or weekday code on a number: left as the number
        return "".join(t for _, t in toks)
    first = next(k for k, (lit, _) in enumerate(toks) if not lit)
    last = max(k for k, (lit, _) in enumerate(toks) if not lit)

    def lit(part):  # a lone backslash is the yen sign in Japanese Excel's currency formats
        return "".join("¥" if t == "\\" else t for _, t in part)

    prefix, suffix = lit(toks[:first]), lit(toks[last + 1 :])
    if "G" in code or "@" in code:
        if not prefix and not suffix:
            return general
        body = f"{x:.10g}"
    elif "E" in code:
        dec = len(re.match(r"[0#?]*", code.split(".", 1)[1]).group(0)) if "." in code.split("E")[0] else 0
        body = f"{x:.{dec}E}"
    else:
        whole = code.split(".", 1)[0].replace("%", "")
        x = x / 1000 ** (len(whole) - len(whole.rstrip(",")))
        dec = len(re.match(r"[0#?]*", code.split(".", 1)[1]).group(0)) if "." in code else 0
        if "%" in code:
            x *= 100
        try:  # Excel rounds half up, not to even
            q = Decimal(repr(x)).quantize(Decimal(1).scaleb(-dec), rounding=ROUND_HALF_UP)
        except (InvalidOperation, ValueError):
            q = x
        body = f"{q:,.{dec}f}" if re.search(r",+[0#?]", whole) else f"{q:.{dec}f}"
        head, dot, tail = body.partition(".")
        zeros = whole.count("0")
        if head.replace(",", "") == "0" and not zeros:
            head = ""
        elif zeros and len(head.replace(",", "")) < zeros:
            head = head.zfill(zeros) if "," not in head else head
        body = head + dot + tail
        if "%" in code:
            body += "%"
    return f"{sign}{prefix}{body}{suffix}"
