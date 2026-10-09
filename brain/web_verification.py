"""Phase 2 Web Verification Broker: the only path from Coding Brain to the internet.

Policy is enforced here, not by the model: HTTPS only, a host allowlist, DNS answers checked
against private, loopback, link-local and metadata ranges on every hop, redirects re-validated,
read-only methods, robots.txt honored, size, time and rate limits, no ambient credentials, and
a cache with ETag revalidation. Every answer is an Evidence record with the actual URL, time,
status, hash, and an outcome of verified, failed or inconclusive. Fetched content is untrusted.

The Docker test sandbox remains offline; this broker runs in the orchestrator process.
"""
import asyncio
import fnmatch
import hashlib
import ipaddress
import json
import os
import re
import socket
import sqlite3
import threading
import time
from dataclasses import dataclass, asdict, field
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser
import httpx

USER_AGENT = "CodingBrain/0.8 (+verification; respects robots.txt)"
METADATA = {"169.254.169.254", "fd00:ec2::254", "100.100.100.200", "metadata.google.internal"}


class WebPolicyError(PermissionError):
    """A request was refused by policy before any network traffic."""


@dataclass
class Evidence:
    url: str
    final_url: str | None
    status: int | None
    retrieved_at: float
    outcome: str  # verified | failed | inconclusive
    reason: str = ""
    sha256: str | None = None
    etag: str | None = None
    content_type: str | None = None
    from_cache: bool = False
    changed: bool | None = None
    redirects: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass
class WebPolicy:
    allow: list[str]
    max_bytes: int = 2_000_000
    timeout: float = 20.0
    max_redirects: int = 5
    min_interval: float = 1.0
    respect_robots: bool = True
    resolver: object = None  # callable(host) -> list[str]; injectable for tests

    def check_url(self, url: str) -> str:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname:
            raise WebPolicyError("Only https URLs are allowed")
        if parsed.username or parsed.password:
            raise WebPolicyError("URLs must not carry credentials")
        host = parsed.hostname.lower().rstrip(".")
        if host in METADATA or host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
            raise WebPolicyError(f"{host} is an internal address")
        if parsed.port not in (None, 443):
            raise WebPolicyError("Only the default https port is allowed")
        if not any(fnmatch.fnmatch(host, pattern) for pattern in self.allow):
            raise WebPolicyError(f"{host} is not on the web allowlist")
        self.check_addresses(host)
        return host

    def check_addresses(self, host: str):
        try:
            literal = ipaddress.ip_address(host)
            addresses = [str(literal)]
        except ValueError:
            resolve = self.resolver or (lambda name: [item[4][0] for item in socket.getaddrinfo(name, 443)])
            try:
                addresses = resolve(host)
            except OSError as error:
                raise WebPolicyError(f"Cannot resolve {host}: {error}") from error
        for address in addresses:
            ip = ipaddress.ip_address(address)
            if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
                    or ip.is_unspecified or str(ip) in METADATA):
                raise WebPolicyError(f"{host} resolves to a non-public address")


class TextExtractor(HTMLParser):
    """Compact technical text from HTML: title, headings, paragraphs, list items, code, links."""
    SKIP = {"script", "style", "nav", "footer", "header", "noscript", "svg", "form", "aside"}
    BLOCK = {"p", "li", "pre", "h1", "h2", "h3", "h4", "tr", "dt", "dd", "blockquote"}

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base, self.skip, self.parts, self.links, self.title, self._in_title = base, 0, [], [], "", False
        self.tag_stack = []

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skip += 1
        if tag == "title":
            self._in_title = True
        if tag in self.BLOCK:
            prefix = {"h1": "# ", "h2": "## ", "h3": "### ", "h4": "#### ", "li": "- ", "pre": "```\n"}.get(tag, "")
            self.parts.append("\n" + prefix)
        if tag == "a" and not self.skip:
            href = dict(attrs).get("href")
            if href and not href.startswith(("#", "javascript:", "mailto:")):
                self.links.append(urljoin(self.base, href))
        self.tag_stack.append(tag)

    def handle_endtag(self, tag):
        if tag in self.SKIP and self.skip:
            self.skip -= 1
        if tag == "title":
            self._in_title = False
        if tag == "pre":
            self.parts.append("\n```")
        if tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
        elif not self.skip:
            self.parts.append(data)

    def text(self) -> str:
        joined = "".join(self.parts)
        joined = re.sub(r"[ \t]+", " ", joined)
        return re.sub(r"\n\s*\n+", "\n\n", joined).strip()


def extract(html: str, base: str, max_chars: int = 6000) -> dict:
    parser = TextExtractor(base)
    try:
        parser.feed(html)
    except Exception:  # malformed markup still yields partial text
        pass
    return {"title": parser.title[:200], "text": parser.text()[:max_chars],
            "links": list(dict.fromkeys(parser.links))[:50]}


class WebCache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS pages (url TEXT PRIMARY KEY, final_url TEXT, status INTEGER, "
                       "fetched_at REAL, sha256 TEXT, etag TEXT, last_modified TEXT, content_type TEXT, "
                       "body BLOB, previous_sha256 TEXT)")

    def get(self, url: str) -> dict | None:
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT url, final_url, status, fetched_at, sha256, etag, last_modified, "
                             "content_type, body, previous_sha256 FROM pages WHERE url=?", (url,)).fetchone()
        keys = ("url", "final_url", "status", "fetched_at", "sha256", "etag", "last_modified",
                "content_type", "body", "previous_sha256")
        return dict(zip(keys, row)) if row else None

    def put(self, record: dict):
        previous = self.get(record["url"])
        with sqlite3.connect(self.path) as db:
            db.execute("INSERT OR REPLACE INTO pages VALUES (?,?,?,?,?,?,?,?,?,?)", (
                record["url"], record["final_url"], record["status"], record["fetched_at"], record["sha256"],
                record.get("etag"), record.get("last_modified"), record.get("content_type"), record["body"],
                previous["sha256"] if previous and previous["sha256"] != record["sha256"]
                else (previous or {}).get("previous_sha256")))


class WebBroker:
    def __init__(self, policy: WebPolicy, cache: WebCache, ttl: float = 86400, transport=None,
                 proxy: str | None = None, telemetry=None):
        self.policy, self.cache, self.ttl = policy, cache, ttl
        self.transport, self.telemetry = transport, telemetry
        self.proxy = proxy
        self.robots, self.last_request, self.lock = {}, {}, threading.Lock()
        self.stats = {"network_requests": 0, "cache_hits": 0, "refused": 0}

    def _client(self):
        # trust_env=False: never pick up netrc credentials or ambient auth from the environment.
        return httpx.AsyncClient(timeout=self.policy.timeout, follow_redirects=False, trust_env=False,
                                 transport=self.transport, proxy=None if self.transport else self.proxy,
                                 verify=os.environ.get("SSL_CERT_FILE") or True,
                                 headers={"User-Agent": USER_AGENT})

    async def _pace(self, host: str):
        # A thread lock reserves the next slot per host, so pacing holds across event loops.
        with self.lock:
            slot = max(time.monotonic(), self.last_request.get(host, 0) + self.policy.min_interval)
            self.last_request[host] = slot
        if slot > time.monotonic():
            await asyncio.sleep(slot - time.monotonic())

    async def _allowed_by_robots(self, client, url: str) -> bool:
        if not self.policy.respect_robots:
            return True
        parsed = urlparse(url)
        root = f"{parsed.scheme}://{parsed.netloc}"
        if root not in self.robots:
            parser = RobotFileParser()
            try:
                response = await client.get(root + "/robots.txt")
                self.stats["network_requests"] += 1
                parser.parse(response.text.splitlines() if response.status_code == 200 else [])
            except httpx.HTTPError:
                parser.parse([])
            self.robots[root] = parser
        return self.robots[root].can_fetch(USER_AGENT, url)

    async def fetch(self, url: str, method: str = "GET", max_age: float | None = None,
                    crawl: bool = True) -> tuple[Evidence, bytes]:
        """Fetch with policy, cache and evidence. Only GET and HEAD are permitted.

        robots.txt governs crawling pages (crawl=True). Deliberate calls to documented,
        programmatic APIs such as package registries pass crawl=False."""
        method = method.upper()
        if method not in {"GET", "HEAD"}:
            self.stats["refused"] += 1
            raise WebPolicyError(f"{method} has side effects and requires explicit authorization")
        try:
            self.policy.check_url(url)
        except WebPolicyError:
            self.stats["refused"] += 1
            raise
        cached = self.cache.get(url)
        age_limit = self.ttl if max_age is None else max_age
        if cached and time.time() - cached["fetched_at"] < age_limit:
            self.stats["cache_hits"] += 1
            return self._evidence(url, cached, from_cache=True), cached["body"] or b""
        async with self._client() as client:
            if crawl and not await self._allowed_by_robots(client, url):
                return Evidence(url, None, None, time.time(), "inconclusive", "Disallowed by robots.txt"), b""
            current, redirects = url, []
            for _ in range(self.policy.max_redirects + 1):
                host = self.policy.check_url(current)  # every hop is re-validated
                await self._pace(host)
                headers = {}
                if cached and current == url:
                    if cached.get("etag"):
                        headers["If-None-Match"] = cached["etag"]
                    if cached.get("last_modified"):
                        headers["If-Modified-Since"] = cached["last_modified"]
                try:
                    async with client.stream(method, current, headers=headers) as response:
                        self.stats["network_requests"] += 1
                        if response.status_code in (301, 302, 303, 307, 308) and "location" in response.headers:
                            redirects.append({"from": current, "status": response.status_code})
                            current = urljoin(current, response.headers["location"])
                            continue
                        if response.status_code == 304 and cached:
                            cached["fetched_at"] = time.time()
                            self.cache.put(cached)
                            return self._evidence(url, cached, from_cache=True, revalidated=True), cached["body"] or b""
                        body = b""
                        async for chunk in response.aiter_bytes():
                            body += chunk
                            if len(body) > self.policy.max_bytes:
                                return Evidence(url, current, response.status_code, time.time(), "inconclusive",
                                                "Response exceeded the size limit", redirects=redirects), b""
                        record = {"url": url, "final_url": current, "status": response.status_code,
                                  "fetched_at": time.time(), "sha256": hashlib.sha256(body).hexdigest(),
                                  "etag": response.headers.get("etag"),
                                  "last_modified": response.headers.get("last-modified"),
                                  "content_type": response.headers.get("content-type"), "body": body}
                        self.cache.put(record)
                        evidence = self._evidence(url, record, redirects=redirects)
                        evidence.changed = bool(cached) and cached["sha256"] != record["sha256"]
                        return evidence, body
                except httpx.HTTPError as error:
                    return Evidence(url, current, None, time.time(), "inconclusive",
                                    f"{type(error).__name__}: {error}"[:300], redirects=redirects), b""
            return Evidence(url, current, None, time.time(), "inconclusive", "Too many redirects",
                            redirects=redirects), b""

    @staticmethod
    def _evidence(url, record, from_cache=False, redirects=None, revalidated=False) -> Evidence:
        status = record["status"]
        if 200 <= status < 300:
            outcome, reason = "verified", "OK"
        elif status in (404, 410):
            outcome, reason = "failed", "Not found" if status == 404 else "Gone"
        else:
            outcome, reason = "inconclusive", f"HTTP {status}"
        if redirects and outcome == "verified":
            reason = f"Moved: {url} now resolves to {record['final_url']}"
        return Evidence(url, record["final_url"], status, record["fetched_at"], outcome, reason,
                        record["sha256"], record.get("etag"), record.get("content_type"), from_cache,
                        False if from_cache else None, redirects or [])

    async def check(self, url: str, crawl: bool = True) -> Evidence:
        evidence, _ = await self.fetch(url, crawl=crawl)
        return evidence

    async def json(self, url: str, max_age: float | None = None) -> tuple[Evidence, object]:
        """Fetch a documented JSON API (not crawling) and parse it."""
        evidence, body = await self.fetch(url, max_age=max_age, crawl=False)
        if evidence.outcome != "verified":
            return evidence, None
        try:
            return evidence, json.loads(body)
        except ValueError:
            evidence.outcome, evidence.reason = "inconclusive", "Response is not valid JSON"
            return evidence, None

    async def text(self, url: str, max_chars: int = 6000) -> dict:
        """Fetch and compact a page or document for a model: never a full dump."""
        evidence, body = await self.fetch(url)
        content = body.decode("utf-8", errors="replace")
        if "json" in (evidence.content_type or "") or content.lstrip().startswith(("{", "[")):
            summary = {"title": "", "text": content[:max_chars], "links": []}
        elif "html" in (evidence.content_type or "") or "<html" in content[:500].lower():
            summary = extract(content, evidence.final_url or url, max_chars)
        else:
            summary = {"title": "", "text": content[:max_chars], "links": []}
        return {"evidence": evidence.as_dict(), **summary}


def build_broker(data: Path, transport=None) -> WebBroker | None:
    """Web verification is off unless BRAIN_WEB_ALLOWLIST names the hosts to allow."""
    allow = [item.strip().lower() for item in os.environ.get("BRAIN_WEB_ALLOWLIST", "").split(",") if item.strip()]
    if not allow:
        return None
    policy = WebPolicy(allow, max_bytes=int(os.environ.get("BRAIN_WEB_MAX_BYTES", "2000000")),
                       respect_robots=os.environ.get("BRAIN_WEB_RESPECT_ROBOTS", "true").lower() != "false")
    fixtures = os.environ.get("BRAIN_WEB_FIXTURES")
    if fixtures and transport is None:
        transport = FixtureTransport(Path(fixtures), record=os.environ.get("BRAIN_WEB_RECORD") == "true",
                                     proxy=os.environ.get("HTTPS_PROXY"))
    return WebBroker(policy, WebCache(data / "web-cache.sqlite3"),
                     ttl=float(os.environ.get("BRAIN_WEB_TTL_SECONDS", "86400")), transport=transport,
                     proxy=os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"))


class FixtureTransport(httpx.AsyncBaseTransport):
    """Deterministic mode: replay recorded responses; optionally record them first."""

    def __init__(self, directory: Path, record: bool = False, proxy: str | None = None):
        self.directory, self.record, self.proxy = directory, record, proxy
        directory.mkdir(parents=True, exist_ok=True)

    def _path(self, request: httpx.Request) -> Path:
        key = hashlib.sha256(f"{request.method} {request.url}".encode()).hexdigest()[:24]
        return self.directory / f"{key}.json"

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        path = self._path(request)
        if path.exists():
            stored = json.loads(path.read_text(encoding="utf-8"))
            return httpx.Response(stored["status"], headers=stored["headers"],
                                  content=stored["body"].encode("utf-8"), request=request)
        if not self.record:
            return httpx.Response(599, text="No recorded fixture", request=request)
        async with httpx.AsyncClient(proxy=self.proxy, trust_env=False, timeout=30,
                                     verify=os.environ.get("SSL_CERT_FILE") or True) as client:
            response = await client.request(request.method, str(request.url), headers=request.headers)
        path.write_text(json.dumps({"url": str(request.url), "status": response.status_code,
                                    "headers": {key: value for key, value in response.headers.items()
                                                if key.lower() in {"content-type", "etag", "last-modified",
                                                                   "location"}},
                                    "body": response.text}), encoding="utf-8")
        return httpx.Response(response.status_code, headers=response.headers, content=response.content,
                              request=request)
