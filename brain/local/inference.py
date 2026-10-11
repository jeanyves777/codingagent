"""Measure whether the local model server really runs several generations at the same time.

Ollama queues requests beyond its parallel setting (OLLAMA_NUM_PARALLEL), so starting more
agents than it serves gives no speed-up. `probe` sends N streaming requests at once and records
when every token arrives: generation overlapped only if tokens from different requests arrived
interleaved, not one request after the other."""
import asyncio
import json
import time
from pathlib import Path

import httpx

PROMPT = "Write a Python function that returns the n-th Fibonacci number, with a docstring. Code only."


async def _stream(client, url: str, model: str, num_predict: int, start: float) -> dict:
    times = []
    async with client.stream("POST", url + "/api/chat", json={
            "model": model, "stream": True, "keep_alive": "10m",
            "messages": [{"role": "user", "content": PROMPT}],
            "options": {"temperature": 0, "num_predict": num_predict, "seed": 1}}) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if line.strip() and '"done":true' not in line.replace(" ", ""):
                times.append(time.monotonic() - start)
    return {"tokens": len(times), "first": times[0] if times else None, "last": times[-1] if times else None,
            "times": times}


def overlap(streams: list[dict]) -> float:
    """The share of the generating time during which at least two requests produced tokens:
    near 0 when the server answered one request after another."""
    spans = sorted((s["first"], s["last"]) for s in streams if s["first"] is not None)
    if len(spans) < 2:
        return 0.0
    points = sorted([(a, 1) for a, _ in spans] + [(b, -1) for _, b in spans])
    active, previous, both, total = 0, points[0][0], 0.0, 0.0
    for at, delta in points:
        if active >= 1:
            total += at - previous
        if active >= 2:
            both += at - previous
        active, previous = active + delta, at
    return round(both / total, 3) if total else 0.0


async def probe(url: str, model: str, streams: int = 2, num_predict: int = 96, timeout: float = 900) -> dict:
    """One warm-up request (loads the model), one request alone, then `streams` at once."""
    url = url.rstrip("/")
    async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
        await _stream(client, url, model, 8, time.monotonic())
        alone, single_rate = None, 0.0
        for _ in range(2):  # the better of two: a single run on a busy machine reads low
            start = time.monotonic()
            run = await _stream(client, url, model, num_predict, start)
            rate = run["tokens"] / max(run["last"] - (run["first"] or 0), 1e-6)
            if rate > single_rate:
                alone, single_rate = run, rate
        start = time.monotonic()
        results = await asyncio.gather(*(_stream(client, url, model, num_predict, start) for _ in range(streams)))
        wall = max(r["last"] or 0 for r in results)
    tokens = sum(r["tokens"] for r in results)
    sequential = sum(r["tokens"] for r in results) / single_rate + sum(1 for _ in results) * (alone["first"] or 0)
    share = overlap(results)
    return {"model": model, "streams": streams, "num_predict": num_predict,
            "single_tokens_per_second": round(single_rate, 2),
            "aggregate_tokens_per_second": round(tokens / wall, 2) if wall else 0.0,
            "wall_seconds": round(wall, 2), "sequential_estimate_seconds": round(sequential, 2),
            "first_token_seconds": [round(r["first"] or 0, 2) for r in results],
            "overlap": share, "concurrent": share >= 0.5}


def parallel_slots(results: list[dict]) -> int:
    """The largest number of streams that really generated at the same time without losing total
    throughput (at least 90% of one stream alone). Agents beyond it would only queue."""
    best = 1
    for result in sorted(results, key=lambda r: r["streams"]):
        if result["concurrent"] and result["aggregate_tokens_per_second"] >= 0.9 * result["single_tokens_per_second"]:
            best = result["streams"]
    return best


def _server_version(url: str) -> str | None:
    try:
        response = httpx.get(url.rstrip("/") + "/api/version", timeout=5, trust_env=False)
        return response.json().get("version") if response.status_code == 200 else None
    except (httpx.HTTPError, ValueError):
        return None


MAX_AGE = 7 * 86400  # settings such as OLLAMA_NUM_PARALLEL change without a new version


def cached_slots(cache: Path, url: str, model: str, check_server: bool = True) -> int | None:
    """The measured number for this model, or None if never measured, measured over a week ago
    or (with `check_server`) on another server version: a new Ollama may serve differently.
    Without `check_server` nothing is contacted (read-only status views)."""
    try:
        entry = json.loads(Path(cache).read_text(encoding="utf-8")).get(f"{url.rstrip('/')}|{model}")
        if not entry or time.time() - float(entry["at"]) > MAX_AGE:
            return None
        if check_server and entry.get("version") != _server_version(url):
            return None
        return max(1, int(entry["slots"]))
    except (OSError, ValueError, AttributeError, KeyError, TypeError):
        return None


def calibrate(cache: Path, url: str, model: str, streams: int, num_predict: int = 64) -> dict:
    """Measure once and remember it: {slots, version, result}. One short probe of `streams`
    simultaneous generations (about a minute on a CPU)."""
    version = _server_version(url)
    result = asyncio.run(probe(url, model, streams=streams, num_predict=num_predict))
    entry = {"slots": parallel_slots([result]), "version": version, "at": time.time(), "result": result}
    cache = Path(cache)
    try:
        data = json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else {}
    except (OSError, ValueError):
        data = {}
    data[f"{url.rstrip('/')}|{model}"] = entry
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return entry
