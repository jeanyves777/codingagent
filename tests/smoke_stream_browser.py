"""Chat shows the engine's steps while it works, streams the reply as it is written, then shows the
formatted final reply in the same bubble (one message, no duplicate)."""
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / "desktop_ui" / "static"
html = (root / "index.html").read_text(encoding="utf-8").replace(
    '<link rel="stylesheet" href="/app.css">',
    "<style>" + (root / "app.css").read_text(encoding="utf-8") + "</style>",
).replace('<script defer src="/app.js"></script>', "")
js = (root / "app.js").read_text(encoding="utf-8")

with sync_playwright() as p:
    chromium = os.environ.get("CB_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if chromium:
        options["executable_path"] = chromium
    browser = p.chromium.launch(**options)
    page = browser.new_page(viewport={"width": 1400, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(html)
    page.evaluate("""() => {
      location.hash = 'token=stream-smoke';
      const at = 1791662000;
      const ev = (seq, kind, message) => ({seq, kind, message, at: at + seq, job_id: 'c1'});
      const demo = window.__demo = {events: [], status: 'running'};
      const response = x => ({ok: true, status: 200, json: async () => x});
      window.fetch = async (url, opts = {}) => {
        const path = String(url);
        if (path === '/api/health') return response({ok: true, boot: 'b', installed_version: '0.13.0', active: null});
        if (path === '/api/state') return response({project: null, projects: [], active: null});
        if (path.startsWith('/api/engine/status')) return response({engine: {available: true, version: '0.13.0', conversation: true, typed_api: true, new_project: true},
          chat_available: true, model: {provider: 'ollama', name: 'qwen2.5-coder:7b', ready: true}, next_action: null, title: 'ok', detail: ''});
        if (path === '/api/setup') return response({readiness: {onboarding_completed: true, components: []}, providers: []});
        if (path === '/api/start') return response({id: 'c1', kind: 'chat', status: 'running', started: at});
        if (path.startsWith('/api/jobs/c1')) {
          const after = Number(new URL('http://x' + path).searchParams.get('after') || 0);
          return response({job: {id: 'c1', kind: 'chat', status: demo.status}, events: demo.events.filter(e => e.seq > after)});
        }
        throw new Error('Unexpected API: ' + path);
      };
      window.__push = (kind, message) => demo.events.push(ev(demo.events.length + 1, kind, message));
    }""")
    page.add_script_tag(content=js)
    page.locator('#footer-status', has_text='Ready').wait_for()
    page.fill('#prompt', 'what is a decorator?')
    page.click('#send')

    # Steps replace the bare "Thinking..." while the engine works.
    page.evaluate("window.__push('step', 'Understanding your request')")
    page.locator('.typing-row .message-content', has_text='Understanding your request').wait_for()
    page.evaluate("window.__push('step', 'Writing the answer with qwen2.5-coder:7b')")
    page.locator('.typing-row .message-content', has_text='Writing the answer').wait_for()
    assert 'Writing the answer' in page.locator('#status-label').inner_text()

    # The reply streams into one bubble...
    page.evaluate("window.__push('delta', 'A decorator ')")
    page.locator('.message.streaming .message-content', has_text='A decorator').wait_for()
    assert page.locator('.typing-row').count() == 0
    page.evaluate("window.__push('delta', 'wraps a function: `@cache`')")
    page.locator('.message.streaming .message-content', has_text='@cache').wait_for()

    # ...then becomes the formatted final reply in the same bubble.
    page.evaluate("window.__push('output', 'A decorator wraps a function: `@cache`'); window.__push('finish', 'Chat completed'); window.__demo.status = 'completed'")
    page.locator('.message-content .inline-code', has_text='@cache').wait_for()
    assert page.locator('.message.streaming').count() == 0
    assistant = page.locator('.message.assistant').all_inner_texts()
    assert sum('A decorator wraps a function' in text for text in assistant) == 1, assistant
    assert page.locator('.activity-entry', has_text='A decorator').count() == 1  # the final reply only; deltas are not logged

    assert not errors, errors
    browser.close()
print("Streaming chat browser PASS: engine steps shown while working, reply streams, final formatted reply in one bubble")
