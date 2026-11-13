---
title: pyropdf を選ぶ
description: pyropdf が得意なこと、まだできないこと、PyMuPDF・pypdf・pikepdf との比較。
---

# pyropdf を選ぶ

## pyropdf が向いている場面

- **信頼できる編集。** 編集はすべて版になります。失敗したトランザクションは自分で元に戻り、
  `doc.history` で undo と redo ができ、`doc.changes` は保存の前に、どの署名と PDF/A・PDF/UA
  の宣言が壊れるかを教えます。
- **触れていない部分を保つ保存。** 増分保存は変わったオブジェクトだけを追記するので、元のバイト列に
  対する署名は有効なままで、編集なしの保存は入力を再現します。
- **読み取りと描画が一つのパッケージに。** 読む順のテキスト、単語、レイアウト、表、Markdown、
  オフライン OCR、pixmap と SVG への描画。必須の依存はありません。
- **寛容なライセンス。** pyropdf は MIT ライセンスです。
- **新しい Python。** CPython 3.11 から 3.15、free-threaded ビルドも含み、一つの文書を並列に読めます。

## まだできないこと

- hayro の bounded construction がリリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を主張しません。[状態と安全性](status.md)を参照してください。
- 描画は MuPDF より遅いです（下記）。
- API はマイナーバージョンのあいだで変わることがあります。

## 他のライブラリとの比較

| | pyropdf | PyMuPDF | pypdf | pikepdf |
| --- | --- | --- | --- | --- |
| ライセンス | MIT | AGPL-3.0 または商用 | BSD-3-Clause | MPL-2.0 |
| 実装 | Rust（描画は hayro） | C（MuPDF） | 純 Python | C++（qpdf） |
| ページの描画 | あり | あり | なし | なし |
| テキスト・単語・表 | あり | あり | テキスト | なし |
| undo と原子的なトランザクション | あり | ジャーナル | なし | なし |

この中で最も機能が揃っているのは PyMuPDF で、pyropdf は自分をそれと比べて測っています。
実在する 15 文書のコーパスを Apple Silicon の macOS で測り（描画の画素は `bytes(pixmap)` で読み取り）、pyropdf の時間を PyMuPDF の時間で割った比（小さいほど速い）：

| 操作 | pyropdf / PyMuPDF |
| --- | ---: |
| 開く | 0.21 |
| テキスト | 0.80 |
| 単語 | 0.79 |
| 検索 | 1.19 |
| 描画 72 dpi | 2.03 |
| 描画 144 dpi | 2.34 |
| 全体保存 | 0.15 |

方法、生の計測値、機能ごとの比較は
[evidence/pymupdf-comparison](https://github.com/yhay81/pyropdf/tree/main/evidence/pymupdf-comparison)
にあります（英語）。
