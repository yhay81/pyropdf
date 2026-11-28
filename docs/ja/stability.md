---
title: API の安定性
description: pyropdf の公開 API の範囲、1.0 前後の変更ルール、非推奨化、制限コード、オプション extras、機能宣言と安全性の扱いを説明します。
---

# API の安定性

pyropdf は [セマンティック バージョニング](https://semver.org/) に従います。このページでは、現在の 1.0 以前の運用ルールと、pyropdf 1.0 リリース時に確定・凍結する安定性契約の両方を定義します（[DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases)）。

## 1.0 以前の現在の状態 {#current-status}

pyropdf は現在 **1.0 未満（0.x）** です。以下の 1.0 安定性契約は**まだ凍結されていません**：

- マイナーリリース（`0.x.0`）では、非推奨期間を置かずに公開 API の名称変更・設計変更・削除を行うことがあります。[DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)（*One way*）に従い、1.0 以前の再設計では互換エイリアスを残さず同じリリースで旧 API を置き換えます。
- パッチリリース（`0.x.y`）は API 互換性を維持しつつ、不具合修正・セキュリティ修正・パッケージング更新を行うことを原則とします。
- すべての破壊的変更と移行先は、[変更履歴](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md) に記載します。
- 自分のタイミングでマイナー更新を取り込みたい場合は、マイナー系列を固定してください（例：`pyropdf~=0.4.0`）。

以下の 1.0 安定性保証は `1.0.0` のリリースから効力を持ちます。

## 公開 API の境界 {#public-api-boundary}

| 対象 | 安定性 | 備考 |
| --- | --- | --- |
| `pyropdf.__all__`、`pyropdf.__version__`、`pyropdf.typed.__all__` | 公開 | 内部サブモジュールではなく `pyropdf` または `pyropdf.typed` からインポートしてください |
| ドキュメント化されたクラス、メソッド、プロパティ、関数、Enum、型エイリアス | 公開 | 引数名、既定値、位置引数かキーワード専用かも契約に含まれます |
| frozen なレコード・仕様 dataclass | 公開 | フィールド名と型は公開対象です。明記がない限り位置引数での生成は保証しません |
| 例外クラスとその継承階層、`LimitError.code`、`LIMIT_CODES` / `LimitCode` | 公開 | プログラムからの処理はメッセージ文言ではなく型と `LimitError.code` に依存してください |
| `METADATA_KEYS` およびドキュメント化された `Literal` オプション値 | 公開 | マイナーリリースで新しい任意値を追加することがあります |
| `_` で始まる名前、`pyropdf._core`、非公開モジュールや未文書化属性 | 内部実装 | 予告なくどのリリースでも変更・削除されることがあります |
| `repr()` の出力、エラーメッセージの文面、警告の出力順 | 内部実装 | 診断用であり、機械的なパース対象ではありません |
| 保存した PDF、PNG、SVG のバイト列の完全一致 | 実装詳細 | 保証するのは描画結果、抽出テキスト、構造的妥当性、増分保存のプレフィックス保持、未変更オブジェクトの無損失保持です |

## 1.0 以降のセマンティック バージョニング {#after-v1}

`1.0.0` 以降は次の規則に従います：

- **メジャーリリース（`X.0.0`）**：非推奨 API の削除や、シグネチャ・型・ドキュメント化された契約の破壊的変更を含むことがあります。
- **マイナーリリース（`1.Y.0`）**：後方互換を保った機能追加、既定値付きのキーワード専用引数の追加、新しい Enum メンバや `Literal` オプション値の追加、新しい `LIMIT_CODES` エントリの追加、既定値付き dataclass フィールドの追加を行います。
- **パッチリリース（`1.Y.Z`）**：公開 API を壊さずに不具合・リグレッション・セキュリティ・パッケージングの問題を修正します。

公開シンボルの削除・改名、必須引数の追加、受け付ける入力型の縮小、返却レコードからのフィールド削除、例外継承階層の変更はメジャーリリースが必要です。型注釈も公開 API の一部であり、公開シグネチャの静的型互換性を壊す変更も破壊的変更として扱います。

## 非推奨化ポリシー {#deprecation-lifecycle}

`1.0.0` リリース後、削除予定の公開 API は次の 4 段階を経ます：

1. ドキュメント上で非推奨と明記し、代替 API と削除可能な最短リリースを示します。
2. 実用上可能な箇所では、呼び出し時に Python 標準の `DeprecationWarning` を送出します。
3. 非推奨化したリリースから、少なくとも **2 マイナーリリースかつ 6 か月間** は動作を維持します。
4. 実際の削除は、その後の **メジャーリリース** でのみ行います。

`PyropdfWarning` は PDF ファイル側の問題（未マップグリフや修復された構造など）を報告するための警告であり、API の非推奨通知には使いません。セキュリティ・法務・上流の重大な緊急事態では短縮経路を採る場合がありますが、その際は変更履歴とリリースノートに明確に記載します。

## 制限コードと処理結果の変化 {#limits-and-behavior}

リソース制限は [`limits.toml`](https://github.com/yhay81/pyropdf/blob/main/limits.toml) に登録され、実行時には `pyropdf.LIMIT_CODES` と `pyropdf.LimitCode` として公開され、[制限とエラー](limits.md) に一覧化されています。

- 既存の `LimitError.code` 識別子は、マイナー・パッチリリースを通じて名称と文書化された意味を保持します。
- リソース枯渇の抜け道を塞ぐため、マイナーまたはパッチリリースで新しい制限コードを追加することがあります。
- 既定上限の引き締めや新しいガードの追加により、以前のリリースでは処理を試みていた不正または過度に高コストな PDF が拒否されるようになる場合があります。拒否の処理はメッセージ文面ではなく、`LimitError`（または基底の `PdfError`）の捕捉と `error.code`・`LIMIT_CODES` の照合で行ってください。
- 以前の結果が誤っていた場合、不具合修正によって抽出テキスト、読み順、表のセル、グリフ枠、描画ピクセル、破損ファイル修復結果が任意のリリースで変わることがあります。目立つ挙動変更は変更履歴に記載します。

## オプション extras とデータパッケージ {#extras-and-data}

コアの `pyropdf` に必須の実行時依存はありません。オプション extras は別管理の依存パッケージまたはデータパッケージを導入します：

| Extra | パッケージ | バージョニングと互換性 |
| --- | --- | --- |
| `recipients` | `asn1crypto`, `cryptography` | `pyproject.toml` で宣言した上流 SemVer 範囲に従います。公開鍵 CMS 受信者暗号化でのみ必要です |
| `ocr` | `pylopdf-ocr-models` | 互換系列に固定した別バージョンのモデルパッケージ。モデル更新により認識精度や座標枠が改善されることがあります |
| `jp`, `ko`, `zh-cn`, `zh-tw` | `pylopdf-fonts-jp`, `pylopdf-fonts-ko`, `pylopdf-fonts-zh-cn`, `pylopdf-fonts-zh-tw` | 別バージョンの Noto CJK 代替フォントパッケージ。グリフ更新により未埋め込み CJK フォントの描画が変わることがあります |
| `layout` *（計画中）* | 評価中（[#1199](https://github.com/yhay81/pyropdf/issues/1199)） | 採用する場合はコア wheel に必須依存を増やさず、別バージョンのオプション extra として提供します |

コア wheel はデータパッケージ更新とは独立した SemVer で進みます。データ形式の非互換変更がある場合は `pyproject.toml` の互換バージョン範囲を更新し、リリースノートに記載します。

## 対応 Python バージョンとターゲット {#python-and-platforms}

- **Python 下限：** `requires-python = ">=3.11"`（Python 3.11〜3.15 をサポート）。最小 Python バージョンの引き上げは、対象系列が上流でサポート終了（EOL）を迎えた後のマイナーまたはメジャーリリースでのみ行い、リリースノートで告知します。
- **ネイティブ wheel：** Linux（`x86_64`, `aarch64`）、macOS（`arm64`, `x86_64`）、Windows（`x86_64`）向けの `abi3-py311` wheel およびソース配布物（`sdist`）。
- **フリースレッド CPython：** `abi3` wheel はフリースレッド実行環境に読み込めないため、上記 5 ターゲット向けに `3.14t`・`3.15t` 専用 wheel を提供します。
- **WebAssembly：** Pyodide および Cloudflare Python Workers 向けの `PyEmscripten`（`wasm32`）wheel。[DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases) の 1.0 リリースゲート対象です。

## 機能宣言と安全性の主張 {#capabilities-and-security}

- **リリースの機能宣言：** 機能範囲を宣言する各リリースには、そのタグとコミットに紐づく再現可能な `capabilities.zip` を添付し、検証済みの対応範囲・操作・対象外・外部エンジン・任意ランタイム・人手確認が必要な検査を明示します。[状態と安全性](status.md) と [仕様の対応状況](specification.md) を参照してください。
- **信頼できない入力への安全性主張：** hayro の bounded construction（[#15](https://github.com/yhay81/pyropdf/issues/15)）がリリースされるまで、どの pyropdf リリースも信頼できない入力への安全性を主張しません。それまでは、`DocumentLimits` と `DocumentLimits.web()` でストアと API の仕事量を制限しつつ、描画・解釈処理は隔離されたワーカープロセス内で実行してください（[制限とエラー](limits.md#reading-a-pdf-in-a-worker)）。
- **脆弱性の報告：** [SECURITY.md](https://github.com/yhay81/pyropdf/blob/main/SECURITY.md) に従い非公開で報告してください。
- **自動互換性チェック：** CI はすべてのプルリクエストで公開エクスポートとシグネチャ、生成 API リファレンス（`tools/api_reference.py --check`）、`limits.toml` 整合性（`tests/test_limit_codes.py`）、仕様対応表の証跡（`tests/test_spec_map.py`）を検証します。
