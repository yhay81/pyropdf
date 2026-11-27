---
title: pyropdf 선택하기
description: pyropdf가 잘하는 일, 아직 지원하지 않는 기능, 그리고 PyMuPDF, pypdf, pikepdf와의 비교.
---

# pyropdf 선택하기

## pyropdf가 적합한 경우

- **신뢰할 수 있는 편집.** 모든 편집은 하나의 버전이 됩니다. 트랜잭션은 실패 시
  자동으로 되돌려지며, `doc.history`로 실행 취소와 다시 실행을 할 수 있고,
  `doc.changes()`는 저장하기 전에 어떤 서명과 PDF/A 또는 PDF/UA 선언이
  깨질지 미리 알려줍니다.
- **건드리지 않은 내용을 그대로 보존하는 저장.** 증분 저장은 변경된 객체만
  덧붙이므로 원본 바이트에 대한 서명이 유효하게 유지되며, 편집 없이 저장하면
  입력 파일을 그대로 재현합니다.
- **읽기와 렌더링을 하나의 패키지로.** 읽기 순서의 텍스트, 단어, 레이아웃, 표,
  Markdown, 오프라인 OCR, 픽스맵 및 SVG 렌더링을 필수 의존성 없이 제공합니다.
- **관대한 라이선스.** pyropdf는 MIT 라이선스를 따릅니다.
- **최신 Python 지원.** 프리 스레드(free-threaded) 빌드를 포함한 CPython
  3.11~3.15를 지원하며, 하나의 문서를 병렬로 읽을 수 있습니다.

## 아직 지원하지 않는 기능

- hayro의 bounded construction이 릴리스되기 전까지는 어떤 pyropdf 릴리스도
  신뢰할 수 없는 입력에 대한 안전성을 주장하지 않습니다.
  [상태 및 안전성](status.md)을 참고하세요.
- 렌더링 속도는 MuPDF보다 느립니다(아래 참고).
- 마이너 버전 사이에서 API가 변경될 수 있습니다.

## 다른 라이브러리와의 비교

| | pyropdf | PyMuPDF | pypdf | pikepdf |
| --- | --- | --- | --- | --- |
| 라이선스 | MIT | AGPL-3.0 또는 상용 | BSD-3-Clause | MPL-2.0 |
| 구현 | Rust (hayro 렌더링) | C (MuPDF) | 순수 Python | C++ (qpdf) |
| 페이지 렌더링 | 지원 | 지원 | 미지원 | 미지원 |
| 텍스트, 단어, 표 | 지원 | 지원 | 텍스트만 | 미지원 |
| 실행 취소 및 원자적 트랜잭션 | 지원 | 저널링 | 미지원 | 미지원 |

이 가운데 가장 기능이 풍부한 라이브러리는 PyMuPDF이며, pyropdf는 이를 기준으로
자체 성능을 측정합니다. 실제 문서 15개로 구성된 코퍼스에서(렌더링 시 원시 샘플
읽기, 5회 실행 중 최단 시간) PyMuPDF 2.1 대비 pyropdf의 소요 시간 비율은
다음과 같습니다(낮을수록 빠르며, 플랫폼에 따라 비율이 달라집니다).

| 작업 | 0.4.0 (Linux x86-64) | 0.3.1 (macOS arm64) |
| --- | ---: | ---: |
| 열기 | 0.27 | 0.13 |
| 텍스트 | 1.38 | 0.50 |
| 단어 | 1.63 | 0.41 |
| 검색 | 0.95 | 0.37 |
| 72 dpi 렌더링 | 1.78 | 1.77 |
| 144 dpi 렌더링 | 1.49 | 1.82 |
| 전체 저장 | 0.12 | 0.16 |

Linux x86-64 릴리스 보고서(`comparison-v0.4.0.zip`)는
[v0.4.0 릴리스](https://github.com/yhay81/pyropdf/releases/tag/v0.4.0)에 첨부되어
있으며, 측정 방법, Apple Silicon macOS 기준치, 원시 측정 시간 및 기능별 비교는
[evidence/pymupdf-comparison](https://github.com/yhay81/pyropdf/tree/main/evidence/pymupdf-comparison)에서
확인할 수 있습니다(영어).
