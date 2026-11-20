"""Send the release contract to a Worker through a loopback Wrangler session."""

from __future__ import annotations

import hashlib
import json
import sys
from http import HTTPStatus
from http.client import HTTPConnection
from pathlib import Path

from smoke_artifact import text_pdf


def check(port: int, wheel: Path) -> None:
    """Verify installed runtime, request isolation, PDF bytes and the typed refusal."""

    def send(path: str, data: bytes | None = None, status: HTTPStatus = HTTPStatus.OK) -> bytes:
        client = HTTPConnection("127.0.0.1", port, timeout=60)
        try:
            client.request("GET" if data is None else "POST", path, data, {"Content-Type": "application/pdf"})
            response = client.getresponse()
            body = response.read()
            if response.status != status:
                msg = f"{path} returned HTTP {response.status}, expected {status.value}: {body[:500]!r}"
                raise RuntimeError(msg)
            return body
        finally:
            client.close()

    runtime = json.loads(send("/runtime"))
    expected = {
        "pyropdf": wheel.name.split("-")[1],
        "python": "3.14.2",
        "pyodide": "314.0.6",
        "platform": "emscripten-5.0.3-wasm32",
    }
    if runtime != expected:
        msg = f"Unexpected Workers runtime: {runtime!r} (expected {expected!r})"
        raise RuntimeError(msg)
    first = "First Worker request"
    second = "Second Worker request"
    source = text_pdf(first)
    edited = send("/edit", source)
    if len(edited) <= len(source) or not edited.startswith(source):
        msg = "The incremental PDF response did not append to the request bytes"
        raise RuntimeError(msg)
    if json.loads(send("/inspect", edited)) != {"pages": 2, "text": first}:
        msg = "The saved PDF lost the added page or the original text"
        raise RuntimeError(msg)
    refusal = json.loads(send("/limit", source, HTTPStatus.REQUEST_ENTITY_TOO_LARGE))
    if refusal != {"error": "LimitError", "code": "file_size"}:
        msg = f"Unexpected typed refusal: {refusal!r}"
        raise RuntimeError(msg)
    if json.loads(send("/inspect", text_pdf(second))) != {"pages": 1, "text": second}:
        msg = "The next request returned the wrong document state"
        raise RuntimeError(msg)
    sys.stdout.write(f"PASS {wheel.name} sha256={hashlib.sha256(wheel.read_bytes()).hexdigest()} {runtime}\n")


if __name__ == "__main__":
    check(int(sys.argv[1]), Path(sys.argv[2]))
