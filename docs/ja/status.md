---
title: 状態と安全性
description: 0.1 alpha が対応するもの、まだ対応しないもの、入力の安全性について pyropdf が主張すること・しないこと。
---

# 状態と安全性

## 状態

pyropdf は **alpha** です。API はマイナーバージョンのあいだで変わることがあり、
変更は[変更履歴](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md)に
載せます。pyropdf は [pylopdf](https://github.com/yhay81/pylopdf) の後継で、
その考え方を新しい土台の上に引き継いでいます。土台は
[DESIGN.md](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md) に書いてあります。

0.1 にまだないもの：

- `page.draw` によるページへの作図とフォントの作成。krilla 側の変更を待っています。
- 作図を必要とするフリーテキスト注釈とスタンプ注釈。
- hayro でレビュー中の変更に依存する、描画要素の出どころ情報とインタプリタの fuel。

進み具合は [issue tree](https://github.com/yhay81/pyropdf/issues/1) で追えます。

## 安全性

hayro の bounded construction（[#15](https://github.com/yhay81/pyropdf/issues/15)）が
リリースされるまで、pyropdf のどのリリースも、信頼できない入力に対する安全性を
主張しません。それまでは、出どころの分からない PDF は、描画と解釈の経路でメモリや
時間を使い果たしうるものとして扱ってください。

`DocumentLimits` は pyropdf 自身が行う操作ごとの仕事量に上限を設けます。コードの一覧は
[制限とエラー](limits.md#limit-codes)にあります。これは上の主張の代わりではありません。

脆弱性は [GitHub security advisories](https://github.com/yhay81/pyropdf/security/advisories/new)
から非公開で報告してください。
