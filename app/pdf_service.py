"""HTML-to-PDF rendering backed by a single shared headless Chromium instance."""

from __future__ import annotations

import sys

from playwright.async_api import Browser, Playwright, async_playwright


class PdfRenderer:
    """Wraps one Playwright/Chromium instance reused across requests.

    Launching a browser per request is expensive, so a single instance is
    started at app startup (see the FastAPI lifespan in main.py) and closed
    at shutdown. Each render opens and closes its own page/tab.
    """

    def __init__(self) -> None:
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None

    async def start(self) -> None:
        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch()
        except NotImplementedError as exc:
            if sys.platform == "win32":
                raise RuntimeError(
                    "Failed to launch Chromium because the current asyncio event "
                    "loop does not support subprocesses. On Windows this happens "
                    "when uvicorn is started with --reload or --workers > 1 (both "
                    "force the SelectorEventLoop). Run uvicorn without those "
                    "flags, e.g. `python -m uvicorn app.main:app`."
                ) from exc
            raise

    async def stop(self) -> None:
        if self._browser is not None:
            await self._browser.close()
            self._browser = None
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    async def render(self, html: str) -> bytes:
        if self._browser is None:
            raise RuntimeError("PdfRenderer has not been started")
        page = await self._browser.new_page()
        try:
            await page.set_content(html, wait_until="networkidle")
            return await page.pdf(format="A4", print_background=True)
        finally:
            await page.close()
