---
title: 选择 pyropdf
description: pyropdf 擅长的场景、目前尚未支持的功能，以及与 PyMuPDF、pypdf 和 pikepdf 的对比。
---

# 选择 pyropdf

## 何时适合使用 pyropdf

- **值得信赖的编辑。** 每一次编辑都会生成一个新版本：事务失败时会自动回滚，`doc.history` 支持撤销与重做，而 `doc.changes()` 会在保存前告知本次保存会破坏哪些数字签名以及 PDF/A 或 PDF/UA 声明。
- **保留未修改内容的保存机制。** 增量保存仅追加发生变更的对象，因此覆盖原始字节的数字签名仍保持有效；未做任何编辑时的保存会完整复现输入内容。
- **读取与渲染集于一身。** 支持按阅读顺序提取文本、单词、版面布局、表格、Markdown、离线 OCR，以及渲染为像素图（pixmap）和 SVG，且无任何必需的第三方依赖。
- **宽松的开源协议。** pyropdf 采用 MIT 许可证。
- **面向现代 Python。** 支持 CPython 3.11 至 3.15（包括自由线程构建），并支持对同一文档并行读取。

## 目前尚未支持的功能

- 在 hayro 的有界构建（bounded construction）发布之前，pyropdf 的任何版本都不主张对不可信输入具有安全性；详见[状态与安全性](status.md)。
- 渲染速度慢于 MuPDF（见下文）。
- 次版本之间可能会调整 API。

## 与其他库的对比

| | pyropdf | PyMuPDF | pypdf | pikepdf |
| --- | --- | --- | --- | --- |
| 许可证 | MIT | AGPL-3.0 或商业许可 | BSD-3-Clause | MPL-2.0 |
| 实现方式 | Rust（由 hayro 渲染） | C（MuPDF） | 纯 Python | C++（qpdf） |
| 页面渲染 | 支持 | 支持 | 不支持 | 不支持 |
| 文本、单词与表格 | 支持 | 支持 | 仅文本 | 不支持 |
| 撤销与原子事务 | 支持 | 日志记录（journalling） | 不支持 | 不支持 |

在这些库中，PyMuPDF 的功能最为完备，也是 pyropdf 用于衡量自身的基准。在包含 15 份真实文档的语料库上（渲染测试读取原始采样数据，取 5 次运行的最快成绩），下表列出 pyropdf 与 PyMuPDF 2.1 的耗时比值（越低越快；不同平台存在差异）：

| 操作 | 0.4.0 (Linux x86-64) | 0.3.1 (macOS arm64) |
| --- | ---: | ---: |
| 打开文档 | 0.27 | 0.13 |
| 提取文本 | 1.38 | 0.50 |
| 提取单词 | 1.63 | 0.41 |
| 搜索 | 0.95 | 0.37 |
| 72 dpi 渲染 | 1.78 | 1.77 |
| 144 dpi 渲染 | 1.49 | 1.82 |
| 完整保存 | 0.12 | 0.16 |

Linux x86-64 的发布对比报告（`comparison-v0.4.0.zip`）附于 [v0.4.0 发布页](https://github.com/yhay81/pyropdf/releases/tag/v0.4.0)；测试方法、Apple Silicon macOS 基准结果、原始计时数据以及逐项功能对比见 [evidence/pymupdf-comparison](https://github.com/yhay81/pyropdf/tree/main/evidence/pymupdf-comparison)（英文）。
