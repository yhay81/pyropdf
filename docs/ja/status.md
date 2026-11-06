---
title: 状態と安全性
description: pyropdf 0.3 が対応するもの、まだ対応しないもの、入力の安全性について pyropdf が主張すること・しないこと。
---

# 状態と安全性

## 状態

API はマイナーバージョンのあいだで変わることがあり、
変更は[変更履歴](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md)に
載せます。土台は [DESIGN.md](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md) に、
ISO 32000-2 のどこまでに対応しているかは[仕様の対応状況](specification.md)にあります。

0.3.0 にまだないもの：

- 墨消しと、矩形の外側の内容の削除。hayro でレビュー中の変更による、描画要素の出どころ情報を必要とします。
- 描画と解釈の仕事量を抑えるインタプリタの fuel とキャンセル。これも hayro でレビュー中の変更です。
- CMYK の pixmap と、SVG の選択できるテキスト。hayro を待っています。
- CFF2 の可変フォントによるキャンバスへの文字の描画。`SVG ` テーブル、JPEG・TIFF・raw のストライクのカラーグリフと、sweep グラデーションや PDF にない合成モードを必要とする COLR グリフ。これらは単色のアウトラインで描くか、拒否します。フォームフィールドとフリーテキスト注釈のカラーグリフ（アウトラインだけを描きます）。

進み具合は [issue tree](https://github.com/yhay81/pyropdf/issues/1) で追えます。

## 安全性

hayro の bounded construction（[#15](https://github.com/yhay81/pyropdf/issues/15)）が
リリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を
主張しません。それまでは、出どころの分からない PDF は、描画と解釈の経路でメモリや
時間を使い果たしうるものとして扱ってください。

アップロードを扱う場合は、[読み取りと描画で適用される制限](limits.md#reading-and-rendering)と、
[別プロセスでメモリと時間を制限する方法](limits.md#reading-a-pdf-in-a-worker)を参照してください。

脆弱性は [GitHub security advisories](https://github.com/yhay81/pyropdf/security/advisories/new)
から非公開で報告してください。
