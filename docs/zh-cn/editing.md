---
title: 编辑
description: 编辑页面、注释、链接、表单、大纲与附件，每一次修改都会生成新版本。
---

# 编辑

下文介绍的每一项编辑操作都会写入文档的新版本。将一组相关编辑放进 `doc.transaction(name)` 中，即可把它们合并为历史记录中的单个步骤；详见[版本与保存](versions.md)。

## 页面 {#pages}

`doc.pages` 是由 `Page` 句柄组成的序列。页面位置移动时，句柄会始终追踪原页面；一旦该页面被删除，继续访问句柄会抛出 `StalePageError`。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pages = doc.pages
    pages.new((595, 842))           # append an A4 page
    pages.insert(0, pages[-1])      # copy it to the front
    pages.move(0, len(pages) - 1)   # and move it to the end
    pages[0].rotation = 90
    del pages[-1]
```

从其他文档导入页面时，会一并带入其资源、注释、表单字段与逻辑结构：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.pages.select([0, 1])        # keep only these, in this order
```

`doc.pages.new_from_image(source)` 会为每一帧图像添加一个页面，按图像分辨率自动确定尺寸（或居中适配到统一的 `size=(width, height)` 页面中），在不重新压缩 JPEG 或 JPEG 2000 数据流的前提下遵循 Exif 方向（1–8）。PNG 会直接读取并将 Alpha 通道保留为软蒙版；若已安装 Pillow，TIFF、GIF 及其他格式会通过 Pillow 读取。若希望在单个可撤销事务中转换一组图像或 CBZ（`.cbz` / ZIP）归档——按自然顺序排列归档条目、忽略 `__MACOSX` 与元数据文件，并在每个数据块上强制执行 `DocumentLimits`——请使用 `doc.pages.new_from_images(sources)` 或 `doc.pages.new_from_cbz(source)`：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmaps = [page.pixmap(dpi=72) for page in doc.pages]
    scan = pyropdf.Document()
    scan.pages.new_from_image(pixmaps[0])            # one image (path, bytes, or Pixmap)
    scan.pages.new_from_images(pixmaps, size=(595, 842))  # a batch fitted to A4
    scan.save("scan.pdf")
```

`page.media_box`、`crop_box`、`bleed_box`、`trim_box` 与 `art_box` 读取经继承并裁剪至 MediaBox 内的页面边框，坐标处于与文本和渲染一致的显示空间：`(0, 0)` 为可见页面左上角。赋值一个 `Rect` 会仅在当前页面设置对应边框；若边框超出 MediaBox，会抛出 `ValueError`。渲染与文本提取会随即遵循新的 CropBox：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.crop_box = (36, 36, page.rect.width - 36, page.rect.height - 36)
    page.pixmap(dpi=72).save("cropped.png")
```

`page.flatten()` 会将注释与控件的外观绘制到页面内容中，并从页面移除这些注释与控件。

## 绘图 {#drawing}

`page.draw()` 返回一个 `Canvas`，用于在显示空间中绘制路径、图像、其他页面与文本；默认绘制在原有页面内容之上，传入 `underlay=True` 则绘制在页面内容之下。整个 `with` 代码块构成一次事务与一个撤销步骤；若块内抛出异常，所有绘制操作都会被丢弃。

```python
import pyropdf
from pyropdf import Color, PathBuilder, Stroke

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    page = doc.pages[0]
    with page.draw() as canvas:
        canvas.rect((36, 36, 236, 136), fill=Color(1, 1, 0, 0.4))
        canvas.circle((300, 100), 40, stroke=Stroke(paint=Color(0, 0, 1), width=2))
        with canvas.clip(PathBuilder().rect((36, 200, 236, 300), radius=12)), canvas.opacity(0.5):
            canvas.page((36, 200, 236, 300), other.pages[0])
        canvas.image((300, 200, 400, 300), page.pixmap(dpi=36))
```

`canvas.image` 接受 JPEG、PNG 或 JPEG 2000 字节数据、文件路径、`Pixmap` 或文档内的图像；`canvas.page` 可放置任意已打开文档的页面，并支持裁剪与旋转。多次绘制的同一图像或页面在文档中只存储一份。`clip`、`clip_text`、`transform`、`opacity`、`blend`、`layer` 与 `tag` 均为作用域上下文，作用于其内部绘制的全部内容。

### 渐变

凡是路径、图形或文本接受 `Color` 的地方（无论是 `fill` 还是 `Stroke` 的 `paint`），均可以使用 `LinearGradient` 与 `RadialGradient`。渐变的坐标位于被绘制对象的坐标空间中，因此会随 `canvas.transform` 一同变换。

```python
import pyropdf
from pyropdf import Color, ColorStop, GradientExtend, LinearGradient, Point, RadialGradient, Stroke

sky = LinearGradient(
    start=Point(0, 0),
    end=Point(0, 200),
    stops=(ColorStop(0, Color(0.2, 0.4, 0.9)), ColorStop(1, Color(1, 1, 1))),
)
sun = RadialGradient(
    start=Point(100, 100),
    start_radius=0,
    end=Point(100, 100),
    end_radius=40,
    # A stop's alpha fades the color out, through a soft mask.
    stops=(ColorStop(0, Color(1, 0.8, 0)), ColorStop(1, Color(1, 0.8, 0, 0))),
)
stripes = LinearGradient(
    start=Point(0, 0),
    end=Point(20, 0),
    stops=(ColorStop(0, Color(0, 0, 0)), ColorStop(1, Color(1, 1, 1))),
    extend=GradientExtend.REFLECT,
)

doc = pyropdf.Document()
page = doc.pages.new((200, 200))
with page.draw() as canvas:
    canvas.rect((0, 0, 200, 200), fill=sky)
    canvas.circle((100, 100), 40, fill=sun)
    canvas.line((0, 180), (200, 180), stroke=Stroke(paint=stripes, width=6))
```

`extend` 控制渐变在起止范围之外的绘制方式：两端颜色向外延续（`PAD`，默认）、仅起点或仅终点延续（`PAD_START`、`PAD_END`）、不绘制（`NONE`）、重复循环（`REPEAT`）或往返镜像（`REFLECT`）。同一画布中相同的函数与着色对象只会写入一次。`page.content()` 与 `page.paths()` 会将轴向和径向着色连同其颜色函数读回为 `ShadingPattern`，并将平铺图案连同所捕获的图案单元格读回为 `TilingPattern`，`canvas.draw` 会重绘它们的填充与描边（轴向与径向着色以有界采样近似输出，平铺图案以有界 `/PatternType 1` 流输出；函数着色与网格着色不会被重绘）。`canvas.draw` 还会使用其 `Image.fill` 颜料重绘内联光栅图像以及间接或内联模板图像遮罩（stencil image mask）。当软蒙版要求将文本填充与描边拆分为两个独立对象时，第二个对象会带有空的 `ActualText`，以确保文本提取仅读取一次。

### 文本

`canvas.text` 从基线起点绘制文本行，`canvas.textbox` 则在指定矩形内自动换行。除非另行指定 `font` 和 `size`，两者默认均使用 11 磅 Helvetica。`Font.standard` 按名称返回 14 种标准字体之一（例如 `"Times-Roman"` 或 `"Courier-Bold"`）；这些字体不嵌入文档，因此文件体积更小。其中 12 种文本字体支持 WinAnsi（大致相当于 Latin-1）字符，遇到其余字符会抛出 `PdfError`。Symbol 与 ZapfDingbats 使用各自内置编码：WinAnsi 字节会选中对应字形，可能与输入字符本身不同。要支持其他文字体系，请用 `Font.from_file` 加载 OpenType 字体，文档仅会将其嵌入一次。对于可变 TrueType 字体，可通过 `weight=700` 在字重轴上选择对应实例。由 COLR 绘制的字形，以及 `sbix` 或 CBDT 中含 PNG 位图条目的字形，无论填充色如何都会以自身彩色绘制，并能正常按文本提取。其余彩色字形会回退绘制其单色轮廓；若某个彩色字形不存在任何轮廓，绘制时会抛出 `PdfError`，而不会在可提取的文本下方留白不画。

```python
import pyropdf
from pyropdf import Color, Font

doc = pyropdf.Document()
for _ in range(2):
    doc.pages.new((300, 200))
note = "Totals include tax. Prices are in euros and valid until the end of the month."
helvetica = Font.standard("Helvetica")
for number, page in enumerate(doc.pages, start=1):
    with page.draw() as canvas:
        canvas.text("Invoice", (20, 40), font=Font.standard("Helvetica-Bold"), size=18)
        box = (20, 60, 160, 80)
        if not canvas.textbox(box, note, size=10, align="justify").fits:
            height = helvetica.text_height(note, 10, width=140)
            canvas.textbox((20, 60, 160, 60 + height), note, size=10, align="justify")
        canvas.text(f"Page {number} of {len(doc.pages)}", (240, 190), size=8, fill=Color(0.4, 0.4, 0.4))
```

文本框要么完全容纳并绘制，要么什么都不画：返回的 `TextFit` 会表明是否装得下，其 `spare_height` 表示最后一行下方的剩余高度，装不下时则为负的缺少高度。`Font.text_height` 与 `Font.text_width` 使用与画布完全一致的排版逻辑进行测量，便于调用方在绘制前确定文本框尺寸。换行遵循 Unicode 断行规则，宽度超过文本框的单词会在字素簇之间断开；若单个字素簇就已超出宽度，则会抛出 `PdfError`。`align` 可取 `"left"`、`"center"`、`"right"` 或 `"justify"`，`line_height` 按 `size` 的倍数指定行距。文本默认黑色填充；传入 `stroke` 可描边，传入 `fill=None` 可仅留空心轮廓，传入 `render_mode=TextRenderMode.INVISIBLE`（`3 Tr`）则可写入不着墨但可搜索的不可见文本。若要将后续路径或图像裁剪到字形轮廓内（`4 Tr`–`7 Tr`），请进入 `with canvas.clip_text(...):` 作用域（默认仅裁剪，传入 `fill` 和/或 `stroke` 可同时绘制并裁剪）。`rotate` 按直角旋转文本，`canvas.transform` 则支持任意角度变换。

使用嵌入的 OpenType 字体并传入 `direction="vertical"` 时，会调用 `harfrust` 进行竖排塑形，并以 `Identity-V` 及 `/DW2`/`/W2` 竖排度量嵌入字体；字列自上而下排布，多列自右向左堆叠。对于多文字混排字符串，可显式传入 `fallback_fonts` 序列（或使用 `doc.fallback_fonts`）按字素簇逐字体解析缺失字符，全程不依赖任何全局字体注册表；实际用到的字体只会在文档中嵌入一次，并在保存时子集化。

```python
import pyropdf
from pyropdf import Color, Font, TextRenderMode

doc = pyropdf.Document()
page = doc.pages.new((300, 200))
latin = Font.standard("Helvetica")
with page.draw() as canvas:
    canvas.text("Hidden OCR layer", (20, 30), render_mode=TextRenderMode.INVISIBLE)
    with canvas.clip_text("CLIP", (20, 120), font=Font.standard("Helvetica-Bold"), size=48):
        canvas.rect((0, 70, 220, 140), fill=Color(0.2, 0.5, 0.9))
    if "ja" in doc.fallback_fonts and (jp := doc.fallback_fonts["ja"].sans) is not None:
        canvas.text("Hello 日本語", (20, 160), font=latin, fallback_fonts=(jp,), size=14)
        canvas.text("縦書き\n二行目", (260, 30), font=jp, size=16, direction="vertical")
```

默认情况下，单次调用的文本上限为 1 MiB UTF-8 字节与 4,096 行，超出时会在绘制任何内容前以 `LimitError` 拒绝；对于可信输入可传入 `max_text_size=None`。详见[限制与错误](limits.md)。

要复制某个 PDF 中已经绘制好的文本，可直接将提取出的文本片段（text run）传给画布。调用 `draw` 期间，源文档必须保持打开状态。

```python
import pyropdf

with pyropdf.open("input.pdf") as source, pyropdf.Document() as target:
    page = source.pages[0]
    copy = target.pages.new((page.rect.width, page.rect.height))
    with copy.draw() as canvas:
        for run in page.content(paths=False, images=False).text_runs:
            canvas.draw(run)
    target.save("copied-text.pdf")
```

每个字形都会保留其原始 PDF 字符代码、文本矩阵与字体字典。画布会从产出这些文本片段的对应版本复制所选字体，因此源文档后续对编码或字体程序的修改不会影响此次绘制。来自同一 `content()` 结果的字体会共享复制后的资源。支持复制嵌入简单字体、标准 14 字体、嵌入的 Identity-H 与 Identity-V 字体，以及带有自有资源的 Type 3 字体。若文本片段包含不可用字体、直接字体字典、未知码宽的复合编码、奇异字形变换矩阵、包裹替换文本、图案填充或软蒙版，会抛出 `ValueError`。源文档已关闭时抛出 `DocumentClosedError`。若画布超出当前文档或源文档的资源限制，整个绘制操作会回滚。

在 15 份文档组成的往返测试语料库中，共重绘了 17,246 个文本片段。其中 `mhlw-doc.pdf` 有 8 个片段使用了未嵌入的非标准字体。语料库测试会以 72 dpi 校验其余页面像素的每个 RGB 通道误差在 16 个色阶以内，并校验上述排除项的数量与原因保持不变。

### 替换文本 {#replacing-text}

`page.replace_text(old, new, *, count=None, fallback=None)` 可在页面正文及其绘制的 Form XObject 中替换文本，覆盖 Type 1、TrueType、Type 3 以及横排（`WMode = 0`）与竖排（`WMode = 1`）的复合字体（Type 0 / CID），包括 `Identity-H`、`Identity-V`、预定义 CMap 以及自定义嵌入 CMap 流。沿着当前书写轴施加的补偿性 `TJ` 位移可保持后续文本位置不变，外层结构标签（`/MCID`）保持完整，匹配的 `/ActualText` 标记内容跨度也会随字形一并更新。

如果未子集化的嵌入 Type 0 字体已包含所需字形，`replace_text` 会自动扩展其 `/W`、`/W2` 和 `/ToUnicode` 条目。若已子集化的字体、Type 3 字体的 `/CharProcs` 或字体编码中缺少某个字形，可传入 `fallback=Font(...)` 用该回退字体绘制缺失字符并保持后续文本对齐；未提供 `fallback` 时则会抛出 `ValueError` 且不修改文档。

```python
import pyropdf

doc = pyropdf.Document()
page = doc.pages.new((300, 120))
with page.draw() as canvas:
    canvas.text("Invoice #001 total", (20, 60), size=14)
page.replace_text("#001", "#002")
if "ja" in doc.fallback_fonts and (jp := doc.fallback_fonts["ja"].sans) is not None:
    page.replace_text("total", "合計", fallback=jp)
```

## 注释

`page.annotations.add` 接受注释规格对象并返回 `Annotation` 句柄。每个注释在写入时都会附带外观流，确保不同阅读器的显示效果一致。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("total")))
    page.annotations.add(pyropdf.Note((40, 40), contents="Check this"))
    page.annotations.add(pyropdf.Square((60, 300, 200, 360), color=pyropdf.Color(0.8, 0.2, 0.1)))
    for annotation in page.annotations:
        print(annotation.type, annotation.rect)
```

支持的注释类型包括文本标记（高亮、下划线、波浪线、删除线）、便签、自由文本（`FreeText`）、图章（`Stamp`）、插入符号（`Caret`）、直线、矩形、圆形、多边形、折线、墨迹、音频（`Sound`）以及文件附件；完整选项请参阅 [API 参考](/api/annotations/)。

## 链接

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.links.add((72, 72, 200, 90), "https://pyropdf.haya.works/")
    page.links.add((72, 100, 200, 118), pyropdf.Destination(0))
```

## 表单 {#forms}

`doc.form` 将完整字段名映射到 `Field` 句柄。为字段赋值会更新字段值并自动生成其各控件的外观。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.widgets.add(pyropdf.TextField("name", (72, 300, 300, 324)))
    page.widgets.add(pyropdf.CheckBox("agree", (72, 340, 90, 358)))
    doc.form["name"].value = "Ada Lovelace"
    doc.form["agree"].value = True
    print({name: field.value for name, field in doc.form.items()})
```

`doc.form.to_fdf()` 与 `doc.form.to_xfdf()` 按 FDF（ISO 32000-2 §12.7.8）和 XFDF（ISO 19444-1）格式导出表单字段值，`parse_fdf()` 与 `parse_xfdf()` 将其解析为 `FdfData` 供检查，`doc.form.import_fdf()` 与 `doc.form.import_xfdf()` 则在单个可撤销事务中将匹配字段应用到表单：

```python
import pyropdf

with pyropdf.Document() as source, pyropdf.Document() as target:
    for doc in (source, target):
        page = doc.pages.new((400, 200))
        page.widgets.add(pyropdf.TextField("name", (40, 40, 240, 64)))
        page.widgets.add(pyropdf.CheckBox("agree", (40, 80, 58, 98)))
    source.form["name"].value = "Ada Lovelace"
    source.form["agree"].value = True

    fdf = source.form.to_fdf(file="form.pdf")
    assert pyropdf.parse_fdf(fdf).fields["name"] == "Ada Lovelace"
    assert target.form.import_fdf(fdf) == ["agree", "name"]

    xfdf = target.form.to_xfdf(include_empty=False)
    assert pyropdf.parse_xfdf(xfdf).fields == {"agree": "Yes", "name": "Ada Lovelace"}
```

## 动作、脚本与触发器 {#actions-and-triggers}

链接、大纲项、演示步骤、`doc.open_action`、`page.triggers`、`widget.triggers` 以及 `Screen` 注释（`annotation.action` 与 `annotation.triggers`）共享同一个 `Target` 动作联合类型，涵盖 `EmbeddedDestination`、`HideAction`、`SetLayerStateAction`、`JavaScriptAction`、`SubmitFormAction`、`ResetFormAction` 与 `ImportDataAction`。文档级脚本存放在 `doc.scripts`（`/Names /JavaScript`）中，目录生命周期脚本存放在 `doc.triggers`（`Catalog["AA"]`）中；pyropdf 从不执行任何脚本。

```python
import pyropdf
from pyropdf import (
    AnnotationTrigger,
    Destination,
    DocumentTrigger,
    EmbeddedDestination,
    EmbeddedTarget,
    HideAction,
    NamedAction,
    PageTrigger,
    PushButton,
    ResetFormAction,
    SubmitFormAction,
    SubmitFormFlags,
    TextField,
)

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.widgets.add(TextField("email", (72, 300, 260, 324)))
    send = page.widgets.add(PushButton("send", (270, 300, 340, 324), caption="Send"))
    doc.open_action = Destination(0)
    doc.scripts["init"] = "var ready = true;"
    doc.triggers[DocumentTrigger.WILL_SAVE] = "console.println('saving');"
    page.triggers[PageTrigger.OPEN] = NamedAction("FirstPage")
    send.triggers[AnnotationTrigger.ACTIVATE] = SubmitFormAction(
        "https://example.com/submit",
        fields=("email",),
        flags=SubmitFormFlags.EXPORT_FORMAT,
    )
    send.triggers[AnnotationTrigger.DOWN] = HideAction("email", hide=False)
    page.links.add(
        (72, 340, 220, 358),
        EmbeddedDestination(page=0, target=EmbeddedTarget(embedded_file="attachment.pdf")),
    )
    page.links.add((230, 340, 340, 358), ResetFormAction(("email",)))
    assert not pyropdf.open(doc.to_bytes()).check()
    print(doc.open_action, list(doc.scripts), send.triggers[AnnotationTrigger.ACTIVATE].url)
```

## 大纲

```python
import pyropdf
from pyropdf import Destination, OutlineEntry

with pyropdf.open("input.pdf") as doc:
    doc.outline = [
        OutlineEntry(1, "Introduction", Destination(0)),
        OutlineEntry(2, "Details", Destination(0)),
    ]
    print([item.title for item in doc.outline])
```

## 文章线索 {#article-threads}

`doc.threads` 以 `Thread` 句柄序列的形式暴露文档的文章线索（`/Threads`）。每条线索包含一个有序的 `Beads` 环形序列，其中的每个 `Bead` 句柄都带有所在页面显示坐标下的 `rect`，同时线索具备 `/I` 信息字典属性（`title`、`author`、`subject`、`keywords`、`created`、`modified`）。借助 `ThreadAction`，可以从链接、大纲项或演示导航步骤跳转到某条线索或其指定节点。

```python
import pyropdf
from pyropdf import BeadSpecification, OutlineEntry, ThreadAction

with pyropdf.open("input.pdf") as doc:
    article = doc.threads.add(
        [
            BeadSpecification(0, (36, 72, 260, 400)),
            BeadSpecification(1, (36, 72, 260, 400)),
        ],
        title="Cover Story",
        author="Editorial",
    )
    article.beads.add(1, (280, 72, 500, 400))
    doc.pages[0].links.add((36, 40, 180, 60), ThreadAction(0, 1))
    doc.outline = [OutlineEntry(1, "Read Cover Story", ThreadAction("Cover Story"))]
    print(article.title, len(article.beads), doc.pages[0].links[-1].target)
```

## 结构树与无障碍 {#structure-and-accessibility}

`doc.structure` 以顶层 `StructureElement` 句柄序列的形式暴露逻辑结构树，`walk()` 按先序遍历所有元素，`doc.structure.role_map` 用于读写 `/RoleMap`。每个元素的 `role`、`title`、`language`（BCP 47 语言标签）、`alt`、`actual_text`、`expanded`（`/E` 缩写展开）与 `page` 均支持赋值和删除，而 `children` 与 `move()` 在重排或更改父节点的同时自动维护 `/P`、`/K`、`/ParentTree` 与 `/IDTree` 的一致性。

```python
import pyropdf
from pyropdf import TagRole

with pyropdf.open("input.pdf") as doc:
    doc.structure.role_map["Callout"] = TagRole.NOTE
    root = doc.structure.add("Document")
    heading = root.children.add(TagRole.HEADING_1, page=0, title="Summary")
    abbr = heading.children.add(TagRole.SPAN, expanded="portable document format")
    note = root.children.add("Callout", page=0, language="en-US", alt="Payment terms")
    print([elem.standard_role for elem in doc.structure.walk()])
    print(abbr.expanded, note.language, note.alt)
```

## 附件与便携式集合 {#attachments}

`doc.attachments` 管理嵌入文件（`/Names /EmbeddedFiles`），每个附件的 `collection_item` 用于读写其 `/CI` `CollectionItem` 值（包括带显示前缀的 `CollectionSubitem`），而 `doc.collection` 用于配置便携式集合的架构、排序方式、配色、分栏面板、自定义 `Navigator` 与 `CollectionFolder` 目录层级。

每个嵌入文件还可以声明关联文件。为 `att.related_files` 赋一个映射，键为其 `/EF` 字典中存在的文件名键（`F` 或 `UF`）。每个关联文件序列都会保持原有顺序，多个名称可以共享同一个数据流。文件附件注释上的 `annotation.file` 也提供相同属性。

```python
import pyropdf
from pyropdf import (
    Collection,
    CollectionField,
    CollectionFieldKind,
    CollectionFolder,
    CollectionSortKey,
    CollectionSubitem,
    CollectionView,
    Navigator,
)

with pyropdf.open("input.pdf") as doc:
    att = doc.attachments.add("data.csv", b"a,b\n1,2\n", description="Source data")
    legend = doc.objects.add_stream(b"units: metres", {"Type": pyropdf.Name("EmbeddedFile")})
    att.related_files = {"F": [pyropdf.RelatedFile(filename="legend.txt", stream=doc.objects[legend])]}
    att.collection_item = {
        "Department": "Finance",
        "Total": CollectionSubitem(42, "$"),
    }
    doc.collection = Collection(
        view=CollectionView.CUSTOM,
        initial_document="data.csv",
        schema={
            "Department": CollectionField(CollectionFieldKind.STRING, "Department", order=1),
            "Total": CollectionField(CollectionFieldKind.NUMBER, "Total", order=2),
        },
        sort=(CollectionSortKey("Department"),),
        navigator=Navigator("FilmStrip"),
        root_folder=CollectionFolder(0, "Portfolio", children=(CollectionFolder(1, "Q1"),)),
    )
    print(doc.attachments["data.csv"].read(), doc.collection.navigator.layout)
```

## Web Capture {#web-capture}

`doc.web_capture.by_url(url)` 与 `by_id(source_digest)` 返回针对某个来源记录的页面集与图像集，每项都是可写的 Arlington 视图。页面集的 `O` 条目标识页面，图像集的 `O` 条目标识图像流，而 `SI` 包含来源信息（包括 URL 别名与存储的抓取命令）。读取这些记录完全离线运行。

要更改来源位置，请向 `doc.web_capture.set_sources(content, source)` 传入一个 `typed.SourceInformation` 视图或视图元组。该操作会更新 URL 索引，并保留共享旧 URL 或新 URL 的其他集合。`remove(content)` 会将集合从索引中解除链接并清除其成员的父标识符。在删除或复制集合中的页面（或使用了集合中图像的页面）之前，请先移除相关集合。重排页面顺序会保留集合成员关系。

要在 PDF 1.3–1.7 中注册以间接对象存储的 `typed.WebCapturePageSet` 或 `typed.WebCaptureImageSet`，请调用 `doc.web_capture.add(content)`，并提供 16 字节来源摘要、成员引用、来源字典以及（对于图像）页面引用计数。PDF 2.0 已弃用新增 Web Capture 记录，但现有记录仍可读取、编辑和移除。

## 页码标签与命名页面 {#page-labels}

`doc.page_labels` 列出文档的 `PageLabelRange` 记录，`page.label` 返回单页的页码标签。赋值一个列表即可替换全部标签定义，第一个范围须从第 0 页开始。插入、移动和删除页面都会保留各页对应的标签。`doc.named_pages` 将 `/Names /Pages` 中的字符串键映射到实时 `Page` 句柄，页面被移除时会自动清理指向它的条目。

```python
import pyropdf
from pyropdf import PageLabelRange, PageLabelStyle

with pyropdf.open("input.pdf") as doc:
    doc.page_labels = [
        PageLabelRange(0, PageLabelStyle.LOWER_ROMAN),   # i, ii, iii, ...
        PageLabelRange(1, prefix="A-"),                  # A-1, A-2, ...
    ]
    doc.named_pages["summary"] = 0
    doc.named_pages["appendix"] = doc.pages[1]
    print(doc.pages[1].label, doc.pages.labeled("A-1"), doc.named_pages["appendix"].index)
```

## 演示文稿与缩略图

`page.duration` 设置演示模式下阅读器自动翻页前停留的秒数，`page.transition` 设置页面的 `Transition` 切换效果，`page.navigation_steps` 列出页内导航的 `NavigationStep` 序列（`/PresSteps`）。`TransitionAction` 可在导航步骤、链接或大纲项中触发切换效果。`page.thumbnail` 以 `Pixmap` 形式解码或替换页面内嵌的 `/Thumb` 缩略图预览；以上属性均可用 `del` 删除。

```python
import pyropdf
from pyropdf import NavigationStep, Transition, TransitionAction, TransitionStyle

with pyropdf.open("input.pdf") as doc:
    slide = doc.pages[0]
    slide.duration = 5.0
    slide.transition = Transition(style=TransitionStyle.WIPE, duration=0.8, direction=0)
    slide.navigation_steps = [
        NavigationStep(
            next_action=TransitionAction(Transition(style=TransitionStyle.FADE)),
            duration=2.0,
        ),
    ]
    slide.thumbnail = slide.pixmap(fit=96)
    assert slide.thumbnail is not None
    print(slide.transition.style, len(slide.navigation_steps), slide.thumbnail.width)
```

## 图层

`doc.layers` 以 `Layer` 记录列表的形式返回可选内容组（OCG），包含名称及默认配置下的可见性；`doc.layers.configs` 列出全部配置。`switch` 用于显示或隐藏某个图层组，`use` 用于应用某套配置，渲染与文本提取都会遵循这两项设置。`visible` 用于求值图层组或成员资格字典的可见性（包括可见性表达式）。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for layer in doc.layers:
        print(layer.name, layer.on)
        if layer.name == "Draft":
            doc.layers.switch(layer.xref, on=False)
    print(doc.pages[0].text())
```

## 元数据

`doc.metadata` 是 Info 字典的实时映射。如果文档带有 XMP 数据包，每次编辑都会在同一事务中同步写入对应的 XMP 属性，从而使 PDF/A 文档的两份元数据保持一致。

```python
from datetime import UTC, datetime

import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.metadata.update(title="Invoice 42", author="Accounts")
    doc.metadata["modified"] = datetime.now(UTC)
    print(doc.metadata["title"])                       # Invoice 42
    print(pyropdf.parse_pdf_date(doc.metadata["modified"]).year > 2000)  # True
    print(doc.xmp is None)                             # True: no XMP packet
```

文档目录的阅读器与无障碍属性直接作为可赋值、可删除的属性挂在 `doc` 上：`doc.page_mode`、`doc.page_layout`、`doc.language`（BCP 47 语言标签）、`doc.mark_info` 与 `doc.viewer_preferences`。

```python
import pyropdf
from pyropdf import Duplex, MarkInfo, PageLayout, PageMode, ReadingDirection, ViewerPreferences

with pyropdf.open("input.pdf") as doc:
    doc.page_mode = PageMode.USE_OUTLINES
    doc.page_layout = PageLayout.TWO_PAGE_LEFT
    doc.language = "en-US"
    doc.mark_info = MarkInfo(marked=True)
    doc.viewer_preferences = ViewerPreferences(
        display_title=True,
        direction=ReadingDirection.L2R,
        duplex=Duplex.FLIP_LONG_EDGE,
        print_page_ranges=((0, 1),),
    )
    print(doc.page_mode, doc.language, doc.viewer_preferences.display_title)
    del doc.viewer_preferences
```

## 签名结构与加密封装 {#signature-structures}

除了[签名](versions.md#signing)本身外，`doc.sign` 与 `doc.prepare_signature` 还可配置 DocMDP 认证权限（`DocMdpPermission`，通过 `doc.certification` 与 `sig.doc_mdp` 读回）以及 FieldMDP 字段锁定（`FieldLock` 与 `FieldMdpAction`，通过 `field.lock` 与 `sig.field_locks` 读回）。`doc.security_store` 将文档安全存储（`/DSS` 以及以 `vri_key` 为键的 `/VRI`）作为去重间接流组成的 `SecurityStore` 进行读写，并在增量保存时不破坏既有签名；`doc.timestamp(timestamper)` 追加 RFC 3161 `/DocTimeStamp`（`sig.is_timestamp`）；`doc.legal_attestation` 编辑 `Catalog["Legal"]`（`LegalAttestation`）。针对专有加密处理程序，`wrap_encrypted_payload` 构建 PDF 2.0 未加密封装文档（ISO 32000-2 §7.6.7），并由 `doc.encrypted_payload` 读回为 `EncryptedPayload`。在由该函数创建的封装文档中，文件规范字典保存显示文件名，而 `doc.collection.initial_document` 保存用于 `doc.attachments` 的不透明键；当封装文档还包含其他文件时，请使用该键来编辑选定的加密载荷。

```python
import pyropdf
from pyropdf import (
    DocMdpPermission,
    EncryptedPayload,
    Encryption,
    FieldLock,
    FieldMdpAction,
    LegalAttestation,
    TextField,
    wrap_encrypted_payload,
)

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].widgets.add(TextField("amount", (72, 300, 220, 324)))
    doc.prepare_signature(
        "author_sig",
        doc_mdp=DocMdpPermission.FORM_FILLING_AND_SIGNATURES,
        field_lock=FieldLock(FieldMdpAction.INCLUDE, ("amount",)),
        reason="Certified invoice",
    )
    doc.legal_attestation = LegalAttestation(annotations=0, optional_content=False)
    assert doc.form["author_sig"].lock == FieldLock(FieldMdpAction.INCLUDE, ("amount",))
    locked_bytes = doc.to_bytes(
        encryption=Encryption(user_password="reader", owner_password="owner"),
    )

with wrap_encrypted_payload(EncryptedPayload(locked_bytes, filename="invoice.pdf")) as wrapper:
    assert wrapper.encrypted_payload is not None
    print(wrapper.encrypted_payload.filename, wrapper.encrypted_payload.subtype)
```

DSS 需要 PDF 2.0。若将其写入较旧版本的文档会引入结构缺陷，赋值操作会抛出 `PdfError` 并保留原始修订版。下例在新建文档上使用了示例签名内容；针对已有签名，请将其实际 CMS 内容传入 `vri_key` 并执行增量保存，以保留已签名的修订版。

```python
import pyropdf
from pyropdf import SecurityStore, ValidationInfo, vri_key

with pyropdf.Document() as doc:
    doc.pages.new((612, 792))
    key = vri_key(b"cms-contents")
    info = ValidationInfo(created="D:20260101000000Z")
    doc.security_store = SecurityStore(vri={key: info})
    assert doc.security_store.vri[key] == info
```

## 视口、地理空间测量与文档要求 {#viewports-and-requirements}

`page.viewports` 在综合考虑 `/CropBox`、`/Rotate` 与 `/UserUnit` 的显示坐标系下读写页面的测量视口（`/VP`），支持用于 CAD 比例换算链的 `RectilinearMeasure`（`NumberFormat`、`NumberFormatFraction`）以及用于 EPSG 或 WKT 坐标系的 `GeospatialMeasure`（`CoordinateSystem`、`PointData`）。数值坐标与换算因子均采用 `decimal.Decimal`，使高精度经纬度能够不经 `float` 舍入而将精确数字写入 PDF 对象存储。`doc.requirements` 管理 `Catalog["Requirements"]`（`DocumentRequirement`、`RequirementType`、`RequirementHandler`），`del page.viewports` 或 `del doc.requirements` 会在单个事务中移除条目并释放无引用对象。

可在视口字典、图像或表单 XObject 流，以及 3D 或 RichMedia 注释的 `obj` 上读取或赋值 `owner.geospatial_measure`。二维配准会将每个归一化局部点与一个地面点配对。对于 3D 注释，请声明 `RequirementType.GEOSPATIAL_3D` 并使用 `dimensions=3` 表示世界坐标与高程。`owner.geospatial_point(index)` 返回所属对象 PDF 坐标空间中的局部点及其对应的地面坐标。地理坐标顺序为先纬度后经度；投影坐标顺序为先东距后北距，且可选的投影矩阵优先于地面坐标数组。声明了 Adobe 扩展级别 3 或更高的 PDF 1.7 文档可以在不升级版本的前提下存储二维地理空间控制信息。若需要升级至 PDF 2.0 且升级会引入结构缺陷，则会在单个事务中拒绝该操作。格式不合法的配准仍可通过原始 PDF 字典进行检查。传入的 `Decimal` 数值在保存时会保留全部精确位；从现有 PDF 读取数值标记时的精度目前取决于底层读取器（详见[解析器追踪议题 #16](https://github.com/yhay81/pyropdf/issues/16)）。

```python
from decimal import Decimal
import pyropdf
from pyropdf import (
    CoordinateSystem,
    DocumentRequirement,
    GeospatialMeasure,
    NumberFormat,
    NumberFormatFraction,
    PointData,
    RectilinearMeasure,
    RequirementHandler,
    RequirementType,
    Viewport,
)

lat = Decimal("35.6812361234567890123456789")
lon = Decimal("139.7671249876543210987654321")

with pyropdf.Document() as doc:
    page = doc.pages.new()
    page.viewports = (
        Viewport(
            (36, 36, 260, 220),
            name="Floor plan",
            measure=RectilinearMeasure(
                "1 in = 10 ft",
                x=(
                    NumberFormat(
                        "ft",
                        Decimal("0.1388888888888889"),
                        fraction=NumberFormatFraction.FRACTION,
                        denominator=16,
                    ),
                ),
                distance=(NumberFormat("ft", Decimal("0.1388888888888889")),),
                area=(NumberFormat("sq ft", Decimal("0.0192901234567901")),),
            ),
        ),
        Viewport(
            (36, 240, 260, 460),
            name="Site map",
            measure=GeospatialMeasure(
                CoordinateSystem(epsg=4326),
                (lat, lon, lat, lon),
                lpts=(0, 0, 1, 1),
                pdu=("M", "SQM", "DEG"),
            ),
            point_data=PointData(("LAT", "LON", "ALT"), ((lat, lon, Decimal("42.5")),)),
        ),
    )
    doc.requirements = (
        DocumentRequirement(
            RequirementType.GEOSPATIAL_2D,
            handlers=(RequirementHandler("JS", script="app.alert('Geospatial viewer required');"),),
            penalty=50,
        ),
    )
    saved = doc.to_bytes()
    assert b"35.6812361234567890123456789" in saved

with pyropdf.open(saved) as reopened:
    assert not reopened.check()
    assert len(reopened.pages[0].viewports) == 2
    assert reopened.requirements[0].type is RequirementType.GEOSPATIAL_2D
```

## 文档交换与结构命名空间 {#interchange-and-structure-namespaces}

`doc.extensions` 管理直接的开发者与 ISO 扩展声明（`DeveloperExtension`、`ISO_EXTENSIONS`）；`associated_files` 为文档、页面、结构元素、注释与文档部件附加带 `AssociatedFileRelationship` 关系的关联文件；`piece_info` 在目录和页面上存储 `/PieceInfo` 应用程序字典；`doc.parts` 构建可供 `DocumentPartAction` 跳转的 `/DPartRoot` 页面范围树；`doc.structure.namespaces` 支持在同一棵结构树中混用 PDF 1.7（`PDF_1_7_NAMESPACE`）、PDF 2.0（`PDF_2_0_NAMESPACE`）以及自定义命名空间。

```python
import pyropdf
from pyropdf import (
    PDF_1_7_NAMESPACE,
    PDF_2_0_NAMESPACE,
    AssociatedFileRelationship,
    DeveloperExtension,
    DocumentPartAction,
    OutlineEntry,
)

with pyropdf.Document() as doc:
    first = doc.pages.new()
    second = doc.pages.new()
    doc.extensions.add(DeveloperExtension("ISO_", "2.0", 32001))
    doc.associated_files.add(
        "source.xml",
        b"<invoice><total>42</total></invoice>",
        media_type="application/xml",
        relationship=AssociatedFileRelationship.SOURCE,
    )
    first.piece_info.set("ACME", {"stage": "approved"})
    part = doc.parts.add_part(first, second, dpm={"Record": "INV-1"})
    doc.outline = [OutlineEntry(1, "Invoice part", DocumentPartAction(part))]

    ns20 = doc.structure.namespaces.add(PDF_2_0_NAMESPACE)
    ns17 = doc.structure.namespaces.add(PDF_1_7_NAMESPACE)
    custom = doc.structure.namespaces.add("https://example.com/ns", role_map={"Callout": ("Aside", ns20)})
    root_elem = doc.structure.add("Document", namespace=ns20)
    root_elem.children.add("TOC", namespace=ns17)
    note = root_elem.children.add("Callout", namespace=custom)
    assert note.resolved_role == ("Aside", PDF_2_0_NAMESPACE)
    saved = doc.to_bytes()

with pyropdf.open(saved) as reopened:
    assert not reopened.check()
    assert reopened.associated_files[0].filename == "source.xml"
    assert isinstance(reopened.outline[0].destination, DocumentPartAction)
```

## 清理净化 {#sanitizing}

`doc.sanitize(path)` 可按各自独立的开关移除 JavaScript、动作、附件、元数据、批注、隐藏文本与未使用对象，并执行完整保存，使输出文件仅包含单一修订版。返回的 `SanitizeReport` 会统计各类项的移除数量，并列出结果文件所破坏或不再主张的数字签名与规范声明。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("clean.pdf", comments=False)
    print(dict(report.removed), report.changes.signatures, report.dropped_claims)
```

## 字体子集化

`doc.subset_fonts()` 在单个事务中将每个嵌入的 TrueType、OpenType 与 CFF 字体程序裁剪至各页面实际绘制的字形（按所有可选内容组均开启计算）。字形 ID 保持不变，仅清空未使用的字形轮廓，因此文本提取结果与字宽完全不受影响。被注释外观或表单默认资源引用的字体程序会保持完整。返回的每个 `SubsetFont` 对象代表一个体积缩减的字体程序。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for font in doc.subset_fonts():
        print(font.name, font.kept, font.glyphs, font.size_before, font.size_after)
    doc.save("smaller.pdf")
```

## 对象

`doc.objects` 将对象编号映射到间接对象，每个值都是强类型 Python 对象（`Dictionary`、`Array`、`Name`、`Stream` 等），修改后会写回为新版本。`doc.check()` 报告文档偏离 Arlington PDF 模型的位置。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    catalog = doc.catalog
    print(catalog["Type"], doc.pages[0].obj["MediaBox"])
    print(doc.check().defects)
```

## 印刷制版数据 {#print-production-data}

`page.box_styles` 描述为 CropBox、BleedBox、TrimBox 与 ArtBox 存储的辅助线样式；省略某项辅助线时沿用阅读器设置，而页面边框本身决定该辅助线是否生效。`page.separations` 按印版顺序列出预分色页面的实际页面句柄。

```python
import pyropdf

with pyropdf.Document() as doc:
    cyan, black = doc.pages.new(), doc.pages.new()
    cyan.separations = {
        cyan: pyropdf.SeparationPlate(colorant=pyropdf.Name("Cyan")),
        black: pyropdf.SeparationPlate(colorant=pyropdf.Name("Black")),
    }
    assert list(black.separations) == [cyan, black]
```

重新赋值整个分色组即可更改成员或印版顺序。删除 `cyan.separations` 会同时清除两个页面上的分色组。移动某张印版页面会保留其身份标识；若要移除或导入分色组中的部分页面，或单独复制某张印版，需先将其从分色组中分离。

`PrinterMark` 与 `Watermark` 均接受同一文档内已有的 Form XObject。注释的 `print_appearance` 会解析选定的常规外观及其资源。在水印上，`fixed_print` 存储相对于印刷介质的变换矩阵与平移比例。对于遗留的陷印网络（trap network），选定的 Form 会暴露印刷色模型、分色名称与陷印区域，而注释字典则保留原始陷印快照。图像或 Form 的 `opi` 暴露现有的代理文件规范与特定版本参数，可用于编辑既有的遗留数据。

这些存储参数用于描述预期的输出意图；设备分色、陷印生成、OPI 替换与物理印机拼版属于渲染引擎的职责范围（见[渲染相关工作](https://github.com/yhay81/pyropdf/issues/248)与[引擎追踪议题](https://github.com/yhay81/pyropdf/issues/7)）。
