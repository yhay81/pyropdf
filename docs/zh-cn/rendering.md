---
title: 渲染
description: 通过 hayro 将页面单页或多线程批量渲染为像素图、PNG、JPEG 与 SVG。
---

# 渲染

pyropdf 通过 [hayro](https://github.com/LaurenzV/hayro) 执行渲染，直接从对象存储中读取当前版本。

## 像素图

`page.pixmap()` 返回一个 `Pixmap`：不可变的 8 位像素数据，可选 RGB 或灰度，可带或不带 Alpha 通道。`dpi` 用于缩放页面，默认为 72。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=144, alpha=True)
    print(pixmap.width, pixmap.height, pixmap.channels)
    pixmap.save("page-1.png")
    jpeg = doc.pages[0].pixmap(dpi=144).to_bytes("jpeg", quality=85)  # JPEG has no alpha
```

像素图支持缓冲区协议（buffer protocol），因此 NumPy 和 Pillow 无需拷贝即可直接读取像素：`numpy.asarray(pixmap)` 的形状为 `(height, width, channels)`。

## 渲染选项

| 选项 | 作用 |
| --- | --- |
| `dpi` | 等比缩放页面；默认值 72 表示每磅 1 像素。 |
| `matrix` | 传入 `Matrix`（例如四分之一旋转 `Matrix(90)`），或传入 4 个或 6 个数值对页面进行缩放、旋转或错切；光栅区域覆盖变换后的页面，因此平移分量会被丢弃。 |
| `fit` | 正整数 `int` 或 `(width, height)` 像素二元组；等比缩放页面（指定 `clip` 时缩放裁剪区域）使其完整落入该边界内；与 `dpi` 和 `matrix` 互斥。 |
| `clip` | 显示坐标下的矩形 `(x0, y0, x1, y1)`；仅光栅化该区域内的像素。 |
| `alpha` | 保留透明度，不再将页面绘制在白色背景上。 |
| `background` | 取值 0 到 255 的 RGB 或 RGBA 元组，绘制在页面底层；未启用 `alpha` 时必须为不透明颜色。 |
| `annotations` | 设为 `False` 时略过批注与表单控件的外观。 |
| `colorspace` | 设为 `"gray"` 时按 ITU-R BT.601 权重转换为灰度图。 |
| `hinting` | 设为 `True` 时按类似 FreeType light hinting 的方式进行字体微调，使小号文字更清晰；默认不启用微调。 |
| `glyph_cache` | 设为 `True` 时每个字形在各字号下仅光栅化一次并复用，可加快文字渲染，但文字垂直位置可能产生最多半像素偏移。缓存内存不计入 `DocumentLimits`，生命周期与该线程针对当前文档版本的渲染缓存相同。默认关闭。 |
| `antialias` | 设为 `False` 时通过 hayro 的锯齿阈值关闭填充、描边与文字的抗锯齿，生成硬边矢量与文字输出，同时仍正常绘制光栅图像（且绕过 `glyph_cache`）。默认开启（`True`）。 |

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    fitted = page.pixmap(fit=1024)
    top = page.pixmap(dpi=200, clip=(0, 0, page.rect.width, 120))
    gray = page.pixmap(dpi=96, colorspace="gray", annotations=False)
    tinted = page.pixmap(background=(255, 250, 235))
    rotated = page.pixmap(matrix=pyropdf.Matrix(90))
    print(fitted.height, top.height, gray.channels, tinted.channels, rotated.width)
```

`clip` 使用显示坐标，即 `page.rect` 以及 `words()`、`search()` 所返回矩形所在的坐标空间；详见[核心概念](concepts.md#display-coordinates)。

单幅光栅图的最大尺寸为宽 65,408 像素、高 65,532 像素、总计 64,000,000 像素，超出则会抛出 `ValueError`。指定 `clip` 时，这些上限仅作用于裁剪后的光栅图，因此局部小区域可以以整页无法承受的倍率渲染：例如 A1 页面角落 100 × 100 磅的区域在 20 倍缩放下只需渲染 2,000 × 2,000 像素，而整页则需要 16 亿像素。如果变换后页面的任一边超过 524,288 像素，裁剪渲染会抛出代码为 `clip_page_side` 的 `LimitError`。在此范围内，即使从页面远端延伸至裁剪区域的图形也能落在距精确位置 1/16 像素以内。

## 图像编码

`pixmap.save(path)` 会按文件扩展名对应的格式写出图像，`pixmap.to_bytes("png")` 或 `to_bytes("jpeg", quality=...)` 则返回编码后的字节数据。除 PNG 和 JPEG 外，两者还支持 PAM、PBM、PGM、PPM、PNM、PSD 和 PostScript。仅 PNG、PAM 和 PSD 保留 Alpha 通道，其余格式会拒绝带透明通道的数据，请先对不透明像素图进行编码。

## 像素操作

每项操作都返回一个新的像素图：`invert(rect=None)`、`gamma(value)`、`tint(black, white)`、`to_gray()`、`to_rgb()`、`with_alpha()`、`without_alpha(background)`、`crop(rect)`、`scale(width, height)`、`rotate(degrees)`、`flip(axis)`、`warp(quad, width, height)`、`fill(value, rect=None)` 以及 `with_pixel(x, y, value)`。`pixel(x, y)`、`colors(rect=None)`、`color_count(rect=None)` 和 `top_color(rect=None)` 用于读取像素信息。所有矩形均以整像素为单位。`Pixmap.from_bytes(data)` 和 `Pixmap.open(path)` 支持读取 PNG、JPEG、PBM、PGM、PPM、PAM、BMP、GIF、TIFF 和 JPEG 2000；如果安装了 Pillow，`to_pil()` 可返回 Pillow 图像对象。

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=72)
    thumbnail = pixmap.to_gray().scale(pixmap.width // 4, pixmap.height // 4)
    thumbnail.invert((0, 0, 10, 10)).save("thumbnail.png")
```

## 批量渲染

`doc.render` 释放 GIL 并在最多 `workers` 个线程上并行渲染多页，按 `pages` 参数的顺序返回编码后的图像字节。它接受除 `clip` 外的所有 `page.pixmap` 选项：`fit` 会依据每页自身的显示尺寸独立计算缩放比，因此尺寸各异的页面和带有 `/UserUnit` 的图纸可以在同一批次中适配统一的像素上限。即使有其他线程同时编辑文档，所有页面也都基于调用启动时的当前版本渲染：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    images = doc.render(fit=2048, format="png", workers=4)
    print(len(images), "pages rendered")
    first_only = doc.render([0], dpi=150, format="jpeg", quality=85)
```

`max_size` 限制整批编码输出的总字节数，默认为 512 MiB。

## SVG

`page.svg()` 以 72 dpi 将页面导出为 SVG 字符串。文字以矢量路径绘制，因此在各类浏览器中呈现效果一致，但不可检索。传入 `annotations=False` 可排除批注，`max_size` 限制输出字符串大小，默认为 64 MiB：

```python
import pyropdf
from pathlib import Path

with pyropdf.open("input.pdf") as doc:
    Path("page-1.svg").write_text(doc.pages[0].svg(), encoding="utf-8")
```

## 文档未嵌入的字体 {#fonts-that-a-document-does-not-embed}

PDF 可以仅按名称引用字体而不嵌入字体程序，这在引用 MS-Gothic、MS-Mincho、SimSun 或 Batang 的日文、中文和韩文文档中十分常见。hayro 默认使用标准 14 字体绘制此类文本，但标准 14 字体不包含 CJK 字形，因此尽管 `page.text()` 能够正常读取文本，渲染画面却会显示为空白。安装对应的字体可选扩展即可提供替代渲染字体：

=== "pip"

    ```bash
    pip install "pyropdf[jp]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[jp]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[jp]"
    ```

`jp`、`ko`、`zh-cn` 和 `zh-tw` 分别安装日文、韩文、简体中文和繁体中文的 Noto Sans 与 Noto Serif 字体。此后，无论是 `page.pixmap()`、`page.svg()`、`doc.render()` 还是 `annotation.pixmap()`，都会使用对应语言的字体绘制 Adobe-Japan1、Korea1、GB1 或 CNS1 字符集的字体，并将名称表明为衬线体（如明朝体、宋体）的字体映射至衬线字形。字体文件会在页面首次需要时按需加载。

`doc.fallback_fonts` 是从语言代码 `"ja"`、`"ko"`、`"zh-CN"` 和 `"zh-TW"` 到 `FallbackFont` 记录的映射，在修改前默认列出已安装的可选扩展。将 `Font.from_file` 加载的字体构造成 `FallbackFont(sans=..., serif=...)` 并赋值，即可改用自定义字体绘制该语言；`del` 删除某语言则会禁用该语言的替代字体；赋值为 `{}` 则对所有语言禁用替代字体：

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(list(doc.fallback_fonts))  # the installed extras, such as ['ja']
    doc.fallback_fonts = {}  # render without fallback fonts
    plain = doc.pages[0].pixmap()
    del doc.fallback_fonts  # back to the installed extras
```

这些替代字体仅隶属于当前 `Document` 对象，不会被写入保存后的文件。文本提取无需这些字体，可直接从字符集读取文本；只有 `exact_bboxes=True` 的精确边界框和 `page.content()` 的文本串会用到它们，以精确度量渲染结果。
