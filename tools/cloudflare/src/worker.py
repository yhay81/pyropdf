"""Private release fixture for the Cloudflare Python Workers fetch entry point."""

from __future__ import annotations

import sysconfig
from http import HTTPStatus
from platform import python_version
from urllib.parse import urlsplit

import pyodide  # ty: ignore[import-not-found]
from workers import Request, Response, WorkerEntrypoint  # ty: ignore[import-not-found]


class Default(WorkerEntrypoint):
    """Handle each release-smoke request with its own document."""

    async def fetch(self, request: Request) -> Response:
        """Inspect a PDF, append a page, or apply the tiny file-size limit."""
        # Native initialization obtains randomness, which Workers allows during requests.
        import pyropdf  # noqa: PLC0415

        path = urlsplit(request.url).path
        if path == "/runtime":
            return Response.from_json(
                {
                    "pyropdf": pyropdf.__version__,
                    "python": python_version(),
                    "pyodide": pyodide.__version__,
                    "platform": sysconfig.get_platform(),
                }
            )
        if request.method != "POST" or path not in {"/inspect", "/edit", "/limit"}:
            return Response(status=HTTPStatus.NOT_FOUND)

        data = await request.bytes()
        limits = pyropdf.DocumentLimits(max_file_size=1) if path == "/limit" else None
        try:
            with pyropdf.open(data, limits=limits) as doc:
                if path == "/edit":
                    doc.pages.new((100, 100))
                    return Response(doc.to_bytes(incremental=True), headers={"Content-Type": "application/pdf"})
                return Response.from_json({"pages": len(doc.pages), "text": doc.pages[0].text().strip()})
        except pyropdf.LimitError as error:
            return Response.from_json(
                {"error": type(error).__name__, "code": error.code}, status=HTTPStatus.REQUEST_ENTITY_TOO_LARGE
            )
