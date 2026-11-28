---
title: 편집
description: 페이지, 주석, 링크, 폼, 개요, 첨부 파일을 편집합니다. 모든 변경은 새로운 버전이 됩니다.
---

# 편집

아래의 모든 편집 작업은 문서의 새로운 버전을 기록합니다. 관련된 편집을
`doc.transaction(name)`으로 묶으면 이력의 한 단계로 통합됩니다. 자세한 내용은
[버전과 저장](versions.md)을 참고하세요.

## 페이지 {#pages}

`doc.pages`는 `Page` 핸들의 시퀀스입니다. 핸들은 페이지가 이동하더라도 해당
페이지를 계속 가리키며, 페이지가 제거되면 `StalePageError`를 발생시킵니다.

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

다른 문서에서 가져온 페이지는 리소스, 주석, 폼 필드 및 구조 정보와 함께 임포트됩니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc, pyropdf.open("input.pdf") as other:
    doc.pages.insert(len(doc.pages), other.pages)
    doc.pages.select([0, 1])        # keep only these, in this order
```

`doc.pages.new_from_image(source)`는 이미지 프레임마다 이미지 해상도에 맞춘 크기
(또는 균일한 `size=(width, height)` 페이지 중앙에 맞춘 크기)의 페이지를 추가합니다.
JPEG나 JPEG 2000 스트림을 재압축하지 않으면서 Exif 방향(1–8)을 반영합니다. PNG는
직접 읽어 알파 채널을 소프트 마스크로 유지하며, TIFF, GIF 등 기타 형식은 Pillow가
설치되어 있으면 Pillow를 통해 읽습니다. 이미지 시퀀스나 CBZ(`.cbz` / ZIP) 아카이브를
실행 취소 가능한 하나의 트랜잭션으로 변환하려면(아카이브 항목을 자연 순으로 정렬하고,
`__MACOSX`와 메타데이터 파일을 무시하며, 모든 청크에 `DocumentLimits`를 적용함)
`doc.pages.new_from_images(sources)` 또는 `doc.pages.new_from_cbz(source)`를
사용하세요.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmaps = [page.pixmap(dpi=72) for page in doc.pages]
    scan = pyropdf.Document()
    scan.pages.new_from_image(pixmaps[0])            # one image (path, bytes, or Pixmap)
    scan.pages.new_from_images(pixmaps, size=(595, 842))  # a batch fitted to A4
    scan.save("scan.pdf")
```

`page.media_box`, `crop_box`, `bleed_box`, `trim_box`, `art_box`는 상속되어 MediaBox
범위로 잘린 페이지 박스를 텍스트 및 렌더링과 동일한 표시 공간으로 읽습니다. `(0, 0)`은
보이는 페이지의 왼쪽 위입니다. `Rect`를 할당하면 해당 페이지에만 박스를 설정하며,
MediaBox를 벗어나는 박스는 `ValueError`를 발생시킵니다. 렌더링과 텍스트 추출은 새
CropBox를 따릅니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.crop_box = (36, 36, page.rect.width - 36, page.rect.height - 36)
    page.pixmap(dpi=72).save("cropped.png")
```

`page.flatten()`은 주석과 위젯의 외관을 페이지 콘텐츠에 그려 넣고 페이지에서
제거합니다.

## 그리기 {#drawing}

`page.draw()`는 표시 공간에서 경로, 이미지, 다른 페이지 및 텍스트를 그리는 `Canvas`를
반환합니다. 기본적으로 페이지 콘텐츠 위에 그리며, `underlay=True`를 지정하면 아래에
그립니다. `with` 블록 전체가 하나의 트랜잭션이자 하나의 실행 취소 단계이며, 블록
안에서 예외가 발생하면 그린 내용이 모두 취소됩니다.

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

`canvas.image`는 JPEG, PNG 또는 JPEG 2000 데이터, 파일 경로, `Pixmap`, 문서 내
이미지를 받으며, `canvas.page`는 열려 있는 다른 문서의 페이지를 필요에 따라
클리핑하거나 회전해 배치합니다. 여러 번 그린 이미지나 페이지는 한 번만 저장됩니다.
`clip`, `clip_text`, `transform`, `opacity`, `blend`, `layer`, `tag`는 그 안에서
그리는 모든 요소에 적용되는 스코프입니다.

### 그라데이션

`LinearGradient`와 `RadialGradient`는 경로, 도형, 텍스트에서 `Color`를 쓸 수 있는
모든 위치(`fill` 또는 `Stroke`의 `paint`)에 사용할 수 있습니다. 그라데이션 좌표는
칠하는 대상과 같은 좌표 공간에 있으므로 `canvas.transform`과 함께 이동합니다.

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

`extend`는 그라데이션 구간 바깥을 어떻게 칠할지 결정합니다. 양 끝 색상을 이어가는
`PAD`(기본값), 시작 또는 끝 색상만 이어가는 `PAD_START` 및 `PAD_END`, 아무것도 칠하지
않는 `NONE`, 구간을 반복하는 `REPEAT`, 왕복 반사하는 `REFLECT`를 선택할 수 있습니다.
동일한 함수와 셰이딩은 하나의 캔버스 안에서 한 번만 기록됩니다. `page.content()`와
`page.paths()`는 축 방향 및 방사형 셰이딩을 색상 함수가 포함된 `ShadingPattern`으로,
타일링 패턴을 캡처된 패턴 셀이 포함된 `TilingPattern`으로 읽어 들이며, `canvas.draw`는
그 채우기와 획을 다시 그립니다(축 방향·방사형 셰이딩은 유계 샘플링 근사로, 타일링 패턴은
유계 `/PatternType 1` 스트림으로 출력하며 함수 및 메시 셰이딩은 다시 그리지 않습니다).
`canvas.draw`는 또한 인라인 래스터 이미지와 간접·인라인 스텐실 이미지 마스크를
`Image.fill` 페인트로 다시 그립니다. 소프트 마스크 때문에 텍스트의
채우기와 획을 별도 객체로 그려야 하는 경우, 두 번째 객체에는 빈 `ActualText`를 붙여
텍스트 추출 시 문자가 한 번만 읽히도록 합니다.

### 텍스트

`canvas.text`는 기준선(baseline) 지점에서 줄을 그리고, `canvas.textbox`는 사각형
영역 안에서 텍스트를 줄 바꿈합니다. 두 메서드 모두 `font`와 `size`를 지정하지 않으면
11포인트 Helvetica로 그립니다. `Font.standard`는 `"Times-Roman"`이나
`"Courier-Bold"`처럼 이름으로 표준 14개 폰트 중 하나를 반환하며, 임베드되지 않으므로
파일 크기가 작게 유지됩니다. 12개의 텍스트 폰트는 WinAnsi(대체로 Latin-1) 문자를
허용하고 그 외의 문자는 `PdfError`로 거부합니다. Symbol과 ZapfDingbats는 내장
인코딩을 사용하므로 WinAnsi 바이트가 선택하는 글리프가 입력 문자와 다를 수 있습니다.
다른 문자 체계에는 `Font.from_file`로 OpenType 폰트를 불러오면 문서에 한 번만
임베드됩니다. 가변 TrueType 폰트에서는 `weight=700`처럼 웨이트 축의 인스턴스를
선택할 수 있습니다. COLR로 칠하거나 `sbix` 또는 CBDT의 PNG 스트라이크를 보유한
글리프는 지정한 채우기 색상과 무관하게 고유 색상으로 그려지며 텍스트로 다시 읽힙니다.
그 밖의 컬러 글리프는 단색 대체 윤곽선으로 그려지며, 윤곽선이 없는 글리프를 그리려
하면 텍스트만 남고 아무것도 그려지지 않는 상황을 막기 위해 `PdfError`를 발생시킵니다.

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

텍스트 상자는 내용이 모두 들어가거나 아니면 아무것도 그리지 않습니다. 반환되는
`TextFit`은 내용이 들어맞았는지 여부(`fits`)를 알려주며, `spare_height`는 마지막 줄
아래에 남은 여백 또는 음수로 표현된 부족한 높이입니다. `Font.text_height`와
`Font.text_width`는 캔버스와 완전히 동일한 배치 방식으로 텍스트를 측정하므로 그리기
전에 상자 크기를 미리 계산할 수 있습니다. 줄 바꿈은 Unicode가 허용하는 위치에서
이루어지며, 상자보다 넓은 단어는 자소 클러스터(grapheme cluster) 경계에서 나뉩니다.
단 하나의 클러스터조차 너비에 들어가지 않으면 `PdfError`가 발생합니다. `align`에는
`"left"`, `"center"`, `"right"`, `"justify"`를 지정할 수 있고, `line_height`는 `size`의
배수로 줄 간격을 설정합니다. 텍스트는 기본적으로 검은색으로 채워집니다. `stroke`를
전달하면 외곽선을 그리고, `fill=None`으로 속을 비우거나,
`render_mode=TextRenderMode.INVISIBLE`(`3 Tr`)을 전달해 잉크를 그리지 않고 검색
가능한 보이지 않는 텍스트를 출력할 수 있습니다. 후속 경로 또는 이미지를 글리프
윤곽선으로 클리핑하려면(`4 Tr`–`7 Tr`) `with canvas.clip_text(...):` 스코프에
진입하세요(기본값은 클리핑 전용이며, `fill`이나 `stroke`를 전달하면 그리기와 클리핑을
함께 수행합니다). `rotate`는 직각 단위로, `canvas.transform`은 임의의 각도로 텍스트를
회전합니다.

임베드된 OpenType 폰트와 함께 `direction="vertical"`을 전달하면 `harfrust`로 세로쓰기
셰이핑을 수행하고 `/DW2`/`/W2` 세로 메트릭과 함께 `Identity-V`로 임베드합니다. 열은
위에서 아래로 흐르며 오른쪽에서 왼쪽으로 쌓입니다. 여러 문자 체계가 섞인 문자열에는
명시적인 `fallback_fonts` 시퀀스(또는 `doc.fallback_fonts`)를 전달해 전역 폰트 레지스트리
없이 폰트별로 누락된 자소 클러스터를 대체할 수 있으며, 실제 사용된 폰트만 한 번
임베드되고 저장 시 서브셋화됩니다.

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

텍스트는 기본적으로 호출당 UTF-8 기준 1 MiB 및 4,096줄로 제한되며, 이를 초과하면
아무것도 그리기 전에 `LimitError`로 거부됩니다. 신뢰할 수 있는 입력에는
`max_text_size=None`을 전달하세요. 자세한 내용은 [제한과 오류](limits.md)를 참고하세요.

PDF에 이미 그려진 텍스트를 복사하려면 추출한 텍스트 런(run)을 캔버스에 전달하세요.
`draw`를 호출하는 동안 원본 문서는 열려 있어야 합니다.

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

각 글리프는 원본 PDF 문자 코드, 텍스트 행렬 및 폰트 딕셔너리를 그대로 유지합니다.
캔버스는 런을 추출한 버전에서 대상 폰트를 복사하므로, 이후 원본의 인코딩이나 폰트
프로그램이 변경되더라도 그리기 결과에는 영향을 주지 않습니다. 같은 `content()` 결과에서
나온 폰트는 복사된 리소스를 공유합니다. 임베드된 단순 폰트, 표준 14개 폰트, 임베드된
Identity-H 및 Identity-V 폰트, 그리고 자체 리소스를 가진 Type 3 폰트를 복사할 수
있습니다. 사용할 수 없는 폰트, 직접 폰트 딕셔너리, 코드 너비를 알 수 없는 복합 인코딩,
특이(singular) 글리프 변환, 대체 텍스트로 감싸인 경우, 패턴 페인트 또는 소프트 마스크가
있는 런은 `ValueError`를 발생시킵니다. 원본 문서가 닫혀 있으면 `DocumentClosedError`가
발생하며, 캔버스가 문서나 원본의 제한을 초과하면 그리기 전체가 롤백됩니다.

15개 문서로 이루어진 왕복(round-trip) 코퍼스에서는 17,246개의 텍스트 런을 다시
그립니다. `mhlw-doc.pdf`의 8개 런은 임베드되지 않은 비표준 폰트를 사용합니다. 코퍼스
테스트는 나머지 페이지의 픽셀이 72 dpi에서 RGB 채널당 16레벨 이내인지 확인하며, 이러한
제외 건수와 사유가 변하지 않는지도 검증합니다.

### 텍스트 치환 {#replacing-text}

`page.replace_text(old, new, *, count=None, fallback=None)`은 페이지 콘텐츠와 페이지가
그리는 Form XObject 내부의 텍스트를 치환합니다. Type 1, TrueType, Type 3뿐만 아니라
가로쓰기(`WMode = 0`)와 세로쓰기(`WMode = 1`) 복합 폰트(Type 0 / CID: `Identity-H`,
`Identity-V`, 사전 정의 CMap, 임베드된 사용자 정의 CMap 스트림)를 모두 지원합니다.
진행 축 방향의 보정 `TJ` 조정을 통해 뒤따르는 텍스트의 위치를 그대로 유지하며, 감싸는
구조 태그(`/MCID`)를 보존하고 일치하는 `/ActualText` 마크드 콘텐츠 스팬도 글리프와 함께
갱신합니다.

서브셋화되지 않은 임베드 Type 0 글꼴에 필요한 글리프가 이미 들어 있으면 `replace_text`가
`/W`, `/W2`, `/ToUnicode` 항목을 자동으로 확장합니다. 서브셋화된 글꼴, Type 3 글꼴의
`/CharProcs`, 또는 글꼴 인코딩에 누락된 글리프가 있을 때는 `fallback=Font(...)`을 전달해
후속 텍스트 정렬을 유지하면서 대체 글꼴로 누락 문자를 그릴 수 있으며, `fallback`을 지정하지
않으면 `ValueError`가 발생하고 문서는 변경되지 않습니다.

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

## 주석

`page.annotations.add`는 사양 객체를 받아 `Annotation` 핸들을 반환합니다. 모든 주석은
외관 스트림과 함께 기록되므로 어떤 뷰어에서나 동일하게 표시됩니다.

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

텍스트 마크업(하이라이트, 밑줄, 물결선, 취소선), 메모, 자유 텍스트(`FreeText`), 스탬프
(`Stamp`), 캐럿(`Caret`), 선, 사각형, 원, 다각형, 꺾은선, 잉크, 사운드(`Sound`), 파일
첨부 주석을 지원하며, 자세한 옵션은 [API 레퍼런스](/api/annotations/)에 나열되어
있습니다.

## 링크

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.links.add((72, 72, 200, 90), "https://pyropdf.haya.works/")
    page.links.add((72, 100, 200, 118), pyropdf.Destination(0))
```

## 폼 {#forms}

`doc.form`은 전체 필드 이름을 `Field` 핸들에 매핑합니다. 값을 설정하면 필드가
갱신되고 해당 위젯의 외관도 즉시 갱신됩니다.

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

`doc.form.to_fdf()`와 `doc.form.to_xfdf()`는 필드 값을 FDF(ISO 32000-2 §12.7.8) 및
XFDF(ISO 19444-1) 형식으로 내보내고, `parse_fdf()`와 `parse_xfdf()`는 이를 `FdfData`로
검사하며, `doc.form.import_fdf()`와 `doc.form.import_xfdf()`는 일치하는 필드를 실행
취소 가능한 하나의 트랜잭션으로 적용합니다.

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

## 액션, 스크립트 및 트리거 {#actions-and-triggers}

링크, 개요 항목, 프레젠테이션 단계, `doc.open_action`, `page.triggers`,
`widget.triggers`, `Screen` 주석(`annotation.action` 및 `annotation.triggers`)은
`EmbeddedDestination`, `HideAction`, `SetLayerStateAction`, `JavaScriptAction`,
`SubmitFormAction`, `ResetFormAction`, `ImportDataAction`을 포함하는 `Target` 액션
유니온을 공유합니다. 문서 수준 스크립트는 `doc.scripts`(`/Names /JavaScript`)에,
카탈로그 수명 주기 스크립트는 `doc.triggers`(`Catalog["AA"]`)에 위치하며, pyropdf는
스크립트를 절대 실행하지 않습니다.

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

## 개요

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

## 기사 스레드 {#article-threads}

`doc.threads`는 문서의 기사 스레드(`/Threads`)를 `Thread` 핸들 시퀀스로 노출합니다.
각 스레드는 해당 페이지의 표시 좌표로 `rect`를 갖는 `Bead` 핸들의 순서 있는 고리
(`Beads`)와 `/I` 메타데이터 프로퍼티(`title`, `author`, `subject`, `keywords`,
`created`, `modified`)를 보관합니다. `ThreadAction`은 링크, 개요 항목 또는
프레젠테이션 내비게이션 단계에서 특정 스레드나 비드(bead)로 이동합니다.

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

## 구조 및 접근성 {#structure-and-accessibility}

`doc.structure`는 논리 구조 트리의 최상위 `StructureElement` 핸들을 노출하고, `walk()`는
모든 요소를 전위 순회(pre-order)로 방문하며, `doc.structure.role_map`은 `/RoleMap`을
읽고 편집합니다. 각 요소의 `role`, `title`, `language`(BCP 47 태그), `alt`,
`actual_text`, `expanded`(`/E` 약어 풀어쓰기), `page`는 모두 할당과 삭제가 가능하며,
`children`과 `move()`는 `/P`, `/K`, `/ParentTree`, `/IDTree`의 일관성을 유지하면서
요소의 순서를 바꾸거나 부모를 변경합니다.

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

## 첨부 파일 및 포터블 컬렉션 {#attachments}

`doc.attachments`는 내장 파일(`/Names /EmbeddedFiles`)을 관리하고, 각 첨부 파일의
`collection_item`은 `/CI` `CollectionItem` 값(`CollectionSubitem` 표시 접두사 포함)을
읽고 쓰며, `doc.collection`은 포터블 컬렉션의 스키마, 정렬 순서, 색상, 분할 창,
사용자 정의 `Navigator` 및 `CollectionFolder` 계층 구조를 구성합니다.

각 내장 파일은 관련 파일(related files)도 지정할 수 있습니다. `/EF` 딕셔너리 아래에
존재하는 파일명 키(`F` 또는 `UF`)를 사용해 매핑을 `att.related_files`에 할당하세요.
각 시퀀스는 순서를 유지하며, 여러 이름이 하나의 스트림을 공유할 수 있습니다. 동일한
프로퍼티를 `annotation.file`에서도 사용할 수 있습니다.

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

`doc.web_capture.by_url(url)`과 `by_id(source_digest)`는 원본에 대해 기록된 페이지
세트와 이미지 세트를 쓰기 가능한 Arlington 뷰로 반환합니다. 페이지 세트의 `O` 항목은
페이지를, 이미지 세트의 `O` 항목은 이미지 스트림을 식별하며, `SI`에는 URL 별칭과
저장된 명령을 포함한 원본 정보가 들어 있습니다. 이러한 레코드 읽기는 오프라인에서
동작합니다.

원본 위치를 변경하려면 `typed.SourceInformation` 뷰 또는 뷰 튜플을 전달해
`doc.web_capture.set_sources(content, source)`를 호출하세요. 이 메서드는 URL 색인을
갱신하면서 기존 URL이나 새 URL을 공유하는 다른 세트를 보존합니다. `remove(content)`는
색인에서 해당 세트의 연결을 해제하고 구성원의 부모 식별자를 지웁니다. 세트에 속한
페이지나 세트의 이미지를 사용하는 페이지를 삭제하거나 복사하기 전에 관련 세트를 먼저
제거하세요. 페이지 순서를 변경해도 세트 소속은 유지됩니다.

PDF 1.3–1.7에서 간접 저장된 `typed.WebCapturePageSet` 또는
`typed.WebCaptureImageSet`을 등록하려면 `doc.web_capture.add(content)`를 호출하세요.
16바이트 원본 다이제스트, 구성원 참조, 원본 딕셔너리, 그리고 이미지의 경우 페이지 참조
횟수를 제공합니다. PDF 2.0에서는 새 Web Capture 레코드 생성이 지원 중단되었지만,
기존 레코드는 계속 읽고 편집하고 제거할 수 있습니다.

## 페이지 레이블 및 명명된 페이지 {#page-labels}

`doc.page_labels`는 문서의 `PageLabelRange` 레코드를 나열하고, `page.label`은 한
페이지의 레이블을 반환합니다. 리스트를 할당하면 기존 레이블이 대체되며 첫 번째 범위는
페이지 0부터 시작합니다. 페이지를 삽입, 이동, 삭제해도 각 페이지의 레이블은 유지됩니다.
`doc.named_pages`는 `/Names /Pages`의 문자열 키를 라이브 `Page` 핸들에 매핑하며,
페이지가 제거되면 해당 항목을 자동으로 정리합니다.

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

## 프레젠테이션 및 썸네일

`page.duration`은 프레젠테이션 중 뷰어가 다음 페이지로 넘어가기 전에 현재 페이지를
표시할 시간(초)을 설정하고, `page.transition`은 `Transition` 전환 효과를,
`page.navigation_steps`는 페이지 내 하위 단계인 `NavigationStep` 시퀀스(`/PresSteps`)를
나열합니다. `TransitionAction`은 내비게이션 단계, 링크 또는 개요 항목에서 전환 효과를
실행합니다. `page.thumbnail`은 페이지에 임베드된 `/Thumb` 미리 보기를 `Pixmap`으로
디코딩하거나 교체하며, `del`로 이 속성들을 모두 제거할 수 있습니다.

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

## 레이어

`doc.layers`는 선택적 콘텐츠 그룹(OCG)을 이름과 기본 구성에서의 표시 여부가 담긴
`Layer` 레코드로 나열하며, `doc.layers.configs`는 구성 목록을 반환합니다. `switch`는
그룹을 표시하거나 숨기고 `use`는 구성을 적용하며, 렌더링과 텍스트 추출은 두 설정을 모두
따릅니다. `visible`은 가시성 표현식(visibility expression)을 포함해 그룹이나 멤버십
딕셔너리의 가시성을 평가합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for layer in doc.layers:
        print(layer.name, layer.on)
        if layer.name == "Draft":
            doc.layers.switch(layer.xref, on=False)
    print(doc.pages[0].text())
```

## 메타데이터

`doc.metadata`는 Info 딕셔너리의 라이브 매핑입니다. 문서에 XMP 패킷이 있으면 각 편집이
같은 트랜잭션 안에서 대응하는 XMP 프로퍼티도 함께 갱신하므로, PDF/A 문서에서 두
사본의 일관성이 유지됩니다.

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

카탈로그의 뷰어 및 접근성 프로퍼티는 `doc`의 할당 및 삭제 가능한 속성으로 제공됩니다:
`doc.page_mode`, `doc.page_layout`, `doc.language`(BCP 47 태그), `doc.mark_info`,
`doc.viewer_preferences`.

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

## 서명 구조 및 암호화 래퍼 {#signature-structures}

[서명](versions.md#signing) 자체에 더해, `doc.sign`과 `doc.prepare_signature`는
DocMDP 인증 권한(`DocMdpPermission`, `doc.certification` 및 `sig.doc_mdp`로 조회)과
FieldMDP 필드 잠금(`FieldLock` 및 `FieldMdpAction`, `field.lock` 및
`sig.field_locks`로 조회)을 구성합니다. `doc.security_store`는 문서 보안 저장소
(`/DSS` 및 `vri_key`로 색인된 `/VRI`)를 중복 제거된 간접 스트림의 `SecurityStore`로
읽고 쓰며 증분 저장 시 이전 서명을 무효화하지 않습니다. `doc.timestamp(timestamper)`는
RFC 3161 `/DocTimeStamp`(`sig.is_timestamp`)를 덧붙이고, `doc.legal_attestation`은
`Catalog["Legal"]`(`LegalAttestation`)을 편집합니다. 독자적인 암호화 핸들러를 위해
`wrap_encrypted_payload`는 PDF 2.0 비암호화 래퍼 문서(ISO 32000-2 §7.6.7)를 생성하며,
`doc.encrypted_payload`가 이를 `EncryptedPayload`로 다시 읽습니다. 이 함수로 생성한
래퍼에서는 파일 사양이 표시 파일명을 보관하고 `doc.collection.initial_document`가
`doc.attachments`용 불투명 키를 보관합니다. 래퍼에 다른 파일이 함께 들어 있는 경우
선택된 페이로드를 편집할 때 이 키를 사용하세요.

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

DSS에는 PDF 2.0이 필요합니다. 이전 버전의 문서에 DSS를 기록할 때 구조적 결함이
발생하게 되면 세터는 `PdfError`를 발생시키고 원래 리비전을 보존합니다. 이 예제는 새
문서에 샘플 서명 데이터를 사용합니다. 기존 서명의 경우 해당 CMS 콘텐츠를 `vri_key`에
전달하고 서명된 리비전을 보존하도록 증분 저장하세요.

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

## 뷰포트, 지리 공간 측정 및 문서 요구 사항 {#viewports-and-requirements}

`page.viewports`는 `/CropBox`, `/Rotate`, `/UserUnit`이 반영된 표시 좌표로 페이지의
측정 뷰포트(`/VP`)를 읽고 설정합니다. CAD 축척 체인용 `RectilinearMeasure`
(`NumberFormat`, `NumberFormatFraction`)와 EPSG 또는 WKT 좌표계용 `GeospatialMeasure`
(`CoordinateSystem`, `PointData`)를 지원합니다. 수치 좌표와 변환 계수는
`decimal.Decimal`을 사용하므로 고정밀 위도/경도 값이 `float` 반올림 없이 정확한 자릿수
그대로 PDF 객체 저장소에 기록됩니다. `doc.requirements`는 `Catalog["Requirements"]`
(`DocumentRequirement`, `RequirementType`, `RequirementHandler`)를 관리하며,
`del page.viewports`나 `del doc.requirements`는 하나의 트랜잭션에서 항목을 제거하고
참조되지 않는 객체를 해제합니다.

뷰포트 딕셔너리, 이미지 또는 폼 XObject 스트림, 3D 또는 RichMedia 주석의 `obj`에서
`owner.geospatial_measure`를 읽거나 할당할 수 있습니다. 2D 정합(registration)은
정규화된 각 로컬 지점을 하나의 지상 지점과 짝짓습니다. 3D 주석에서는
`RequirementType.GEOSPATIAL_3D`를 선언하고 세계 좌표와 고도에 `dimensions=3`을
사용하세요. `owner.geospatial_point(index)`는 소유자의 PDF 좌표 공간에서의 로컬
지점과 그 지상 좌표를 반환합니다. 지리 좌표는 위도, 경도 순서이며 투영 좌표는 동향
(easting), 북향(northing) 순서이고, 선택적 투영 행렬이 지상 좌표 배열보다 우선합니다.
Adobe 확장 레벨 3 이상을 선언한 PDF 1.7 문서는 버전을 바꾸지 않고도 2D 지리 제어점을
저장할 수 있습니다. PDF 2.0이 필요한데 업그레이드 시 구조적 결함이 생기게 된다면
하나의 트랜잭션으로 거부됩니다. 형식이 잘못된 정합 정보도 원시 PDF 딕셔너리를 통해
계속 검사할 수 있습니다. `Decimal` 입력은 저장 시 정확한 자릿수를 그대로 유지합니다.
PDF에서 읽은 숫자 토큰의 정밀도는 현재 하위 리더를 따릅니다
([파서 추적 이슈 #16](https://github.com/yhay81/pyropdf/issues/16) 참고).

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

## 문서 교환 및 구조 네임스페이스 {#interchange-and-structure-namespaces}

`doc.extensions`는 직접 개발자 및 ISO 확장(`DeveloperExtension`, `ISO_EXTENSIONS`)을
관리하고, `associated_files`는 `AssociatedFileRelationship`을 지정해 문서, 페이지,
구조 요소, 주석 및 문서 파트에 파일을 연관시키며, `piece_info`는 카탈로그와 페이지에
`/PieceInfo` 애플리케이션 딕셔너리를 저장합니다. `doc.parts`는 `DocumentPartAction`이
가리킬 수 있는 `/DPartRoot` 페이지 범위 트리를 구성하며, `doc.structure.namespaces`는
하나의 구조 트리 안에 PDF 1.7(`PDF_1_7_NAMESPACE`), PDF 2.0(`PDF_2_0_NAMESPACE`) 및
사용자 정의 네임스페이스를 함께 사용할 수 있게 합니다.

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

## 새니타이즈 {#sanitizing}

`doc.sanitize(path)`는 JavaScript, 액션, 첨부 파일, 메타데이터, 댓글, 숨겨진 텍스트 및
미사용 객체를 각 항목별 스위치에 따라 제거하고 문서를 전체 저장하므로, 결과 파일은
하나의 리비전만 갖게 됩니다. 반환되는 `SanitizeReport`는 종류별 제거 건수를 집계하고,
결과 문서가 무효화하거나 더 이상 선언하지 않는 서명 및 프로파일 주장을 나열합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    report = doc.sanitize("clean.pdf", comments=False)
    print(dict(report.removed), report.changes.signatures, report.dropped_claims)
```

## 폰트 서브셋화

`doc.subset_fonts()`는 모든 선택적 콘텐츠 그룹을 켠 상태에서 페이지가 실제로 그리는
글리프만 남도록 임베드된 각 TrueType, OpenType, CFF 폰트 프로그램을 하나의
트랜잭션으로 축소합니다. 글리프 ID는 그대로 두고 사용하지 않는 윤곽선만 비우므로
텍스트 추출과 문자 폭이 변하지 않습니다. 주석 외관이나 폼의 기본 리소스에서 참조하는
프로그램은 온전히 유지됩니다. 반환되는 각 `SubsetFont`는 크기가 줄어든 프로그램을
보고합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    for font in doc.subset_fonts():
        print(font.name, font.kept, font.glyphs, font.size_before, font.size_after)
    doc.save("smaller.pdf")
```

## 객체

`doc.objects`는 객체 번호를 간접 객체에 매핑하며, 모든 값은 타입이 지정된 Python 객체
(`Dictionary`, `Array`, `Name`, `Stream` 등)로서 편집 시 새 버전으로 기록됩니다.
`doc.check()`는 문서가 Arlington PDF 모델에서 벗어난 부분을 보고합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    catalog = doc.catalog
    print(catalog["Type"], doc.pages[0].obj["MediaBox"])
    print(doc.check().defects)
```

## 인쇄 제작 데이터 {#print-production-data}

`page.box_styles`는 CropBox, BleedBox, TrimBox, ArtBox에 저장된 가이드라인 설정을
나타냅니다. 생략된 가이드는 리더의 설정을 따르며, 가이드 적용 여부는 페이지 박스
자체가 결정합니다. `page.separations`는 분판(pre-separated)된 시트의 실제 페이지
핸들을 판 순서대로 나열합니다.

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

구성원이나 순서를 바꾸려면 그룹을 다시 할당하세요. `cyan.separations`를 삭제하면 두
페이지 모두에서 그룹이 지워집니다. 판을 이동해도 정체성은 유지됩니다. 그룹의 일부를
제거하거나 임포트하거나 개별 판을 복제하려면 먼저 그룹에서 분리해야 합니다.

`PrinterMark`와 `Watermark`는 같은 문서에 이미 존재하는 Form XObject를 받습니다. 주석의
`print_appearance`는 선택된 기본 외관과 그 리소스를 해석합니다. 워터마크의
`fixed_print`는 인쇄 매체 기준의 행렬과 이동 비율을 저장합니다. 레거시 트랩
네트워크에서는 선택된 Form이 프로세스 컬러 모델, 분판 이름 및 트랩 영역을 노출하며,
주석 딕셔너리는 원래의 트래핑 스냅샷을 유지합니다. 이미지나 Form의 `opi`는 기존
프록시 파일 사양과 버전별 매개변수를 노출합니다. 기존 레거시 데이터를 편집할 때 이
뷰들을 사용하세요.

저장된 매개변수는 의도된 출력을 기술합니다. 장치 분판, 트랩 생성, OPI 대체 및 물리적
인쇄기 배치는 렌더링 엔진의 영역입니다([렌더링 작업](https://github.com/yhay81/pyropdf/issues/248)
및 [엔진 트래커](https://github.com/yhay81/pyropdf/issues/7) 참고).
