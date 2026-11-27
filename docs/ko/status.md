---
title: 상태 및 안전성
description: pyropdf 0.4가 지원하는 범위, 아직 지원하지 않는 항목, 그리고 입력 안전성에 대해 pyropdf가 주장하는 것과 주장하지 않는 것.
---

# 상태 및 안전성

## 상태

마이너 버전 사이에서 API가 변경될 수 있으며([API 안정성 정책](stability.md) 참고),
모든 변경 사항은 [변경 이력](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md)에
기록됩니다. 기반 설계는 [DESIGN.md](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md)에,
각 부분이 ISO 32000-2를 얼마나 다루는지는 [명세 지원 현황](specification.md)에
정리되어 있습니다.

릴리스된 버전의 경우 [릴리스 페이지](https://github.com/yhay81/pyropdf/releases)에서
해당 태그를 선택하고 `capabilities.zip`을 열어 확인하세요. 릴리스 노트에서도 같은
아카이브로 연결됩니다. 이 보고서는 프로세서 역할, 테스트된 작업, 제외 항목 및
제한 사항을 명시하며, 입력 프로파일, 외부 엔진, 선택적 런타임을 각각 구분하여
표시합니다. 0.3.1 버전은 이러한 아카이브 도입 이전의 릴리스이므로 공개된 검증
범위가 없습니다.

0.4.1에 아직 포함되지 않은 기능:

- 가림 처리(redaction) 및 사각형 바깥의 콘텐츠 제거 — 검토 중인 hayro 변경 사항의
  콘텐츠 출처 정보(provenance)가 필요합니다.
- 렌더링과 해석 작업을 제한하는 인터프리터 연료(fuel) 및 취소 기능 — 역시 검토 중인
  hayro 변경 사항에 의존합니다.
- CMYK 픽스맵 및 SVG 내 선택 가능한 텍스트 — hayro 지원을 기다리고 있습니다.

진행 상황은 [이슈 트리](https://github.com/yhay81/pyropdf/issues/1)에서 추적합니다.

## 안전성 {#safety}

hayro의 bounded construction([#15](https://github.com/yhay81/pyropdf/issues/15))이
릴리스되기 전까지는 어떤 pyropdf 릴리스도 신뢰할 수 없는 입력에 대한 안전성을
주장하지 않습니다. 그전까지 출처를 알 수 없는 PDF는 렌더링 및 해석 경로에서 메모리나
시간을 고갈시킬 수 있는 것으로 간주하세요.

사용자가 제공한 문서를 처리할 때는
[페이지 읽기 및 렌더링 중 적용되는 제한](limits.md#reading-and-rendering)과
[워커 프로세스에서 메모리와 시간 제한을 적용하는 방법](limits.md#reading-a-pdf-in-a-worker)을
참고하세요.

보안 취약점은 [GitHub 보안 권고](https://github.com/yhay81/pyropdf/security/advisories/new)를
통해 비공개로 제보해 주세요.
