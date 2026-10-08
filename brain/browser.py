"""Controlled Playwright browser for JavaScript-rendered pages.

The model gets one operation, inspect(url): navigate, wait for rendering, and return a compact
text snapshot, the page title, console errors, failed requests and a network summary. Every
request the page makes, including redirects and subresources, is checked against the same web
policy as the broker and aborted if refused. The model cannot click, type, submit forms, or run
JavaScript; Coding Brain itself only reads rendered text. Downloads and permissions are denied.
"""
import os
import re
import time
from urllib.parse import urlparse
from .web_verification import Evidence, WebPolicy, WebPolicyError

SAFE_SCHEMES = {"data", "blob", "about"}


class BrowserInspector:
    def __init__(self, policy: WebPolicy, executable: str | None = None, proxy: str | None = None,
                 timeout_ms: int = 20_000, fixtures: dict | None = None):
        self.policy, self.timeout_ms = policy, timeout_ms
        self.executable = executable or os.environ.get("BRAIN_BROWSER_EXECUTABLE")
        self.proxy = proxy
        # fixtures: {url: (status, content_type, body)} for deterministic, offline inspection
        self.fixtures = fixtures

    def _allowed(self, url: str) -> bool:
        scheme = urlparse(url).scheme
        if scheme in SAFE_SCHEMES:
            return True
        try:
            self.policy.check_url(url)
            return True
        except WebPolicyError:
            return False

    async def inspect(self, url: str, max_chars: int = 6000, settle_ms: int = 1500) -> dict:
        self.policy.check_url(url)  # refuse before launching anything
        from playwright.async_api import async_playwright
        console, failed, refused, responses = [], [], [], []
        launch = {"headless": True}
        if self.executable:
            launch["executable_path"] = self.executable
        if self.proxy and not self.fixtures:
            launch["proxy"] = {"server": self.proxy}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch)
            try:
                context = await browser.new_context(accept_downloads=False, java_script_enabled=True,
                                                    service_workers="block", user_agent="CodingBrain/0.8")

                async def route(request_route):
                    target = request_route.request.url
                    if not self._allowed(target):
                        refused.append(target[:200])
                        return await request_route.abort("blockedbyclient")
                    if self.fixtures is not None:
                        if target in self.fixtures:
                            status, content_type, body = self.fixtures[target]
                            return await request_route.fulfill(status=status, content_type=content_type, body=body)
                        return await request_route.fulfill(status=404, body="Not recorded")
                    return await request_route.continue_()
                await context.route("**/*", route)
                page = await context.new_page()
                page.on("console", lambda message: console.append(f"{message.type}: {message.text}"[:300])
                        if message.type in ("error", "warning") else None)
                page.on("pageerror", lambda error: console.append(f"pageerror: {error}"[:300]))
                page.on("requestfailed", lambda request: failed.append(
                    f"{request.method} {request.url[:200]}: {request.failure}"))
                page.on("response", lambda response: responses.append((response.status, response.url)))
                started = time.time()
                try:
                    main = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                    await page.wait_for_timeout(settle_ms)
                except Exception as error:  # navigation errors become inconclusive evidence
                    return {"evidence": Evidence(url, None, None, started, "inconclusive",
                                                 f"{type(error).__name__}: {error}"[:300]).as_dict(),
                            "console": console[:20], "refused_requests": refused[:20]}
                status = main.status if main else None
                text = re.sub(r"\n\s*\n+", "\n\n", await page.inner_text("body"))[:max_chars]
                outcome = ("verified" if status and 200 <= status < 300 else
                           "failed" if status in (404, 410) else "inconclusive")
                evidence = Evidence(url, page.url, status, started, outcome, f"HTTP {status}" if status else "")
                return {"evidence": evidence.as_dict(), "title": (await page.title())[:200], "text": text,
                        "console": console[:20], "failed_requests": failed[:20], "refused_requests": refused[:20],
                        "network": {"requests": len(responses),
                                    "errors": [f"{code} {where[:200]}" for code, where in responses if code >= 400][:20]}}
            finally:
                await browser.close()
