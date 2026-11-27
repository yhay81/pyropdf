---
title: 实用示例
description: 常见 PDF 处理任务的简短完整示例——合并、拆分、渲染缩略图、提取图像与文本、添加图章与水印、填写与展平表单、涂黑标记、清理净化与密码保护。
---

# 实用示例

以下每个示例都是可直接运行的完整程序。只需将 `input.pdf` 替换为你自己的文件即可。

## 合并文档

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.save("merged.pdf")
```

导入的页面会一并带入其资源、注释、表单字段与逻辑结构。

## 按页拆分为独立文件

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        with pyropdf.Document() as part:
            part.pages.insert(0, page)
            part.save(f"page-{number}.pdf")
```

## 保留、重排、旋转或删除页面

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages.select([1, 0])    # keep these pages, in this order
    doc.pages[0].rotation = 90  # turn the new first page clockwise
    del doc.pages[-1]           # delete the last one
    doc.save("reordered.pdf")
```

## 将图像或 CBZ 归档转换为 PDF

```python
import io
import zipfile

import pyropdf

# Build an in-memory CBZ archive from rendered pages, then convert it to a PDF.
archive = io.BytesIO()
with pyropdf.open("input.pdf") as src, zipfile.ZipFile(archive, "w") as cbz:
    for number, page in enumerate(src.pages, start=1):
        cbz.writestr(f"chapter1/page{number}.png", page.pixmap(dpi=72).to_bytes("png"))

archive.seek(0)
with pyropdf.Document(limits=pyropdf.DocumentLimits.web()) as out:
    out.pages.new_from_cbz(archive)
    out.save("comic.pdf")
```

`doc.pages.new_from_cbz` 会按文件名的自然顺序读取归档成员（使 `page2.png` 排在 `page10.png` 之前），跳过 `__MACOSX` 与元数据文件，遵循 Exif 方向信息，不重新压缩直接嵌入 JPEG 与 JPEG 2000，并在单个可撤销事务中原子化执行 `DocumentLimits`（包括文件大小、单条目及总解码字节数、解压比、帧数与像素总数）。若要处理一组有序的图像路径、字节串或 `Pixmap`，请使用 `doc.pages.new_from_images(paths)`；传入 `size=(595, 842)` 可将所有页面统一适配到指定尺寸。

## 将每一页渲染为 PNG

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, data in enumerate(doc.render(dpi=150, format="png"), start=1):
        Path(f"page-{number}.png").write_bytes(data)
```

`doc.render` 会并行渲染各页。生成缩略图只需以较低的 `dpi`（例如 36）执行同一调用即可。

## 渲染缩略图并提取页面图像

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, thumb in enumerate(doc.render(dpi=36, format="png"), start=1):
        Path(f"thumb-{number}.png").write_bytes(thumb)
    for page_number, page in enumerate(doc.pages, start=1):
        for image in page.images():
            stored = image.stored()
            if stored is not None:
                Path(f"page-{page_number}-image-{image.index}.{stored.extension}").write_bytes(stored.data)
            else:
                image.pixmap().save(f"page-{page_number}-image-{image.index}.png")
```

`doc.render(dpi=36, format="png")` 会并行渲染低分辨率缩略图（也可以传入 `fit=256` 将最长边限制在指定像素内）。`page.images()` 列出页面上绘制的所有嵌入图像：`stored()` 会不经重新编码直接透传 JPEG、JPEG 2000、JBIG2 与 CCITT 原始字节，而 `pixmap()` 会将其余图像（连同软蒙版作为 Alpha 通道）解码以便导出为 PNG。若要在 PDF 内部嵌入 `/Thumb` 预览图，直接赋值 `page.thumbnail = page.pixmap(fit=96)` 即可。

## 提取文本或 Markdown

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    Path("input.txt").write_text(doc.text(), encoding="utf-8")
    Path("input.md").write_text(doc.markdown(), encoding="utf-8")
```

Markdown 输出会保留标题与表格结构，非常适合构建搜索索引或作为大语言模型的输入。

## 高亮某个词的全部匹配项

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("highlight"):
        for page in doc.pages:
            if hits := page.search("total"):
                page.annotations.add(pyropdf.Highlight(hits))
    doc.save("highlighted.pdf", incremental=True)
```

## 按页和作者列出审阅批注

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        for annotation in page.annotations:
            if comment := (annotation.contents or "").strip():
                author = (annotation.author or "").strip() or "Unknown"
                print(f"Page {number} ({author}): {comment}")
```

`page.annotations` 包含便签、高亮及其他标记注释，会自动排除链接、表单控件与弹出窗口。

## 添加图章与斜向水印

```python
import pyropdf

font = pyropdf.Font.standard("Helvetica-Bold")
with pyropdf.open("input.pdf") as doc:
    with doc.transaction("stamp and watermark"):
        doc.pages[0].annotations.add(
            pyropdf.Stamp((36, 36, 180, 76), icon=pyropdf.StampIcon.CONFIDENTIAL)
        )
        label = "CONFIDENTIAL"
        width = font.text_width(label, 48)
        for page in doc.pages:
            cx, cy = page.rect.width / 2, page.rect.height / 2
            place = pyropdf.Matrix(-35) * pyropdf.Matrix(1, 0, 0, 1, cx, cy)
            with page.draw() as canvas, canvas.opacity(0.25), canvas.transform(place):
                canvas.text(label, (-width / 2, 0), font=font, size=48, fill=pyropdf.Color(0.7, 0.1, 0.1))
    doc.save("watermarked.pdf")
```

`Stamp` 会添加一个自带外观流的标准橡皮图章注释，而 `page.draw()` 则直接在每一页的显示空间中绘制半透明斜向水印（传入 `underlay=True` 可将其绘于页面内容下方）。将两者包裹在 `doc.transaction` 中，整个编辑即可作为单个撤销步骤管理。

## 从字典填充表单

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:  # make a form to fill
    page = doc.pages[0]
    page.widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    page.widgets.add(pyropdf.CheckBox("approved", (72, 340, 90, 358)))
    page.widgets.add(pyropdf.ComboBox("status", (72, 372, 220, 396), choices=["Draft", "Final"]))
    doc.save("form.pdf")

values = {"name": "Ada Lovelace", "approved": True, "status": "Final"}
with pyropdf.open("form.pdf") as doc:
    with doc.transaction("fill"):
        for name, value in values.items():
            doc.form[name].value = value
    doc.save("filled-editable.pdf", incremental=True)
    doc.flatten()  # optional: draw the values into the pages for good
    doc.save("filled.pdf")
```

设置 `doc.form[name].value` 会重新生成对应控件的外观。使用 `incremental=True` 保存可保留原始字节并维持表单可编辑；调用 `doc.flatten()` 则会将填写后的外观绘制进页面内容并移除交互式表单字段。

## 移除无文本页面

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    blank = [index for index, page in enumerate(doc.pages) if not page.text().strip()]
    with doc.transaction("remove blank pages"):
        for index in reversed(blank):
            del doc.pages[index]
    doc.save("without-blanks.pdf")
```

## 为每一页添加书签

```python
import pyropdf
from pyropdf import Destination, OutlineEntry

with pyropdf.open("input.pdf") as doc:
    doc.outline = [
        OutlineEntry(1, page.text().strip().splitlines()[0] or f"Page {index + 1}", Destination(index))
        for index, page in enumerate(doc.pages)
        if page.text().strip()
    ]
    doc.save("bookmarked.pdf")
```

## 减小文件体积

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.subset_fonts()
    doc.save("smaller.pdf", compress=True, object_streams=True, prune=True, dedupe=True)
```

`subset_fonts` 仅保留页面实际绘制的字形，`prune` 丢弃不可达的对象，`dedupe` 则确保相同对象只写入一次。

## 分享前清理文档

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("shared.pdf")
    print(dict(report.removed))
```

`sanitize` 可移除 JavaScript、动作、附件、元数据、批注、隐藏文本与未使用对象，每类均配有独立开关。

## 标记涂黑区域并清理文档

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("mark for redaction"):
        for page in doc.pages:
            for quad in page.search("total", quads=True):
                page.annotations.add(
                    pyropdf.RedactionMark(
                        quad.rect,
                        quads=(quad,),
                        overlay_text="REDACTED",
                        default_appearance=b"/Helv 10 Tf 1 1 1 rg",
                        interior_color=pyropdf.Color(0, 0, 0),
                    )
                )
    report = doc.sanitize("marked-clean.pdf", comments=False)
    print(dict(report.removed))
```

传入 `comments=False` 可在 `sanitize` 执行一次完整保存、清除 JavaScript、动作、附件、元数据、隐藏文本与未使用对象的同时，保留 `/Redact` 审阅标记。`RedactionMark` 仅标记待审阅的涂黑区域而不删除底层页面内容；移除底层内容的完整涂黑功能在 [#362](https://github.com/yhay81/pyropdf/issues/362) 中追踪。

## 使用密码保护文档

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save("locked.pdf", encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"))
```

## 在工作量限制下打开文档

```python
import pyropdf

def page_count(data: bytes) -> int | None:
    try:
        with pyropdf.open(data, limits=pyropdf.DocumentLimits.web()) as doc:
            return len(doc.pages)
    except pyropdf.LimitError as error:
        print("refused:", error.code)
    except pyropdf.PdfError as error:
        print("unreadable:", error)
    return None

with open("input.pdf", "rb") as file:
    print(page_count(file.read()))
```

资源限制用于约束每项操作的工作量；关于这些限制并不作出的安全承诺，详见[状态与安全性](status.md)。

## 创建无障碍归档图表

[下载图表生成程序](https://raw.githubusercontent.com/yhay81/pyropdf/main/docs/includes/accessible-archive.py)。该脚本从空白文档出发，生成一页带逻辑标签的图表。你需要提供一个覆盖文本字符的可嵌入字体，以及用于 RGB 绘图颜色的 ICC 色彩配置文件。可使用本仓库自带的小型字体与 RGB 配置文件同时生成新旧两套标准版本：

```sh
uv run python -c "import runpy; from pathlib import Path; Path('chart.icc').write_bytes(runpy.run_path('tests/assets/pdfa/generate.py')['icc']())"
FONT=crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf
uv run python docs/includes/accessible-archive.py "$FONT" chart.icc chart.pdf
uv run python docs/includes/accessible-archive.py "$FONT" chart.icc earlier.pdf --earlier
```

| 输出文件 | 归档与无障碍规范 | 生成脚本写入的内容 |
| --- | --- | --- |
| `chart.pdf` | PDF/A-4 与 PDF/UA-2 | PDF 2.0 文件头、XMP 中的 2020 与 2024 修订年份，以及文档及其三个子元素上的标准 PDF 2.0 命名空间 |
| `earlier.pdf` | PDF/A-2a 与 PDF/UA-1 | PDF 1.7 文件头、A 级一致性、XMP 中的 PDF/UA 标识扩展架构，以及隐式 PDF 1.7 结构命名空间 |

两个版本都会嵌入字体与 RGB 输出配置文件，将内容语言声明为美式英语，并指示阅读器显示 XMP 文档标题。要人工复核无障碍体验，可打开渲染页面并与 Poppler 的阅读顺序报告进行比对：

```sh
pdfinfo -struct-text chart.pdf
pdftoppm -png -singlefile -scale-to 520 chart.pdf chart
```

标签按顺序依次为标题“Air flow”、段落“Air flow fell to 1”以及一张图。在渲染图像中，上方柱条标注为 2，下方柱条标注为 1 且长度为上方的一半。请确认图形的替代文本准确传达了两次试验之间的这一变化。要验证可机检规则，请显式针对每项规范执行校验：

```sh
verapdf --flavour 4 chart.pdf
verapdf --flavour ua2 chart.pdf
verapdf --flavour 2a earlier.pdf
verapdf --flavour ua1 earlier.pdf
```

配套测试会编辑图形的替代说明，并检验完整保存与增量保存的结果。删除替代说明会触发 PDF/UA-2 规则 `8.2.5.28.2-1` 与 PDF/UA-1 规则 `7.3-1` 失败，而两项归档（PDF/A）校验仍能通过。
