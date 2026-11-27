---
description: Python다운 사용성과 Rust 코어를 결합한 PDF 라이브러리. Python에서 PDF를 읽고, 렌더링하고, 편집하고, 서명합니다. 모든 편집은 되돌릴 수 있는 버전이 되며, 실패한 편집은 문서를 원래 그대로 유지합니다.
home: true
hide:
  - navigation
  - toc
---

<section class="pp-hero pp-reel" aria-labelledby="pp-hero-title" markdown>
<div class="pp-hero__text">
  <h1 id="pp-hero-title"><span class="pp-accent">Python다운 사용성</span>과 <span class="pp-accent">Rust 코어</span>로 다루는 PDF.</h1>
  <p class="pp-lede">
    pyropdf는 PDF를 읽고, 렌더링하고, 편집하고, 서명합니다. 페이지는 시퀀스, 폼 필드는 매핑, 편집은 트랜잭션으로 다루며, 내부의 Rust 버전 저장소가 모든 변경을 되돌릴 수 있게 합니다.
  </p>
  <div class="pp-hero__start">
    <div class="pp-install" aria-label="설치 명령어">
    <span class="pp-install__prompt" aria-hidden="true">$</span>
    <code tabindex="0">pip install pyropdf</code>
    <button type="button" data-copy-command="pip install pyropdf" data-copied-label="복사됨" aria-label="설치 명령어 복사"><span data-copy-label>복사</span></button>
    </div>
    <p class="pp-actions">
    <a class="pp-button pp-button--primary" href="getting-started/">시작하기</a>
    <a class="pp-button pp-button--video" href="#pp-overview" data-jump-video><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.25 12.5 8 5 12.75z" fill="currentColor"/></svg>30초 둘러보기</a>
    <a class="pp-button" href="/api/" hreflang="en">API 레퍼런스</a>
  </p>
  </div>
</div>
  <div class="pp-reel__steps" role="radiogroup" aria-label="단계">
    <input type="radio" name="pp-step" id="pp-s1" value="1" checked>
    <label for="pp-s1"><span class="pp-reel__n">1</span><span class="pp-reel__title">읽기</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s2" value="2">
    <label for="pp-s2"><span class="pp-reel__n">2</span><span class="pp-reel__title">주석</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s3" value="3">
    <label for="pp-s3"><span class="pp-reel__n">3</span><span class="pp-reel__title">폼 채우기</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <input type="radio" name="pp-step" id="pp-s4" value="4">
    <label for="pp-s4"><span class="pp-reel__n">4</span><span class="pp-reel__title">스탬프</span><span class="pp-reel__bar" aria-hidden="true"></span></label>
    <button type="button" class="pp-reel__toggle" data-reel-toggle data-state="playing" data-pause-label="단계 자동 전환 일시정지" data-play-label="단계 자동 전환 재개" aria-label="단계 자동 전환 일시정지" title="단계 자동 전환 일시정지">
      <svg class="pp-reel__icon-pause" viewBox="0 0 16 16" aria-hidden="true"><rect x="4" y="3" width="2.75" height="10" rx="0.8" fill="currentColor"/><rect x="9.25" y="3" width="2.75" height="10" rx="0.8" fill="currentColor"/></svg>
      <svg class="pp-reel__icon-play" viewBox="0 0 16 16" aria-hidden="true"><path d="M5 3.25 12.5 8 5 12.75z" fill="currentColor"/></svg>
    </button>
  </div>
<div class="pp-reel__code" markdown>
<div class="pp-reel__panel" data-step="1" markdown>

```python title="read.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    words = doc.pages[0].words()
    for word in words[:3]:
        print(word.text, *(round(v) for v in word.bbox))
    print(len(words), "words")
```

<p class="pp-reel__label">출력</p>

```text
--8<-- "en/assets/hero/read.txt"
```

</div>
<div class="pp-reel__panel" data-step="2" markdown>

```python title="annotate.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("highlight totals"):
        hits = page.search("total")
        page.annotations.add(pyropdf.Highlight(hits))
    doc.save("output.pdf", incremental=True)
```

</div>
<div class="pp-reel__panel" data-step="3" markdown>

```python title="fill.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("approve"):
        field = pyropdf.TextField("approver", (30, 316, 170, 334))
        page.widgets.add(field)
        doc.form["approver"].value = "Ada Lovelace"
    doc.save("output.pdf", incremental=True)
```

</div>
<div class="pp-reel__panel" data-step="4" markdown>

```python title="stamp.py"
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("stamp"):
        box = (196, 300, 310, 346)
        stamp = pyropdf.Stamp(box, icon=pyropdf.StampIcon.APPROVED)
        page.annotations.add(stamp)
    doc.save("output.pdf", incremental=True)
```

</div>
</div>
<div class="pp-viewer" role="group" aria-label="송장의 각 리비전을 pyropdf로 렌더링해 보여 주는 PDF 뷰어">
  <div class="pp-viewer__bar">
    <span class="pp-viewer__file"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M4 1.5h5.5L13 5v9.5H4z" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/><path d="M9.5 1.5V5H13" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>invoice.pdf</span>
    <span class="pp-viewer__revs"><span class="pp-reel__rev" data-step="1">rev 1 · 열기 직후</span><span class="pp-reel__rev" data-step="2">rev 2 · highlight totals</span><span class="pp-reel__rev" data-step="3">rev 3 · approve</span><span class="pp-reel__rev" data-step="4">rev 4 · stamp</span></span>
    <span class="pp-viewer__page" aria-label="1페이지 중 1페이지">1 / 1</span>
    <a class="pp-viewer__open" href="/assets/hero/reviewed.pdf" title="reviewed.pdf 열기" aria-label="reviewed.pdf 열기"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="M9 2.5h4.5V7M13.5 2.5 7.5 8.5M12 9.5v4H2.5V4h4" fill="none" stroke="currentColor" stroke-width="1.3" stroke-linecap="round" stroke-linejoin="round"/></svg></a>
  </div>
  <div class="pp-viewer__canvas">
    <figure class="pp-viewer__sheet">
      <img class="pp-reel__frame" data-step="1" src="/assets/hero/revision-1.png" width="1057" height="1375" alt="pyropdf가 렌더링한 송장과 page.words()가 찾은 단어들의 경계 상자">
      <img class="pp-reel__frame" data-step="2" src="/assets/hero/revision-2.png" width="1057" height="1375" alt="Total 항목이 강조 표시된 송장의 pyropdf 렌더링 결과">
      <img class="pp-reel__frame" data-step="3" src="/assets/hero/revision-3.png" width="1057" height="1375" alt="Approved by 필드가 채워진 송장의 pyropdf 렌더링 결과">
      <img class="pp-reel__frame" data-step="4" src="/assets/hero/revision-4.png" width="1057" height="1375" alt="Approved 스탬프가 찍힌 송장의 pyropdf 렌더링 결과">
      <img class="pp-reel__words" data-step="1" src="/assets/hero/words.svg" width="340" height="442" alt="">
    </figure>
  </div>
</div>
<div class="pp-hero__foot">
  <ul class="pp-facts" aria-label="프로젝트 핵심 정보">
    <li>MIT 라이선스</li>
    <li>필수 의존성 없음</li>
    <li>CPython 3.11–3.15 (free-threaded 지원)</li>
  </ul>
<p class="pp-stage__caption">모든 프레임은 pyropdf가 직접 렌더링한 결과입니다. <a href="https://github.com/yhay81/pyropdf/blob/main/tools/hero_invoice.py">tools/hero_invoice.py</a>가 네 코드 조각을 앞 단계의 출력에 순서대로 실행하며, <a href="/assets/hero/reviewed.pdf">reviewed.pdf</a>에는 네 리비전이 모두 보존되어 있습니다.</p>
</div>
</section>

<section class="pp-section pp-showcase">
  <div class="pp-showcase__head">
    <header class="pp-section__head">
      <p class="pp-eyebrow">소개 영상</p>
      <h2 id="pp-overview">30초 만에 보는 pyropdf</h2>
      <p>실시간 단어 추출과 폼 스탬프 추가부터 O(1) 실행 취소, 예외 롤백, 원본 바이트를 보존하는 증분 저장까지 — 버전 관리 저장소가 동작하는 모습을 확인해 보세요.</p>
    </header>
  </div>
  <div class="pp-showcase__stage">
    <figure class="pp-video-figure">
      <div class="pp-video-frame">
        <div class="pp-video-frame__bar">
          <span class="pp-video-frame__title"><svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true"><path d="M13.5 0.67s.74 2.65.74 4.8c0 2.06-1.35 3.73-3.41 3.73-2.07 0-3.63-1.67-3.63-3.73l.03-.36C5.21 7.51 4 10.62 4 14c0 4.42 3.58 8 8 8s8-3.58 8-8C20 8.61 17.41 3.8 13.5.67zM11.71 19c-1.78 0-3.22-1.4-3.22-3.14 0-1.62 1.05-2.76 2.81-3.12 1.77-.36 3.6-1.21 4.62-2.58.39 1.29.59 2.65.59 4.04 0 2.65-2.15 4.8-4.8 4.8z"/></svg>pyropdf 소개 영상</span>
          <span class="pp-video-frame__spec">0:33 · 영어 음성 및 자막</span>
        </div>
        <video
          controls
          playsinline
          loading="lazy"
          preload="none"
          width="1280"
          height="720"
          poster="/assets/video/pyropdf-intro-poster.webp"
          aria-labelledby="pp-overview"
          aria-details="pp-intro-transcript"
        >
          <source src="/assets/video/pyropdf-intro.webm" type="video/webm">
          <source src="/assets/video/pyropdf-intro.mp4" type="video/mp4">
          <track src="/assets/video/pyropdf-intro-en.vtt" kind="captions" srclang="en" label="English" default>
          <p>사용 중인 브라우저가 임베드 비디오 재생을 지원하지 않습니다. <a href="/assets/video/pyropdf-intro.mp4">pyropdf 소개 영상(MP4) 다운로드</a>.</p>
        </video>
      </div>
      <figcaption>pyropdf의 실제 송장 렌더링, 버전 그래프 기반 실행 취소/다시 실행, 실패 시 롤백, 증분 바이트 스트림을 30초 내레이션으로 소개합니다(영어 음성 및 자막 제공).</figcaption>
    </figure>
    <div class="pp-showcase__rail">
      <p class="pp-rail__heading">챕터로 이동</p>
      <div class="pp-chapters" role="group" aria-label="영상 챕터">
        <button type="button" class="pp-chapter is-active" data-video-seek="2.89">
          <span class="pp-chapter__time">0:03</span>
          <span class="pp-chapter__body">
            <strong>Python다운 사용성, Rust 코어</strong>
            <span>파편화된 도구 대신 하나의 통합 아키텍처</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="6.53">
          <span class="pp-chapter__time">0:06</span>
          <span class="pp-chapter__body">
            <strong>읽기, 강조, 폼 채우기 및 스탬프</strong>
            <span>하나의 저장소에서 즉시 동기화되는 편집과 hayro 미리보기</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="14.55">
          <span class="pp-chapter__time">0:14</span>
          <span class="pp-chapter__body">
            <strong>원자적 실행 취소와 롤백</strong>
            <span>복사 없는 히스토리 이동과 예외 안전성</span>
          </span>
        </button>
        <button type="button" class="pp-chapter" data-video-seek="22.38">
          <span class="pp-chapter__time">0:22</span>
          <span class="pp-chapter__body">
            <strong>증분 저장과 MIT 라이선스</strong>
            <span>원본 바이트 보존과 유효한 PAdES 서명 유지</span>
          </span>
        </button>
      </div>
      <details id="pp-intro-transcript" class="pp-transcript">
        <summary>대본 보기 (영어)</summary>
        <div class="pp-transcript__body" lang="en">
          <p class="pp-transcript__cue">[Warm ambient synth pulse begins]</p>
          <p>Working with PDFs in Python shouldn't feel fragile. Meet pyropdf — Python ergonomics, powered by a Rust core.</p>
          <p>Read text, tables, and Markdown. Highlight matches, fill forms, stamp pages, and render live previews, all from one versioned object store.</p>
          <p>Every edit runs in an atomic transaction. Undo or redo in an instant, and if anything fails, your document stays untouched.</p>
          <p>Save incrementally to keep every original byte and signature. Zero required dependencies. MIT licensed. <code>pip install pyropdf</code>.</p>
          <p class="pp-transcript__cue">[Warm synth chord resolves and fades]</p>
        </div>
      </details>
    </div>
  </div>
</section>

<section class="pp-section">
  <header class="pp-section__head">
    <p class="pp-eyebrow">주요 기능</p>
    <h2 id="pp-features">문서 전체를 다루는 하나의 패키지</h2>
    <p>읽기, 렌더링, 편집이 하나의 저장소를 공유하므로, 읽는 결과는 언제나 방금 편집한 최신 버전입니다.</p>
  </header>
  <div class="pp-cards">
    <a class="pp-card" href="reading/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 3.5h7l3.5 3.5v9.5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1v-12a1 1 0 0 1 1-1Z"/><path d="M12 3.5V7h3.5M7 10.5h6M7 13.5h4.5"/></svg></span>
        <h3>읽기</h3>
        <p>읽기 순서 텍스트, 단어, 문자, 레이아웃, 검색, 표, Markdown 추출.</p>
        <code>page.markdown()</code>
      </article>
    </a>
    <a class="pp-card" href="reading/#offline-ocr">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M3.5 7V4.5a1 1 0 0 1 1-1H7M13 3.5h2.5a1 1 0 0 1 1 1V7M16.5 13v2.5a1 1 0 0 1-1 1H13M7 16.5H4.5a1 1 0 0 1-1-1V13"/><path d="M6.5 10h7M8 7.5h4M8 12.5h4"/></svg></span>
        <h3>스캔 문서 인식</h3>
        <p><code>ocr</code> extra의 PP-OCR 모델로 오프라인 OCR을 수행하고 보이지 않는 텍스트 레이어로 기록합니다.</p>
        <code>page.apply_ocr(engine=OcrEngine())</code>
      </article>
    </a>
    <a class="pp-card" href="rendering/">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><circle cx="7.5" cy="8.25" r="1.25"/><path d="m17 13-3.5-3.5L7.5 15.5"/></svg></span>
        <h3>렌더링</h3>
        <p>hayro 기반 픽스맵, PNG, JPEG, SVG 출력과 여러 페이지의 병렬 렌더링.</p>
        <code>page.pixmap(dpi=144)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#pages">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="4" y="4" width="8.5" height="11" rx="1.5"/><path d="M8.5 4V3a1 1 0 0 1 1-1H15a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1h-2.5"/></svg></span>
        <h3>페이지 편집</h3>
        <p>페이지 삽입, 가져오기, 이동, 회전, 자르기, 병합(flatten)과 메타데이터, 페이지 레이블, 북마크, 레이어 편집.</p>
        <code>page.rotation = 90</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#drawing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="m13.5 3.5 3 3-8.5 8.5-3.5.5.5-3.5 8.5-8.5Z"/><path d="m11.5 5.5 3 3"/></svg></span>
        <h3>그리기 및 주석</h3>
        <p>경로, 이미지, 페이지, 텍스트 그리기와 강조 표시, 메모, 도형, 링크, 자유 텍스트, 스탬프 추가.</p>
        <code>page.draw()</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#forms">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="14" height="12" rx="2"/><path d="M6.5 8.5h7M6.5 12h4.5"/></svg></span>
        <h3>폼 작성</h3>
        <p>필드 값을 읽고 설정하며, 위젯을 추가하고, 외형을 최신 상태로 유지합니다.</p>
        <code>doc.form["name"].value = "Ada"</code>
      </article>
    </a>
    <a class="pp-card" href="versions/#saving">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 2.75 16 5v4.75c0 3.7-2.55 6.45-6 7.5-3.45-1.05-6-3.8-6-7.5V5l6-2.25Z"/><path d="m7.5 10 1.75 1.75L13 8"/></svg></span>
        <h3>저장 및 서명</h3>
        <p>변경된 내용만 뒤에 추가하는 증분 저장, AES-256 암호화, 새 리비전에 기록되는 PAdES 서명.</p>
        <code>doc.sign("approval", signer)</code>
      </article>
    </a>
    <a class="pp-card" href="editing/#sanitizing">
      <article>
        <span class="pp-card__icon" aria-hidden="true"><svg viewBox="0 0 20 20" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"><path d="M10 3v3M10 14v3M3 10h3M14 10h3M5.2 5.2l2.1 2.1M12.7 12.7l2.1 2.1M14.8 5.2l-2.1 2.1M7.3 12.7l-2.1 2.1"/></svg></span>
        <h3>정리 및 최적화</h3>
        <p>공유하기 전에 JavaScript, 첨부 파일, 메타데이터, 숨겨진 텍스트를 제거하고 글꼴을 서브셋화합니다.</p>
        <code>doc.sanitize("shared.pdf")</code>
      </article>
    </a>
  </div>
</section>

<section class="pp-section pp-split" markdown>
<div class="pp-split__text">
  <header class="pp-section__head">
    <p class="pp-eyebrow">아키텍처</p>
    <h2 id="pp-versions">버전 위에 세워진 설계</h2>
    <p>모든 편집은 파싱된 사본을 제자리에서 수정하지 않고 문서의 새 버전을 기록합니다. 모든 보장은 이 구조에서 비롯됩니다.</p>
  </header>
  <ul class="pp-points">
    <li><span class="pp-points__n" aria-hidden="true">01</span><span class="pp-points__body"><strong>건드린 만큼만 비용이 듭니다.</strong>엔진은 다시 직렬화한 파일이 아니라 저장소를 직접 읽습니다. 페이지 하나를 회전하면 객체 하나만 기록합니다.</span></li>
    <li><span class="pp-points__n" aria-hidden="true">02</span><span class="pp-points__body"><strong>모든 변경을 되돌릴 수 있습니다.</strong>트랜잭션은 <code>doc.history</code>의 이름 붙은 한 단계가 되며, 실행 취소는 문서를 복사하지 않고 버전 사이를 이동합니다.</span></li>
    <li><span class="pp-points__n" aria-hidden="true">03</span><span class="pp-points__body"><strong>실패는 흔적을 남기지 않습니다.</strong>예외가 발생한 편집은 시작 시점의 버전을 그대로 유지하며, 제한 설정은 모든 작업량을 타입이 지정된 <code>LimitError</code>로 멈춥니다.</span></li>
  </ul>
  <p class="pp-more"><a href="versions/">버전과 저장</a><a href="concepts/">핵심 개념</a></p>
</div>
<div class="pp-split__code pp-version-card" markdown>
<div class="pp-version-card__bar" aria-hidden="true">
  <span class="pp-vnode">v0 <em>opened</em></span>
  <span class="pp-varrow">→</span>
  <span class="pp-vnode pp-vnode--active">v1 <em>rotate</em></span>
  <span class="pp-varrow">↶</span>
  <span class="pp-vnode pp-vnode--undo">v0 <em>undone</em></span>
</div>

```python
import pyropdf

with pyropdf.open("input.pdf") as doc:
    page = doc.pages[0]
    with doc.transaction("rotate"):
        page.rotation = 90
    print(doc.history.steps)   # ('rotate',)
    doc.history.undo()
    print(page.rotation)       # 0
```

</div>
</section>

<section class="pp-section pp-closing">
  <div class="pp-closing__box">
    <div class="pp-closing__text">
      <p class="pp-eyebrow">현황과 투명성</p>
      <h2 id="pp-adopt">도입하기 전에</h2>
      <p>API는 마이너 버전 사이에서 여전히 변경될 수 있으며, 렌더링 속도는 MuPDF보다 느립니다. hayro의 제한된 구조 생성(bounded construction)이 릴리스되기 전까지 pyropdf의 어떤 릴리스도 신뢰할 수 없는 입력에 대한 안전성을 주장하지 않습니다. 제한 설정이 보장하는 범위와 보장하지 않는 범위는 <a href="status/">상태 및 안전성</a>에서, PyMuPDF·pypdf·pikepdf와의 성능 측정 비교는 <a href="choosing/">pyropdf 선택하기</a>에서 확인할 수 있습니다.</p>
    </div>
    <div class="pp-closing__links">
      <a class="pp-button pp-button--primary" href="getting-started/">가이드 시작하기</a>
      <a class="pp-button" href="choosing/">라이브러리 비교</a>
    </div>
  </div>
</section>
