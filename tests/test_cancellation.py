"""CancellationToken: DocumentLimits.cancellation stops operations from any thread."""

from __future__ import annotations

import threading
import time

import pytest
from test_pages import numbered

import pyropdf
from pyropdf import CancellationToken, DocumentLimits


def large() -> bytes:
    """Return a document whose full save takes a while: 2,000 pages."""
    return numbered(2_000)


def test_a_token_starts_uncancelled_and_cancels_for_good() -> None:
    token = CancellationToken()
    assert not token.cancelled
    assert repr(token) == "<CancellationToken>"
    token.cancel()
    token.cancel()
    assert token.cancelled
    assert repr(token) == "<cancelled CancellationToken>"


def test_a_cancelled_token_refuses_open_and_every_later_operation() -> None:
    token = CancellationToken()
    doc = pyropdf.open(numbered(3), limits=DocumentLimits(cancellation=token))
    assert len(doc.to_bytes()) > 0
    token.cancel()
    with pytest.raises(pyropdf.LimitError) as caught:
        doc.to_bytes()
    assert caught.value.code == "cancelled"
    with pytest.raises(pyropdf.LimitError) as caught:
        pyropdf.open(numbered(3), limits=DocumentLimits(cancellation=token))
    assert caught.value.code == "cancelled"


def test_another_thread_cancels_an_operation_that_releases_the_gil() -> None:
    token = CancellationToken()
    doc = pyropdf.open(large(), limits=DocumentLimits(cancellation=token))
    outcome: list[BaseException | None] = []
    started = threading.Event()

    def save_until_cancelled() -> None:
        started.set()
        try:
            for _ in range(10_000):
                doc.to_bytes()
        except pyropdf.LimitError as error:
            outcome.append(error)
        else:
            outcome.append(None)

    worker = threading.Thread(target=save_until_cancelled)
    worker.start()
    started.wait()
    # The worker holds no GIL while it saves, so this thread runs meanwhile.
    time.sleep(0.05)
    token.cancel()
    worker.join(timeout=30)
    assert not worker.is_alive()
    assert len(outcome) == 1
    error = outcome[0]
    assert isinstance(error, pyropdf.LimitError)
    assert error.code == "cancelled"


def test_one_token_stops_several_documents_and_threads() -> None:
    token = CancellationToken()
    limits = DocumentLimits(cancellation=token)
    docs = [pyropdf.open(numbered(200), limits=limits) for _ in range(4)]
    codes: list[str] = []
    lock = threading.Lock()

    def work(doc: pyropdf.Document) -> None:
        try:
            while True:
                doc.to_bytes()
        except pyropdf.LimitError as error:
            with lock:
                codes.append(error.code)

    threads = [threading.Thread(target=work, args=(doc,)) for doc in docs]
    for thread in threads:
        thread.start()
    time.sleep(0.05)
    token.cancel()
    for thread in threads:
        thread.join(timeout=30)
    assert codes == ["cancelled"] * 4


def test_cancellation_must_be_a_token() -> None:
    with pytest.raises(TypeError, match="CancellationToken"):
        DocumentLimits(cancellation=True)  # ty: ignore[invalid-argument-type]
    assert DocumentLimits.web().cancellation is None
