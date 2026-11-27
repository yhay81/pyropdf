---
title: 제한과 오류
description: DocumentLimits가 검사하는 작업 범위, 제한된 워커 프로세스에서 PDF를 읽는 방법, pyropdf 오류 처리 방법을 알아봅니다.
---

# 제한과 오류

## 제한

`DocumentLimits`는 파일 크기, 페이지 및 객체 수, 그리고 pyropdf가 각 작업 중에 계량하는 처리량을 검사합니다. 검사가 작업을 거부하면 `LimitError.code`가 어떤 제한에 도달했는지 알려 줍니다.

```python
import pyropdf

limits = pyropdf.DocumentLimits(max_pages=1_000, max_decompressed_size=64 * 2**20, max_duration=10.0)
try:
    with pyropdf.open("input.pdf", limits=limits) as doc:
        print(doc.pages[0].text())
except pyropdf.LimitError as error:
    print("refused:", error.code)
```

`DocumentLimits.web()`은 문서를 열 때 모든 객체를 검사합니다. 페이지 콘텐츠 스트림에 10 MiB, 저장소를 통해 디코딩하는 개별 스트림에 64 MiB, hayro에 전달하는 PDF 바이트(`max_interpretation_size`)에 64 MiB 상한을 둡니다. 이 검사를 통과한 PDF라도 텍스트 추출이나 렌더링 과정에서 훨씬 큰 크기로 확장될 수 있습니다. 이 프로파일에서는 `max_memory_size`와 `max_duration`이 설정되지 않습니다.

`CancellationToken`을 사용하면 다른 스레드에서 작업 취소를 요청할 수 있습니다. pyropdf는 작업 예산을 확인할 때 이 토큰을 검사합니다.

```python
import pyropdf

token = pyropdf.CancellationToken()
limits = pyropdf.DocumentLimits(cancellation=token)
with pyropdf.open("input.pdf", limits=limits) as doc:
    token.cancel()
    try:
        doc.pages[0].text()
    except pyropdf.LimitError as error:
        print(error.code)  # cancelled
```

## 복합 작업 {#composite-operations}

| 작업 | 거부가 보고되는 지점 |
| --- | --- |
| `doc.structure.walk(max_elements=n)` | `n`을 초과하는 첫 번째 요소에서 `object_count`가 발생합니다. 순회가 끝난 뒤에야 이터레이터를 반환하며, 순회 중에는 `memory_size`와 취소도 함께 확인합니다. |
| `doc.open_action` | 임베드된 목적지를 읽을 때 대상 참조 탐색 단계를 하나의 작업으로 합산합니다. 메모리 거부는 `memory_size`, 취소는 `cancelled`를 보고합니다. |
| `doc.form.import_fdf()` 및 `import_xfdf()` | 인코딩된 입력에 대해 FDF는 `limits.max_file_size`(`file_size`)를, XFDF는 `max_size`(`form_field_input_size`)를 검사합니다. 두 메서드 모두 필드 탐색과 값 채우기를 하나의 문서 작업으로 처리합니다. |
| `with page.draw()` | 캔버스 안에 중첩된 가져오기 작업은 남은 예산을 공유합니다. 제한 거부로 블록을 빠져나가면 보류 중인 그리기와 필드 편집이 함께 폐기됩니다. |

메모리나 개수 제한으로 거부된 뒤에도 후속 작업에는 새 예산이 주어집니다. 반면 취소된 토큰은 이를 사용하는 문서에서 계속 취소 상태로 유지됩니다.

## 읽기 및 렌더링 {#reading-and-rendering}

`page.text()`, `page.words()`, `page.paths()`, `page.content()`, `page.pixmap()`은 모두 hayro를 사용해 페이지를 해석합니다. 아래 표는 다섯 메서드 모두에 적용됩니다.

| 필드 | pyropdf가 검사하는 범위 | 제한을 초과할 수 있는 부분 |
| --- | --- | --- |
| `max_interpretation_size` | 원본 바이트, 브리지의 상호 참조 업데이트, 편집된 객체, 합성된 외형을 포함해 hayro에 전달되는 PDF 바이트 크기. | 그 안의 필터링된 스트림과 이미지는 hayro가 디코딩할 때 훨씬 크게 확장될 수 있음. |
| `max_page_content_size` | `page.operations` 등에서 저장소가 필터를 디코딩한 뒤 페이지 콘텐츠 스트림의 크기. | 위 다섯 메서드에서는 hayro가 페이지 콘텐츠를 직접 디코딩함. |
| `max_decompressed_size` | 저장소를 통해 읽는 개별 스트림의 디코딩 후 크기. 필터가 출력을 할당한 뒤에 저장소가 크기를 검사할 수 있음. | 렌더링 중 디코딩되는 이미지 등 hayro가 직접 디코딩하는 스트림은 이 검사를 우회함. |
| `max_memory_size` | pyropdf가 작업 메모리 예산에 청구하는 할당량. | hayro의 작업 메모리와 래스터 버퍼로 인해 프로세스 전체 사용량이 이 한도를 넘을 수 있음. |
| `max_duration` | pyropdf의 예산 검사 시점에서의 경과 시간. | hayro가 디코딩, 해석 또는 그리기를 수행하는 동안 제한 시간이 지날 수 있으며, pyropdf는 다음 검사 시점에만 이를 보고할 수 있음. |

`text()`와 `words()`의 글리프·텍스트 제한, `paths()`와 `content()`의 요소 수 제한, `pixmap()`의 래스터 크기 제한은 각 호출이 수집하거나 반환하는 양을 제한합니다. 출력이 적은 페이지라도 해석하는 데 상당한 작업이 들 수 있습니다. 업스트림 작업은 [제한된 스트림 디코딩 (#48)](https://github.com/yhay81/pyropdf/issues/48), [인터프리터 예산과 취소 (#105)](https://github.com/yhay81/pyropdf/issues/105), [제한된 구조 생성 (#15)](https://github.com/yhay81/pyropdf/issues/15)에서 다룹니다.

파서 내부 예산은 [#573](https://github.com/yhay81/pyropdf/issues/573), 글꼴 셰이핑은 [#580](https://github.com/yhay81/pyropdf/issues/580), PNG 디코드 버퍼는 [#561](https://github.com/yhay81/pyropdf/issues/561)에서 추적 중입니다. 프로젝트는 엔진의 작업 카운터와 취소 기능을 기다리고 있으며([#678](https://github.com/yhay81/pyropdf/issues/678)), 제한된 구조 생성의 릴리스 조건은 [#243](https://github.com/yhay81/pyropdf/issues/243)으로 유지됩니다.

## 워커 프로세스에서 PDF 읽기 {#reading-a-pdf-in-a-worker}

사용자가 애플리케이션으로 PDF를 전송하는 경우에는 별도의 프로세스에서 문서를 열고 페이지 작업을 수행하세요. 아래 Linux 예제는 `input.pdf` 첫 페이지의 단어 수를 셉니다. util-linux의 `prlimit`이 Python이 시작되기 전에 워커의 가상 주소 공간을 1 GiB로 제한하고, GNU `timeout`이 경과 시간 10초 후 워커를 종료합니다.

```sh
--8<-- "includes/read-in-worker.sh"
```

워크로드에 맞는 제한을 선택하고, 0이 아닌 종료 코드는 실패한 요청으로 처리하세요. 요청에 렌더링된 페이지가 필요할 때도 같은 워커에서 `pixmap()`을 호출할 수 있습니다. [상태 및 안전성](status.md#safety)도 함께 참고하세요.

## 제한 코드 {#limit-codes}

표는 영문 원문 그대로 제공합니다.

--8<-- "limit-codes.md"

## 오류

pyropdf가 발생시키는 모든 오류는 `PdfError`를 상속합니다.

| 오류 | 발생 조건 |
| --- | --- |
| `MalformedPdfError` | 입력이 읽을 수 있는 PDF가 아님. |
| `InternalError` | pyropdf 또는 엔진 내부에서 Rust 패닉이 발생함. 메시지에 패닉 내용이 유지되며, 라이브러리 결함으로 제보해 주세요. |
| `PasswordError` | 비밀번호가 올바르지 않음. |
| `EncryptedDocumentError` | 잠긴 문서를 `unlock` 전에 읽으려 함. |
| `LimitError` | 작업이 제한에 도달했거나 취소됨. `code`가 해당 제한을 가리킴. |
| `StalePageError`, `StaleObjectError` | 핸들이 가리키던 페이지나 객체가 사라진 뒤 사용됨. |
| `DocumentClosedError` | `close` 이후에 문서를 사용함. |
| `OcrError` | OCR 모델이 없거나 실패함. |

`InternalError`는 `except PdfError`와 `except Exception` 모두로 잡을 수 있습니다. 실패한 트랜잭션은 폐기되며 같은 문서를 계속 사용할 수 있습니다. 프로세스의 패닉 훅은 변경하지 않으므로 기본 진단 메시지는 그대로 표준 오류(stderr)에 출력됩니다. 이 경계는 언와인드(unwind)되는 패닉만 포착하며, 런타임이 즉시 중단(abort)되는 경우에는 복구할 수 없습니다.
