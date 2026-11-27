---
title: API 稳定性
description: 说明 pyropdf 的公共 API 边界、1.0 前后的变更规则，以及弃用、资源限制、可选扩展与能力声明的治理策略。
---

# API 稳定性

pyropdf 遵循[语义化版本规范](https://semver.org/)。本文档同时说明当前的 1.0 前规则，以及将在 pyropdf 1.0 发布时正式冻结的 1.0 稳定性契约（[DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases)）。

## 1.0 之前的当前状态 {#current-status}

pyropdf 目前处于 **1.0 之前（0.x）**阶段。下文的 1.0 稳定性契约**尚未**冻结：

- 次版本发布（`0.x.0`）可能会在不设弃用过渡期的情况下重命名、重塑或移除公共 API。依据 [DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)（*One way*），1.0 之前的重新设计会在同一版本中直接替换旧 API，而不保留兼容别名。
- 补丁版本发布（`0.x.y`）力求保持 API 兼容性，专注于缺陷修复、安全修复和打包更新。
- 每一项破坏性变更及其替代方案都会记录在[更新日志](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md)中。
- 如果希望按自己的节奏采纳次版本变更，请固定次版本系列（例如 `pyropdf~=0.4.0`）。

下文所述的 1.0 稳定性保证将在 `1.0.0` 发布时生效。

## 公共 API 边界 {#public-api-boundary}

| 接口范围 | 稳定性 | 说明 |
| --- | --- | --- |
| `pyropdf.__all__`、`pyropdf.__version__` 与 `pyropdf.typed.__all__` | 公共 | 请从 `pyropdf` 或 `pyropdf.typed` 导入，而非内部子模块 |
| 文档化的类、方法、属性、函数、枚举与类型别名 | 公共 | 参数名称、默认值以及位置参数/仅关键字参数约定均属于契约内容 |
| 不可变（frozen）记录与规格 dataclass | 公共 | 字段名与类型属于公共契约；除非文档明确说明，否则不保证位置参数构造 |
| 异常类及其继承层次、`LimitError.code` 以及 `LIMIT_CODES` / `LimitCode` | 公共 | 程序化错误处理应依赖异常类型与 `LimitError.code`，而非消息措辞 |
| `METADATA_KEYS` 与文档化的 `Literal` 选项值 | 公共 | 次版本中可能会新增可选值 |
| 以 `_` 开头的私有名称、`pyropdf._core` 以及未文档化的模块 | 内部 | 可能在任意版本中未经通知即变更或移除 |
| `repr()` 输出、错误消息措辞以及警告触发顺序 | 内部 | 仅供诊断参考，不适合机器解析 |
| 保存后的 PDF、PNG 与 SVG 的精确字节序列 | 实现细节 | 保证的内容是视觉渲染结果、提取文本、结构有效性、增量保存的前缀保留以及未修改对象的无损保留 |

## 1.0 之后的语义化版本规范 {#after-v1}

自 `1.0.0` 起：

- **主版本发布（`X.0.0`）**可能移除已弃用的 API，或对签名、类型及文档化契约进行破坏性变更。
- **次版本发布（`1.Y.0`）**添加向后兼容的功能、带默认值的仅关键字可选参数、新的枚举成员或 `Literal` 选项值、新的 `LIMIT_CODES` 条目，或带默认值的额外 dataclass 字段。
- **补丁版本发布（`1.Y.Z`）**修复缺陷、回归问题以及安全或打包缺陷，不破坏公共 API。

移除或重命名公共符号、新增必填参数、收紧可接受的输入类型、从返回记录中删除字段，或更改异常继承层次，均须发布主版本。类型注解属于公共 API 的一部分：破坏公共签名静态类型兼容性的改动同样视为破坏性变更。

## 弃用策略 {#deprecation-lifecycle}

`1.0.0` 发布后，任何计划移除的公共 API 均遵循四阶段生命周期：

1. 文档将该 API 标记为已弃用，指明替代方案，并写明最早可移除该 API 的版本号。
2. 在可行的情况下，调用或使用该 API 时发出 Python 标准的 `DeprecationWarning`。
3. 已弃用的 API 在宣布弃用的版本之后，至少在**两个次版本且六个月**内保持可用。
4. 实际移除仅在后续的**主版本**中进行。

`PyropdfWarning` 用于报告 PDF 文件自身的问题（例如未映射字形或已修复的结构），绝不用于 API 弃用通知。若遇安全、法律或上游关键紧急情况，可能需要缩短过渡周期；此类例外都会在更新日志和发布说明中醒目标注。

## 限制代码与结果变更 {#limits-and-behavior}

资源限制注册于 [`limits.toml`](https://github.com/yhay81/pyropdf/blob/main/limits.toml)，在运行时通过 `pyropdf.LIMIT_CODES` 与 `pyropdf.LimitCode` 公开，并记录在[限制与错误](limits.md)中。

- 现有的 `LimitError.code` 标识符在次版本与补丁版本中保持名称与文档化语义不变。
- 为堵住资源耗尽漏洞，次版本或补丁版本中可能会新增限制代码。
- 收紧默认上限或新增防护检查，可能会拒绝早期版本曾尝试处理的畸形或开销过大的 PDF。请通过捕获 `LimitError`（或其基类 `PdfError`）并将 `error.code` 与 `LIMIT_CODES` 比对来处理拒绝情形，切勿匹配错误消息文本。
- 当先前结果本身有误时，缺陷修复可能在任意版本中改变提取文本、阅读顺序、表格单元格、字形边框、渲染像素或损坏文件修复输出。显著的行为变更会列入更新日志。

## 可选扩展与数据包 {#extras-and-data}

核心 `pyropdf` 包没有任何运行时必需依赖。可选扩展（extra）会安装独立版本化的依赖或数据包：

| 可选扩展 | 安装包 | 版本控制与兼容性 |
| --- | --- | --- |
| `recipients` | `asn1crypto`、`cryptography` | 遵循 `pyproject.toml` 中声明的上游语义化版本范围；仅公钥 CMS 收件人加密需要 |
| `ocr` | `pylopdf-ocr-models` | 独立版本化并固定在兼容系列的模型包；模型更新可能会提升识别准确率与边框精度 |
| `jp`、`ko`、`zh-cn`、`zh-tw` | `pylopdf-fonts-jp`、`pylopdf-fonts-ko`、`pylopdf-fonts-zh-cn`、`pylopdf-fonts-zh-tw` | 独立版本化的 Noto CJK 回退字体包；字形更新可能会影响未嵌入 CJK 字体的回退渲染结果 |
| `layout` *（规划中）* | 评估中（[#1199](https://github.com/yhay81/pyropdf/issues/1199)） | 若采纳，将以独立版本化的可选扩展提供，不会为核心 wheel 引入必需依赖 |

核心 wheel 遵循自身独立的语义化版本节奏，不受数据包更新影响；若数据包格式发生不兼容变更，则会在 `pyproject.toml` 中同步调整兼容版本范围并在发布说明中写明。

## 支持的 Python 版本与目标平台 {#python-and-platforms}

- **最低 Python 版本：** `requires-python = ">=3.11"`（支持 Python 3.11 至 3.15）。提高最低 Python 版本仅会在相应上游 Python 系列结束生命周期（EOL）后的次版本或主版本中进行，并在发布说明中提前告知。
- **原生 wheel：** 为 Linux（`x86_64`、`aarch64`）、macOS（`arm64`、`x86_64`）与 Windows（`x86_64`）提供 `abi3-py311` wheel，同时提供源码分发包（`sdist`）。
- **自由线程 CPython：** 针对上述五个原生目标平台提供专用的 `3.14t` 与 `3.15t` wheel（因为 `abi3` wheel 无法在自由线程解释器上加载）。
- **WebAssembly：** 面向 Pyodide 与 Cloudflare Python Workers 的 `PyEmscripten`（`wasm32`）wheel；按照 [DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases) 要求纳入 1.0 发布准入门槛。

## 能力声明与安全承诺 {#capabilities-and-security}

- **发布版能力声明：** 每个发布能力声明的版本都会附带一份绑定到该精确标签与提交的可复现 `capabilities.zip` 归档，列明已验证范围、操作、排除项、外部引擎、可选运行时以及需要人工审查的检查项。详见[状态与安全性](status.md)与[规范覆盖情况](specification.md)。
- **输入安全性声明：** 在 hayro 的有界构建（[#15](https://github.com/yhay81/pyropdf/issues/15)）发布之前，pyropdf 的任何版本都不主张对不可信输入具有安全性。在此之前，`DocumentLimits` 与 `DocumentLimits.web()` 会约束对象存储与 API 层的工作量，而对来历不明的 PDF 进行渲染与解释时，仍应在隔离的工作进程中运行（见[限制与错误](limits.md#reading-a-pdf-in-a-worker)）。
- **安全漏洞披露：** 请按 [SECURITY.md](https://github.com/yhay81/pyropdf/blob/main/SECURITY.md) 指引私下报告漏洞。
- **自动化兼容性检查：** CI 会在每个 Pull Request 上强制检查公共导出与签名、生成的 API 参考文档（`tools/api_reference.py --check`）、`limits.toml` 一致性（`tests/test_limit_codes.py`）以及规范映射证据（`tests/test_spec_map.py`）。
