"""Exercise the validator downloader against deterministic, local HTTP responses."""

from __future__ import annotations

import hashlib
import importlib.util
import threading
from contextlib import nullcontext
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("fetch_verapdf", ROOT / "tools/fetch_verapdf.py")
assert SPEC is not None
assert SPEC.loader is not None
DOWNLOADER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(DOWNLOADER)
JAR = b"test jar bytes"


@pytest.mark.parametrize(
    ("responses", "paths", "error"),
    [
        ([(429, b""), (200, JAR)], ["/first", "/first"], None),
        ([(404, b""), (200, JAR)], ["/first", "/second"], None),
        ([(404, b"")] * 2, ["/first", "/second"], "Could not download"),
        ([(503, b"")] * 4, ["/first", "/first", "/second", "/second"], "Could not download"),
        ([(200, b"wrong content")], ["/first"], "SHA-256 mismatch"),
    ],
    ids=["rate-limited", "fallback", "missing", "retry-limit", "wrong-digest"],
)
def test_only_verified_downloads_are_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    responses: list[tuple[int, bytes]],
    paths: list[str],
    error: str | None,
) -> None:
    requested: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            """Serve the next scripted response."""
            index = len(requested)
            requested.append(self.path)
            status, body = responses[index] if index < len(responses) else (404, b"")
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    monkeypatch.setenv("no_proxy", "127.0.0.1")
    destination = tmp_path / "validator.jar"
    previous = b"previous jar"
    destination.write_bytes(previous)
    with HTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            urls = tuple(f"http://127.0.0.1:{server.server_port}/{name}" for name in ("first", "second"))
            with pytest.raises(RuntimeError, match=error) if error else nullcontext():
                DOWNLOADER.fetch_jar(destination, urls, hashlib.sha256(JAR).hexdigest())
        finally:
            server.shutdown()
            thread.join()

    assert requested == paths
    assert destination.read_bytes() == (previous if error else JAR)
    assert list(tmp_path.iterdir()) == [destination]
