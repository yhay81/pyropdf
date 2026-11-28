---
title: 내용 읽기
description: 텍스트, 단어, 문자, 레이아웃, 검색, 표, Markdown, JSON, 검색 청크, 태그 지정 읽기 순서 및 오프라인 OCR.
---

# 내용 읽기

모든 읽기 API는 문서의 현재 버전을 대상으로 동작하므로, 저장하지 않아도 방금 수행한 편집 결과를 바로 읽을 수 있습니다.

## 텍스트와 단어

`page.text()`는 레이아웃에 따른 공백과 줄바꿈을 유지한 채 읽기 순서로 텍스트를 반환합니다. `page.words()`는 경계 사각형이 포함된 `Word` 항목을, `page.chars()`는 글리프 상자가 포함된 문자를 반환합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())
    print([word.text for word in page.words(clip=(0, 0, 300, 200))])
    print(doc.text())  # every page, joined in order
```

`clip`은 페이지 왼쪽 위 모서리를 원점으로 하는 표시 좌표 사각형으로 읽기 범위를 제한합니다. 기본적으로 마크드 콘텐츠의 `/ActualText`가 감싼 글리프의 텍스트를 대체하지만, `page.text()`, `doc.text()`, `page.words()`, `page.layout()`, `page.chars()`, `page.search()`, `annotation.text()`에 `actual_text=False`를 전달하면 기반 글리프의 유니코드 값을 직접 확인할 수 있습니다.

### 가로·세로 혼용 방향과 일본어 루비(후리가나) {#mixed-orientations-and-japanese-ruby-furigana}

가로쓰기 띠 영역이나 사이드바(제목, 캡션, 표, 바닥글)와 세로쓰기 CJK 열(다단 *단구미* 레이아웃 포함)이 섞여 있는 페이지는 정렬 전에 일관된 방향 영역으로 분할됩니다. 가로 영역은 위에서 아래·왼쪽에서 오른쪽으로, 세로 CJK 열은 해당 영역 내에서 위에서 아래·오른쪽에서 왼쪽으로 읽힙니다.

가로 한자 바로 위나 세로 한자 바로 오른쪽에 배치되는 작은 가나 문자열과 Markdown·JSON·청크 내보내기의 PDF 구조 `/Ruby`(`/RB`/`/RT`/`/RP`) 요소 같은 일본어 루비(후리가나) 주석은 `ruby` 옵션으로 제어합니다.

- `"omit"`(기본값): 후리가나를 생략하여 읽기, 검색, Markdown·JSON·청크 내보내기에서 기본 단어가 끊어지지 않도록 유지합니다.
- `"parenthesize"`: 각 후리가나를 해당 기본 문자 바로 뒤에 괄호로 인라인 삽입합니다(예: `東京(とうきょう)`).
- `"keep"`: 루비 런을 일반 텍스트로 그대로 보존합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())                       # omits furigana by default
    print(page.text(ruby="parenthesize"))    # inline furigana, e.g. 東京(とうきょう)
    print(page.markdown(ruby="parenthesize"))
```

## 레이아웃

`page.layout()`은 텍스트를 블록, 줄, 스팬 단위로 묶어 글꼴, 크기, 색상 정보와 함께 반환합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    layout = doc.pages[0].layout()
    for block in layout.blocks:
        for line in block.lines:
            print([span.text for span in line.spans])
```

## 검색

`page.search`는 일치 항목마다 사각형을 반환하며, `quads=True`를 지정하면 강조 주석에 바로 전달할 수 있는 사변형(quad)을 반환합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    hits = page.search("total")
    if hits:
        page.annotations.add(pyropdf.Highlight(hits))
```

## 표와 Markdown

`page.tables()`는 구분선을 기준으로 표를 찾으며, `strategy="text"`를 지정하면 텍스트 정렬을 바탕으로 표를 찾습니다. 테두리가 없거나 일부만 그어진 격자에는 표시 공간의 `horizontal_lines` / `vertical_lines` 안내선을 직접 전달하거나 `snap_tolerance`, `join_tolerance`, `min_gutter`, `min_words_vertical`, `min_words_horizontal`을 조정할 수 있습니다. 각 `Table`은 감지된 `header_rows`와 `headers`, `to_dicts()`, 선택적 `to_dataframe()`(`pandas` 필요), 그리고 `markdown()`을 제공합니다. `page.markdown()`과 `doc.markdown()`은 표를 포함한 페이지를 Markdown으로 변환합니다.

문서에 태그가 지정되어 있고 신뢰성 검사(`/MarkInfo << /Marked true >>`, `/Suspects` 없음, 유효한 트리, 비-아티팩트 페이지 텍스트에 대한 충분한 범위)를 통과하면, `markdown()`은 기본적으로 PDF 구조 트리를 사용합니다(`use_structure="auto"`). 이 경우 제목(`H1`–`H6`, `H`, `Title`), 목록(`L`/`LI`/`Lbl`/`LBody`), 표(`Table`/`THead`/`TBody`/`TFoot`/`TR`/`TH`/`TD`), 인용구, 코드 블록, `/Alt` 설명이 있는 그림, `/ActualText` 대체 텍스트 및 논리적 읽기 순서를 구조 트리에서 가져오고, `Artifact`로 표시된 머리글과 바닥글은 건너뜁니다. 사용자 정의 역할은 `/RoleMap`과 PDF 2.0 네임스페이스를 통해 해석됩니다. `/E` 약어 확장을 적용하려면 `expand_abbreviations=True`를, 신뢰성 검사 없이 구조 트리를 읽으려면 `use_structure="always"`를, 기하학적 레이아웃을 강제하려면 `use_structure="never"`를 전달하세요. `"auto"` 모드에서는 태그가 없는 페이지나 해석되지 않은 Form XObject 마크드 콘텐츠가 있는 페이지가 자동으로 기하학적 추출로 폴백됩니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for table in doc.pages[0].tables():
        print(table.headers, table.to_dicts())
        print(table.markdown())
    print(doc.markdown())
    print(doc.markdown(use_structure="never"))  # force geometric layout
```

## JSON 및 검색 청크 {#json-and-retrieval-chunks}

`doc.to_json()`은 제목, 단락, 목록 항목, 표, 이미지, 인용구, 코드 블록 같은 동일한 페이지 요소를 JSON으로 내보냅니다. 각 요소에는 페이지 인덱스와 표시 공간의 경계 상자가 포함되며, 구조 트리에서 추출된 경우 선택적 `alt_text`와 상속된 BCP 47 `language`, 이미지 내보내기를 켠 경우 `image` 필드도 함께 담깁니다. `doc.to_chunks()`는 연속된 요소들을 문자 수 한도(`max_chars`, 기본값 2000) 내의 Markdown 검색 청크로 묶으며, 제목이 나올 때마다 새 청크를 시작하므로 각 `Chunk`는 소속 `section`, `pages`, 대표 `language`, 출처 정보를 지닌 `elements`를 보존합니다. 표, 이미지, 제목, 코드 블록은 중간에서 분할되지 않으며, 한도를 초과한 경우 `chunk.oversized`로 표시됩니다.

세 가지 문서 내보내기 메서드인 `doc.markdown()`, `doc.to_json()`, `doc.to_chunks()`는 조합 가능한 검색 제어 옵션을 공유합니다(단일 페이지 `page.markdown()`도 `margins`, `images`, `max_graphics`를 지원합니다).

- **`margins`**: 정방향 및 회전된 페이지 모두에서 표시 공간의 본문 영역 바깥에 있는 머리글, 바닥글, 사이드바, 표, 이미지를 제외합니다. 네 면에 공통으로 적용할 숫자 하나, `(horizontal, vertical)` 또는 `(left, top, right, bottom)`을 전달할 수 있습니다.
- **`ignore_headers_footers=True`**: 여러 페이지를 선택했을 때 상단과 하단 띠 영역에 반복되는 머리글과 바닥글을 결정론적으로 제거합니다. `Page 1 of 10`, `1 / 10`, `- 1 -`, `Page iv of xii`처럼 순차적으로 증가하는 아라비아·로마 숫자 페이지 번호를 정규화해 판별하면서, 첫 페이지의 고유 제목과 본문 줄은 그대로 보존합니다.
- **`images`**: 기본값은 `None`(순수 메모리 내 처리, 파일시스템 쓰기 없음, 이미지 디코딩 없음)입니다. 디렉터리 경로를 전달하면 내보내기가 성공한 뒤 제한된 PNG 또는 JPEG 파일(`page-{page}-image-{index}.{ext}`)을 원자적으로 기록하고 Markdown 이미지 링크를 생성합니다. 콜백을 전달하면 디스크에 쓰지 않고 각 `ExportedImage`(`page`, `index`, `bbox`, `filename`, `media_type`, `data`, `width`, `height`, `alt_text`)를 받아 링크 대상 문자열(또는 링크 생략 시 `None`)을 반환할 수 있습니다.
- **`max_graphics`**: 페이지당 검사할 그래픽 요소(경로, 세그먼트, 클립, 이미지) 수를 제한합니다(기본값 65,536). 벡터가 매우 많은 CAD나 지도 페이지에서 작업이 멈추지 않고 `content_element_count` 코드의 `LimitError`를 발생시키며, 신뢰할 수 있는 입력에서는 `None`을 전달해 해제할 수 있습니다.

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

## 태그 지정 읽기 순서 {#tagged-reading-order}

태그가 지정된 PDF에서는 `doc.structure.reading_order()`가 여러 페이지에 걸쳐 구조 트리의 논리적 순서대로 텍스트를 추출합니다. 페이지의 마크드 콘텐츠 시퀀스를 읽고 `Artifact` 요소는 생략하며, 태그가 없는 문서에서는 빈 튜플 `()`을 반환합니다. 각 `StructureText`는 구조 요소 `element`, 콘텐츠 참조 `item`, 텍스트 `text`(대체 텍스트가 없는 Form XObject처럼 텍스트를 해석할 수 없는 경우 `None`)를 보관합니다. 요소의 `actual_text`와 `alt`(`Figure`, `Formula`, `Form` 대상)는 그려진 글리프를 대체하며, `expand_abbreviations=True`를 지정하면 `/E` 약어 확장을 사용합니다. `page.draw()`는 Form XObject 안에 그리므로, 그린 콘텐츠에 태그를 달아 논리적 텍스트로 추출하려면 `canvas.tag()`에 `actual_text`, `alt` 또는 `expanded`를 전달하세요. 구조 트리 작성 방법은 [구조 트리와 접근성](editing.md#structure-and-accessibility)을 참고하세요.

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

## 콘텐츠 항목

`page.content()`는 글리프 런, 경로, 이미지, 클립 등 페이지가 그리는 항목들을 이를 감싸는 마크드 콘텐츠와 함께 반환합니다. 추출된 각 `Color`는 sRGB의 `red`, `green`, `blue`, `alpha` 값과 함께 PDF에 저장된 `colorspace`(`"DeviceGray"`, `"DeviceRGB"`, `"DeviceCMYK"`, `"ICCBased"`, `"CalGray"`, `"CalRGB"`, `"Lab"`, `"Indexed"`, `"Separation"`, `"DeviceN"` 등) 및 원본 `components`를 유지합니다(동등 비교와 해시는 sRGB와 alpha만 비교합니다). 각 `TextRun`은 `writing_mode`(`WritingMode.HORIZONTAL` 또는 `WritingMode.VERTICAL`)를 노출하며, `FontRef`는 PDF 폰트 객체 식별 정보(`xref`, `subtype`, `name`, `is_embedded`)를 보고합니다. `page.operations`는 콘텐츠 스트림을 연산자 단위로 읽고 씁니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(len(page.content()), "items")
    print(page.operations[:3])
```

`page.paths()`는 같은 경로들을 `Paths` 컬럼 형태로 반환합니다. 경로 하나가 한 행에 해당하고 각 경로의 세그먼트도 별도의 컬럼에 담기며, 경로나 세그먼트마다 Python 객체를 만들지 않으므로 수십만 개의 선이 있는 도면도 페이지를 렌더링하는 것과 비슷한 시간 안에 읽을 수 있습니다. `column()`은 NumPy가 복사 없이 읽을 수 있는 읽기 전용 `memoryview`를 반환하며, pyarrow, polars, DuckDB는 Arrow PyCapsule 인터페이스를 통해 테이블 전체를 바로 가져올 수 있습니다. 개별 행은 필요할 때 `Path` 레코드로 생성됩니다. `paths.clusters()`(및 `page.content().path_clusters()`)는 설정 가능한 `x_tolerance`와 `y_tolerance` 안에서 표시 공간의 인접한 선과 채우기 경로를 불변 `PathCluster` 레코드(`bbox`, `path_indices`, `count`)로 묶으며, 기본적으로 페이지 전체 배경 사각형을 제외하고(`exclude_background=True`) `DocumentLimits`를 지키면서 `max_clusters`로 결과 수를 제한합니다.

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

`page.chars()`(및 `page.layout(chars=True).chars`) 역시 글리프마다 Python 객체를 만들지 않는 동일한 컬럼형 `Chars` 구조로 문자를 노출합니다. `chars.names`는 글리프 상자(`x0`, `y0`, `x1`, `y1`), 기준선 원점(`origin_x`, `origin_y`), UTF-8 텍스트 버퍼(`text_offsets`, `text_data`), 레이아웃 인덱스(`block`, `line`, `span`), `is_synthetic`, 콘텐츠 출처(`stream`, `operation`, 없을 때는 `0`) 컬럼 이름을 나열합니다. `chars.column(name)`은 NumPy가 복사 없이 읽는 읽기 전용 `memoryview`를 반환하고, `chars[start:stop]`은 복사 없이 슬라이스합니다. pyarrow, polars, DuckDB는 Arrow PyCapsule 인터페이스(`__arrow_c_schema__`와 `__arrow_c_array__`, 단일 UTF-8 `text` 필드와 nullable `stream`·`operation` 포함)로 테이블 전체를 Arrow 구조체 배열로 가져오며, 인덱싱이나 순회 시 필요에 따라 `Char` 레코드를 생성합니다.

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

## 이미지

`page.images()`는 페이지가 그리는 이미지를 `page.content()`와 같은 순서로 나열합니다. `pixmap()`은 렌더러와 같은 방식으로 소프트 마스크를 알파 채널로 디코딩하며, `stored()`는 문서에 저장된 JPEG, JPEG 2000, JBIG2 또는 CCITT 데이터를 그대로 반환합니다. `doc.replace_image(xref, data)`는 이미지를 JPEG, JPEG 2000 또는 PNG 데이터로 교체하고, `doc.remove_image(xref)`는 해당 이미지를 그리는 모든 페이지에서 이미지를 비웁니다.

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

## 오프라인 OCR {#offline-ocr}

`ocr` extra를 설치하면 `OcrEngine()`이 PP-OCR 모델을 불러오고, `page.ocr`이 인식된 단어 목록을 반환합니다. `page.apply_ocr`은 이 단어들을 보이지 않는 텍스트 레이어로 기록하여 `text()`와 `search()`로 찾을 수 있게 합니다.

두 메서드 모두 `mode` 옵션을 지원합니다.

- `"full"`: 기존 텍스트 유무와 관계없이 페이지 전체(또는 `clip` 영역)를 인식합니다(`page.ocr`의 기본값).
- `"skip_existing"`: 추출 가능한 텍스트가 이미 있는 페이지(또는 `clip` 영역)를 건너뜁니다(`page.apply_ocr`의 기본값).
- `"hybrid"`: 읽을 수 있는 네이티브 텍스트로 덮이지 않은 래스터 이미지 영역만 찾아내며(보이지 않는 텍스트, `/Artifact` 마크, 미세한 가장자리 겹침은 무시), `DocumentLimits` 범위 내에서 인접한 띠 이미지나 밀집된 장식 이미지를 병합해 해당 영역만 OCR로 인식한 뒤 기존 네이티브 단어와 중복되는 결과를 걸러냅니다.

```python
import pyropdf

engine = pyropdf.OcrEngine()
with pyropdf.open("input.pdf") as doc:
    words = doc.pages[0].ocr(engine=engine, dpi=150, mode="hybrid")
    print([word.text for word in words])
    # Or write only the missing raster text into a mixed page:
    doc.pages[0].apply_ocr(engine=engine, dpi=150, mode="hybrid")
```

모델을 불러오는 과정이 가장 오래 걸리므로 엔진은 한 번만 생성해 재사용하세요.
