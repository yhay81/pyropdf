---
title: 快速开始
description: 安装 pyropdf，打开文档，读取并渲染页面，在事务中编辑并保存。
---

# 快速开始

## 安装

=== "pip"

    ```bash
    pip install pyropdf
    ```

=== "uv"

    ```bash
    uv add pyropdf
    ```

=== "Poetry"

    ```bash
    poetry add pyropdf
    ```

预编译 wheel 包覆盖 Linux、macOS 和 Windows 上的 CPython 3.11 至 3.15，并为自由线程（free-threaded）构建提供独立 wheel 包。pyropdf 没有任何必需依赖。离线 OCR 需要配套模型，可通过可选扩展安装：

=== "pip"

    ```bash
    pip install "pyropdf[ocr]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[ocr]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[ocr]"
    ```

`jp`、`ko`、`zh-cn` 和 `zh-tw` 可选扩展提供用于渲染文档未嵌入的日文、韩文和中文字符的字体；详见[渲染](rendering.md#fonts-that-a-document-does-not-embed)。

## Pyodide

发布工作流会构建面向 Pyodide 314.0.6（CPython 3.14、Emscripten 5.0.3、`pyemscripten_2026_0_wasm32`）的核心 wheel 包，并发布到 PyPI 和 GitHub Releases。你可以直接使用 `pip install pyropdf` 安装，也可以从发布附件下载 `.whl` 文件并在匹配的 Pyodide 虚拟环境中安装：

```bash
path/to/pyodide-venv/bin/pip install ./pyropdf-*-pyemscripten_2026_0_wasm32.whl
```

在安装有 Python 3.14.2、Node.js 24 及本仓库指定 Rust 工具链的 Linux 环境中，运行 `bash tools/build_pyodide.sh` 可复现构建并在 Pyodide 中测试已安装的扩展。该测试使用 PDF 字节数据和单线程验证提取、渲染和编辑功能。OCR 模型与 CJK 字体仍作为按需下载的可选项提供。

## Cloudflare Python Workers

配合 `python_workers` 兼容性标志和 `2026-11-01` 兼容性日期即可使用上述核心 wheel 包。请在 Worker 的 `fetch` 方法内部执行 `import pyropdf`——该原生扩展在导入时需要随机数，而 Workers 运行时仅在请求上下文中提供随机源。

在检出仓库中准备好 Node.js 24、Python 3.14 和 uv 0.13.9 后，可针对下载的 wheel 包运行官方本地运行时检查：

```bash
bash tools/cloudflare/smoke.sh local dist/*-pyemscripten_2026_0_wasm32.whl
```

脚本会通过 pywrangler 安装 wheel，并向回环（loopback）监听的 Worker 发送 PDF 字节数据。它会检查保存后的字节、重新打开以验证新增页面和原始文本、校验 `file_size` 限制错误，并在下一次请求中打开另一份独立文档。发布工作流会记录 wheel 文件名、SHA-256、兼容性日期和各运行时版本。

执行 **1.0 发布前的托管环境验证（hosted 1.0 release check）**时，请在专用的 Cloudflare Workers 账号中使用同一候选发布 wheel。设置具有 Workers Scripts 编辑权限的 `CLOUDFLARE_ACCOUNT_ID` 和 `CLOUDFLARE_API_TOKEN`，然后运行：

```bash
bash tools/cloudflare/smoke.sh hosted dist/*-pyemscripten_2026_0_wasm32.whl
```

该命令会部署一个名称唯一的私有 Python Worker，通过远程服务绑定发送相同的请求，并在退出时删除该 Worker。请将部署的版本 ID 与 `hosted` 验证结果随发布一并记录。Pull Request 的 CI 仅使用回环监听执行 `local` 检查，无需 Cloudflare 凭据。

## 打开与关闭

`pyropdf.open` 接受文件路径或文档的字节数据。文档对象是上下文管理器，退出 `with` 代码块时自动关闭；`pyropdf.Document()` 则创建一个空文档。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(len(doc.pages), doc.pages[0].rect)

doc = pyropdf.Document()
doc.pages.new((595, 842))
data = doc.to_bytes()
doc.close()
```

`pyropdf.probe` 无需加载完整文档即可读取页数、PDF 版本和元数据：

```python
import pyropdf

info = pyropdf.probe("input.pdf")
print(info.page_count, info.version, info.needs_password)
```

## 读取页面

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())  # (1)!
    for word in page.words():
        print(word.text, word.bbox)  # (2)!
    print(page.search("total"))  # (3)!
```

1. 按阅读顺序提取文本：分栏、表格和标题都会按照人类阅读顺序输出，而不是内容流的绘制顺序。
2. 边界框采用显示坐标：以查看器所呈现页面的左上角为原点，单位为磅（point），不受页面旋转影响。详见[核心概念](concepts.md#display-coordinates)。
3. 返回矩形列表，每处匹配对应一个矩形，可直接传入 `pyropdf.Highlight` 等批注对象。

关于版面布局、字符、表格、Markdown 与 OCR，请参阅[读取内容](reading.md)。

## 渲染页面

```python title="render.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].pixmap(dpi=144).save("page-1.png")
```

## 编辑与保存

每次编辑都会生成文档的一个新版本。事务可将多次编辑合并为一个命名步骤，供 `doc.history` 撤销；如果代码块抛出异常，文档会保持原样不变。

```python title="edit.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate and note"):  # (1)!
        page.rotation = 90
        page.annotations.add(pyropdf.Note((40, 40), contents="Checked"))
    doc.save("output.pdf", incremental=True)  # (2)!
```

1. 两项编辑合并为 `doc.history` 中的一个命名步骤。若代码块抛出异常，两项改动都不会保留，文档维持操作前的版本。
2. 仅将本次编辑写入的对象追加到原始字节之后，从而保持覆盖原始字节的数字签名依然有效。

增量保存仅追加编辑写入的对象；默认的完整保存则会重写整个文件。[版本与保存](versions.md)详细介绍了两者的区别。

## 后续阅读

- [读取内容](reading.md)：文本、单词、版面布局、表格、Markdown 与 OCR。
- [渲染](rendering.md)：像素图、批量渲染与 SVG。
- [编辑](editing.md)：页面、批注、链接、表单、大纲与附件。
- [版本与保存](versions.md)：事务、撤销、变更报告与加密。
- [限制与错误](limits.md)：为每项操作的工作量设置上限。
