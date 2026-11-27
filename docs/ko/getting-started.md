---
title: 시작하기
description: pyropdf를 설치하고, 문서를 열어 페이지를 읽고 렌더링한 뒤, 트랜잭션으로 편집하여 저장합니다.
---

# 시작하기

## 설치

=== "pip"

    ```bash
    pip install pyropdf
    ```

=== "uv"

    ```bash
    uv add pyropdf
    ```

=== "Poetry"

    ```bash
    poetry add pyropdf
    ```

Linux, macOS, Windows용 CPython 3.11~3.15 휠(wheel)을 제공하며, free-threaded 빌드용 별도 휠도 함께 배포합니다. pyropdf에는 필수 의존성이 없습니다. 오프라인 OCR은 모델이 필요하며 extra로 설치할 수 있습니다.

=== "pip"

    ```bash
    pip install "pyropdf[ocr]"
    ```

=== "uv"

    ```bash
    uv add "pyropdf[ocr]"
    ```

=== "Poetry"

    ```bash
    poetry add "pyropdf[ocr]"
    ```

`jp`, `ko`, `zh-cn`, `zh-tw` extra는 문서에 임베드되지 않은 일본어, 한국어, 중국어 글꼴을 렌더링하기 위한 서체를 설치합니다. 자세한 내용은 [렌더링](rendering.md#fonts-that-a-document-does-not-embed)을 참고하세요.

## Pyodide

릴리스 워크플로는 Pyodide 314.0.6(CPython 3.14, Emscripten 5.0.3, `pyemscripten_2026_0_wasm32`)용 코어 휠을 빌드하여 PyPI와 GitHub Releases에 게시합니다. `pip install pyropdf`로 바로 설치하거나, 릴리스에서 `.whl` 파일을 내려받아 호환되는 Pyodide 가상 환경에 설치할 수 있습니다.

```bash
path/to/pyodide-venv/bin/pip install ./pyropdf-*-pyemscripten_2026_0_wasm32.whl
```

Python 3.14.2, Node.js 24 및 이 저장소의 Rust 툴체인이 설치된 Linux 환경에서는 `bash tools/build_pyodide.sh`로 빌드를 재현하고 Pyodide에서 설치된 확장을 실행해 볼 수 있습니다. 이 테스트는 PDF 바이트 입력과 단일 스레드 환경에서 추출, 렌더링, 편집을 검증합니다. OCR 모델과 CJK 글꼴은 선택적 다운로드로 유지됩니다.

## Cloudflare Python Workers

위의 코어 휠을 `python_workers` 호환성 플래그와 `2026-11-01` 호환성 날짜와 함께 사용하세요. `pyropdf`는 Worker의 `fetch` 메서드 안에서 임포트해야 합니다. 네이티브 확장이 임포트 시점에 난수를 필요로 하며, 이 런타임은 요청 컨텍스트 안에서만 난수를 제공하기 때문입니다.

Node.js 24, Python 3.14, uv 0.13.9가 설치된 체크아웃 환경에서는 다음 명령으로 내려받은 휠을 공식 로컬 런타임에서 검증할 수 있습니다.

```bash
bash tools/cloudflare/smoke.sh local dist/*-pyemscripten_2026_0_wasm32.whl
```

이 스크립트는 pywrangler로 휠을 설치하고 루프백 Worker로 PDF 바이트를 전송합니다. 저장된 바이트를 다시 열어 추가된 페이지와 원본 텍스트를 검증하고, `file_size` 제한 오류를 확인하며, 다음 요청에서 다른 문서를 독립적으로 여는지 확인합니다. 릴리스 워크플로는 휠 이름, SHA-256, 호환성 날짜, 런타임 버전을 함께 기록합니다.

**1.0 릴리스용 호스티드 검증(hosted 1.0 release check)**에는 동일한 릴리스 후보 휠을 전용 Cloudflare Workers 계정에서 실행합니다. 해당 계정의 `CLOUDFLARE_ACCOUNT_ID`와 Workers Scripts 편집 권한이 있는 `CLOUDFLARE_API_TOKEN`을 설정한 뒤 다음을 실행하세요.

```bash
bash tools/cloudflare/smoke.sh hosted dist/*-pyemscripten_2026_0_wasm32.whl
```

이 명령은 고유한 이름의 비공개 Python Worker를 배포하고, 원격 서비스 바인딩을 통해 동일한 요청을 보낸 뒤, 종료 시 Worker를 삭제합니다. 배포된 버전 ID와 `hosted` 검증 결과를 릴리스와 함께 기록하세요. 풀 리퀘스트 CI는 Cloudflare 자격 증명 없이 루프백 리스너를 사용하는 `local` 검증을 실행합니다.

## 열기와 닫기

`pyropdf.open`은 파일 경로나 문서 바이트를 받습니다. 문서는 컨텍스트 매니저로 동작하므로 블록을 빠져나갈 때 자동으로 닫히며, `pyropdf.Document()`는 빈 문서를 생성합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    print(len(doc.pages), doc.pages[0].rect)

doc = pyropdf.Document()
doc.pages.new((595, 842))
data = doc.to_bytes()
doc.close()
```

`pyropdf.probe`는 문서 전체를 불러오지 않고 페이지 수, PDF 버전, 메타데이터를 빠르게 읽습니다.

```python
import pyropdf

info = pyropdf.probe("input.pdf")
print(info.page_count, info.version, info.needs_password)
```

## 페이지 읽기

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    print(page.text())  # (1)!
    for word in page.words():
        print(word.text, word.bbox)  # (2)!
    print(page.search("total"))  # (3)!
```

1. 읽기 순서 텍스트: 다단 구성, 표, 제목이 콘텐츠 스트림에 그려진 순서가 아니라 사람이 읽는 순서대로 반환됩니다.
2. 경계 상자는 표시 좌표를 사용합니다. 페이지 회전 여부와 관계없이 뷰어에 표시되는 페이지의 왼쪽 위를 원점으로 하는 포인트 단위입니다. [핵심 개념](concepts.md#display-coordinates)을 참고하세요.
3. 일치 항목마다 하나씩 반환되는 사각형 목록으로, `pyropdf.Highlight` 같은 주석에 그대로 전달할 수 있습니다.

레이아웃, 문자, 표, Markdown, OCR은 [내용 읽기](reading.md)에서 다룹니다.

## 페이지 렌더링

```python title="render.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].pixmap(dpi=144).save("page-1.png")
```

## 편집과 저장

모든 편집은 문서의 새 버전을 만듭니다. 트랜잭션은 여러 편집을 `doc.history`로 되돌릴 수 있는 이름 붙은 한 단계로 묶어 주며, 블록 안에서 예외가 발생하면 문서를 편집 이전 상태로 되돌립니다.

```python title="edit.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate and note"):  # (1)!
        page.rotation = 90
        page.annotations.add(pyropdf.Note((40, 40), contents="Checked"))
    doc.save("output.pdf", incremental=True)  # (2)!
```

1. 두 편집이 `doc.history`의 이름 붙은 한 단계가 됩니다. 블록 안에서 예외가 발생하면 두 변경 모두 반영되지 않고 문서가 이전 버전을 유지합니다.
2. 편집으로 기록된 객체만 원본 바이트 뒤에 추가하므로, 원본 바이트에 적용된 전자 서명이 유효하게 유지됩니다.

증분 저장은 편집으로 기록된 객체만 뒤에 추가합니다. 기본값인 전체 저장은 파일을 새로 작성합니다. 두 방식의 차이는 [버전과 저장](versions.md)에서 설명합니다.

## 다음 단계

- [내용 읽기](reading.md): 텍스트, 단어, 레이아웃, 표, Markdown, OCR.
- [렌더링](rendering.md): 픽스맵, 일괄 렌더링, SVG.
- [편집](editing.md): 페이지, 주석, 링크, 폼, 북마크, 첨부 파일.
- [버전과 저장](versions.md): 트랜잭션, 실행 취소, 변경 보고서, 암호화.
- [제한과 오류](limits.md): 각 작업의 처리량 제한.
