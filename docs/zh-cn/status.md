---
title: 状态与安全性
description: pyropdf 0.4 已涵盖的能力、尚未实现的部分，以及 pyropdf 对输入安全性的声明边界。
---

# 状态与安全性

## 状态

次版本之间可能会调整 API（见 [API 稳定性策略](stability.md)），所有变更均记录于[更新日志](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md)。架构基础详见 [DESIGN.md](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md)，各模块对 ISO 32000-2 的覆盖程度详见[规范覆盖情况](specification.md)。

对于已发布的版本，可在[发布页面](https://github.com/yhay81/pyropdf/releases)选择对应标签并打开 `capabilities.zip`（发布说明中也链接了同一归档文件）。其中的报告会写明处理器角色、已验证的操作、排除项与限制条件，并分别标识输入配置文件、外部引擎与可选运行时环境。0.3.1 版本早于该机制，因此没有公开发布的已验证范围归档。

0.4.1 版本中尚未包含的内容：

- 彻底涂黑（redaction）以及移除矩形区域之外的内容，这依赖于 hayro 中正在评审的内容溯源（content provenance）改动；
- 用于约束渲染与解释工作量的解释器燃料（fuel）与取消机制，同样是 hayro 中正在评审的改动；
- CMYK 像素图与 SVG 中的可选中文本，均在等待 hayro 支持。

开发进度统一在[问题追踪树](https://github.com/yhay81/pyropdf/issues/1)中维护。

## 安全性 {#safety}

在 hayro 的有界构建（[#15](https://github.com/yhay81/pyropdf/issues/15)）发布之前，pyropdf 的任何版本都不主张对不可信输入具有安全性。在此之前，请将来历不明的 PDF 视为可能在渲染和解释路径中耗尽内存或时间的输入加以防范。

对于用户提供的文档，请参阅[页面读取与渲染期间生效的限制](limits.md#reading-and-rendering)，以及[如何在工作进程中强制执行内存与时间限制](limits.md#reading-a-pdf-in-a-worker)。

如需私下报告安全漏洞，请通过 [GitHub 安全公告](https://github.com/yhay81/pyropdf/security/advisories/new)提交。
