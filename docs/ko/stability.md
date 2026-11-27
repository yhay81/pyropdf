---
title: API 안정성
description: pyropdf의 공개 API 범위, 1.0 전후의 변경 규칙, 지원 중단 정책, 제한 코드, 선택적 extras, 기능 선언 및 안전성 원칙을 설명합니다.
---

# API 안정성

pyropdf는 [유의적 버전 관리(Semantic Versioning)](https://semver.org/)를 따릅니다.
이 문서에서는 현재의 1.0 이전 규칙과 pyropdf 1.0 릴리스 시 동결될 1.0 안정성 계약을
모두 명시합니다([DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases)).

## 1.0 이전 현재 상태 {#current-status}

pyropdf는 현재 **1.0 이전(0.x)** 단계입니다. 아래의 안정성 계약은 **아직 동결되지
않았습니다**.

- 마이너 릴리스(`0.x.0`)는 지원 중단 유예 기간 없이 공개 API의 이름을 변경하거나,
  구조를 개편하거나, 제거할 수 있습니다.
  [DESIGN.md §10.1](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#101-rules)
  (*One way*)에 따라 1.0 이전의 재설계에서는 호환 별칭을 남기지 않고 같은 릴리스에서
  기존 API를 대체합니다.
- 패치 릴리스(`0.x.y`)는 API 호환성을 유지하면서 버그 수정, 보안 수정 및 패키징
  업데이트에 집중하는 것을 목표로 합니다.
- 모든 호환성 깨짐(breaking change)과 대체 API는
  [변경 이력](https://github.com/yhay81/pyropdf/blob/main/CHANGELOG.md)에 기록됩니다.
- 마이너 릴리스 변경 사항을 원하는 시점에 반영하려면 마이너 시리즈를 고정하세요
  (예: `pyropdf~=0.4.0`).

아래 섹션의 1.0 안정성 보장은 `1.0.0`이 릴리스될 때부터 효력이 발생합니다.

## 공개 API 경계 {#public-api-boundary}

| 대상 | 안정성 | 비고 |
| --- | --- | --- |
| `pyropdf.__all__`, `pyropdf.__version__`, `pyropdf.typed.__all__` | 공개 | 내부 서브모듈이 아니라 `pyropdf` 또는 `pyropdf.typed`에서 임포트하세요 |
| 문서화된 클래스, 메서드, 프로퍼티, 함수, Enum 및 타입 별칭 | 공개 | 매개변수 이름, 기본값, 위치/키워드 전용 구분도 계약에 포함됩니다 |
| frozen 레코드 및 사양(specification) dataclass | 공개 | 필드 이름과 타입은 공개 대상이며, 별도로 문서화되지 않은 한 위치 인자 생성은 보장하지 않습니다 |
| 예외 클래스, 예외 상속 계층, `LimitError.code`, `LIMIT_CODES` / `LimitCode` | 공개 | 프로그래밍 방식의 오류 처리는 메시지 문구가 아니라 예외 타입과 `LimitError.code`에 의존해야 합니다 |
| `METADATA_KEYS` 및 문서화된 `Literal` 옵션 값 | 공개 | 마이너 릴리스에서 새로운 선택적 값이 추가될 수 있습니다 |
| `_`로 시작하는 비공개 이름, `pyropdf._core`, 문서화되지 않은 모듈 | 내부 | 예고 없이 어떤 릴리스에서든 변경되거나 제거될 수 있습니다 |
| `repr()` 출력, 오류 메시지 문구, 경고 발생 순서 | 내부 | 진단 목적이며 기계적 파싱을 위한 것이 아닙니다 |
| 저장된 PDF, PNG, SVG의 정확한 직렬화 바이트열 | 구현 세부 사항 | 시각적 렌더링, 추출된 텍스트, 구조적 유효성, 증분 저장의 접두사 보존, 변경되지 않은 객체의 무손실 보존을 보장합니다 |

## 1.0 이후의 유의적 버전 관리 {#after-v1}

`1.0.0`부터는 다음 규칙을 따릅니다.

- **메이저 릴리스(`X.0.0`)**는 지원 중단된 API를 제거하거나 시그니처, 타입 또는
  문서화된 계약에 호환성이 깨지는 변경을 가할 수 있습니다.
- **마이너 릴리스(`1.Y.0`)**는 하위 호환되는 기능, 기본값이 있는 선택적 키워드 전용
  매개변수, 새로운 Enum 멤버 또는 `Literal` 옵션 값, 새로운 `LIMIT_CODES` 항목,
  기본값이 있는 추가 dataclass 필드를 도입합니다.
- **패치 릴리스(`1.Y.Z`)**는 공개 API를 깨뜨리지 않고 버그, 회귀(regression), 보안
  또는 패키징 결함을 수정합니다.

공개 심볼을 제거하거나 이름을 변경하는 것, 필수 매개변수를 추가하는 것, 허용되는
입력 타입을 좁히는 것, 반환 레코드에서 필드를 제거하는 것, 예외 상속 계층을 바꾸는
것은 모두 메이저 릴리스가 필요합니다. 타입 어노테이션도 공개 API의 일부이므로, 공개
시그니처의 정적 타이핑 호환성을 깨뜨리는 변경 역시 호환성 깨짐으로 간주합니다.

## 지원 중단 정책 {#deprecation-lifecycle}

`1.0.0`이 릴리스된 후, 제거 예정인 공개 API는 다음 4단계 수명 주기를 따릅니다.

1. 문서에 해당 API가 지원 중단 예정임을 표시하고, 대체 API와 제거 가능한 가장 이른
   릴리스를 명시합니다.
2. 실용적으로 가능한 모든 곳에서 해당 API를 호출하거나 사용할 때 Python 표준
   `DeprecationWarning`을 발생시킵니다.
3. 지원 중단 표시된 API는 해당 릴리스 이후 최소 **2회의 마이너 릴리스 및 6개월**
   동안 정상 동작을 유지합니다.
4. 실제 제거는 그 이후의 **메이저** 릴리스에서만 이루어집니다.

`PyropdfWarning`은 PDF 파일 자체의 문제(매핑되지 않은 글리프나 복구된 구조 등)를
보고하는 데 사용되며 API 지원 중단 알림에는 절대 사용하지 않습니다. 보안, 법적 또는
중대한 업스트림 긴급 상황에서는 더 짧은 절차가 필요할 수 있으며, 이러한 예외는 변경
이력과 릴리스 노트에 명시적으로 강조 표시됩니다.

## 제한 코드 및 처리 결과 변경 {#limits-and-behavior}

리소스 제한은 [`limits.toml`](https://github.com/yhay81/pyropdf/blob/main/limits.toml)에
등록되어 있으며, 런타임에는 `pyropdf.LIMIT_CODES`와 `pyropdf.LimitCode`로 노출되고
[제한과 오류](limits.md)에 문서화되어 있습니다.

- 기존 `LimitError.code` 식별자는 마이너 및 패치 릴리스 전반에 걸쳐 이름과 문서화된
  의미를 유지합니다.
- 리소스 고갈 취약점을 메우기 위해 마이너 또는 패치 릴리스에서 새로운 제한 코드가
  추가될 수 있습니다.
- 기본 제한을 강화하거나 새 가드를 추가하면, 이전 릴리스에서는 처리를 시도했던
  손상되었거나 과도하게 비용이 큰 PDF가 거부될 수 있습니다. 이러한 거부는 메시지
  문구를 매칭하지 말고 `LimitError`(또는 그 기본 클래스인 `PdfError`)를 잡아
  `error.code`를 `LIMIT_CODES`와 대조하여 처리하세요.
- 이전 결과가 잘못되었던 경우, 버그 수정으로 인해 어떤 릴리스에서든 추출된 텍스트,
  읽기 순서, 표 셀, 글리프 박스, 렌더링된 픽셀 또는 손상된 파일의 복구 출력이 달라질
  수 있습니다. 눈에 띄는 동작 변경은 변경 이력에 기록됩니다.

## 선택적 extras 및 데이터 패키지 {#extras-and-data}

코어 `pyropdf`에는 필수 런타임 의존성이 전혀 없습니다. 선택적 extras는 별도로 버전이
관리되는 의존성 또는 데이터 패키지를 설치합니다.

| Extra | 패키지 | 버전 관리 및 호환성 |
| --- | --- | --- |
| `recipients` | `asn1crypto`, `cryptography` | `pyproject.toml`에 선언된 업스트림 SemVer 범위를 따릅니다. 공개 키 CMS 수신자 암호화에만 필요합니다 |
| `ocr` | `pylopdf-ocr-models` | 호환 시리즈로 고정된 별도 버전의 모델 패키지입니다. 모델 갱신으로 인식 정확도와 경계 상자가 개선될 수 있습니다 |
| `jp`, `ko`, `zh-cn`, `zh-tw` | `pylopdf-fonts-jp`, `pylopdf-fonts-ko`, `pylopdf-fonts-zh-cn`, `pylopdf-fonts-zh-tw` | 별도 버전의 Noto CJK 대체 폰트 패키지입니다. 글리프 업데이트에 따라 미포함 CJK 폰트의 대체 렌더링이 달라질 수 있습니다 |
| `layout` *(계획 중)* | 평가 중([#1199](https://github.com/yhay81/pyropdf/issues/1199)) | 채택될 경우 코어 휠에 필수 의존성을 추가하지 않고 별도 버전의 선택적 extra로 제공됩니다 |

코어 휠은 데이터 패키지 업데이트와 독립적인 자체 SemVer 주기를 유지합니다. 데이터
패키지 형식에 호환되지 않는 변경이 발생하면 `pyproject.toml`의 호환 버전 범위를
올리고 릴리스 노트에 기록합니다.

## 지원 Python 버전 및 타깃 {#python-and-platforms}

- **Python 최소 버전:** `requires-python = ">=3.11"`(Python 3.11~3.15 지원). 최소
  Python 버전 상향은 해당 업스트림 Python 시리즈가 지원 종료(EOL)에 도달한 이후의
  마이너 또는 메이저 릴리스에서만 이루어지며, 릴리스 노트를 통해 사전 공지합니다.
- **네이티브 휠:** Linux(`x86_64`, `aarch64`), macOS(`arm64`, `x86_64`),
  Windows(`x86_64`)용 `abi3-py311` 휠과 소스 배포판(`sdist`)을 제공합니다.
- **프리 스레드 CPython:** `abi3` 휠은 프리 스레드 인터프리터에서 로드되지 않으므로
  위 5개 네이티브 타깃에 대해 `3.14t` 및 `3.15t` 전용 휠을 제공합니다.
- **WebAssembly:** Pyodide와 Cloudflare Python Workers를 대상으로 하는
  `PyEmscripten`(`wasm32`) 휠을 제공하며,
  [DESIGN.md §13](https://github.com/yhay81/pyropdf/blob/main/DESIGN.md#13-phases)에
  따라 1.0 릴리스 게이트로 승격되었습니다.

## 기능 선언 및 보안 주장 {#capabilities-and-security}

- **릴리스 기능 선언:** 기능 범위를 게시하는 각 릴리스에는 해당 태그와 커밋에
  정확히 묶인 재현 가능한 `capabilities.zip` 아카이브가 첨부되며, 검증된 범위, 작업,
  제외 항목, 외부 엔진, 선택적 런타임, 사람이 직접 검토해야 하는 검사 항목이
  명시됩니다. [상태 및 안전성](status.md)과 [명세 지원 현황](specification.md)을
  참고하세요.
- **입력 안전성 주장:** hayro의 bounded construction
  ([#15](https://github.com/yhay81/pyropdf/issues/15))이 릴리스되기 전까지는 어떤
  pyropdf 릴리스도 신뢰할 수 없는 입력에 대한 안전성을 주장하지 않습니다. 그전까지
  `DocumentLimits`와 `DocumentLimits.web()`은 객체 저장소와 API 작업량을 제한하며,
  출처를 알 수 없는 PDF의 렌더링과 해석은 격리된 워커 프로세스 안에서 실행해야
  합니다([제한과 오류](limits.md#reading-a-pdf-in-a-worker)).
- **보안 취약점 제보:** 취약점은
  [SECURITY.md](https://github.com/yhay81/pyropdf/blob/main/SECURITY.md)를 통해
  비공개로 제보해 주세요.
- **자동 호환성 검사:** CI는 모든 풀 리퀘스트에서 공개 내보내기(export), 시그니처,
  생성된 API 문서(`tools/api_reference.py --check`), `limits.toml` 일치 여부
  (`tests/test_limit_codes.py`), 명세 매핑 증빙(`tests/test_spec_map.py`)을
  검증합니다.
