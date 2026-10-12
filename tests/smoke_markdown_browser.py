"""Real chat routes render Markdown as safe, styled DOM for saved and new replies."""
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / 'desktop_ui' / 'static'
html = (root / 'index.html').read_text(encoding='utf-8').replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root / 'app.css').read_text(encoding='utf-8') + '</style>',
).replace('<script defer src="/app.js"></script>', '')
js = (root / 'app.js').read_text(encoding='utf-8')
reply = '''### What I can help you with

I can help with programming, software development, and document creation.

1. **Chat and answer programming questions:** Explain code, debug problems, and review algorithms.

2. **Change your projects:** Fix bugs, add features, write tests, and refactor code.
   - **Review:** Inspect the changes before accepting them.
   - *Verify:* Run your project's tests in the sandbox.

3. **Create new software projects:** Build from your description and requirements.

4. **Write documents:** Prepare PDF, Word, Excel, Markdown, and HTML files.

> Your projects remain under your control.

---

[Documentation](https://example.com/docs) and [unsafe](javascript:alert(1)).
<img src=x onerror=alert(1)>
'''
output = Path(os.environ.get('CB_DESKTOP_MARKDOWN_SCREENSHOT', str(root.parents[1] / 'dist/Markdown-Chat.png')))
output.parent.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    chromium = os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    options = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if chromium:
        options['executable_path'] = chromium
    browser = p.chromium.launch(**options)
    page = browser.new_page(viewport={'width': 1400, 'height': 1100})
    errors, dialogs = [], []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: (dialogs.append(dialog.message), dialog.dismiss()))
    page.set_content(html)
    page.evaluate(r'''reply => {
      location.hash = 'token=markdown-smoke';
      const demo = window.__demo = {reply, opened: []};
      const response = x => ({ok: true, status: 200, json: async () => x});
      window.fetch = async (url, opts = {}) => {
        const path = String(url);
        if (path === '/api/health') return response({ok: true, boot: 'b', installed_version: '0.13.0', active: null});
        if (path === '/api/state') return response({project: null, projects: [], active: null});
        if (path === '/api/conversation') return response({turns: [{role: 'assistant', content: 'Saved **bold** reply without code.'}], available: true});
        if (path.startsWith('/api/engine/status')) return response({engine: {available: true, version: '0.13.0', conversation: true, typed_api: true},
          chat_available: true, model: {name: 'qwen2.5-coder:7b', ready: true}, title: 'ok', detail: ''});
        if (path === '/api/setup') return response({readiness: {onboarding_completed: true, components: []}, providers: []});
        if (path === '/api/start') return response({id: 'c1', kind: 'chat', status: 'running', started: 1791662000});
        if (path.startsWith('/api/jobs/c1')) return response({job: {id: 'c1', kind: 'chat', status: 'completed'}, events: [
          {seq: 1, kind: 'output', message: reply, at: 1791662000, job_id: 'c1'},
          {seq: 2, kind: 'finish', message: 'done', at: 1791662001, job_id: 'c1'}]});
        if (path === '/api/open-url') { demo.opened.push(JSON.parse(opts.body).url); return response({opened: true}); }
        throw new Error('Unexpected API: ' + path);
      };
    }''', reply)
    page.add_script_tag(content=js)
    page.locator('.message.history strong', has_text='bold').wait_for()
    page.fill('#prompt', 'what are your capabilities?')
    page.click('#send')
    content = page.locator('.message.assistant:not(.history) .message-content').last
    content.locator('h5', has_text='What I can help').wait_for()
    assert content.locator('ol > li').count() == 4
    assert content.locator('ul > li').count() == 2
    assert content.locator('strong').count() == 5
    assert content.locator('em', has_text='Verify:').count() == 1
    assert '**' not in content.inner_text()
    assert content.locator('blockquote').count() == 1
    assert content.locator('hr').count() == 1
    assert page.locator('img').count() == 0 and not dialogs
    assert content.locator('.markdown-link').count() == 1
    assert 'onerror=alert(1)' in content.inner_text()
    page.screenshot(path=str(output), full_page=True)
    content.locator('.markdown-link').click()
    assert not page.evaluate('window.__demo.opened')
    page.locator('#action-approve').click()
    page.wait_for_function("window.__demo.opened.length === 1")
    assert page.evaluate('window.__demo.opened[0]') == 'https://example.com/docs'
    page.set_viewport_size({'width': 390, 'height': 844})
    assert content.evaluate('(node) => node.scrollWidth <= node.clientWidth + 1')
    assert not errors, errors
    browser.close()
print('Markdown browser PASS: saved and completed replies, bold, headings, nested lists, italics, quotes, safe links, mobile layout')
