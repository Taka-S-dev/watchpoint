"""A demo of watchpoint you can run on this PC: sample files, a local web page, and changes on demand.

    uv run python demo/demo.py           # make the samples (first time) and open the window
    uv run python demo/demo.py change    # make the next round of changes (run while the window is open)
    uv run python demo/demo.py reset     # delete the samples and the history, to start over

Everything lives in %TEMP%\\watchpoint-demo; your own config and data are not touched.
The web page is served on 127.0.0.1 only, while the window is open.
"""

from __future__ import annotations

import datetime as dt
import functools
import os
import re
import shutil
import sys
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from openpyxl import Workbook, load_workbook

ROOT = Path(tempfile.gettempdir()) / "watchpoint-demo"
FILES = ROOT / "files"
WEB = ROOT / "web"
CONFIG = ROOT / "targets.yaml"
ROUND = ROOT / "round.json"
PORTS = range(18765, 18800)  # the first free one is chosen once and kept in the config

QUESTIONS = [
    ["〇〇案件 質問表"],
    [],
    ["No.", "質問", "回答者", "状態", "回答"],
    [1, "ログの保持期間は？", "佐藤", "未回答", ""],
    [2, "バックアップの頻度は？", "鈴木", "回答済", "毎日 2:00"],
    [3, "画面の文字コードは？", "佐藤", "未回答", ""],
]

CONFIG_TEXT = """\
# watchpoint のデモ（demo/demo.py が作った設定。好きに書き換えてよい）
vars:
  me: 佐藤

targets:
  - name: 質問表（自分の行）
    source: '{files}\\質問表.xlsx'
    tags: [デモ]
    sheets: [質問一覧]
    header_row: auto
    key: No.
    where:
      回答者: ${{me}}
    points: |
      新しく足された行は自分宛ての新しい質問。要旨を1行で。
      状態が変わった行は、何から何に変わったかを挙げる。
    notify_when: 自分宛ての新しい質問があるとき

  - name: 集計（合計と承認）
    source: '{files}\\集計.xlsx'
    tags: [デモ]
    cells: {{合計: 集計!C6, 承認: 集計!C8}}
    points: 合計が変わったら前の値と差を書く。承認欄が埋まったら知らせる。

  - name: 仕様書
    source: '{files}\\仕様書.md'
    tags: [デモ]
    points: 保持期間と性能の数字が変わったら、前と後を挙げる。

  - name: 議事録フォルダ
    source: '{files}\\議事録'
    tags: [デモ]
    report: [added]

  - name: 提案書（保存されたかだけ）
    source: '{files}\\提案書.pptx'
    tags: [デモ]
    watch: timestamp

  - name: お知らせページ
    source: http://127.0.0.1:{port}/news.html
    tags: [デモ, Web]
    selector: main
    points: 新しいお知らせの見出しを挙げる。

  - name: コミット履歴（コマンド）
    command: [findstr, /r, ., '{files}\\コミット履歴.tsv']   # 読むだけのコマンド。初めは「許可していない」と出る
    tags: [デモ]
    format: tsv
    fields: [rev, author, date, message]
    key: rev
    points: docs/ への新しいコミットを1行で。
"""

SPEC = """\
# 仕様書

## ログ
- 保持期間は 30 日
- 出力先は D:\\logs

## 性能
- 画面の応答は 2 秒以内
"""

NEWS = """\
<!doctype html><meta charset="utf-8"><title>お知らせ</title>
<header>お知らせポータル（デモ）　閲覧数 {views}</header>
<main>
<h1>お知らせ</h1>
<ul>
{items}
</ul>
<p>最終更新: {updated}</p>
</main>
"""


def _book(path: Path, sheets: dict[str, list[list]]) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)


def _news(items: list[str], views: int) -> None:
    body = "\n".join(f"<li>{i}</li>" for i in items)
    (WEB / "news.html").write_text(
        NEWS.format(items=body, views=views, updated=dt.datetime.now().strftime("%Y-%m-%d %H:%M")), encoding="utf-8"
    )


def setup() -> None:
    if CONFIG.exists():
        return
    (FILES / "議事録").mkdir(parents=True, exist_ok=True)
    WEB.mkdir(parents=True, exist_ok=True)
    _book(FILES / "質問表.xlsx", {"表紙": [["〇〇案件"], ["質問表"]], "質問一覧": QUESTIONS})
    _book(
        FILES / "集計.xlsx",
        {
            "集計": [
                ["月次集計"],
                [],
                ["項目", "", "件数"],
                ["A", "", 50],
                ["B", "", 70],
                ["合計", "", 120],
                [],
                ["承認", "", ""],
            ]
        },
    )
    (FILES / "仕様書.md").write_text(SPEC, encoding="utf-8")
    (FILES / "議事録" / "2026-11-16_定例.txt").write_text("前回の議事録\n", encoding="utf-8")
    (FILES / "提案書.pptx").write_bytes(b"demo slide deck v1")
    (FILES / "コミット履歴.tsv").write_text(
        "r2\tsato\t2026-11-20 09:00\t設計書を追加\nr1\tsato\t2026-11-19 18:00\t初期構成\n", encoding="utf-8"
    )
    _news(["11/20 システム更新のお知らせ", "11/18 健康診断の日程"], views=120)
    CONFIG.write_text(CONFIG_TEXT.format(files=FILES, port=_free_port()), encoding="utf-8")
    ROUND.write_text("0", encoding="utf-8")
    print(f"デモを作りました: {ROOT}")


def _round() -> int:
    try:
        return int(ROUND.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0


def change() -> None:
    if not CONFIG.exists():
        sys.exit("先に `uv run python demo/demo.py` でデモを作ってください")
    n = _round() + 1
    said: list[str] = []

    wb = load_workbook(FILES / "質問表.xlsx")
    ws = wb["質問一覧"]
    if n == 1:
        ws["D4"], ws["E4"] = "回答済", "90 日"
        ws.append([4, "障害時の連絡先は？", "佐藤", "未回答", ""])
        ws["E5"] = "毎日 3:00"  # 鈴木さんの行: where で外れるので知らせない
        said += ["質問表: No.1 が回答済に、自分宛ての No.4 を追加（鈴木さんの行の変更は知らせない）"]
    else:
        no = ws.max_row - 2
        ws.append([no, f"追加の質問 {n}", "佐藤", "未回答", ""])
        said += [f"質問表: 自分宛ての No.{no} を追加"]
    wb.save(FILES / "質問表.xlsx")

    wb = load_workbook(FILES / "集計.xlsx")
    ws = wb["集計"]
    ws["C4"] = ws["C4"].value + 15
    ws["C6"] = ws["C4"].value + ws["C5"].value
    if n == 1:
        ws["C8"] = "山田"
        said += [f"集計: 合計が {ws['C6'].value} に、承認欄に「山田」"]
    else:
        said += [f"集計: 合計が {ws['C6'].value} に"]
    wb.save(FILES / "集計.xlsx")

    spec = (FILES / "仕様書.md").read_text(encoding="utf-8")
    days = int(re.search(r"保持期間は (\d+) 日", spec).group(1))
    spec = spec.replace(f"保持期間は {days} 日", f"保持期間は {days * 3} 日")
    if n == 1:
        spec += "- 同時に使える人数は 200 人\n"
    (FILES / "仕様書.md").write_text(spec, encoding="utf-8")
    said += [f"仕様書: 保持期間を {days} 日 → {days * 3} 日" + ("、性能に1行追加" if n == 1 else "")]

    day = dt.date(2026, 11, 16) + dt.timedelta(days=7 * n)
    (FILES / "議事録" / f"{day:%Y-%m-%d}_定例.txt").write_text(f"第 {n + 1} 回の議事録\n", encoding="utf-8")
    said += [f"議事録フォルダ: {day:%Y-%m-%d}_定例.txt を追加"]

    with (FILES / "提案書.pptx").open("ab") as f:
        f.write(f" v{n + 1}".encode())
    said += ["提案書: 上書き保存（中身は見ず、保存されたことだけ）"]

    history = FILES / "コミット履歴.tsv"
    rev = f"r{n + 2}"
    who = ["yamada", "kato", "suzuki"][n % 3]
    history.write_text(
        f"{rev}\t{who}\t{dt.datetime.now():%Y-%m-%d %H:%M}\tdocs/ の変更 その {n}\n"
        + history.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    said += [f"コミット履歴: {rev}（{who}）を追加（コマンドの出力で見る）"]

    items = [f"11/{20 + n} 新しいお知らせ その {n}", "11/20 システム更新のお知らせ", "11/18 健康診断の日程"]
    _news(items, views=120 + n * 37)  # 閲覧数は <main> の外なので知らせない
    said += [f"お知らせページ: 「新しいお知らせ その {n}」を追加（閲覧数の変化は見る部分の外）"]

    ROUND.write_text(str(n), encoding="utf-8")
    print(f"変更 {n} 回目:")
    for s in said:
        print(f"  - {s}")
    print("画面で「すべてチェック」を押すと出ます。")


def reset() -> None:
    if ROOT.exists():
        shutil.rmtree(ROOT)
    print(f"消しました: {ROOT}")


def _free_port() -> int:
    for port in PORTS:
        try:
            ThreadingHTTPServer(("127.0.0.1", port), SimpleHTTPRequestHandler).server_close()
            return port
        except OSError:
            continue
    sys.exit(f"{PORTS.start}〜{PORTS.stop - 1} のポートがすべて使われています")


def serve() -> ThreadingHTTPServer:
    text = CONFIG.read_text(encoding="utf-8")
    port = int(re.search(r"http://127\.0\.0\.1:(\d+)/", text).group(1))
    handler = functools.partial(SimpleHTTPRequestHandler, directory=str(WEB))
    handler.log_message = lambda *a: None  # type: ignore[method-assign]
    try:
        server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        new = _free_port()  # taken by something else since: move (the web page's baseline is taken again)
        CONFIG.write_text(text.replace(f"127.0.0.1:{port}/", f"127.0.0.1:{new}/"), encoding="utf-8")
        print(f"ポート {port} が使われていたので {new} に変えました（お知らせページは基準を取り直します）")
        server = ThreadingHTTPServer(("127.0.0.1", new), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "open"
    if cmd == "change":
        change()
    elif cmd == "reset":
        reset()
    elif cmd == "open":
        setup()
        server = serve()
        from watchpoint import desktop

        print("窓を開きます。閉じるとデモのページも止まります。変更を起こすには、別の窓で demo\\change.bat")
        try:
            desktop.run(CONFIG)
        finally:
            server.shutdown()
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    os.chdir(Path(__file__).resolve().parent.parent)
    main()
