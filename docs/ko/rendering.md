---
title: 렌더링
description: hayro를 통해 페이지를 픽스맵, PNG, JPEG, SVG로 한 페이지씩 또는 병렬 배치로 렌더링합니다.
---

# 렌더링

pyropdf는 [hayro](https://github.com/LaurenzV/hayro)를 사용해 객체 저장소의 현재 버전을 직접 읽어 렌더링합니다.

## Pixmap

`page.pixmap()`은 불변 8비트 픽셀 객체인 `Pixmap`을 반환합니다. RGB 또는 그레이스케일과 알파 채널 포함 여부를 선택할 수 있으며, `dpi`로 페이지 배율을 지정합니다(기본값 72).

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=144, alpha=True)
    print(pixmap.width, pixmap.height, pixmap.channels)
    pixmap.save("page-1.png")
    jpeg = doc.pages[0].pixmap(dpi=144).to_bytes("jpeg", quality=85)  # JPEG has no alpha
```

픽스맵은 버퍼 프로토콜을 지원하므로 NumPy와 Pillow가 복사 없이 픽셀을 읽을 수 있습니다. `numpy.asarray(pixmap)`의 형태는 `(height, width, channels)`입니다.

## 옵션

| 옵션 | 효과 |
| --- | --- |
| `dpi` | 페이지를 균일하게 확대·축소합니다. 기본값 72는 1포인트당 1픽셀입니다. |
| `matrix` | 90도 회전을 위한 `Matrix(90)` 같은 `Matrix`, 또는 페이지를 확대·회전·기울이는 숫자 4개나 6개. 래스터는 변환된 페이지를 덮도록 생성되므로 평행 이동 성분은 무시됩니다. |
| `fit` | 픽셀 단위의 양의 정수 또는 `(width, height)` 쌍. 페이지(또는 지정된 `clip`)를 해당 범위 안에 맞도록 균일하게 확대·축소하며, `dpi` 및 `matrix`와 함께 사용할 수 없습니다. |
| `clip` | 표시 좌표 사각형 `(x0, y0, x1, y1)`. 이 영역의 픽셀만 래스터화합니다. |
| `alpha` | 페이지를 흰색 배경에 칠하지 않고 투명도를 유지합니다. |
| `background` | 페이지 아래에 칠할 0~255 범위의 RGB 또는 RGBA 튜플. `alpha`가 없으면 불투명해야 합니다. |
| `annotations` | `False`로 설정하면 주석과 폼 위젯 외형을 제외합니다. |
| `colorspace` | `"gray"`로 설정하면 ITU-R BT.601 가중치로 그레이스케일로 변환합니다. |
| `hinting` | `True`로 설정하면 FreeType의 light hinting처럼 텍스트에 힌팅을 적용해 작은 글자를 선명하게 만듭니다. 기본값은 힌팅을 적용하지 않습니다. |
| `glyph_cache` | `True`로 설정하면 크기별로 각 글리프를 한 번만 래스터화해 재사용하므로 텍스트 렌더링이 빨라지지만, 세로 방향으로 최대 0.5픽셀 어긋날 수 있습니다. 캐시 메모리는 `DocumentLimits` 바깥에 있으며 해당 문서 버전에 대한 스레드 렌더링 캐시와 수명을 함께합니다. 기본값은 비활성입니다. |
| `antialias` | `False`로 설정하면 hayro의 앨리어싱 임계값을 통해 채우기, 선, 텍스트의 안티앨리어싱을 꺼서 윤곽이 또렷한 벡터·텍스트 출력을 만듭니다. 래스터 이미지는 정상적으로 그려지며 `glyph_cache`는 우회됩니다. 기본값은 `True`입니다. |

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

`clip`은 `page.rect`나 `words()`, `search()`가 반환하는 경계 상자와 같은 표시 좌표를 사용합니다. [핵심 개념](concepts.md#display-coordinates)을 참고하세요.

래스터는 너비 최대 65,408픽셀, 높이 최대 65,532픽셀, 총 64,000,000픽셀까지 허용되며 이를 초과하면 `ValueError`가 발생합니다. `clip`을 지정하면 이 제한이 잘라낸 래스터에 적용되므로, 전체 페이지로는 렌더링할 수 없는 고배율에서도 작은 영역을 렌더링할 수 있습니다. 예를 들어 A1 페이지의 100 × 100포인트 모서리는 20배 확대해도 2,000 × 2,000픽셀로 렌더링할 수 있는 반면, 전체 페이지는 16억 픽셀이 필요합니다. 클립 렌더링 시 변환된 페이지의 어느 한 변이 524,288픽셀을 초과하면 `clip_page_side` 코드의 `LimitError`가 발생합니다. 이 한도 내에서는 페이지 반대편에서 클립 영역까지 뻗어 들어오는 도형도 원래 위치의 1/16픽셀 오차 안으로 정확히 배치됩니다.

## 인코딩

`pixmap.save(path)`는 파일 확장자에 해당하는 형식으로 저장하고, `pixmap.to_bytes("png")`나 `to_bytes("jpeg", quality=...)`는 인코딩된 바이트를 반환합니다. 두 메서드 모두 PNG와 JPEG 외에 PAM, PBM, PGM, PPM, PNM, PSD, PostScript를 지원합니다. 알파 채널을 보존하는 형식은 PNG, PAM, PSD뿐이며 나머지 형식은 알파 채널을 거부하므로 불투명 픽스맵으로 인코딩해야 합니다.

## 픽셀 연산

모든 연산은 새 픽스맵을 반환합니다: `invert(rect=None)`, `gamma(value)`, `tint(black, white)`, `to_gray()`, `to_rgb()`, `with_alpha()`, `without_alpha(background)`, `crop(rect)`, `scale(width, height)`, `rotate(degrees)`, `flip(axis)`, `warp(quad, width, height)`, `fill(value, rect=None)`, `with_pixel(x, y, value)`. 픽셀 조회에는 `pixel(x, y)`, `colors(rect=None)`, `color_count(rect=None)`, `top_color(rect=None)`을 사용합니다. 사각형 좌표는 정수 픽셀 단위입니다. `Pixmap.from_bytes(data)`와 `Pixmap.open(path)`는 PNG, JPEG, PBM, PGM, PPM, PAM, BMP, GIF, TIFF, JPEG 2000을 읽으며, Pillow가 설치되어 있으면 `to_pil()`로 Pillow 이미지를 얻을 수 있습니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pixmap = doc.pages[0].pixmap(dpi=72)
    thumbnail = pixmap.to_gray().scale(pixmap.width // 4, pixmap.height // 4)
    thumbnail.invert((0, 0, 10, 10)).save("thumbnail.png")
```

## 일괄 렌더링

`doc.render`는 GIL을 해제한 상태로 최대 `workers`개의 스레드에서 여러 페이지를 병렬 렌더링하고, `pages` 순서대로 인코딩된 이미지를 반환합니다. `clip`을 제외한 `page.pixmap`의 모든 옵션을 받으며, `fit`은 각 페이지의 자체 표시 크기를 기준으로 배율을 계산하므로 크기나 `/UserUnit`이 서로 다른 페이지가 섞인 문서도 한 번의 배치로 동일한 픽셀 상한 안에 맞출 수 있습니다. 다른 스레드가 문서를 편집하고 있더라도 모든 페이지는 호출 시작 시점의 버전에서 렌더링됩니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    images = doc.render(fit=2048, format="png", workers=4)
    print(len(images), "pages rendered")
    first_only = doc.render([0], dpi=150, format="jpeg", quality=85)
```

`max_size`는 전체 배치의 인코딩된 바이트 크기를 제한하며 기본값은 512 MiB입니다.

## SVG

`page.svg()`는 페이지를 72 dpi의 SVG 문자열로 반환합니다. 텍스트는 경로(path)로 변환되므로 모든 브라우저에서 동일하게 보이지만 검색은 할 수 없습니다. `annotations=False`로 주석을 제외할 수 있으며, `max_size`가 문자열 크기를 제한합니다(기본값 64 MiB).

```python
import pyropdf
from pathlib import Path

with pyropdf.open("input.pdf") as doc:
    Path("page-1.svg").write_text(doc.pages[0].svg(), encoding="utf-8")
```

## 문서에 임베드되지 않은 글꼴 {#fonts-that-a-document-does-not-embed}

일본어, 중국어, 한국어 문서에서 MS-Gothic, MS-Mincho, SimSun, Batang 등을 쓰는 경우처럼, PDF는 글꼴 프로그램을 임베드하지 않고 이름만 지정할 수 있습니다. hayro는 이런 글꼴을 Standard 14 글꼴로 그리는데, 여기에는 CJK 글리프가 없으므로 `page.text()`로는 텍스트를 읽을 수 있어도 렌더링 결과에서는 빈칸으로 나타납니다. 글꼴 extra를 설치하면 이를 대신 그릴 서체가 함께 설치됩니다.

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

`jp`, `ko`, `zh-cn`, `zh-tw`는 각각 일본어, 한국어, 중국어 간체, 중국어 번체용 Noto Sans와 Noto Serif 서체를 설치합니다. 설치 후에는 `page.pixmap()`, `page.svg()`, `doc.render()`, `annotation.pixmap()` 등 모든 렌더링에서 Adobe-Japan1, Korea1, GB1, CNS1 문자 컬렉션의 글꼴을 해당 언어의 서체로 그리며, 명조체(Mincho)처럼 이름에 세리프 계열임이 드러나는 글꼴은 세리프 서체로 그립니다. 각 서체는 페이지가 처음 필요로 할 때 로드됩니다.

`doc.fallback_fonts`는 `"ja"`, `"ko"`, `"zh-CN"`, `"zh-TW"` 언어 키를 `FallbackFont` 레코드에 매핑하며, 별도로 수정하기 전까지는 설치된 extra 목록을 반영합니다. `Font.from_file`로 불러온 서체를 `FallbackFont(sans=..., serif=...)`로 할당해 원하는 서체로 교체하거나, 특정 언어 키를 `del`로 삭제해 대체 글꼴 없이 그리거나, `{}`를 할당해 모든 언어에서 대체 글꼴을 끌 수 있습니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(list(doc.fallback_fonts))  # the installed extras, such as ['ja']
    doc.fallback_fonts = {}  # render without fallback fonts
    plain = doc.pages[0].pixmap()
    del doc.fallback_fonts  # back to the installed extras
```

이 글꼴 설정은 `Document` 객체에만 속하며 파일에 저장되지 않습니다. 텍스트 추출은 대체 글꼴 없이도 문자 컬렉션에서 문자를 읽어 냅니다. 대체 글꼴은 렌더링 결과를 측정하기 위한 `exact_bboxes=True`의 정밀 경계 상자와 `page.content()`의 텍스트 런에서만 사용됩니다.
