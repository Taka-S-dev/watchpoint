# 作りと、足すときの置き場所

watchpoint のコードの分け方と、機能を足すときにどこを触るかをまとめる。

## 層

上の層は下の層を使うが、下の層は上の層を使わない。この向きは `tests/test_layout.py` で確かめている（循環する import、下の層から上の層への import、`.gitignore` に許可されていないファイルがあると落ちる）。

```
画面・CLI        cli/        コマンド（グループごとに 1 ファイル）
                 gui.py      ページの HTML・API のルート表・--browser のサーバー
                 gui.html    ページ（HTML・CSS・JS を 1 枚に。ビルドなしで窓に渡すため）
                 desktop.py  WebView2 の窓（ルート表をそのまま呼ぶ）
     ↓
処理             app.py      ページと CLI が共通で使う操作（チェックを始める、ルールを試す・足す、設定を書き換える）
     ↓
中心             check.py    取ってきて、確認済みと比べて、結果を残す
                 report.py   結果を表・Markdown・世代にまとめる
                 rules.py    要注意（ルールと期限）
                 editing.py  設定ファイルを、コメントや並びを残したまま書き換える
                 diffing.py  2 つのテキストの比べ方（見出し付きの差分、行の中の変化、記録ごとの比較）
                 store.py    データフォルダ（写し・状態・記録）
     ↓
データ取得       fetchers/   取ってくる（種類ごとに 1 ファイル）
                 textify/    取ってきたものをテキストにする（形式ごとに 1 ファイル）
                 commands.py 見張りが動かすコマンド（安全な一覧・許可・実行）
     ↓
土台             config.py   設定の型と読み込み
                 timewords.py 時間の言葉（30m、平日、毎週金 17:00）

同梱の部品       src/officetext/  Office ファイルを人が見るとおりに読む（watchpoint とは別のパッケージ）
```

`officetext` は、ほかの道具もそのまま使えるように、watchpoint を一切頼らない別のパッケージとして、テストごと同梱している（`src/officetext/tests/`。頼らないことも `test_standalone.py` で確かめている）。説明は [src/officetext/README.md](../src/officetext/README.md)。見せ方は使う側が決め、watchpoint では `textify/word.py` と `textify/excel.py` が、比べる用の行にする（そのテストは `tests/test_excel_lines.py` など、watchpoint の側に置く）。

## 守っている約束

- **元のファイルに書かない。** 道具（`fetchers/`）が書いてよいのは、渡された作業フォルダだけ。Office は手元のコピーを開き、SharePoint のファイルは読み取り専用で開けなかったら保存せずに閉じる。
- **設定ファイルだけでは、書き換えるコマンドを動かせない。** `commands.py` の安全な一覧（git は履歴を読むサブコマンドだけ）にないものは、本人がターミナルで `allow --unsafe` したときだけ動く。
- **AI には自動で送らない。** AI 向けの出力（`check -f md`、ページの「AI 用にコピー」）は、本人が手で渡す。

## 足すときの置き場所

| 足したいもの | 触るところ |
|---|---|
| 取り込み先の種類（例: 新しい API） | `fetchers/<種類>.py` に `fetch(target, workdir) -> Fetched` を書き、`fetchers/__init__.py` の `fetch()` に 1 行足す。`config.py` の `FetchKind`（`fetch:` に書ける値）に足し、URL から見分けられるなら `Target.kind` にも見分け方を足す |
| ファイルの形式（例: .pptx） | Office の形式なら、読み方を `officetext/<形式>.py` に書き（結果の型は `model.py` に足す）、比べる用の行にするのを `textify/<形式>.py` に書く。それ以外の形式は `textify/<形式>.py` だけ。どちらも `textify/__init__.py` の `file_to_text()` に 1 行足す |
| Office の読み落とし（例: 図の中の文字） | `officetext/` を直す。watchpoint の行に出したいなら、`textify/word.py` か `textify/excel.py` でその項目を行にする |
| コマンド | 仲間の `cli/<グループ>.py` に足す。新しいグループなら `cli/` にファイルを作り、`cli/__init__.py` に並べたい位置で import を足す（`tests/test_layout.py` の並び順も直す） |
| ページと CLI の両方から使う操作 | `app.py` の `App` にメソッドを足す。ページから呼ぶなら `gui.py` の `api_routes()` にルートを足し、`gui.html` から呼ぶ |
| 設定の項目 | `config.py` の型に足す。並びの決まり（書き換えたときにどこへ入るか）は `editing.py` の `ORDER` |

新しいフォルダを作ったら、`.gitignore` の許可の一覧に足す（足さないと、コミットにも ruff の検査にも入らない）。
