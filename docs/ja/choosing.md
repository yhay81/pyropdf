---
title: pyropdf を選ぶ
description: pyropdf が得意なこと、まだできないこと、PyMuPDF・pypdf・pikepdf との比較。
---

# pyropdf を選ぶ

## pyropdf が向いている場面

- **信頼できる編集。** 編集はすべて版になります。失敗したトランザクションは自分で元に戻り、
  `doc.history` で undo と redo ができ、`doc.changes()` は保存の前に、どの署名と PDF/A・PDF/UA
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
実在する 15 文書のコーパスで PyMuPDF 2.1 と比べた値（描画は生サンプル読み取り、5 回計測の最速値）、
pyropdf の時間を PyMuPDF の時間で割った比（小さいほど速く、プラットフォームにより差があります）：

| 操作 | 0.4.0 (Linux x86-64) | 0.3.1 (macOS arm64) |
| --- | ---: | ---: |
| 開く | 0.27 | 0.13 |
| テキスト | 1.38 | 0.50 |
| 単語 | 1.63 | 0.41 |
| 検索 | 0.95 | 0.37 |
| 描画 72 dpi | 1.78 | 1.77 |
| 描画 144 dpi | 1.49 | 1.82 |
| 全体保存 | 0.12 | 0.16 |

Linux x86-64 のリリース比較レポート（`comparison-v0.4.0.zip`）は
[v0.4.0 リリース](https://github.com/yhay81/pyropdf/releases/tag/v0.4.0) に添付されています。
計測方法、Apple Silicon の macOS 基準値、生の計測値、機能ごとの比較は
[evidence/pymupdf-comparison](https://github.com/yhay81/pyropdf/tree/main/evidence/pymupdf-comparison)
にあります（英語）。
