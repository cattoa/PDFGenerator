"""Console-script entry point: runs the API with uvicorn using configured settings."""

from __future__ import annotations

import argparse

import uvicorn

from app.config import get_settings


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(prog="pdfgenerator", description="Run the PDF Generator API")
    parser.add_argument("--host", default=settings.host, help=f"Bind host (default: {settings.host})")
    parser.add_argument("--port", type=int, default=settings.port, help=f"Bind port (default: {settings.port})")
    args = parser.parse_args()

    # --reload/--workers are intentionally not exposed: on Windows they force the
    # SelectorEventLoop, which cannot launch the Playwright/Chromium subprocess.
    uvicorn.run("app.main:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
