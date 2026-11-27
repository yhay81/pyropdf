---
title: 레시피
description: 자주 쓰는 PDF 작업을 짧고 완성된 프로그램으로 살펴봅니다 — 병합, 분할, 썸네일 렌더링, 이미지와 텍스트 추출, 스탬프, 워터마크, 폼 입력과 평탄화, 가림 처리 표시, 새니타이즈, 비밀번호 보호.
---

# 레시피

각 레시피는 그대로 실행할 수 있는 완성된 프로그램입니다. `input.pdf`를 사용하려는 파일로 바꾸세요.

## 문서 병합하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.save("merged.pdf")
```

가져온 페이지는 리소스, 주석, 폼 필드 및 구조 정보를 함께 가져옵니다.

## 페이지별로 한 파일씩 분할하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        with pyropdf.Document() as part:
            part.pages.insert(0, page)
            part.save(f"page-{number}.pdf")
```

## 페이지 선택, 순서 변경, 회전 또는 삭제하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages.select([1, 0])    # keep these pages, in this order
    doc.pages[0].rotation = 90  # turn the new first page clockwise
    del doc.pages[-1]           # delete the last one
    doc.save("reordered.pdf")
```

## 이미지나 CBZ 아카이브를 PDF로 변환하기

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

`doc.pages.new_from_cbz`는 아카이브 항목을 자연스러운 파일명 순서(`page2.png`가
`page10.png`보다 먼저 오도록)로 읽고, `__MACOSX`와 메타데이터 파일을 건너뛰며,
Exif 방향을 반영하고, JPEG 및 JPEG 2000을 재압축 없이 임베드합니다. 또한 파일 크기,
항목별 및 전체 디코딩 바이트, 압축 해제 비율, 프레임 수, 픽셀 수에 대한
`DocumentLimits`를 실행 취소 가능한 하나의 원자적 트랜잭션으로 강제합니다. 순서가
정해진 이미지 경로, 바이트 또는 `Pixmap` 컬렉션에는 `doc.pages.new_from_images(paths)`를
사용하고, 모든 페이지를 동일한 크기에 맞추려면 `size=(595, 842)`를 전달하세요.

## 모든 페이지를 PNG로 렌더링하기

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, data in enumerate(doc.render(dpi=150, format="png"), start=1):
        Path(f"page-{number}.png").write_bytes(data)
```

`doc.render`는 병렬로 렌더링합니다. 썸네일을 만들려면 같은 호출에 36처럼 낮은 `dpi`를
전달하면 됩니다.

## 썸네일 렌더링 및 페이지 이미지 추출하기

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

`doc.render(dpi=36, format="png")`는 저해상도 썸네일을 병렬로 렌더링합니다(또는
`fit=256`을 전달해 긴 변의 픽셀 상한을 지정할 수 있습니다). `page.images()`는
페이지에 그려진 모든 임베디드 이미지를 나열하며, `stored()`는 JPEG, JPEG 2000, JBIG2,
CCITT 바이트를 재인코딩 없이 그대로 반환하고, 그 외의 이미지는 `pixmap()`이 소프트
마스크를 알파 채널로 포함해 디코딩하여 PNG로 저장할 수 있습니다. PDF 자체에 `/Thumb`
미리 보기를 포함하려면 `page.thumbnail = page.pixmap(fit=96)`을 할당하세요.

## 텍스트 또는 Markdown 추출하기

```python
from pathlib import Path

import pyropdf

with pyropdf.open("input.pdf") as doc:
    Path("input.txt").write_text(doc.text(), encoding="utf-8")
    Path("input.md").write_text(doc.markdown(), encoding="utf-8")
```

Markdown은 제목과 표를 보존하므로 검색 색인과 언어 모델에 적합합니다.

## 특정 단어가 나타나는 모든 곳 강조 표시하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    with doc.transaction("highlight"):
        for page in doc.pages:
            if hits := page.search("total"):
                page.annotations.add(pyropdf.Highlight(hits))
    doc.save("highlighted.pdf", incremental=True)
```

## 페이지 및 작성자별로 검토 댓글 나열하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for number, page in enumerate(doc.pages, start=1):
        for annotation in page.annotations:
            if comment := (annotation.contents or "").strip():
                author = (annotation.author or "").strip() or "Unknown"
                print(f"Page {number} ({author}): {comment}")
```

`page.annotations`는 메모, 하이라이트 등 마크업 주석을 포함하며 링크, 폼 위젯,
팝업은 제외합니다.

## 스탬프와 대각선 워터마크 추가하기

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

`Stamp`는 고유한 외관 스트림을 갖춘 표준 고무 스탬프 주석을 추가하고, `page.draw()`는
모든 페이지의 표시 공간에 반투명 대각선 워터마크를 직접 그립니다(페이지 본문 아래에
그리려면 `underlay=True`를 전달하세요). 두 작업을 `doc.transaction`으로 묶으면 전체
편집이 하나의 실행 취소 단계로 유지됩니다.

## 딕셔너리에서 폼 채우기

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

`doc.form[name].value`를 설정하면 각 위젯의 외관이 다시 생성됩니다.
`incremental=True`로 저장하면 원본 바이트를 보존하면서 필드를 편집 가능한 상태로
유지하고, `doc.flatten()`을 호출하면 입력된 외관을 페이지 콘텐츠에 그려 넣고 대화형
필드를 제거합니다.

## 텍스트가 없는 페이지 제거하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    blank = [index for index, page in enumerate(doc.pages) if not page.text().strip()]
    with doc.transaction("remove blank pages"):
        for index in reversed(blank):
            del doc.pages[index]
    doc.save("without-blanks.pdf")
```

## 각 페이지에 책갈피 추가하기

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

## 파일 크기 줄이기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.subset_fonts()
    doc.save("smaller.pdf", compress=True, object_streams=True, prune=True, dedupe=True)
```

`subset_fonts`는 페이지가 실제로 그리는 글리프만 남기고, `prune`은 도달할 수 없는
객체를 제거하며, `dedupe`는 동일한 객체를 한 번만 기록합니다.

## 공유하기 전에 문서 정리하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("shared.pdf")
    print(dict(report.removed))
```

`sanitize`는 JavaScript, 액션, 첨부 파일, 메타데이터, 댓글, 숨겨진 텍스트 및 미사용
객체를 제거하며, 각 항목마다 개별 스위치를 제공합니다.

## 가림 처리 표시 및 새니타이즈하기

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

`comments=False`를 지정하면 `/Redact` 검토 표시를 유지한 채 `sanitize`가 한 번의
전체 저장으로 JavaScript, 액션, 첨부 파일, 메타데이터, 숨겨진 텍스트 및 미사용
객체를 제거합니다. `RedactionMark`는 하부 페이지 콘텐츠를 삭제하지 않고 검토용
영역만 표시하며, 콘텐츠를 완전히 제거하는 영구 가림 처리는
[#362](https://github.com/yhay81/pyropdf/issues/362)에서 추적하고 있습니다.

## 비밀번호로 보호하기

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save("locked.pdf", encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"))
```

## 작업량을 제한하여 문서 열기

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

제한 설정은 각 작업의 작업량을 한정합니다. 제한으로 보장되지 않는 범위는
[상태 및 안전성](status.md)을 참고하세요.

## 접근성 있는 장기 보존용 차트 만들기

[차트 프로그램 다운로드](https://raw.githubusercontent.com/yhay81/pyropdf/main/docs/includes/accessible-archive.py).
이 프로그램은 빈 문서에서 시작해 태그가 지정된 1페이지짜리 차트를 생성합니다.
텍스트를 포함하는 임베드 가능 폰트와 RGB 그리기 색상에 대응하는 ICC 프로파일을
전달하세요. 이 저장소에 포함된 소형 폰트와 RGB 프로파일로 두 가지 판본을 모두
실행하려면 다음과 같이 합니다.

```sh
uv run python -c "import runpy; from pathlib import Path; Path('chart.icc').write_bytes(runpy.run_path('tests/assets/pdfa/generate.py')['icc']())"
FONT=crates/pyropdf-engine/src/fonts/testdata/NotoSansJP-subset.otf
uv run python docs/includes/accessible-archive.py "$FONT" chart.icc chart.pdf
uv run python docs/includes/accessible-archive.py "$FONT" chart.icc earlier.pdf --earlier
```

| 출력 파일 | 장기 보존 및 접근성 프로파일 | 작성기가 제공하는 내용 |
| --- | --- | --- |
| `chart.pdf` | PDF/A-4 및 PDF/UA-2 | PDF 2.0 헤더, XMP의 2020년 및 2024년 개정 연도, 문서와 세 자식 요소의 표준 PDF 2.0 네임스페이스 |
| `earlier.pdf` | PDF/A-2a 및 PDF/UA-1 | PDF 1.7 헤더, 적합성 수준 A, XMP의 PDF/UA 식별 확장 스키마, 암묵적 PDF 1.7 구조 네임스페이스 |

두 판본 모두 폰트와 RGB 출력 프로파일을 임베드하고, 콘텐츠 언어를 미국 영어로
지정하며, 뷰어가 XMP 제목을 표시하도록 요청합니다. 접근성을 검토하려면 페이지를
열어 Poppler의 읽기 순서 보고서와 비교해 보세요.

```sh
pdfinfo -struct-text chart.pdf
pdftoppm -png -singlefile -scale-to 520 chart.pdf chart
```

태그는 순서대로 제목 "Air flow", 문단 "Air flow fell to 1", 그리고 그림(figure)으로
구성됩니다. 이미지에서 위쪽 막대는 2, 아래쪽 막대는 1로 표시되며 아래쪽 막대의
길이는 위쪽의 절반입니다. 그림의 대체 설명이 두 시행 사이의 이러한 변화를 동일하게
전달하는지 확인하세요. 기계적으로 검증할 수 있는 규칙은 각 프로파일을 명시해
검사합니다.

```sh
verapdf --flavour 4 chart.pdf
verapdf --flavour ua2 chart.pdf
verapdf --flavour 2a earlier.pdf
verapdf --flavour ua1 earlier.pdf
```

테스트에서는 그림 설명을 편집한 뒤 전체 저장과 증분 저장을 모두 검사합니다. 설명을
삭제하면 PDF/UA-2 규칙 `8.2.5.28.2-1`과 PDF/UA-1 규칙 `7.3-1`에서 실패하지만, 두
장기 보존(PDF/A) 검사는 여전히 통과합니다.
