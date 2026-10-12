"""Background PR validation in the window: a green pull request is offered for merging (Merge sends the
shown head), a failed one shows its log and a Fix it that starts a planned task; each state is asked once,
and nothing here involves the active job."""
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
    page = browser.new_page(viewport={"width": 1400, "height": 1600})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.set_content(html)
    page.evaluate("""() => {
      location.hash = 'token=reviews-smoke';
      const demo = window.__demo = {merges: [], starts: [], polls: 0};
      const response = x => ({ok: true, status: 200, json: async () => x});
      const project = {name: 'Coding_Agent', path: 'C:/p', git: 'main'};
      const items = [
        {task_id: 'aaaa1111', ask_merge: true, failed: 0, pending: 0, head: 'a'.repeat(40), url: 'https://github.com/o/r/pull/1',
         goal: 'Add a title test', question: 'All 3 checks are green on https://github.com/o/r/pull/1. Merge it?'},
        {task_id: 'bbbb2222', ask_merge: false, failed: 1, pending: 0, head: 'b'.repeat(40), url: 'https://github.com/o/r/pull/2',
         goal: 'Fix pagination', failure_log: 'FAILED tests/test_page.py::test_next - assert 2 == 3'},
        {task_id: 'cccc3333', ask_merge: false, failed: 0, pending: 2, head: 'c'.repeat(40), url: 'https://github.com/o/r/pull/3'}];
      window.fetch = async (url, opts = {}) => {
        const path = String(url);
        if (path === '/api/health') return response({ok: true, boot: 'b', installed_version: '0.13.0', active: null});
        if (path === '/api/state') return response({project, projects: [project], active: null});
        if (path === '/api/conversation') return response({project: 'C:/p', turns: [], available: true});
        if (path.startsWith('/api/engine/status')) return response({engine: {available: true, version: '0.13.0', conversation: true, typed_api: true, new_project: true},
          chat_available: true, model: {provider: 'ollama', name: 'm', ready: true}, next_action: null, title: 'ok', detail: ''});
        if (path === '/api/setup') return response({readiness: {onboarding_completed: true, components: []}, providers: []});
        if (path.startsWith('/api/pull-requests/merge')) { demo.merges.push(JSON.parse(opts.body)); return response({merged: true}); }
        if (path.startsWith('/api/pull-requests')) { demo.polls++; return response({enabled: true, pull_requests: items}); }
        if (path === '/api/start') { demo.starts.push(JSON.parse(opts.body)); return response({id: 'j1', kind: 'run', status: 'running', started: 1}); }
        if (path.startsWith('/api/jobs/j1')) return response({job: {id: 'j1', kind: 'run', status: 'running'}, events: []});
        return response({});
      };
    }""")
    page.add_script_tag(content=js)
    page.locator('.message', has_text='READY TO MERGE').wait_for()
    page.locator('.message', has_text='CHECKS FAILED').wait_for()
    assert page.locator('.message', has_text='pull/3').count() == 0  # still validating: nothing asked yet
    assert 'FAILED tests/test_page.py::test_next' in page.locator('.message', has_text='CHECKS FAILED').text_content()

    page.locator('.message', has_text='READY TO MERGE').get_by_role('button', name='Merge').click()
    page.wait_for_function("window.__demo.merges.length === 1")
    assert page.evaluate("window.__demo.merges[0]") == {"task_id": "aaaa1111", "head": "a" * 40}

    page.locator('.message', has_text='CHECKS FAILED').get_by_role('button', name='Fix it').click()
    page.wait_for_function("window.__demo.starts.length === 1")
    start = page.evaluate("window.__demo.starts[0]")
    assert start["mode"] == "run" and start["fix_of"] == "bbbb2222" and "pull/2" in start["message"]

    # Each state is asked once, however often the watcher reports it.
    assert page.locator('.message', has_text='READY TO MERGE').count() == 1
    assert not errors, errors
    browser.close()
print("Background PR validation browser PASS: merge offered only when green with the shown head; failures show the log and Fix it plans a repair of the same PR")
