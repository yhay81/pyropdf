---
title: 버전과 저장
description: 트랜잭션, 실행 취소와 다시 실행, 변경 보고서, 증분 저장과 전체 저장, 암호화 및 PAdES 서명.
---

# 버전과 저장

편집과 핸들의 기반이 되는 버전 모델은 [핵심 개념](concepts.md)을 참고하세요.

## 트랜잭션

`doc.transaction(name)`은 블록 안의 편집을 하나의 단계로 묶습니다. 블록에서 예외가 발생하면 문서는 복사 없이 이전 버전으로 되돌아갑니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    try:
        with doc.transaction("rotate"):
            page.rotation = 90
            raise RuntimeError("changed my mind")
    except RuntimeError:
        pass
    print(page.rotation)  # 0: nothing of the block stayed
```

트랜잭션 바깥의 편집은 각각 독립된 한 단계가 됩니다.

## 실행 취소와 다시 실행

`doc.history`는 각 단계를 이름순으로 보관하며, `undo`와 `redo`로 단계 사이를 이동합니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate"):
        page.rotation = 90
    with doc.transaction("note"):
        page.annotations.add(pyropdf.Note((40, 40), contents="Later"))
    print(doc.history.steps, doc.history.position)  # ('rotate', 'note') 2
    doc.history.undo()
    doc.history.undo()
    print(page.rotation, len(page.annotations))      # 0 0
    doc.history.redo()
    print(page.rotation)                             # 90
```

## 저장 {#saving}

`doc.save(path)`와 `doc.to_bytes()`는 현재 버전을 기록합니다.

- **증분 저장**(`incremental=True`)은 편집으로 기록된 객체만 원본 바이트 뒤에 덧붙입니다. 원본 바이트는 그대로 유지되므로 원본 바이트에 적용된 전자 서명이 유효하게 남습니다.
- **전체 저장**(기본값)은 파일 전체를 새로 작성합니다. `compress`, `object_streams`, `prune`, `renumber`, `dedupe` 옵션으로 출력을 제어할 수 있습니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    doc.save("appended.pdf", incremental=True)
    doc.save("rewritten.pdf", compress=True, object_streams=True, prune=True)
```

편집 없이 저장한 문서는 입력을 그대로 재현합니다. 증분 저장은 바이트 단위까지 일치하며, 전체 저장은 모든 값이 일치합니다.

## 변경 보고서

증분 저장은 기존 서명이 보호하는 바이트를 보존합니다. `report.signatures`는 해당 바이트 범위 안의 객체에 가해진 편집을 식별합니다. 전체 저장은 서명된 바이트를 다시 쓰므로 문서를 열었을 때 존재하던 모든 서명을 나열합니다.

증분 저장의 경우 `report.permissions`는 적용 가능한 각 서명의 DocMDP 인증 또는 FieldMDP 잠금 아래에서 예정된 각 편집의 허용 여부를 설명합니다. 각 항목에는 서명, 변환(transform) 종류, 편집 유형, 객체 번호, 영향을 받는 폼 필드가 담깁니다. 분류된 편집에서는 `permitted`가 `True` 또는 `False`가 되며, `None`은 추가 검토가 필요함을 뜻합니다. 이 보고서는 문서를 연 이후의 편집만 다루며, 원본 서명 검증이나 이전 리비전 평가는 별개의 작업입니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    report = doc.changes(incremental=True)
    for effect in report.permissions:
        print(effect.signature, effect.transform, effect.field, effect.permitted, effect.reason)
```

P=1은 폼 작성을 금지하고, P=2는 폼 작성과 서명을 허용하며, P=3은 마크업 주석 변경까지 허용합니다. DocMDP가 허용하는 폼 작성이라도 FieldMDP 잠금에 의해 금지될 수 있습니다. 이 보고서는 참고용이며 검증기마다 더 엄격한 정책을 적용할 수 있습니다. 예를 들어 pyHanko의 기본 차이 분석 정책은 P=3에서도 마크업 변경을 문제로 표시합니다([pyHanko 차이 분석](https://docs.pyhanko.eu/en/latest/api-docs/pyhanko.sign.diff_analysis.html) 참고).

`report.claims`는 편집으로 인해 깨질 수 있는 PDF/A 및 PDF/UA 규격 선언을 나열합니다.

## 암호화

`Encryption`은 비밀번호, 권한 및 암호화 방식을 설정하며 기본 방식은 AES-256입니다. 암호화된 문서는 `pyropdf.open(..., password=...)` 또는 `doc.unlock`으로 엽니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.save(
        "locked.pdf",
        encryption=pyropdf.Encryption(user_password="reader", owner_password="owner"),
    )

with pyropdf.open("locked.pdf", password="reader") as doc:
    print(doc.encryption_method, doc.permissions)
```

`Encryption` 또는 `RecipientEncryption`에서 `method="aes-gcm"`을 지정하면 ISO/TS 32003:2023 확장이 적용된 PDF 2.0 파일로 저장합니다. 저장소는 암호화된 문자열과 스트림을 반환하기 전에 인증 태그를 검증하며, 인증 태그가 손상된 경우 `MalformedPdfError`를 발생시킵니다. 증분 업데이트와 `Encryption.KEEP`을 지정한 전체 저장 모두 수정되지 않은 객체의 암호문을 그대로 유지합니다. 다른 암호화 방식으로 변경하려면 전체 저장이 필요합니다. AES-GCM 파일의 직접 페이지 렌더링과 텍스트 추출은 hayro의 인증된 객체 소스 지원을 기다리고 있습니다([엔진 통합 #968](https://github.com/yhay81/pyropdf/issues/968)).

256비트 비밀번호 또는 수신자 암호화 옵션에 `integrity=True`를 전달하면 ISO/TS 32004:2024 문서 MAC을 추가할 수 있습니다. 출력 파일을 열어 `doc.verify_integrity()`를 호출하면 원본 바이트를 검증하며, 상태는 `"valid"`, `"invalid"`, `"absent"`, `"locked"` 중 하나를 반환합니다. `"valid"` 결과는 단독 토큰인지, 아니면 서명 또는 문서 타임스탬프에 부착된 토큰인지를 함께 식별합니다. 서명 신뢰성 검증은 `signature.verify()`가 보고합니다.

유효한 MAC이 있는 문서에서 증분 편집을 수행하면 원본 리비전을 유지한 채 새 단독 토큰을 기록하며, 서명과 타임스탬프는 새 토큰을 서명에 부착합니다. `Encryption.KEEP`을 지정한 전체 저장은 단독 보호를 갱신하고, 새 256비트 암호화로 교체할 때도 새 MAC을 요청할 수 있습니다. 서명된 MAC 리비전의 전체 저장, 보호 제거, MAC 보호 출력의 선형화(linearization)는 쓰기 전에 거부됩니다.

### 표준 비밀번호 암호화 {#standard-password-encryption}

아래 표는 표준 비밀번호 핸들러를 정리한 것입니다. `V`와 `R`은 암호화 사전의 알고리즘 버전과 리비전입니다. 나열된 모든 형식은 사용자 비밀번호와 소유자 비밀번호 둘 다로 열 수 있습니다. 사용자 비밀번호로 열었을 때 보고되는 권한은 파일에 기록된 값을 따르며, 소유자 비밀번호는 모든 권한을 부여합니다.

| 기존 핸들러 | V / R | 전체 저장 시 새 비밀번호 암호화 | 직접 페이지 렌더링 |
| --- | --- | --- | --- |
| RC4, 40 bit | 1 / 2 | 미지원 | 사용자 또는 소유자 비밀번호 |
| RC4, 128 bit | 2 / 3 | 미지원 | 사용자 또는 소유자 비밀번호 |
| RC4, V2 crypt filter | 4 / 4 | 미지원 | 사용자 또는 소유자 비밀번호 |
| AES-128, AESV2 | 4 / 4 | `method="aes-128"` | 사용자 또는 소유자 비밀번호 |
| AES-256, AESV3 | 5 / 5 | 미지원 | 사용자 또는 소유자 비밀번호 |
| AES-256, AESV3 | 5 / 6 | `method="aes-256"` (기본값) | 사용자 또는 소유자 비밀번호 |
| AES-GCM, AESV4 | 6 / 7 | `method="aes-gcm"` | [엔진 지원 대기 중](https://github.com/yhay81/pyropdf/issues/968) |

이 일곱 가지 핸들러 모두에서 `Encryption.KEEP`을 지정한 전체 저장은 수정되지 않은 암호문을 복사하고 새 문자열과 스트림만 기존 핸들러로 암호화합니다. 증분 저장도 기본적으로 동일하게 동작하며 원본 파일을 앞부분으로 유지합니다(편집이 없으면 원본 바이트 그대로 반환). 기본값인 `encryption=None`으로 전체 저장하면 암호화가 제거됩니다.

바이트 리더는 UTF-8 자격 증명을 받습니다. 리비전 2~4에서는 유효 사용자 비밀번호를 UTF-8로 전달할 수 있는 경우 소유자 접근도 함께 동작합니다. 비-UTF-8 사용자 비밀번호를 가진 구형 파일도 소유자 인증 후 편집하고 저장할 수 있지만, 해당 인코딩에서는 직접 렌더링과 텍스트 추출 시 `EncryptedDocumentError`가 발생합니다([바이트 인코딩 자격 증명 #1043](https://github.com/yhay81/pyropdf/issues/1043)).

## 서명 {#signing}

### 서명 알고리즘 {#signature-algorithms}

아래는 ISO/TS 32002:2022 표 3·4에 정의된 PDF 2.0 타원 곡선과 문서 다이제스트 조합입니다. ISO/TS 32001:2022는 서명 및 타임스탬프 다이제스트에 SHA3-256, SHA3-384, SHA3-512, SHAKE256을 추가합니다. 서명자 또는 `Document.timestamp`에서 `digest_algorithm=`으로 다이제스트를 선택할 수 있으며, 확장 알고리즘을 사용하면 pyropdf가 필요한 ISO 확장 항목을 자동으로 기록합니다. SHAKE256은 64바이트를 생성합니다.

| 서명 키 | 허용되는 문서 다이제스트 | 개인 키 연산 |
| --- | --- | --- |
| NIST P-256 | `sha256`, `sha3_256` | PEM, PKCS#12 또는 콜백 |
| NIST P-384 | `sha384`, `sha3_384` | PEM, PKCS#12 또는 콜백 |
| NIST P-521 | `sha512`, `sha3_512` | PEM, PKCS#12 또는 콜백 |
| Brainpool P256r1 | `sha256`, `sha384`, `sha512`, `sha3_256`, `sha3_384`, `sha3_512` | PEM, PKCS#12 또는 콜백 |
| Brainpool P384r1 | `sha384`, `sha512`, `sha3_384`, `sha3_512` | PEM, PKCS#12 또는 콜백 |
| Brainpool P512r1 | `sha512`, `sha3_512` | PEM, PKCS#12 또는 콜백 |
| Ed25519 | `sha512` | PEM, PKCS#12 또는 콜백, pure Ed25519 |
| Ed448 | `shake256` | PEM, PKCS#12 또는 콜백, pure Ed448 |

`Signature.verify`는 PDF 바이트 범위의 다이제스트와 서명 속성에 대한 서명을 검증합니다. P-521, Brainpool 및 Edwards 키를 불러오고 검증하려면 `cryptography`를 설치하세요. RSA 콜백에서도 PKCS#1 v1.5(RFC 9688)와 함께 세 가지 SHA-3 다이제스트를 사용할 수 있습니다. Ed448의 경우 속성이 있는 CMS는 정수 매개변수 512를 갖는 `id-shake256-len`을 지정하며, 속성이 없는 CMS는 매개변수가 생략된 `id-shake256`을 사용합니다. Ed25519에 대응하는 다이제스트는 매개변수가 생략된 SHA-512입니다. ISO/TS 32001은 서명 필드 시드 사전의 `/DigestMethod` 이름을 확장하며, 정오표 236(Erratum 236)은 서명 참조 사전에 대한 제안 변경을 제거합니다.

`tests/test_signing.py`와 `tests/test_verify.py`의 재현 가능한 픽스처는 표의 각 행에 대한 키와 인증서를 생성합니다. 20가지 조합 모두에 대해 기존 필드를 채우고 다시 서명한 뒤, pyHanko 0.38.2에서 두 리비전을 모두 검증하고 독립적으로 서명된 PDF도 함께 확인합니다. pyHanko의 Ed25519 픽스처 인코더는 RFC 8419에서 생략을 요구하는 SHA-512 매개변수를 명시적으로 생략합니다. 별도 테스트에서는 명시적 NULL 거부 동작과 로컬 RFC 3161 기관을 사용한 네 가지 확장 타임스탬프 다이제스트를 검증합니다. pyHanko와 그 ASN.1 코덱 및 암호화 백엔드 버전은 `uv.lock`에 고정되어 있습니다.

`Signer.from_pem`과 `Signer.from_pkcs12`는 표의 키와 인증서 체인을 불러오며, `Signer.callback`은 외부 키, HSM 또는 PKCS#11 토큰에 개인 키 연산을 위임합니다. `doc.sign(field, signer)`는 CMS / PAdES 서명(`B-B`, 또는 `timestamper` 콜백이 RFC 3161 토큰을 반환하면 `B-T`)을 새 증분 리비전에 기록하므로 이전에 서명된 바이트가 그대로 유지되며, 서명된 PDF 바이트를 반환합니다. `doc.signatures`는 열린 파일의 서명된 필드를 나열하고, `sig.verify(trust=...)`는 각 서명, 타임스탬프 및 이후 수정 사항을 오프라인으로 검증합니다.

```python
from pathlib import Path

import pyropdf


def sign_pdf(key_pem: bytes, cert_pem: bytes) -> None:
    signer = pyropdf.Signer.from_pem(key_pem, cert_pem)
    with pyropdf.open("input.pdf") as doc:
        signed = doc.sign("approval", signer, reason="Approved")
    Path("signed.pdf").write_bytes(signed)


with pyropdf.open("input.pdf") as doc:
    for sig in doc.signatures:
        check = sig.verify()
        print(sig.field, check.intact, check.valid, check.modifications)
```

## 리비전 {#revisions}

`doc.revisions`는 열린 PDF의 증분 파일 리비전을 오래된 순(`0 .. n - 1`, `sig.revision`과 일치)으로 나열합니다. 각 `Revision`은 `end`와 함께 `added`, `updated`, `freed`, `objects`에 간접 객체 번호를 보고하며, `rev.open()`은 파일 바이트를 공유하는 독립적인 `Document`로 해당 리비전을 엽니다. 예를 들어 `doc.revisions[sig.revision].open()`은 `sig`가 서명한 정확한 시점의 문서 상태를 엽니다.

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    doc.pages[0].rotation = 90
    updated = doc.to_bytes(incremental=True)

with pyropdf.open(updated) as doc:
    for rev in doc.revisions:
        with rev.open() as earlier:
            print(rev.index, rev.updated, earlier.pages[0].rotation)
```
