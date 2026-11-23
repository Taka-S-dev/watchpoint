"""Word documents as lines to compare, from what officetext reads.

officetext reads the document the way a person sees it (automatic numbers, header and footer,
footnotes, text boxes, content controls, tracked changes); this only decides how each part is
written as a line, so a change anywhere shows as the line it is on.
"""

from __future__ import annotations

from pathlib import Path

from officetext import read_document

from .tables import one_line

HEADER = "[ヘッダー] "
FOOTER = "[フッター] "
TEXTBOX = "[テキストボックス] "


def docx_to_text(path: Path) -> str:
    """The header, the body in document order (headings as Markdown headings, with their numbers),
    text boxes, footnotes, then the footer."""
    doc = read_document(path)
    out = [HEADER + line for line in doc.header]
    for b in doc.blocks:
        label = f"{b.label} " if b.label else ""
        if b.kind == "heading":
            out.append("#" * min(b.level, 6) + " " + label + b.text)
        elif b.kind == "list":
            out.append(label + b.text)
        elif b.kind == "paragraph":
            out.append(b.text)
        elif b.kind == "textbox":
            out.append(TEXTBOX + one_line(b.text))
        elif b.kind == "table":
            for row in b.rows:
                cells = [one_line(c) for c in row]
                if any(cells):
                    out.append("| " + " | ".join(cells) + " |")
        # a table of contents says nothing the headings do not, and its page numbers go stale
    out += [f"[^{label}] {text}" for label, text in doc.notes.items()]
    out += [FOOTER + line for line in doc.footer]
    return "\n".join(out) + "\n"
