---
title: 读取内容
description: 文本、单词、字符、版面布局、搜索、表格、Markdown、JSON、检索分块、带标签阅读顺序与离线 OCR。
---

# 读取内容

所有读取器都作用于文档的当前版本，因此无需保存即可立即看到刚刚完成的编辑。

## 文本与单词

`page.text()` 按阅读顺序返回文本，并依据版面布局保留空格与换行。`page.words()` 返回带矩形框的 `Word` 项，`page.chars()` 则返回带字形边界框的字符数据。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    print([word.text for word in page.words(clip=(0, 0, 300, 200))])
    print(doc.text())  # every page, joined in order
```

`clip` 将读取范围限定在显示坐标下的某个矩形内，原点为页面左上角。默认情况下，标记内容中的 `/ActualText` 会替换其所包围字形的文本；向 `page.text()`、`page.words()`、`page.layout()`、`page.chars()`、`page.search()` 或 `annotation.text()` 传入 `actual_text=False`，即可直接查看底层字形的原始 Unicode 值。

### 混排方向与日语注音（振假名） {#mixed-orientations-and-japanese-ruby-furigana}

对于同时包含横排区域或边栏（标题、图注、表格、页脚）与竖排 CJK 分栏（含多段段组版式）的页面，排序前会先按一致的书写方向划分区域：横排区域自上而下、自左向右阅读，竖排 CJK 分栏在各自区域内自上而下、自右向左阅读。

日语注音（振假名）——紧贴横排汉字上方或竖排汉字右侧的小号假名，以及 Markdown 导出中的 PDF 结构 `/Ruby`（`/RB`/`/RT`/`/RP`）元素——可通过 `ruby` 选项控制：

- `"omit"`（默认）：省略振假名，使正文词语在阅读、搜索和 Markdown 导出中保持完整。
- `"parenthesize"`：将每段振假名以圆括号内联插入其对应汉字之后（例如 `東京(とうきょう)`）。
- `"keep"`：将注音文本按普通文本保留。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())                       # omits furigana by default
    print(page.text(ruby="parenthesize"))    # inline furigana, e.g. 東京(とうきょう)
    print(page.markdown(ruby="parenthesize"))
```

## 版面布局

`page.layout()` 将文本组织为块（block）、行（line）和样式段（span），并附带字体、字号和颜色信息。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    layout = doc.pages[0].layout()
    for block in layout.blocks:
        for line in block.lines:
            print([span.text for span in line.spans])
```

## 搜索

`page.search` 返回每处匹配的矩形列表（传入 `quads=True` 时返回四边形列表），可直接用于创建高亮批注：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    hits = page.search("total")
    if hits:
        page.annotations.add(pyropdf.Highlight(hits))
```

## 表格与 Markdown

`page.tables()` 根据表格框线识别表格，传入 `strategy="text"` 时也可基于文本对齐关系识别。对于无框线或部分有框线的网格，可显式传入显示空间下的 `horizontal_lines` / `vertical_lines` 辅助线，或调整 `snap_tolerance`、`join_tolerance`、`min_gutter`、`min_words_vertical` 与 `min_words_horizontal`。每个 `Table` 都会暴露检测到的 `header_rows` 和 `headers`，以及 `to_dicts()`、可选的 `to_dataframe()`（需安装 `pandas`）和 `markdown()`。`page.markdown()` 与 `doc.markdown()` 会将页面转换为包含表格在内的 Markdown。

当文档带有结构标签且通过可信度检查（包含 `/MarkInfo << /Marked true >>`、无 `/Suspects`、结构树有效且对非 Artifact 页面文本覆盖率足够）时，`markdown()` 默认使用 PDF 结构树（`use_structure="auto"`）：标题（`H1`–`H6`、`H`、`Title`）、列表（`L`/`LI`/`Lbl`/`LBody`）、表格（`Table`/`THead`/`TBody`/`TFoot`/`TR`/`TH`/`TD`）、引用块、代码块、带 `/Alt` 描述的图示、`/ActualText` 替代文本及逻辑阅读顺序均取自结构树，同时自动跳过标记为 `Artifact` 的页眉和页脚。自定义角色会通过 `/RoleMap` 与 PDF 2.0 命名空间解析。传入 `expand_abbreviations=True` 可启用 `/E` 缩写展开，传入 `use_structure="always"` 可跳过可信度检查直接读取结构树，传入 `use_structure="never"` 则强制采用几何版面分析。在 `"auto"` 模式下，未加标签的页面以及包含未解析 Form XObject 标记内容的页面会自动回退至几何提取。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for table in doc.pages[0].tables():
        print(table.headers, table.to_dicts())
        print(table.markdown())
    print(doc.markdown())
    print(doc.markdown(use_structure="never"))  # force geometric layout
```

## JSON 与检索分块 {#json-and-retrieval-chunks}

`doc.to_json()` 将相同的页面元素——标题、段落、列表项、表格、图像、引用块和代码块——导出为 JSON，每个元素均带有页码索引和显示空间边界框，在基于结构树提取时还会附带可选的 `alt_text` 与继承的 BCP 47 `language`，启用图像导出时亦包含 `image` 字段。`doc.to_chunks()` 按字符预算（`max_chars`，默认 2000）将连续元素聚合为 Markdown 检索分块，并在每个标题处开启新分块，使每个 `Chunk` 都保留所属章节 `section`、覆盖页码 `pages`、主要语言 `language` 以及带来源信息的 `elements`。表格、图像、标题和代码块绝不会被拆分（超出预算时 `chunk.oversized` 会标记为 `True`）。

三种文档级导出方法——`doc.markdown()`、`doc.to_json()` 和 `doc.to_chunks()`——共享一组可组合的检索控制选项（单页版 `page.markdown()` 也支持单页控制项 `margins`、`images` 和 `max_graphics`）：

- **`margins`**：无论页面正立还是旋转，均按显示空间边距排除正文区域外的页眉、页脚、侧边栏、表格和图像。可传入单个数值用于四边，或传入 `(horizontal, vertical)` 或 `(left, top, right, bottom)`。
- **`ignore_headers_footers=True`**：在多页选择范围内确定性地抑制顶部和底部区域重复出现的页眉与页脚——会自动归一化连续的阿拉伯数字与罗马数字页码（如 `Page 1 of 10`、`1 / 10`、`- 1 -`、`Page iv of xii`）——同时保留首页独有的标题和正文行。
- **`images`**：默认为 `None`（纯内存处理，零磁盘写入，不解码图像）。传入目录路径时，会在导出成功后原子性地写出受限的 PNG 或 JPEG 文件（`page-{page}-image-{index}.{ext}`）并生成 Markdown 图像链接；传入回调函数时，则不落盘，而是向回调逐个传递 `ExportedImage`（包含 `page`、`index`、`bbox`、`filename`、`media_type`、`data`、`width`、`height`、`alt_text`），并采用回调返回的链接目标（返回 `None` 则省略该链接）。
- **`max_graphics`**：限制每页检查的图形元素（路径、线段、裁剪和图像）数量（默认 65,536；遇到高密度矢量 CAD 或地图页面时将抛出代码为 `content_element_count` 的 `LimitError` 而非卡死；对于可信输入可传入 `None` 取消上限）。

```python
import json
from pathlib import Path
import pyropdf

with pyropdf.open("input.pdf") as doc:
    md = doc.markdown(
        margins=(36, 54, 36, 54),
        ignore_headers_footers=True,
        images=Path("exported_images"),
        max_graphics=10_000,
    )
    data = json.loads(
        doc.to_json(
            ignore_headers_footers=True,
            images=lambda img: f"https://cdn.example/{img.filename}",
        )
    )
    print(len(data["pages"]), [elem["kind"] for elem in data["pages"][0]["elements"]])
    for chunk in doc.to_chunks(
        max_chars=500,
        margins=(36, 54),
        ignore_headers_footers=True,
        expand_abbreviations=True,
    ):
        print(chunk.index, chunk.pages, chunk.section, chunk.language, chunk.text)
```

## 带标签的阅读顺序 {#tagged-reading-order}

对于带标签的 PDF，`doc.structure.reading_order()` 会跨页按文档结构树的逻辑顺序提取文本：读取页面标记内容序列、略过 `Artifact` 元素，对未加标签的文档则返回 `()`。每个 `StructureText` 保留其结构元素 `element`、内容引用 `item` 以及文本 `text`（当文本无法解析时为 `None`，例如不含替代文本的 Form XObject）。元素的 `actual_text` 和 `alt`（位于 `Figure`、`Formula`、`Form` 上）会覆盖实际绘制的字形，传入 `expand_abbreviations=True` 时则会使用 `/E` 缩写展开。由于 `page.draw()` 会将内容绘制到 Form XObject 中，在为绘制内容打标签以供逻辑提取时，请向 `canvas.tag()` 传入 `actual_text`、`alt` 或 `expanded`。编写结构树的方法参见[结构与无障碍](editing.md#structure-and-accessibility)。

```python
import pyropdf
from pyropdf import TagRole

with pyropdf.Document() as doc:
    page = doc.pages.new((360, 200))
    with page.draw() as canvas:
        with canvas.tag(TagRole.HEADING_1, actual_text="Summary"):
            canvas.text("Summary", (36, 40), size=18)
        with canvas.tag(TagRole.SPAN, actual_text="PDF", expanded="Portable Document Format"):
            canvas.text("PDF", (36, 70), size=12)
        with canvas.tag(TagRole.FIGURE, alt="Revenue chart"):
            canvas.rect((36, 90, 140, 140), fill=pyropdf.Color(0.2, 0.4, 0.8))
        with canvas.tag(TagRole.ARTIFACT):
            canvas.text("Page 1", (36, 180), size=9)
    for item in doc.structure.reading_order(expand_abbreviations=True):
        print(item.element.standard_role, item.text)
```

## 绘制内容项

`page.content()` 返回页面绘制的所有元素——字形串、路径、图像和裁剪区域——并附带包围它们的标记内容。每个提取出的 `Color` 除 sRGB 的 `red`、`green`、`blue` 和 `alpha` 值外，还保留 PDF 中存储的 `colorspace`（如 `"DeviceGray"`、`"DeviceRGB"`、`"DeviceCMYK"`、`"ICCBased"`、`"CalGray"`、`"CalRGB"`、`"Lab"`、`"Indexed"`、`"Separation"` 或 `"DeviceN"`）及原始 `components`（相等性比较与哈希仅比较 sRGB 和 alpha）。每个 `TextRun` 暴露其 `writing_mode`（`WritingMode.HORIZONTAL` 或 `WritingMode.VERTICAL`），其 `FontRef` 报告 PDF 字体对象标识（`xref`、`subtype`、`name` 与 `is_embedded`）。`page.operations` 则以操作符形式读写内容流。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(len(page.content()), "items")
    print(page.operations[:3])
```

`page.paths()` 以列式 `Paths` 返回相同路径：每行对应一条路径，所有路径的线段存入独立列中，不为单条路径或线段分配 Python 对象，因此包含数十万条线段的图纸也能在接近页面渲染耗时内完成读取。`column()` 返回可供 NumPy 零拷贝读取的只读 `memoryview`，pyarrow、polars 和 DuckDB 则可通过 Arrow PyCapsule 接口直接导入整张表。逐行访问时会按需构造 `Path` 记录。`paths.clusters()`（以及 `page.content().path_clusters()`）按可配置的 `x_tolerance` 和 `y_tolerance` 将显示空间中相邻的描边与填充路径聚类为不可变的 `PathCluster` 记录（含 `bbox`、`path_indices` 与 `count`），默认排除整页背景矩形（`exclude_background=True`），并在遵循 `DocumentLimits` 的同时用 `max_clusters` 限制聚类数量。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    paths = doc.pages[0].paths()
    print(len(paths), "paths of", len(paths.column("verb")), "segments")
    # numpy.asarray(paths.column("stroke_red")) reads the same buffer.
    red = paths.column("stroke_red")
    print([path.bbox for path, value in zip(paths, red) if value == 1])
    for cluster in paths.clusters(x_tolerance=3.0, y_tolerance=3.0):
        print(cluster.bbox, cluster.count)
```

`page.chars()`（以及 `page.layout(chars=True).chars`）同样以列式 `Chars` 形式暴露字符，不为单个字形创建 Python 对象。`chars.names` 列出全部列名：字形边界框（`x0`、`y0`、`x1`、`y1`）、基线原点（`origin_x`、`origin_y`）、UTF-8 文本缓冲区（`text_offsets`、`text_data`）、版面索引（`block`、`line`、`span`）、`is_synthetic` 以及内容来源（`stream`、`operation`，缺失时为 `0`）。`chars.column(name)` 返回可供 NumPy 零拷贝读取的只读 `memoryview`，`chars[start:stop]` 支持零拷贝切片，pyarrow、polars 和 DuckDB 可通过 Arrow PyCapsule 接口（`__arrow_c_schema__` 与 `__arrow_c_array__`，含单个 `text` UTF-8 字段以及可空的 `stream` 和 `operation`）将其导入为 Arrow struct 数组；索引或迭代时按需生成 `Char` 记录。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    chars = doc.pages[0].chars()
    x0 = chars.column("x0")
    offsets = chars.column("text_offsets")
    data = bytes(chars.column("text_data"))
    # numpy.asarray(x0) reads the same buffer; pyarrow.array(chars) uses Arrow PyCapsule.
    print(len(chars), chars.names)
    print([(data[offsets[i] : offsets[i + 1]].decode(), x0[i]) for i in range(min(3, len(chars)))])
```

## 图像

`page.images()` 按与 `page.content()` 相同的顺序列出页面绘制的所有图像。`pixmap()` 像渲染器一样解码图像，并将其软遮罩作为 Alpha 通道；`stored()` 则按文档原始存储形式返回 JPEG、JPEG 2000、JBIG2 或 CCITT 数据。`doc.replace_image(xref, data)` 可用 JPEG、JPEG 2000 或 PNG 数据替换指定图像，`doc.remove_image(xref)` 可清空该图像，两者均对所有绘制该图像的页面生效。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for image in doc.pages[0].images():
        stored = image.stored()
        if stored is not None:
            print(image.item.xref, stored.extension, len(stored.data))
        else:
            image.pixmap().save(f"image-{image.index}.png")
```

## 离线 OCR {#offline-ocr}

安装 `ocr` 可选扩展后，`OcrEngine()` 会加载 PP-OCR 模型，`page.ocr` 返回识别出的单词列表。`page.apply_ocr` 会将这些单词添加为不可见文本层，使 `text()` 和 `search()` 能够检索到它们。

两个方法均接受 `mode` 选项：

- `"full"`：无论是否存在已有文本，都识别整页（或 `clip` 区域），这是 `page.ocr` 的默认值。
- `"skip_existing"`：若页面（或 `clip` 区域）已包含可提取文本则直接跳过，这是 `page.apply_ocr` 的默认值。
- `"hybrid"`：仅定位未被可读原生文本覆盖的光栅图像区域（忽略不可见文本、`/Artifact` 标记和微小边缘重叠），在 `DocumentLimits` 限制内合并相邻条带或密集装饰图像，仅对这些区域执行 OCR，并过滤掉与现有原生文本重复的单词。

```python
import pyropdf

engine = pyropdf.OcrEngine()
with pyropdf.open("input.pdf") as doc:
    words = doc.pages[0].ocr(engine=engine, dpi=150, mode="hybrid")
    print([word.text for word in words])
    # Or write only the missing raster text into a mixed page:
    doc.pages[0].apply_ocr(engine=engine, dpi=150, mode="hybrid")
```

请创建一次引擎并反复复用：加载模型是最耗时的环节。
