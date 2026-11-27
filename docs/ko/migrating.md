---
title: PyMuPDF 및 pylopdf에서 마이그레이션
description: PyMuPDF(fitz/pymupdf) 및 아카이브된 pylopdf 0.15/0.16에서 pyropdf로 옮기기 위한 비교 가이드와 자동 코드 변환 CLI 안내.
---

# PyMuPDF 및 pylopdf에서 마이그레이션

pyropdf는 MIT 라이선스의 Rust 코어와 단일 버전 관리 객체 저장소 위에서, 자체적인
Python다운 API로 일상적인 PDF 작업을 처리합니다.
[DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)
(*One way*)에 따라 `pyropdf` 패키지에는 **호환 별칭이나 `fitz` 심(shim)이 포함되어
있지 않습니다**. 대신 이 가이드에서 PyMuPDF와 `pylopdf` 0.15의 주요 패턴을 표준
pyropdf 코드와 나란히 비교해 보여주며, 마이그레이션 시 기계적인 이름 변경은
`tools/codemod.py`로 자동화할 수 있습니다.

## 자동 코드 변환(codemod) {#codemod}

저장소에 포함된 무의존성 AST codemod를 실행하여 기계적인 이름 변경을 미리 보거나
일괄 적용할 수 있습니다.

```bash
# 파일을 수정하지 않고 unified diff 미리 보기
python3 tools/codemod.py --dry-run --diff path/to/your_app/

# PyMuPDF (fitz / pymupdf) 호출부를 제자리에서 변환
python3 tools/codemod.py --from pymupdf path/to/your_app/

# pylopdf 0.15 호출부를 제자리에서 변환
python3 tools/codemod.py --from pylopdf path/to/your_app/

# 마이그레이션되지 않은 호출이 남아 있으면 CI에서 실패 처리
python3 tools/codemod.py --check path/to/your_app/
```

이 codemod는 주석과 서식을 보존한 채 임포트, `doc.pages` 인덱싱 및 순회, 저장
키워드 옵션, 메타데이터/첨부 파일/텍스트 호출, Enum 상수를 기계적으로 다시 씁니다.
동작 차이가 있거나 자동 변환할 수 없는 API(`apply_redactions`, `Story`,
`get_text("dict")`, `csCMYK`, `insert_text`, `get_toc`, 불변 기하 객체의 제자리
수정 등)에 대해서는 조치 가능한 `file:line: [code] message` 경고를 출력합니다.

## 주요 동작 및 설계 차이 {#differences}

코드를 한 줄씩 옮기기 전에 의도적으로 다르게 설계된 네 가지를 확인하세요.

1. **모든 곳에서 표시 공간(display space) 좌표 사용.** 페이지에서 읽거나 쓰는 모든
   좌표(`page.rect`, `word.bbox`, `canvas.text`, 주석, 링크, 페이지 박스)는
   CropBox와 `/Rotate`가 적용된 후 보이는 페이지의 **왼쪽 위**를 원점으로 삼고 `y`가
   아래로 증가하는 포인트 단위 표시 공간을 사용합니다. `/Rotate`를 되돌리거나 `y`축을
   수동으로 뒤집을 필요가 없습니다.
2. **모든 페이지 인덱스가 0부터 시작.** `doc.pages`, `doc.outline`,
   `doc.page_labels`는 모두 0 기반 페이지 인덱스를 사용합니다(PyMuPDF의 `get_toc()`와
   `set_toc()`는 1 기반입니다).
3. **원자적 트랜잭션과 불변 값.** 편집 작업을 `with doc.transaction():`으로 묶으면
   실행 취소 가능한 하나의 버전(`doc.history`)이 되며 예외가 발생하면 모든 변경이
   롤백됩니다. 기하 타입(`Point`, `Rect`, `Matrix`)과 `Pixmap`은 불변 값이므로 제자리
   수정 대신 `r = r | p`, `r = r & o`, `m = ~m`을 사용합니다.
4. **PDF 전용 범위와 명시적 세터.** `pyropdf.open()`은 PDF 파일만 읽습니다(XPS,
   EPUB, HTML `Story`는 지원하지 않으며, 이미지는 `doc.pages.new_from_image()`로
   변환합니다). 핸들 세터(`page.rotation = 90`, `doc.form["name"].value = "Ada"`)는
   종속 구조를 즉시 반영하므로 `widget.update()`나 `annot.update()`가 필요 없습니다.

## 열기, 사전 조회 및 저장 {#open-save}

| PyMuPDF / pylopdf 0.15 | pyropdf | 비고 |
| --- | --- | --- |
| `import fitz` / `import pymupdf` / `import pylopdf` | `import pyropdf` | 런타임 별칭 모듈 없음 |
| `fitz.open()` | `pyropdf.Document()` | 빈 PDF 생성 |
| `fitz.open(stream=data, filetype="pdf")` | `pyropdf.open(data)` | 경로, `bytes`, `bytearray`, `memoryview` 허용 |
| `pylopdf.peek_metadata(path)` | `pyropdf.probe(path)` | 전체를 열지 않고 헤더/메타데이터 읽기 |
| `doc.save(path, deflate=True, linear=True, use_objstms=True)` | `doc.save(path, compress=True, linearize=True, object_streams=True)` | `garbage=1..4`는 `prune=True`, `renumber=True`, `dedupe=True`로 대체 |
| `doc.saveIncr()` | `doc.save(path, incremental=True)` | 원본 바이트를 정확한 접두사로 보존 |
| `doc.tobytes()` | `doc.to_bytes()` | 메모리에서 PDF 바이트 반환 |

```py title="이전 (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    data = doc.tobytes(deflate=True, linear=True)
    doc.save("out.pdf", deflate=True, garbage=4)
```

```python title="이후 (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    data = doc.to_bytes(compress=True, linearize=True)
    doc.save("out.pdf", compress=True, prune=True, renumber=True, dedupe=True)
```

## 페이지, 박스 및 기하 타입 {#pages-geometry}

| PyMuPDF | pyropdf | 비고 |
| --- | --- | --- |
| `len(doc)`, `doc.page_count` | `len(doc.pages)` | 라이브 시퀀스 뷰 |
| `doc[i]`, `doc.load_page(i)` | `doc.pages[i]` | `Page` 핸들은 이동/삽입 후에도 해당 페이지를 계속 추적 |
| `page.number`, `page.parent` | `page.index`, `page.document` | 상수 시간 `page.index` |
| `doc.delete_page(i)`, `doc.move_page(a, b)` | `del doc.pages[i]`, `doc.pages.move(a, b)` | 참조 규칙으로 링크/개요 유효성 유지 |
| `doc.insert_pdf(other)` | `doc.pages.extend(other.pages)` | 또는 `doc.pages.insert(index, page)` |
| `page.set_rotation(90)`, `page.remove_rotation()` | `page.rotation = 90`, `page.remove_rotation()` | 표시 좌표 일관성 유지 |
| `r.include_point(p)`, `r.intersect(o)`, `m.invert()` | `r = r | p`, `r = r & o`, `m = ~m` | 기하 값은 불변 |

```py title="이전 (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc.load_page(0)
    page.set_rotation(90)
    r = fitz.Rect(10, 20, 100, 80)
    r.include_point((140, 120))
    doc.move_page(0, 1)
```

```python title="이후 (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.rotation = 90
    r = pyropdf.Rect(10, 20, 100, 80) | (140, 120)
    doc.pages.move(0, 1)
    assert page.index == 1 and r.x1 == 140
```

## 텍스트, 단어, 검색, 표 및 Markdown {#reading}

| PyMuPDF / pymupdf4llm | pyropdf | 비고 |
| --- | --- | --- |
| `page.get_text()` | `page.text()` | 기본값은 표시 읽기 순서. `/ActualText`를 끄려면 `actual_text=False` 전달 |
| `page.get_text("words")` | `page.words()` | frozen `Word` 레코드(`.bbox`, `.text`, `.block`, `.line`, `.word`) 반환 |
| `page.get_text("dict")` / `"html"` / `"xml"` | `page.layout(chars=True).to_dict()` / `.to_html()` / `.to_xml()` | 컬럼 기반 `page.chars()`도 제공 |
| `page.search_for(q)` | `page.search(q)` | 회전된 사변형(quad)이 필요하면 `quads=True` 전달 |
| `page.find_tables()` | `page.tables()` | `.rows`와 `.cells`를 가진 `Table` 레코드 반환 |
| `pymupdf4llm.to_markdown(doc)` / `to_chunks` | `doc.markdown()`, `doc.to_chunks()`, `doc.to_json()` | 기본 내장. PDF 구조 트리를 자동 활용(`use_structure="auto"`) |

```py title="이전 (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    text = page.get_text(sort=True)
    words = [(w[4], w[:4]) for w in page.get_text("words")]
    hits = page.search_for("Invoice")
```

```python title="이후 (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    text = page.text()
    words = [(word.text, word.bbox) for word in page.words()]
    hits = page.search("Invoice")
    md = doc.markdown()
    chunks = doc.to_chunks(max_chars=1000)
```

## 렌더링 및 Pixmap {#rendering}

| PyMuPDF | pyropdf | 비고 |
| --- | --- | --- |
| `page.get_pixmap(dpi=150, annots=False)` | `page.pixmap(dpi=150, annotations=False)` | `antialias=`, `hinting=`, `glyph_cache=`, `colorspace="rgb" \| "gray"` 지원 |
| `page.get_svg_image()` | `page.svg()` | 텍스트를 경로(path)로 출력(`text_as_path=False`는 `#435` 대기 중) |
| `pix.save("page.png")`, `pix.tobytes("jpeg")` | `pix.save("page.png")`, `pix.to_bytes("jpeg")` | `bytes(pix)` / `memoryview(pix)`로 PEP 3118 버퍼 노출 |
| `pix.invert_irect()`, `pix.tint_with(...)` | `pix.invert()`, `pix.tint(...)`, `pix.rotate(...)`, `pix.crop(...)` | 불변: 각 연산은 새 `Pixmap` 반환 |

```py title="이전 (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    pix = doc[0].get_pixmap(dpi=144, alpha=False, annots=True)
    pix.save("page.png")
```

```python title="이후 (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    pix = doc.pages[0].pixmap(dpi=144, alpha=False, annotations=True, antialias=True)
    pix.save("page.png")
    gray = pix.to_gray()
```

## Canvas로 그리기 {#drawing}

| PyMuPDF / pylopdf 0.15 | pyropdf | 비고 |
| --- | --- | --- |
| `page.new_shape()` / `draw_rect` / `draw_line` | `with page.draw() as canvas:` | 정상 종료 시 원자적으로 커밋, 예외 발생 시 롤백 |
| `page.insert_text(pt, text, fontsize=14)` | `canvas.text(text, pt, font=..., size=14)` | 표시 좌표(왼쪽 위 원점) |
| `page.insert_textbox(rect, text)` | `canvas.textbox(rect, text, font=..., size=12)` | `TextFit` 레코드 반환 |
| `Font(fontname="Helvetica")` / `Font(fontfile=p)` | `Font.standard("Helvetica")` / `Font.from_file(p)` | 임베디드 폰트는 `harfrust`로 셰이핑되고 저장 시 서브셋화 |
| `page.insert_image(rect, filename=p)` | `canvas.image(rect, p)` | 경로, bytes, `Pixmap`, `PageImage` 허용 |
| `page.show_pdf_page(rect, src_doc, pno)` | `canvas.page(rect, src_doc.pages[pno])` | `clip=`, `rotate=`, `keep_aspect=` 지원 |

```py title="이전 (PyMuPDF)"
import fitz

doc = fitz.open()
page = doc.new_page(width=400, height=200)
page.draw_rect(fitz.Rect(20, 20, 380, 180), color=(0.1, 0.3, 0.7), width=2)
page.insert_text((36, 72), "Hello from PDF", fontsize=18)
doc.save("drawn.pdf")
```

```python title="이후 (pyropdf)"
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

## 주석, 링크 및 폼 {#annotations-forms}

| PyMuPDF / pylopdf 0.15 | pyropdf | 비고 |
| --- | --- | --- |
| `page.annots()`, `page.add_highlight_annot(quads)` | `page.annotations`, `page.annotations.add(Highlight(quads))` | 사양 객체가 외관 스트림을 즉시 생성 |
| `page.get_links()`, `page.insert_link(d)` | `page.links`, `page.links.add(rect, target)` | 대상은 URI 문자열 또는 타입이 지정된 목적지/액션 레코드 |
| `doc.set_form_field(name, value)` / `widget.update()` | `doc.form[name].value = value` | 외관을 즉시 재생성(`update()` 메서드 없음) |
| `page.add_widget(w)` | `page.widgets.add(TextField(name, rect, ...))` | 검증된 frozen 사양 객체 |
| `doc.bake(annots=True, widgets=True)` | `doc.flatten(annotations=True, widgets=True)` | 페이지 콘텐츠로 평탄화 |

```py title="이전 (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    page = doc[0]
    page.add_highlight_annot(page.search_for("Invoice"))
    page.add_text_annot((36, 36), "Reviewed")
    doc.save("annotated.pdf")
```

```python title="이후 (pyropdf)"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    page.annotations.add(pyropdf.Highlight(page.search("Invoice")))
    page.annotations.add(pyropdf.Note((36, 36), contents="Reviewed"))
    page.widgets.add(pyropdf.TextField("reviewer", (36, 120, 220, 144), value="Ada"))
    doc.form["reviewer"].value = "Ada Lovelace"
    doc.save("annotated.pdf")
```

## 암호화, 서명, 트랜잭션 및 실행 취소 {#security-transactions}

| PyMuPDF | pyropdf | 비고 |
| --- | --- | --- |
| `doc.needs_pass`, `doc.authenticate(pw)` | `doc.needs_password`, `doc.unlock(pw)` | 잘못된 비밀번호 시 `0` 반환 대신 `PasswordError` 발생 |
| `doc.save(p, encryption=..., user_pw=...)` | `doc.save(p, encryption=Encryption(user_password=...))` | 표준 AES, 공개 키 수신자, AES-GCM 지원 |
| `doc.journal_undo()`, `doc.journal_redo()` | `with doc.transaction("name"):`, `doc.history.undo()`, `doc.history.redo()` | `doc.history.save()` / `doc.history.restore(data)`로 영속화 가능 |
| — | `doc.changes(incremental=True)` | 저장 전 무효화되는 서명과 PDF/A 또는 PDF/UA 선언 보고서 |
| — | `doc.sign(...)`, `doc.signatures` | PAdES B-B / B-T 서명 및 서명 검증 기본 내장 |

```py title="이전 (PyMuPDF)"
import fitz

with fitz.open("input.pdf") as doc:
    doc.journal_enable()
    doc.journal_start_op("rotate")
    doc[0].set_rotation(90)
    doc.journal_stop_op()
    doc.journal_undo()
    doc.save("encrypted.pdf", encryption=fitz.PDF_ENCRYPT_AES_256, user_pw="open")
```

```python title="이후 (pyropdf)"
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

## 알려진 미지원 기능 및 제외 항목 {#gaps}

아래 항목에 의존하는 워크플로를 마이그레이션하기 전에 현재 상태를 확인하세요(전체
비교표는 [evidence/pymupdf-comparison/features.md](https://github.com/yhay81/pyropdf/blob/main/evidence/pymupdf-comparison/features.md)
참고).

| 영역 | 현재 상태 | 추적 이슈 |
| --- | --- | --- |
| 영구 가림 처리(`apply_redactions`) | `RedactionMark` 주석과 `doc.sanitize()`는 지원합니다. 하부 텍스트/이미지/벡터의 영구 제거는 중첩 출처 정보(provenance) 지원을 기다리고 있습니다 | [#362](https://github.com/yhay81/pyropdf/issues/362), [#104](https://github.com/yhay81/pyropdf/issues/104) |
| 영구 클리핑(`clip_to_rect`) | `page.cropbox`는 바깥 콘텐츠를 제거하지 않고 숨기기만 합니다 | [#420](https://github.com/yhay81/pyropdf/issues/420) |
| CMYK 래스터 픽스맵(`csCMYK`) | `page.pixmap()`은 RGB와 회색조를 출력합니다. CMYK 래스터화는 업스트림 엔진 지원을 기다리고 있습니다 | [#425](https://github.com/yhay81/pyropdf/issues/425), [#433](https://github.com/yhay81/pyropdf/issues/433) |
| SVG 내 선택 가능한 텍스트(`text_as_path=False`) | `page.svg()`는 글리프를 벡터 경로로 출력합니다 | [#435](https://github.com/yhay81/pyropdf/issues/435) |
| 비 PDF 입력 및 HTML 레이아웃(`Story`, `insert_htmlbox`) | 의도적으로 범위에서 제외되었습니다(`pyropdf`는 PDF와 이미지-PDF 변환만 다룹니다) | [#355](https://github.com/yhay81/pyropdf/issues/355) |
| 입력 안전성 | [상태 및 안전성](status.md#safety)과 [API 안정성](stability.md)을 참고하세요 | [#15](https://github.com/yhay81/pyropdf/issues/15) |
