"""Settings that are valid but fit the use badly: said when the config is checked, never refused.

An error stops a config; advice does not. It is for a choice that works today and goes wrong later, such
as watching a column of a table by its cells: a row inserted in the middle moves every cell below it,
and all of them read as changed. Whoever wrote the settings (a person in the page, an AI editing the
file) sees it where the config is checked, with what to write instead.
"""

from __future__ import annotations

from collections import Counter
from typing import TYPE_CHECKING

from .textify import CellRefError, cell_refs

if TYPE_CHECKING:
    from .config import Config, Target

LONG_RANGE_ROWS = 20  # a range of cells this many rows tall is a column of a table, not a few fixed cells

ROWS_INSTEAD = (
    "表の列を見るなら、行ごとの見張りが合っています（行の挿入や並べ替えでずれない）:\n"
    "  key: No.                     # 行を見分ける列（番号や ID）\n"
    "  columns: [質問, ステータス]  # 変わったら知らせる列（見出しの名前で）\n"
    "  show: [回答者, 期限]         # 変わった行と一緒に見るだけの列（任意）"
)


def target_advice(t: Target, cell_targets_on_source: int = 1) -> list[str]:
    """What fits this target's use better, if anything."""
    if not t.cells:
        return []
    try:
        refs = cell_refs(t)
    except CellRefError:
        return []  # the config check says what is wrong with them
    long = [r for r in refs if r.max_row - r.min_row + 1 >= LONG_RANGE_ROWS]
    if not long:
        return []
    rows = max(r.max_row - r.min_row + 1 for r in long)
    said = (
        f"cells で、列を {rows} 行続けて見ています。cells は番地で比べるので、途中に行が挿入されると、"
        "その下のセルが全部「変わった」と出ます。"
    )
    if cell_targets_on_source > 1:
        said += f"同じファイルを cells で見る見張りがほかにもあります（{cell_targets_on_source} 件）。1 つにまとめられます。"
    return [said + "\n" + ROWS_INSTEAD]


def config_advice(cfg: Config) -> list[str]:
    """Advice for every target, each line starting with the target's name."""
    on_source = Counter(t.source for t in cfg.targets if t.cells)
    out = []
    for t in cfg.targets:
        out += [f"「{t.name}」: {a}" for a in target_advice(t, on_source[t.source])]
    return out
