---
title: 从 PyMuPDF 与 pylopdf 迁移
description: 从 PyMuPDF（fitz / pymupdf）以及已归档的 pylopdf 0.15/0.16 迁移至 pyropdf 的对照指南，以及自动化代码迁移命令行工具。
---

# 从 PyMuPDF 与 pylopdf 迁移

pyropdf 基于采用 MIT 许可的 Rust 核心与单一版本化对象存储，通过贴合 Python 习惯的独立 API 覆盖日常 PDF 处理工作流。遵循 [DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)（*One way*），`pyropdf` 包本身**不包含任何兼容别名或 `fitz` 垫片层**。本指南将常见的 PyMuPDF 与 `pylopdf` 0.15 用法一一映射到规范的 pyropdf 等价写法，你也可以使用 `tools/codemod.py` 在迁移时自动完成机械性重命名。

## 自动代码迁移工具（codemod） {#codemod}

运行仓库中零依赖的 AST 代码迁移工具，即可预览或原地应用机械性重命名：

```bash
# 仅预览 unified diff，不修改任何文件
python3 tools/codemod.py --dry-run --diff path/to/your_app/

# 原地改写 PyMuPDF（fitz / pymupdf）调用
python3 tools/codemod.py --from pymupdf path/to/your_app/

# 原地改写 pylopdf 0.15 调用
python3 tools/codemod.py --from pylopdf path/to/your_app/

# 若仍残留未迁移调用，则在 CI 中以非零状态失败退出
python3 tools/codemod.py --check path/to/your_app/
```

该工具会保留原有注释与代码格式，自动重写导入语句、`doc.pages` 索引与迭代、保存关键字选项、元数据/附件/文本调用以及枚举常量，并对存在行为差异或无法机械转换的 API（`apply_redactions`、`Story`、`get_text("dict")`、`csCMYK`、`insert_text`、`get_toc`、不可变几何对象的原地修改等）输出格式为 `file:line: [code] message` 的可操作警告。

## 主要设计与行为差异 {#differences}

在逐行移植代码之前，请先确认以下四项有意为之的设计差异：

1. **全链路统一使用显示空间坐标。** 从页面读取或写入页面的所有坐标（`page.rect`、`word.bbox`、`canvas.text`、注释、链接、页面边框）均采用显示空间：单位为点（point），原点位于可见页面的**左上角**，`y` 轴向下递增，且已应用 CropBox 与 `/Rotate`。无需再手动撤销 `/Rotate` 或翻转 `y` 轴。
2. **页码索引统一从 0 开始。** `doc.pages`、`doc.outline` 与 `doc.page_labels` 全部使用从 0 开始的页码索引（而 PyMuPDF 的 `get_toc()` 和 `set_toc()` 从 1 开始）。
3. **原子事务与不可变值对象。** 将一组编辑放入 `with doc.transaction():` 中即可合并为一个可撤销版本（`doc.history`），发生异常时全部自动回滚。几何对象（`Point`、`Rect`、`Matrix`）与 `Pixmap` 均为不可变值：请使用 `r = r | p`、`r = r & o`、`m = ~m`，而不是原地修改。
4. **专注于 PDF 且属性赋值即时生效。** `pyropdf.open()` 仅读取 PDF 文件（不处理 XPS、EPUB 或 HTML `Story`；图像转 PDF 请使用 `doc.pages.new_from_image()`）。通过句柄设置属性（如 `page.rotation = 90`、`doc.form["name"].value = "Ada"`）会立即更新关联结构，无需调用 `widget.update()` 或 `annot.update()`。

## 打开、探测与保存 {#open-save}

| PyMuPDF / pylopdf 0.15 | pyropdf | 说明 |
| --- | --- | --- |
| `import fitz` / `import pymupdf` / `import pylopdf` | `import pyropdf` | 不提供运行时别名模块 |
| `fitz.open()` | `pyropdf.Document()` | 创建空白 PDF |
| `fitz.open(stream=data, filetype="pdf")` | `pyropdf.open(data)` | 支持路径、`bytes`、`bytearray` 或 `memoryview` |
| `pylopdf.peek_metadata(path)` | `pyropdf.probe(path)` | 无需完整打开文档即可读取头部与元数据 |
| `doc.save(path, deflate=True, linear=True, use_objstms=True)` | `doc.save(path, compress=True, linearize=True, object_streams=True)` | 使用 `prune=True`、`renumber=True`、`dedupe=True` 替代 `garbage=1..4` |
| `doc.saveIncr()` | `doc.save(path, incremental=True)` | 完整保留原始字节作为前缀 |
| `doc.tobytes()` | `doc.to_bytes()` | 在内存中返回 PDF 字节串 |

```py title="迁移前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    data = doc.tobytes(deflate=True, linear=True)
    doc.save("out.pdf", deflate=True, garbage=4)
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    data = doc.to_bytes(compress=True, linearize=True)
    doc.save("out.pdf", compress=True, prune=True, renumber=True, dedupe=True)
```

## 页面、边框与几何对象 {#pages-geometry}

| PyMuPDF | pyropdf | 说明 |
| --- | --- | --- |
| `len(doc)`, `doc.page_count` | `len(doc.pages)` | 实时序列视图 |
| `doc[i]`, `doc.load_page(i)` | `doc.pages[i]` | 页面移动或插入后，`Page` 句柄仍指向原页面 |
| `page.number`, `page.parent` | `page.index`, `page.document` | `page.index` 为常数时间查询 |
| `doc.delete_page(i)`, `doc.move_page(a, b)` | `del doc.pages[i]`, `doc.pages.move(a, b)` | 引用完整性规则自动维持链接与大纲有效 |
| `doc.insert_pdf(other)` | `doc.pages.extend(other.pages)` | 指定位置插入可用 `doc.pages.insert(index, page)` |
| `page.set_rotation(90)`, `page.remove_rotation()` | `page.rotation = 90`, `page.remove_rotation()` | 显示坐标始终保持一致 |
| `r.include_point(p)`, `r.intersect(o)`, `m.invert()` | `r = r | p`, `r = r & o`, `m = ~m` | 几何对象为不可变值 |

```py title="迁移前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc.load_page(0)
    page.set_rotation(90)
    r = fitz.Rect(10, 20, 100, 80)
    r.include_point((140, 120))
    doc.move_page(0, 1)
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.rotation = 90
    r = pyropdf.Rect(10, 20, 100, 80) | (140, 120)
    doc.pages.move(0, 1)
    assert page.index == 1 and r.x1 == 140
```

## 文本、单词、搜索、表格与 Markdown {#reading}

| PyMuPDF / pymupdf4llm | pyropdf | 说明 |
| --- | --- | --- |
| `page.get_text()` | `page.text()` | 默认按显示阅读顺序输出；传入 `actual_text=False` 可关闭 `/ActualText` 替换 |
| `page.get_text("words")` | `page.words()` | 返回不可变 `Word` 记录（`.bbox`、`.text`、`.block`、`.line`、`.word`） |
| `page.get_text("dict")` / `"html"` / `"xml"` | `page.layout(chars=True).to_dict()` / `.to_html()` / `.to_xml()` | 同时提供列式接口 `page.chars()` |
| `page.search_for(q)` | `page.search(q)` | 传入 `quads=True` 可获取带旋转角度的四边形 |
| `page.find_tables()` | `page.tables()` | 返回含 `.rows` 与 `.cells` 的 `Table` 记录 |
| `pymupdf4llm.to_markdown(doc)` / `to_chunks` | `doc.markdown()`, `doc.to_chunks()`, `doc.to_json()` | 原生内置；自动利用 PDF 结构树（`use_structure="auto"`） |

```py title="迁移前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    text = page.get_text(sort=True)
    words = [(w[4], w[:4]) for w in page.get_text("words")]
    hits = page.search_for("Invoice")
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    text = page.text()
    words = [(word.text, word.bbox) for word in page.words()]
    hits = page.search("Invoice")
    md = doc.markdown()
    chunks = doc.to_chunks(max_chars=1000)
```

## 渲染与 Pixmap {#rendering}

| PyMuPDF | pyropdf | 说明 |
| --- | --- | --- |
| `page.get_pixmap(dpi=150, annots=False)` | `page.pixmap(dpi=150, annotations=False)` | 支持 `antialias=`、`hinting=`、`glyph_cache=`、`colorspace="rgb" \| "gray"` |
| `page.get_svg_image()` | `page.svg()` | 将文字输出为矢量路径（`text_as_path=False` 见 `#435`） |
| `pix.save("page.png")`, `pix.tobytes("jpeg")` | `pix.save("page.png")`, `pix.to_bytes("jpeg")` | 通过 `bytes(pix)` / `memoryview(pix)` 暴露 PEP 3118 缓冲区 |
| `pix.invert_irect()`, `pix.tint_with(...)` | `pix.invert()`, `pix.tint(...)`, `pix.rotate(...)`, `pix.crop(...)` | 不可变对象：每次操作均返回新的 `Pixmap` |

```py title="迁移前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    pix = doc[0].get_pixmap(dpi=144, alpha=False, annots=True)
    pix.save("page.png")
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pix = doc.pages[0].pixmap(dpi=144, alpha=False, annotations=True, antialias=True)
    pix.save("page.png")
    gray = pix.to_gray()
```

## 使用 Canvas 绘图 {#drawing}

| PyMuPDF / pylopdf 0.15 | pyropdf | 说明 |
| --- | --- | --- |
| `page.new_shape()` / `draw_rect` / `draw_line` | `with page.draw() as canvas:` | 正常退出时原子提交，发生异常时自动回滚 |
| `page.insert_text(pt, text, fontsize=14)` | `canvas.text(text, pt, font=..., size=14)` | 显示空间坐标（左上角为原点） |
| `page.insert_textbox(rect, text)` | `canvas.textbox(rect, text, font=..., size=12)` | 返回 `TextFit` 记录 |
| `Font(fontname="Helvetica")` / `Font(fontfile=p)` | `Font.standard("Helvetica")` / `Font.from_file(p)` | 嵌入字体由 `harfrust` 塑形并在保存时子集化 |
| `page.insert_image(rect, filename=p)` | `canvas.image(rect, p)` | 接受路径、字节串、`Pixmap` 或 `PageImage` |
| `page.show_pdf_page(rect, src_doc, pno)` | `canvas.page(rect, src_doc.pages[pno])` | 支持 `clip=`、`rotate=`、`keep_aspect=` |

```py title="迁移前（PyMuPDF）"
import fitz

doc = fitz.open()
page = doc.new_page(width=400, height=200)
page.draw_rect(fitz.Rect(20, 20, 380, 180), color=(0.1, 0.3, 0.7), width=2)
page.insert_text((36, 72), "Hello from PDF", fontsize=18)
doc.save("drawn.pdf")
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.Document() as doc:
    page = doc.pages.new((400, 200))
    with page.draw() as canvas:
        canvas.rect(
            (20, 20, 380, 180),
            stroke=pyropdf.Stroke(paint=pyropdf.Color(0.1, 0.3, 0.7), width=2),
        )
        canvas.text(
            "Hello from PDF",
            (36, 72),
            font=pyropdf.Font.standard("Helvetica"),
            size=18,
        )
    doc.save("drawn.pdf")
```

## 注释、链接与表单 {#annotations-forms}

| PyMuPDF / pylopdf 0.15 | pyropdf | 说明 |
| --- | --- | --- |
| `page.annots()`, `page.add_highlight_annot(quads)` | `page.annotations`, `page.annotations.add(Highlight(quads))` | 规格对象会立即生成完备的外观流 |
| `page.get_links()`, `page.insert_link(d)` | `page.links`, `page.links.add(rect, target)` | 目标为 URI 字符串或强类型目标/动作记录 |
| `doc.set_form_field(name, value)` / `widget.update()` | `doc.form[name].value = value` | 立即重新生成控件外观，无需 `update()` 方法 |
| `page.add_widget(w)` | `page.widgets.add(TextField(name, rect, ...))` | 经校验的不可变规格对象 |
| `doc.bake(annots=True, widgets=True)` | `doc.flatten(annotations=True, widgets=True)` | 将外观展平写入页面内容 |

```py title="迁移前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    page.add_highlight_annot(page.search_for("Invoice"))
    page.add_text_annot((36, 36), "Reviewed")
    doc.save("annotated.pdf")
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("Invoice")))
    page.annotations.add(pyropdf.Note((36, 36), contents="Reviewed"))
    page.widgets.add(pyropdf.TextField("reviewer", (36, 120, 220, 144), value="Ada"))
    doc.form["reviewer"].value = "Ada Lovelace"
    doc.save("annotated.pdf")
```

## 加密、签名、事务与撤销 {#security-transactions}

| PyMuPDF | pyropdf | 说明 |
| --- | --- | --- |
| `doc.needs_pass`, `doc.authenticate(pw)` | `doc.needs_password`, `doc.unlock(pw)` | 密码错误时抛出 `PasswordError`，而非返回 `0` |
| `doc.save(p, encryption=..., user_pw=...)` | `doc.save(p, encryption=Encryption(user_password=...))` | 支持标准 AES、公钥收件人加密与 AES-GCM |
| `doc.journal_undo()`, `doc.journal_redo()` | `with doc.transaction("name"):`, `doc.history.undo()`, `doc.history.redo()` | 可通过 `doc.history.save()` / `doc.history.restore(data)` 持久化历史 |
| — | `doc.changes(incremental=True)` | 在保存前报告会失效的数字签名以及 PDF/A 或 PDF/UA 声明 |
| — | `doc.sign(...)`, `doc.signatures` | 内置 PAdES B-B / B-T 签名与签名验证 |

```py title="迁移前（PyMuPDF）"
import fitz

with fitz.open("input.pdf") as doc:
    doc.journal_enable()
    doc.journal_start_op("rotate")
    doc[0].set_rotation(90)
    doc.journal_stop_op()
    doc.journal_undo()
    doc.save("encrypted.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="open")
```

```python title="迁移后（pyropdf）"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("rotate"):
        doc.pages[0].rotation = 90
    doc.history.undo()
    doc.history.redo()
    report = doc.changes(incremental=True)
    assert report.signatures == ()
    doc.save(
        "encrypted.pdf",
        encryption=pyropdf.Encryption(user_password="open", owner_password="admin"),
    )
```

## 已知差距与排除项 {#gaps}

在迁移依赖以下能力的工作流之前，请先核对当前状态（完整功能对照表见 [evidence/pymupdf-comparison/features.md](https://github.com/yhay81/pyropdf/blob/main/evidence/pymupdf-comparison/features.md)）：

| 领域 | 当前状态 | 追踪议题 |
| --- | --- | --- |
| 永久涂黑（`apply_redactions`） | 已支持 `RedactionMark` 注释与 `doc.sanitize()`；永久移除底层文本、图像与矢量内容仍在等待嵌套溯源支持 | [#362](https://github.com/yhay81/pyropdf/issues/362), [#104](https://github.com/yhay81/pyropdf/issues/104) |
| 永久裁剪矩形外内容（`clip_to_rect`） | `page.cropbox` 仅隐藏矩形之外的显示区域，不移除底层内容流 | [#420](https://github.com/yhay81/pyropdf/issues/420) |
| CMYK 光栅像素图（`csCMYK`） | `page.pixmap()` 支持输出 RGB 与灰度图像；CMYK 光栅化在等待上游引擎支持 | [#425](https://github.com/yhay81/pyropdf/issues/425), [#433](https://github.com/yhay81/pyropdf/issues/433) |
| SVG 中的可选中文本（`text_as_path=False`） | `page.svg()` 目前将字形输出为矢量路径 | [#435](https://github.com/yhay81/pyropdf/issues/435) |
| 非 PDF 输入与 HTML 排版（`Story`、`insert_htmlbox`） | 有意不纳入范围（`pyropdf` 专注于 PDF 及图像转 PDF） | [#355](https://github.com/yhay81/pyropdf/issues/355) |
| 输入安全性 | 请参阅[状态与安全性](status.md#safety)与[API 稳定性](stability.md) | [#15](https://github.com/yhay81/pyropdf/issues/15) |
