"""Serve the compiled personal app from the API's origin."""

from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .config import Settings


def mount_frontend(app: FastAPI, settings: Settings) -> None:
    dist = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    index = dist / "index.html"
    if not index.is_file():
        if os.environ.get("RUNWISE_SERVE_FRONTEND", "").lower() == "true":
            raise RuntimeError("Personal deployment requires a built frontend/dist directory")
        return

    app.mount("/assets", StaticFiles(directory=dist / "assets"), name="frontend-assets")

    def index_response() -> FileResponse:
        return FileResponse(
            index,
            headers={
                "Cache-Control": "no-cache",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
            },
        )

    @app.get("/", include_in_schema=False)
    def frontend_index() -> FileResponse:
        return index_response()

    @app.get("/share/{token}", include_in_schema=False)
    def frontend_share(token: str) -> FileResponse:
        # The token is interpreted by the client/API, never as a file path.
        return index_response()
