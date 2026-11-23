"""Web pages: the text of the chosen part, blocks as lines and tables as rows."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import Target


_BLOCK = {
    "p",
    "div",
    "section",
    "article",
    "main",
    "header",
    "footer",
    "aside",
    "nav",
    "table",
    "thead",
    "tbody",
    "tfoot",
    "ul",
    "ol",
    "dl",
    "dt",
    "dd",
    "pre",
    "blockquote",
    "form",
    "fieldset",
    "figure",
    "figcaption",
    "details",
    "summary",
    "hr",
    "address",
}


def html_to_text(html: str | bytes, target: Target) -> str:
    """Readable text of a page (or of the parts picked by `selector`), without scripts and styles."""
    from bs4 import BeautifulSoup, NavigableString

    soup = BeautifulSoup(html, "html.parser")
    for el in soup(["script", "style", "noscript", "template", "svg"]):
        el.decompose()
    for sel in target.remove:
        for el in soup.select(sel):
            el.decompose()
    if target.selector:
        roots = soup.select(target.selector)
        if not roots:
            raise ValueError(f"selector「{target.selector}」に当たる部分がページにありません")
    else:
        roots = [soup.body or soup]

    lines: list[str] = []
    buf: list[str] = []

    def flush() -> None:
        text = re.sub(r"\s+", " ", "".join(buf)).strip()
        if text:
            lines.append(text)
        buf.clear()

    def walk(node) -> None:
        for child in node.children:
            if isinstance(child, NavigableString):
                buf.append(str(child))
                continue
            name = child.name or ""
            if re.fullmatch(r"h[1-6]", name):
                flush()
                text = child.get_text(" ", strip=True)
                if text:
                    lines.append("#" * int(name[1]) + " " + text)
            elif name == "tr":
                flush()
                cells = [c.get_text(" ", strip=True).replace("|", "｜") for c in child.find_all(["th", "td"])]
                if any(cells):
                    lines.append("| " + " | ".join(cells) + " |")
            elif name == "li":
                flush()
                text = child.get_text(" ", strip=True)
                if text:
                    lines.append("- " + text)
            elif name == "br":
                flush()
            elif name == "img":
                alt = child.get("alt")
                if alt:
                    buf.append(f"[画像: {alt}]")
            elif name in _BLOCK:
                flush()
                walk(child)
                flush()
            else:
                walk(child)

    for root in roots:
        walk(root)
        flush()
    return "\n".join(lines) + "\n"
