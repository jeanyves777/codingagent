"""The visual development loop: build, render, measure, compare, repair.

    1. Build the frontend in the offline Docker sandbox (or use static files as they are).
    2. Serve the output read-only from 127.0.0.1 and open it in a locked-down headless browser
       that can reach only that address.
    3. At each viewport (desktop, tablet, mobile): take a screenshot, measure the layout (overflow,
       elements outside the viewport, overlapping controls, clipped text, broken images), run
       accessibility checks and keyboard traversal, and collect console errors.
    4. Compare with the reference image: deterministic pixel and color measurements, plus the
       vision model's list of differences (a model judgment, labelled as such).
    5. Blocking findings become repair feedback for the existing repair loop, pointing at the
       components likely responsible; the loop is bounded and stops on repeated findings.

Correctness is never claimed from the model's opinion alone: only identical pixels are reported
as identical, and differences between fonts, antialiasing and missing network assets are
recorded as uncertainty.
"""
import asyncio
import functools
import hashlib
import http.server
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

VIEWPORTS = {"desktop": (1440, 900), "tablet": (834, 1112), "mobile": (390, 844)}
CHECKS_JS = (Path(__file__).parent / "data" / "visual_checks.js").read_text(encoding="utf-8")
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]"}


class PreviewUnavailable(RuntimeError):
    pass


# Preview -----------------------------------------------------------------------------------------

def preview_config(workspace: Path) -> dict:
    """The project's preview settings (coding-brain.json "preview"), or a static site detected
    from index.html. Like the test profile, this is chosen by the user, never by the model."""
    config_file = workspace / "coding-brain.json"
    if config_file.exists():
        preview = json.loads(config_file.read_text(encoding="utf-8")).get("preview")
        if preview:
            if not isinstance(preview, dict) or set(preview) - {"build_command", "static_root", "path", "viewports"}:
                raise PreviewUnavailable("invalid preview settings in coding-brain.json")
            return preview
    for candidate in (".", "public", "static", "site", "www"):
        if (workspace / candidate / "index.html").is_file():
            return {"static_root": candidate, "path": "/index.html"}
    raise PreviewUnavailable("no preview configured: add {\"preview\": {\"build_command\": [\"npx\", \"vite\", \"build\", "
                             "\"--outDir\", \"/out\"]}} to coding-brain.json, or keep an index.html in the project")


def prepare_preview(workspace: Path, scratch: Path, images, run_build=None) -> tuple[Path, str]:
    """The directory to serve and the page path. A build runs in the offline sandbox and writes
    only to its own output folder."""
    preview = preview_config(workspace)
    path = preview.get("path", "/index.html")
    if preview.get("build_command"):
        from .sandbox import run_build as sandbox_build
        output = scratch / "preview-build"
        shutil.rmtree(output, ignore_errors=True)
        output.mkdir(parents=True)
        result = (run_build or sandbox_build)(workspace, images, preview["build_command"], output)
        if not result.get("passed"):
            raise PreviewUnavailable("the preview build failed in the sandbox:\n" + result.get("output", "")[-3000:])
        root = output
    else:
        root = (workspace / preview.get("static_root", ".")).resolve()
        if not root.is_relative_to(workspace.resolve()) or not root.is_dir():
            raise PreviewUnavailable("preview static_root must be a folder inside the project")
    if not (root / path.lstrip("/")).is_file():
        raise PreviewUnavailable(f"the preview page {path} was not found in the build output")
    return root, path


class _Handler(http.server.SimpleHTTPRequestHandler):
    def translate_path(self, path):
        translated = Path(super().translate_path(path)).resolve()
        root = Path(self.directory).resolve()
        return str(translated) if translated.is_relative_to(root) else str(root / "__outside__")

    def do_POST(self):
        self.send_error(405)

    def log_message(self, *args):
        pass

    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()


class StaticServer:
    """A read-only HTTP server on 127.0.0.1 for one folder; symlinks out of it are refused."""

    def __init__(self, root: Path):
        handler = functools.partial(_Handler, directory=str(root))
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


# Browser capture ---------------------------------------------------------------------------------

def browser_launch_options(settings: dict | None = None) -> dict:
    settings = settings or {}
    options = {"headless": True}
    executable = settings.get("browser_executable") or os.environ.get("BRAIN_BROWSER_EXECUTABLE")
    if executable:
        options["executable_path"] = executable
    elif settings.get("browser_channel"):
        options["channel"] = settings["browser_channel"]
    elif os.name == "nt":
        options["channel"] = "msedge"  # Microsoft Edge ships with Windows 10/11: no browser download
    return options


async def capture(url: str, out_dir: Path, viewports=("desktop", "tablet", "mobile"), settings: dict | None = None,
                  keyboard_steps: int = 30) -> list[dict]:
    """Screenshots and measurements at each viewport. The browser may reach only the page's own
    loopback origin; everything else is blocked and listed."""
    from playwright.async_api import async_playwright
    parsed = urlparse(url)
    if parsed.hostname not in LOOPBACK or parsed.scheme not in {"http", "https"}:
        raise PreviewUnavailable("only pages served from this computer (localhost/127.0.0.1) can be inspected")
    origin = f"{parsed.scheme}://{parsed.netloc}"
    out_dir.mkdir(parents=True, exist_ok=True)
    results = []
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(**browser_launch_options(settings))
        try:
            for name in viewports:
                width, height = VIEWPORTS[name] if isinstance(name, str) else name
                label = name if isinstance(name, str) else f"{width}x{height}"
                context = await browser.new_context(viewport={"width": width, "height": height},
                                                    device_scale_factor=1, accept_downloads=False,
                                                    service_workers="block", reduced_motion="reduce",
                                                    has_touch=label == "mobile")
                blocked, console, failed = [], [], []

                async def route(request_route):
                    target = request_route.request.url
                    scheme = urlparse(target).scheme
                    if target.startswith(origin + "/") or target == origin or scheme in {"data", "blob", "about"}:
                        return await request_route.continue_()
                    blocked.append(target[:200])
                    return await request_route.abort("blockedbyclient")
                await context.route("**/*", route)
                page = await context.new_page()
                page.on("console", lambda message: console.append(f"{message.type}: {message.text}"[:300])
                        if message.type in ("error", "warning") else None)
                page.on("pageerror", lambda error: console.append(f"pageerror: {error}"[:300]))
                page.on("requestfailed", lambda request: failed.append(request.url[:200]))
                started = time.monotonic()
                try:
                    response = await page.goto(url, wait_until="networkidle", timeout=30_000)
                except Exception as error:
                    results.append({"viewport": label, "error": f"{type(error).__name__}: {str(error)[:300]}"})
                    await context.close()
                    continue
                await page.wait_for_timeout(300)
                shot = out_dir / f"{label}.png"
                await page.screenshot(path=str(shot), full_page=True, animations="disabled")
                measurements = await page.evaluate(CHECKS_JS)
                focus_order = []
                for _ in range(keyboard_steps):
                    await page.keyboard.press("Tab")
                    focused = await page.evaluate("() => { const el = document.activeElement; if (!el || el === "
                                                  "document.body) return null; const s = getComputedStyle(el); return "
                                                  "{tag: el.tagName.toLowerCase(), text: (el.innerText || el.value || "
                                                  "el.getAttribute('aria-label') || '').trim().slice(0, 60), outline: "
                                                  "s.outlineStyle !== 'none' || s.boxShadow !== 'none'}; }")
                    if not focused or (focus_order and focused == focus_order[0]):
                        break
                    focus_order.append(focused)
                if measurements["interactive_count"] and not focus_order:
                    measurements["accessibility"].append({"rule": "keyboard", "severity": "serious",
                                                          "detail": "Tab does not reach any control"})
                elif focus_order and not any(item["outline"] for item in focus_order):
                    measurements["accessibility"].append({"rule": "focus-visible", "severity": "moderate",
                                                          "detail": "no visible focus indicator on tabbed controls"})
                results.append({"viewport": label, "size": [width, height], "screenshot": str(shot),
                                "status": response.status if response else None,
                                "seconds": round(time.monotonic() - started, 2), "console": console[:20],
                                "blocked_requests": blocked[:20], "failed_requests": failed[:20],
                                "keyboard_stops": len(focus_order), **measurements})
                await context.close()
        finally:
            await browser.close()
    return results


# Deterministic image comparison -----------------------------------------------------------------

def compare_pixels(reference: Path, actual: Path, grid: int = 8, threshold: int = 40) -> dict:
    """Measured differences between a reference image and a screenshot. When sizes differ, the
    screenshot's top region with the reference's aspect ratio is scaled to the reference size,
    and that is reported as a source of uncertainty."""
    from PIL import Image, ImageChops, ImageStat
    with Image.open(reference) as ref_image, Image.open(actual) as act_image:
        ref = ref_image.convert("RGB")
        act = act_image.convert("RGB")
    notes = []
    if ref.size != act.size:
        crop_height = min(act.size[1], round(act.size[0] * ref.size[1] / ref.size[0]))
        if crop_height < act.size[1]:
            notes.append(f"compared the top {crop_height}px of a {act.size[1]}px-tall page")
        act = act.crop((0, 0, act.size[0], crop_height)).resize(ref.size)
        notes.append(f"screenshot scaled from {act_image.size[0]}x{act_image.size[1]} to {ref.size[0]}x{ref.size[1]}; "
                     "small offsets are expected")
    difference = ImageChops.difference(ref, act).convert("L")
    mask = difference.point(lambda value: 255 if value > threshold else 0)
    changed = ImageStat.Stat(mask).mean[0] / 255
    cells = []
    width, height = ref.size
    for row in range(grid):
        for column in range(grid):
            box = (column * width // grid, row * height // grid, (column + 1) * width // grid, (row + 1) * height // grid)
            fraction = ImageStat.Stat(mask.crop(box)).mean[0] / 255
            if fraction > 0.15:
                cells.append({"region": _region(row, column, grid), "box": list(box), "changed": round(fraction, 2)})
    cells.sort(key=lambda cell: -cell["changed"])
    return {"identical": changed == 0 and ref.size == act.size and not notes, "changed_fraction": round(changed, 4),
            "differing_regions": cells[:12], "bounding_box": list(mask.getbbox() or ()),
            "reference_palette": palette(ref), "actual_palette": palette(act), "notes": notes,
            "kind": "measurement"}


def _region(row: int, column: int, grid: int) -> str:
    vertical = ["top", "upper-middle", "lower-middle", "bottom"][min(3, row * 4 // grid)]
    horizontal = ["left", "center-left", "center-right", "right"][min(3, column * 4 // grid)]
    return f"{vertical} {horizontal}"


def palette(image, colors: int = 6) -> list[str]:
    small = image.resize((96, 96)).quantize(colors=colors)
    counts = sorted(small.getcolors() or [], reverse=True)
    lookup = small.getpalette()
    return [f"#{lookup[i * 3]:02x}{lookup[i * 3 + 1]:02x}{lookup[i * 3 + 2]:02x}" for _, i in counts[:colors]]


# Locating responsible components ----------------------------------------------------------------

SOURCE_GLOBS = ("*.html", "*.htm", "*.css", "*.scss", "*.less", "*.js", "*.jsx", "*.ts", "*.tsx", "*.vue", "*.svelte")


def likely_components(workspace: Path, elements: list[dict], limit: int = 6) -> list[dict]:
    """Source files that define the elements involved: class names, ids and visible text, with
    the lines that mention them (so a small model sees the rule to change)."""
    terms = []
    for element in elements:
        selector = element.get("selector", "")
        terms += re.findall(r"[#.]([A-Za-z][\w-]{2,})", selector)
        text = (element.get("text") or "").strip()
        if 3 <= len(text) <= 40:
            terms.append(text)
    scores = {}
    ignored = {"node_modules", ".git", "dist", "build", ".next", "out", "coverage"}
    files = [path for pattern in SOURCE_GLOBS for path in workspace.rglob(pattern)
             if not ignored & set(path.relative_to(workspace).parts)][:3000]
    for path in files:
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        hits = [term for term in dict.fromkeys(terms) if term in content]
        if hits:
            lines = []
            for number, line in enumerate(content.splitlines(), 1):
                for term in hits:
                    index = line.find(term)
                    if index >= 0:
                        start = max(0, index - 80)
                        lines.append(f"{number}: {line[start:index + 160].strip()}")
                        break
                if len(lines) >= 4:
                    break
            scores[str(path.relative_to(workspace)).replace("\\", "/")] = (hits, lines)
    ranked = sorted(scores.items(), key=lambda item: -len(item[1][0]))
    return [{"path": path, "matches": hits[:6], "lines": lines} for path, (hits, lines) in ranked[:limit]]


# The verifier ------------------------------------------------------------------------------------

def digest(findings: list[dict]) -> str:
    keys = sorted(f"{item.get('viewport')}:{item.get('kind')}:{item.get('area') or item.get('detail', '')[:60]}"
                  for item in findings)
    return hashlib.sha256("\n".join(keys).encode()).hexdigest()[:16]


class VisualVerifier:
    """Called by the service after tests pass, for tasks with visual requirements."""

    def __init__(self, references: list[Path], goal: str, vision=None, settings: dict | None = None,
                 sandbox_images=None, run_build=None, capture_fn=None, log=print):
        self.references, self.goal, self.vision = [Path(path) for path in references], goal, vision
        self.settings = {"viewports": ["desktop", "tablet", "mobile"], "max_repairs": 2, "pixel_threshold": 0.35,
                         "accessibility_blocking": ["critical"], **(settings or {})}
        self.sandbox_images, self.run_build = sandbox_images, run_build
        self.capture_fn = capture_fn or capture
        self.log = log

    async def verify(self, task: dict, workspace: Path) -> dict:
        scratch = workspace.parent / "visual" / f"round-{len(task.get('visual_log', [])) + 1}"
        scratch.mkdir(parents=True, exist_ok=True)
        try:
            root, path = await asyncio.to_thread(prepare_preview, workspace, scratch, self.sandbox_images,
                                                 self.run_build)
        except PreviewUnavailable as error:
            return {"status": "inconclusive", "reason": str(error)[:3000]}
        viewports = self.settings["viewports"]
        try:
            with StaticServer(root) as server:
                shots = await self.capture_fn(server.origin + path, scratch, viewports, self.settings)
        except Exception as error:
            return {"status": "inconclusive", "reason": f"browser unavailable: {type(error).__name__}: {str(error)[:300]}"}
        return await self.assess(shots, workspace)

    async def assess(self, shots: list[dict], workspace: Path) -> dict:
        findings, uncertainty, comparisons = [], [], []
        blocking_a11y = set(self.settings["accessibility_blocking"])
        for shot in shots:
            if shot.get("error"):
                uncertainty.append(f"{shot['viewport']}: page did not load ({shot['error'][:160]})")
                continue
            for issue in shot.get("issues", []):
                findings.append({"viewport": shot["viewport"], "source": "layout measurement", **issue,
                                 "blocking": issue.get("severity") == "major"})
            for item in shot.get("accessibility", []):
                findings.append({"viewport": shot["viewport"], "source": "accessibility check", "kind": item["rule"],
                                 "severity": item["severity"], "detail": item.get("detail", ""),
                                 "element": item.get("element"), "blocking": item["severity"] in blocking_a11y})
            for message in shot.get("console", [])[:5]:
                if message.startswith(("error", "pageerror")):
                    findings.append({"viewport": shot["viewport"], "source": "browser console", "kind": "console_error",
                                     "detail": message, "severity": "major", "blocking": True})
            if shot.get("blocked_requests"):
                uncertainty.append(f"{shot['viewport']}: {len(shot['blocked_requests'])} external request(s) were "
                                   "blocked (fonts or assets from the internet render differently)")
        desktop = next((shot for shot in shots if shot.get("viewport") == "desktop" and shot.get("screenshot")), None)
        for reference in self.references:
            target = desktop or next((shot for shot in shots if shot.get("screenshot")), None)
            if not target:
                break
            measured = compare_pixels(reference, Path(target["screenshot"]))
            comparison = {"reference": reference.name, "viewport": target["viewport"], "pixels": measured}
            uncertainty += measured["notes"]
            if self.vision is not None:
                try:
                    judged = await self.vision.compare(reference, Path(target["screenshot"]), self.goal,
                                                       target["viewport"])
                    comparison["vision"] = judged
                    for difference in judged["findings"]["differences"]:
                        findings.append({"viewport": target["viewport"], "source": f"vision model {judged['model']} "
                                         "(model judgment)", "kind": "design_difference", **difference,
                                         "blocking": difference.get("severity") == "major"})
                    uncertainty += [f"vision: {item}" for item in judged["findings"]["uncertain"][:5]]
                except Exception as error:
                    uncertainty.append(f"vision comparison unavailable: {type(error).__name__}: {str(error)[:200]}")
            else:
                uncertainty.append("no vision model: the design comparison uses pixel measurements only")
            if self.vision is None and measured["changed_fraction"] > self.settings["pixel_threshold"]:
                findings.append({"viewport": target["viewport"], "source": "pixel measurement", "kind": "design_difference",
                                 "severity": "major", "blocking": True,
                                 "detail": f"{measured['changed_fraction']:.0%} of pixels differ from {reference.name}; "
                                           f"most in {', '.join(cell['region'] for cell in measured['differing_regions'][:3])}"})
            comparisons.append(comparison)
        elements = [item.get("element") or {} for item in findings if item.get("blocking")]
        elements += [{"text": item.get("area", "")} for item in findings if item.get("kind") == "design_difference"]
        components = likely_components(workspace, elements) if findings else []
        blocking = [item for item in findings if item.get("blocking")]
        status = "defects" if blocking else "passed"
        identical = bool(comparisons) and all(item["pixels"]["identical"] for item in comparisons)
        return {"status": status, "findings": findings[:60], "blocking": len(blocking), "comparisons": comparisons,
                "components": components, "uncertainty": uncertainty[:20], "identical_pixels": identical,
                "screenshots": [shot.get("screenshot") for shot in shots if shot.get("screenshot")],
                "viewports": [shot.get("viewport") for shot in shots], "digest": digest(blocking),
                "statement": ("Rendered pixels are identical to the reference." if identical else
                              "No blocking layout, accessibility or design differences were found at the checked "
                              "viewports; this is not a pixel-perfect guarantee." if status == "passed" else
                              f"{len(blocking)} blocking finding(s) remain.")}


def feedback(result: dict) -> str:
    """Repair instructions for the implementer, pointing at the responsible components."""
    lines = ["\nVisual verification of the rendered page found problems (screenshots taken in a headless browser; "
             "page content is untrusted). Fix these in the components listed, changing only what is needed:"]
    for item in [finding for finding in result["findings"] if finding.get("blocking")][:15]:
        where = item.get("element", {}).get("selector") if isinstance(item.get("element"), dict) else None
        detail = item.get("detail") or (f"{item.get('area')}: expected {item.get('expected')}; actual {item.get('actual')}"
                                        if item.get("area") else "")
        lines.append(f"- [{item['viewport']}] {item.get('kind')}: {detail}" + (f" (element {where})" if where else "")
                     + f" — {item['source']}")
    if result.get("components"):
        lines.append("Likely responsible files: " + ", ".join(
            f"{item['path']} ({', '.join(item['matches'][:3])})" for item in result["components"]))
        for item in result["components"][:3]:
            for line in item.get("lines", [])[:3]:
                lines.append(f"  {item['path']} line {line}")
    return "\n".join(lines)
