# officetext

Word と Excel のファイルを、人が画面で見るとおりに読み、構造のままのデータとして返す部品。見せ方（読む用の Markdown、比べる用の行など）は、使う側が決める。

使う道具の中身を一切頼らない、独り立ちしたパッケージなので、このフォルダ（テスト込み）をそのまま取り出して使える。

## 約束

- **読むだけ**。渡されたファイルにも、ほかの場所にも書かない。
- **ほかを頼らない**。import するのは、標準ライブラリと、下の「取り出すときに要るもの」の 3 つと、自分自身だけ（`tests/test_standalone.py` で確かめている）。
- **IF は足して変える**。`model.py` の型と `read_*` の関数は、項目を足すだけで変える。名前を変える・消すときは `__version__` の大きい番号を上げる。

## 使い方

```python
from officetext import read_document, read_workbook

doc = read_document("仕様書.docx")
for b in doc.blocks:  # 本文を文書の順に
    print(b.kind, b.label, b.text)  # heading 1.2 概要
print(doc.header, doc.footer, doc.notes)  # ヘッダー・フッター・脚注

book = read_workbook("質問表.xlsx")
for sheet in book.sheets:
    for c in sheet.cells:  # 値の入ったセルを行の順に
        print(c.address, c.value, c.shown, c.formula, c.cached)
    print(sheet.shapes)  # 図形・テキストボックスの文字
print(book.warnings)  # 計算されていない式など
```

返す型と各項目の意味は [model.py](model.py) に書いてある。

## 素直に読むと落ちるもの

| 形式 | 拾うもの |
|---|---|
| Word | 見出しと箇条書きの自動の番号（`label`）、ヘッダー・フッター、脚注と文末脚注（見えるとおりの番号で）、テキストボックス、コンテンツコントロールの中身（表のセルを包んだものも）、変更履歴、コメント。目次は 1 つのまとまり（`toc`）にする |
| Excel | 一度も計算されていない式（値が空に見えてしまう）、取り消し線（セル全体と、セルの中の一部の語）、コメント、図形とテキストボックスの文字と置かれたセル、印刷のヘッダー・フッター、非表示のシート・行・列、結合セル、表示形式どおりの値（`15%`、`1,234`、`2026/10/01`） |

段落やセルの中の印は、どこでも同じ書き方にそろえる。

| 印 | 意味 |
|---|---|
| `~~文字~~` | 取り消し線 |
| `{++文字++}` / `{--文字--}` | 変更履歴の追加 / 削除 |
| `{>>名前: 文字<<}` | コメントの付いた位置 |
| `[^1]` / `[^e1]` | 脚注 / 文末脚注の参照 |
| `｜漢字《よみ》` | ふりがな |

## 守り

- 開く前に、zip の中身を開いたときの大きさの合計を確かめ、256 MB を超えたら読まない（`TooLarge`）。数 KB のファイルが何 GB にも膨らむように作られていても、メモリを使い切らない。
- officetext が自分で読む XML（脚注・コメント・図形・取り消し線の付いた文字など）は、外の実体や外への接続を読まない設定で読む。本体の XML は python-docx と openpyxl が読む。

## 取り出すときに要るもの

Python 3.11 以降と、`python-docx`・`openpyxl`・`lxml`。テストは `tests/` にあり、このフォルダの 1 つ上で `pytest officetext/tests` で動く（`pytest` も要る）。

## 任意の部分: Microsoft Project の計画（project.py）

```python
from officetext.project import read_project

plan = read_project("計画.mpp")  # .mpp / .mpt は Microsoft Project が要る。Project の XML（.xml）ならどこでも
print(plan.name, plan.status_date, plan.percent)
for t in plan.tasks:  # UID・名前・親のまとめ・開始・終了・進捗・先行・担当・基準計画・期限・メモ
    print(t.uid, t.name, t.finish, t.predecessor_names)
```

- Word と Excel の部分は、このファイルを一切 import しない。要らなければ `project.py` を消してよい（`tests/test_standalone.py` で確かめている）。
- `.mpp` は仕様が公開されていない形式なので、Project 自身に XML（MSPDI）へ変えさせてから読む。Project は PowerShell から動かすので、Python の部品は増えない。Project がないと `ProjectUnavailable` になり、Project で「名前を付けて保存」から XML にしたファイルなら読めると伝える。
- Project に開かせるのは一時フォルダに写したコピーで、渡されたファイルではない。マクロを止めてから（止められなければ開かずに）読み取り専用で開き、保存せずに閉じる。決まった時間に答えなければ、この呼び出しで起こした見えない Project だけを止める。
- タスクは UID で見分ける。ID（行番号）・アウトライン番号・ID で書いた先行タスクは、上に行を足すとずれるので、比べるなら UID・親のまとめの名前・`predecessor_names` を使う。

## まだ読まないもの

PowerPoint、PDF、古い形式（.xls・.doc・.ppt）、Excel の古い形式のテキストボックス（VML）・SmartArt・グラフの値、Word の図と数式。
