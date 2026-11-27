---
title: 限制与错误
description: 了解 DocumentLimits 的检查范围、如何在受限的工作进程中读取 PDF，以及如何处理 pyropdf 的各类错误。
---

# 限制与错误

## 限制

`DocumentLimits` 用于检查文件大小、页数、对象数，以及 pyropdf 在每项操作期间计量的工作量。当检查触发并拒绝操作时，`LimitError.code` 会指明所触及的具体限制。

```python
import pyropdf

limits = pyropdf.DocumentLimits(max_pages=1_000, max_decompressed_size=64 * 2**20, max_duration=10.0)
try:
    with pyropdf.open("input.pdf", limits=limits) as doc:
        print(doc.pages[0].text())
except pyropdf.LimitError as error:
    print("refused:", error.code)
```

`DocumentLimits.web()` 会在打开文档时检查每个对象。它为单个页面内容流设置 10 MiB 上限，为通过对象存储解码的单个流设置 64 MiB 上限，并为交给 hayro 处理的 PDF 字节数设置 64 MiB 的 `max_interpretation_size` 上限。通过这些检查的 PDF 在文本提取或渲染期间仍可能展开为大得多的内存占用。该预设配置不设置 `max_memory_size` 与 `max_duration`。

`CancellationToken` 允许另一线程请求取消操作。pyropdf 会在轮询操作预算时检查该令牌。

```python
import pyropdf

token = pyropdf.CancellationToken()
limits = pyropdf.DocumentLimits(cancellation=token)
with pyropdf.open("input.pdf", limits=limits) as doc:
    token.cancel()
    try:
        doc.pages[0].text()
    except pyropdf.LimitError as error:
        print(error.code)  # cancelled
```

## 复合操作 {#composite-operations}

| 操作 | 触发拒绝时的报告方式 |
| --- | --- |
| `doc.structure.walk(max_elements=n)` | 超出 `n` 的第一个元素会抛出 `object_count`。遍历全部完成后才会返回迭代器；遍历过程同时遵守 `memory_size` 与取消信号。 |
| `doc.open_action` | 读取嵌入目标时，其所有跳转步数记入同一次操作。内存超限报告 `memory_size`，取消则报告 `cancelled`。 |
| `doc.form.import_fdf()` 与 `import_xfdf()` | 对于编码后的输入，FDF 检查 `limits.max_file_size`（`file_size`），XFDF 检查 `max_size`（`form_field_input_size`）。两者查找并填写字段的过程同属一次文档操作。 |
| `with page.draw()` | 嵌套在画布中的导入操作共享画布的剩余预算。若因触发限制退出代码块，待提交的绘制与字段修改会一并丢弃。 |

因内存或计数超限被拒绝后，后续新操作会获得全新的预算；而已取消的令牌会对其绑定的所有文档持续保持取消状态。

## 读取与渲染 {#reading-and-rendering}

`page.text()`、`page.words()`、`page.paths()`、`page.content()` 和 `page.pixmap()` 全部依赖 hayro 解释页面。下表适用于这五个方法。

| 字段 | pyropdf 检查的内容 | 仍可能超出限制的部分 |
| --- | --- | --- |
| `max_interpretation_size` | 交给 hayro 的 PDF 字节数，包括原始字节、桥接层交叉引用更新、已编辑对象以及合成的外观流。 | 其中经过滤压缩的流与图像在 hayro 解码时仍可能膨胀到远大于该限制的体积。 |
| `max_page_content_size` | 对象存储对页面内容流解码过滤后的大小（例如供 `page.operations` 使用时）。 | 上述五个方法由 hayro 直接解码页面内容。 |
| `max_decompressed_size` | 通过对象存储读取的单个流的解码后大小。过滤器可能在存储检查大小之前先行分配输出缓冲区。 | 由 hayro 直接解码的流（包括渲染期间解码的图像）会绕过该检查。 |
| `max_memory_size` | pyropdf 计入当前操作内存预算的分配量。 | hayro 的工作内存与光栅缓冲区可能使整个进程的内存占用超出此上限。 |
| `max_duration` | pyropdf 执行预算检查时的已耗时。 | 当 hayro 正在解码、解释或绘制时可能已经超时；pyropdf 只能在下一次检查点报告超时。 |

`text()` 和 `words()` 的字形与文本上限、`paths()` 和 `content()` 的元素数量上限以及 `pixmap()` 的光栅尺寸上限，分别限制了每次调用收集或返回的输出规模。但即便输出很少的页面，解释过程本身也可能消耗可观计算量。上游相关工作跟踪于[有界流解码 (#48)](https://github.com/yhay81/pyropdf/issues/48)、[解释器预算与取消 (#105)](https://github.com/yhay81/pyropdf/issues/105)以及[有界构造 (#15)](https://github.com/yhay81/pyropdf/issues/15)。

解析器内部预算跟踪于 [#573](https://github.com/yhay81/pyropdf/issues/573)，字体塑形跟踪于 [#580](https://github.com/yhay81/pyropdf/issues/580)，PNG 解码缓冲区跟踪于 [#561](https://github.com/yhay81/pyropdf/issues/561)。项目正在等待引擎侧的工作计数器与取消支持（[#678](https://github.com/yhay81/pyropdf/issues/678)）。有界构造的发布条件仍记录在 [#243](https://github.com/yhay81/pyropdf/issues/243)。

## 在工作进程中读取 PDF {#reading-a-pdf-in-a-worker}

当用户向应用提交 PDF 时，应在独立进程中打开文件并执行页面操作。下面这个 Linux 示例用于统计 `input.pdf` 首页的单词数：`prlimit`（util-linux）在 Python 启动前将工作进程的虚拟地址空间上限设为 1 GiB，GNU `timeout` 在 10 秒墙钟时间后强制终止该进程。

```sh
--8<-- "includes/read-in-worker.sh"
```

请结合业务负载设定合适上限，并将非零退出码视为请求失败。若请求需要渲染页面，也可在同一工作进程中调用 `pixmap()`。另请参见[状态与安全性](status.md#safety)。

## 限制代码 {#limit-codes}

下表保留英文原文。

--8<-- "limit-codes.md"

## 错误

pyropdf 抛出的所有错误均继承自 `PdfError`：

| 错误 | 抛出时机 |
| --- | --- |
| `MalformedPdfError` | 输入不是可读的 PDF。 |
| `InternalError` | pyropdf 或底层引擎中发生 Rust panic，错误消息保留原始 panic 信息。请作为库缺陷提交报告。 |
| `PasswordError` | 密码错误。 |
| `EncryptedDocumentError` | 在调用 `unlock` 之前尝试读取已加密锁定的文档。 |
| `LimitError` | 操作触发某项限制或被取消；`code` 字段标明具体原因。 |
| `StalePageError`、`StaleObjectError` | 句柄对应的页面或对象已不存在。 |
| `DocumentClosedError` | 在 `close` 之后继续使用文档。 |
| `OcrError` | 缺少 OCR 模型或识别失败。 |

`InternalError` 可同时被 `except PdfError` 与 `except Exception` 捕获。失败的事务会被丢弃，同一文档对象仍可继续使用。进程的 panic hook 保持不变，默认诊断信息仍输出到标准错误（stderr）。该边界仅捕获可展开（unwind）的 panic，无法从直接中止（abort）的运行时中恢复。
