"""The page stays readable: text against its background passes WCAG AA (4.5:1) in both light and dark."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

CSS = (Path(__file__).resolve().parent.parent / "src" / "watchpoint" / "gui.css").read_text(encoding="utf-8")

# text colour, background colour: each pair the page draws small text with
PAIRS = [
    ("text", "bg"), ("text", "panel"), ("muted", "bg"), ("muted", "panel"), ("muted", "dim-bg"),
    ("changed", "changed-bg"), ("failed", "failed-bg"), ("ok", "ok-bg"), ("first", "first-bg"),
    ("add", "add-bg"), ("del", "del-bg"), ("mod", "mod-bg"), ("col", "col-bg"), ("accent-text", "accent"),
    ("add", "panel"), ("del", "panel"),  # what is now / was before, in the table of changes
    ("add", "changed-bg"), ("del", "changed-bg"), ("muted", "changed-bg"),  # inside a changed cell of the sheet
]  # fmt: skip


def _tokens(block: str) -> dict[str, str]:
    return dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", block))


def _themes() -> dict[str, dict[str, str]]:
    light = _tokens(CSS[CSS.index(":root {") : CSS.index("}", CSS.index(":root {"))])
    start = CSS.index("@media (prefers-color-scheme: dark)")
    dark = {**light, **_tokens(CSS[start : CSS.index("}", CSS.index(":root {", start))])}
    return {"light": light, "dark": dark}


def _contrast(a: str, b: str) -> float:
    def lum(h: str) -> float:
        c = [int(h[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        c = [x / 12.92 if x <= 0.03928 else ((x + 0.055) / 1.055) ** 2.4 for x in c]
        return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]

    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize(("fg", "bg"), PAIRS)
def test_text_is_readable_on_its_background(theme: str, fg: str, bg: str) -> None:
    t = _themes()[theme]
    ratio = _contrast(t[fg], t[bg])
    assert ratio >= 4.5, f"{theme}: --{fg} on --{bg} is {ratio:.2f}:1"


def test_font_sizes_come_from_the_type_scale() -> None:
    """Five named steps in :root; a size typed in place would start a sixth."""
    assert (
        re.findall(
            r"font-size:\s*[\d.]+(?:px|em|rem|pt)|font:\s*[\d.]+(?:px|em|rem|pt)",
            CSS[CSS.index("color-scheme: light;") :],
        )
        == []
    )
    assert len(re.findall(r"--fs-[a-z]+:", CSS)) == 5


def test_no_rule_styles_a_change_kind_on_its_own() -> None:
    """added / changed / removed / columns name a badge, a table row and a cell; a rule for the bare class
    (once a panel shown after adding a target) dresses every one of them as that panel."""
    bare = re.findall(r"(?:^|[\s,}])\.(added|changed|removed|columns)\b", CSS, re.M)
    assert not bare
