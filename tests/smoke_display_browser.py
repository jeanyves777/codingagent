"""The display library: the model's cb-ui blocks are drawn as tables, stats, charts, option cards with
sources and actions, steps, comparisons, callouts and next actions - from text nodes and SVG only.
Malformed or hostile blocks fall back to text; unsourced prices are labelled; links need confirmation."""
from pathlib import Path
import json
import os
import shutil
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / "desktop_ui" / "static"
html = (root / "index.html").read_text(encoding="utf-8").replace(
    '<link rel="stylesheet" href="/app.css">',
    "<style>" + (root / "app.css").read_text(encoding="utf-8") + "</style>",
).replace('<script defer src="/app.js"></script>', "")
js = (root / "app.js").read_text(encoding="utf-8")
output = Path(os.environ.get("CB_DESKTOP_DISPLAY_SCREENSHOT", "/mnt/data/CodingBrain-Desktop-Display.png"))


def block(data):
    return "```cb-ui\n" + json.dumps(data) + "\n```"


REPLY = "\n".join([
    "Here is your quarter at a glance.",
    block({"type": "stats", "title": "Q1 sales", "items": [{"label": "Revenue", "value": "$48,200", "delta": "+12%"},
          {"label": "Orders", "value": 312, "delta": "-3%"}, {"label": "Avg order", "value": "$154"}],
          "source": {"label": "sales.csv"}}),
    block({"type": "chart", "kind": "bar", "title": "Monthly revenue", "labels": ["Jan", "Feb", "Mar"],
           "series": [{"name": "2026", "values": [14100, 15900, 18200]}, {"name": "2025", "values": [12000, 14200, 16100]}], "unit": "$"}),
    block({"type": "chart", "kind": "pie", "title": "By channel", "labels": ["Web", "Store", "Phone"],
           "series": [{"name": "share", "values": [60, 30, 10]}]}),
    block({"type": "table", "title": "Top products", "columns": ["Product", "Units", "Revenue"],
           "rows": [["Widget", 120, 6000], ["Gadget", 80, 9600], ["<img src=x onerror=alert(1)>", 1, 2]]}),
    block({"type": "options", "title": "Flights (example layout)", "items": [
        {"title": "Air Example 101", "subtitle": "ABJ → CDG · nonstop · 6h 35m", "price": "€612", "badges": ["Best value"],
         "details": ["Departs 23:40", "1 checked bag"], "source": {"label": "airline site", "url": "https://example.com/flight"},
         "actions": [{"label": "Compare baggage", "ask": "Compare the baggage rules of these flights"},
                     {"label": "View offer", "url": "https://example.com/flight"}]},
        {"title": "Made Up Air 7", "subtitle": "ABJ → CDG · 1 stop", "price": "€455",
         "actions": [{"label": "Bad link", "url": "javascript:alert(1)"}]}]}),
    block({"type": "steps", "title": "Plan", "items": [{"title": "Collect data", "status": "done"},
          {"title": "Draft report", "detail": "Two pages", "status": "current"}, {"title": "Review"}]}),
    block({"type": "compare", "options": ["Plan A", "Plan B"], "rows": [{"label": "Cost", "values": ["Low", "High"]},
          {"label": "Speed", "values": ["Slow", "Fast"]}], "recommendation": "Plan A fits the budget."}),
    block({"type": "callout", "tone": "warning", "title": "Live data", "text": "I cannot fetch live fares."}),
    block({"type": "actions", "items": [{"label": "Make it a PDF", "ask": "Turn this into a PDF report"}]}),
    "```cb-ui\n{not json at all\n```",
    block({"type": "unknown-widget", "x": 1}),
])

with sync_playwright() as p:
    chromium = os.environ.get("CB_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if chromium:
        options["executable_path"] = chromium
    browser = p.chromium.launch(**options)
    page = browser.new_page(viewport={"width": 1480, "height": 2200})
    errors, dialogs = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.set_content(html)
    page.evaluate("""(reply) => {
      location.hash = 'token=display-smoke';
      const at = 1791662000;
      const demo = window.__demo = {starts: [], opened: []};
      const response = x => ({ok: true, status: 200, json: async () => x});
      window.fetch = async (url, opts = {}) => {
        const path = String(url);
        if (path === '/api/health') return response({ok: true, boot: 'b', installed_version: '0.13.0', active: null});
        if (path === '/api/state') return response({project: null, projects: [], active: null});
        if (path === '/api/conversation') return response({project: null, turns: [], available: true});
        if (path.startsWith('/api/engine/status')) return response({engine: {available: true, version: '0.13.0', conversation: true, typed_api: true, new_project: true},
          chat_available: true, model: {provider: 'ollama', name: 'qwen2.5-coder:3b', ready: true}, next_action: null, title: 'ok', detail: ''});
        if (path === '/api/setup') return response({readiness: {onboarding_completed: true, components: []}, providers: []});
        if (path === '/api/open-url') { demo.opened.push(JSON.parse(opts.body).url); return response({opened: true}); }
        if (path === '/api/start') {
          demo.starts.push(JSON.parse(opts.body));
          const id = 'c' + demo.starts.length;
          demo[id] = demo.starts.length === 1 ? [{seq: 1, kind: 'output', message: reply, at, job_id: id}, {seq: 2, kind: 'finish', message: 'done', at, job_id: id}]
            : [{seq: 1, kind: 'output', message: 'ok', at, job_id: id}, {seq: 2, kind: 'finish', message: 'done', at, job_id: id}];
          return response({id, kind: 'chat', status: 'running', started: at});
        }
        const match = path.match(/^\\/api\\/jobs\\/(c\\d+)/);
        if (match) return response({job: {id: match[1], kind: 'chat', status: 'completed'}, events: demo[match[1]]});
        throw new Error('Unexpected API: ' + path);
      };
    }""", REPLY)
    page.add_script_tag(content=js)
    page.locator('#footer-status', has_text='Ready').wait_for()
    page.fill('#prompt', 'show me my q1 sales')
    page.click('#send')
    page.locator('.ui-stat-value', has_text='$48,200').wait_for(state='attached')
    page.locator('.ui-step').first.wait_for(state='attached')

    # Every component draws.
    for selector, count in [('.ui-stat', 3), ('.ui-chart-svg rect', 6), ('.ui-chart-svg.pie path', 3), ('.ui-table tbody tr', 5),
                            ('.ui-option', 2), ('.ui-step', 3), ('.ui-compare-table tbody tr', 2), ('.ui-callout.warning', 1)]:
        assert page.locator(selector).count() >= count, (selector, page.locator(selector).count())
    assert page.locator('.ui-stat-delta.down').count() == 1 and 'sales.csv' in page.locator('.ui-source').first.inner_text()
    # Hostile text stays text; malformed and unknown blocks fall back to readable code.
    assert page.locator('img').count() == 0 and not dialogs
    assert page.locator('.code-lang', has_text='Display block (could not be drawn)').count() == 2
    # Governance: an unsourced price is labelled; a javascript: link is never offered.
    second = page.locator('.ui-option').nth(1)
    assert 'Unverified example' in second.inner_text() and second.locator('.ui-action').count() == 0
    assert 'Unverified example' not in page.locator('.ui-option').first.inner_text()
    page.screenshot(path=str(output), full_page=True)

    # A link asks first, then opens through the bridge; an "ask" action sends a follow-up.
    page.locator('.ui-action', has_text='View offer').click()
    page.locator('#action-approve').click()
    page.wait_for_function("window.__demo.opened.length === 1")
    assert page.evaluate("window.__demo.opened[0]") == "https://example.com/flight"
    page.locator('.ui-action', has_text='Make it a PDF').click()
    page.wait_for_function("window.__demo.starts.length === 2")
    assert page.evaluate("window.__demo.starts[1].message") == "Turn this into a PDF report"

    assert not errors, errors
    browser.close()
print("Display library browser PASS: stats, bar/pie charts, table, option cards with sources/actions, steps, compare, callout, next actions; safe fallbacks; links confirmed")
