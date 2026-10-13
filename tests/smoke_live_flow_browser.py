"""Agent flow, ownership, progress, code display, attachments and reconnect in a real browser."""
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
output = Path(os.environ.get("CB_DESKTOP_FLOW_SCREENSHOT", "/mnt/data/CodingBrain-Desktop-Flow.png"))

with sync_playwright() as p:
    chromium = os.environ.get("CB_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if chromium:
        options["executable_path"] = chromium
    browser = p.chromium.launch(**options)
    page = browser.new_page(viewport={"width": 1480, "height": 940})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: (_ for _ in ()).throw(AssertionError("Browser dialogs are forbidden")))
    page.set_content(html)
    page.evaluate("""() => {
      location.hash = 'token=flow-smoke';
      const project = {name:'calc',path:'C:/projects/calc',git:'main'};
      const at = 1791662000;
      const ev = (seq, kind, message, data) => ({seq, kind, message, at: at + seq, job_id:'j1', data});
      const demo = window.__demo = {online:true, boot:'b1', version:'0.11.0', starts:[], uploads:0, events:[
        ev(1,'start','Starting the Coding Brain engine'),
        ev(2,'stage','goal COMPLETED: Fix add()', {phase:'goal',status:'COMPLETED',agent:'user'}),
        ev(3,'stage','planning RUNNING: planning', {phase:'planning',status:'RUNNING',agent:'coding_brain'}),
        ev(4,'stage','planning COMPLETED: plan ready', {phase:'planning',status:'COMPLETED',agent:'implementer',provider:'ollama',model:'qwen2.5-coder:7b'}),
        ev(5,'stage','review RUNNING: reviewing', {phase:'review',status:'RUNNING',agent:'reviewer',provider:'claude_cli',model:'claude-sonnet'}),
        ev(6,'stage','sandbox RUNNING: running tests', {phase:'sandbox',status:'RUNNING',agent:'sandbox'}),
        ev(7,'output','Here is the fix:\\n```python\\ndef add(a, b):\\n    # sum\\n    return a + b\\n```\\nCall `add(1, 2)`.')
      ], status:'running'};
      const response = (x, ok=true, status=200) => ({ok, status, json: async () => x});
      window.fetch = async (url, opts={}) => {
        const path = String(url);
        if (!demo.online) throw new TypeError('Failed to fetch');
        if (path === '/api/health') return response({ok:true, boot:demo.boot, installed_version:demo.version, active:null});
        if (path === '/api/state') return response({project, projects:[project], active:null});
        if (path.startsWith('/api/engine/status'))
          return response({engine:{available:true,version:demo.version,conversation:true,typed_api:true,new_project:true,attachments:true},
            chat_available:true, model:{provider:'ollama',name:'qwen2.5-coder:7b',ready:true}, next_action:null,
            title:'Engine and chat interface available', detail:'ok'});
        if (path === '/api/setup') return response({readiness:{onboarding_completed:true,components:[]},providers:[]});
        if (path.startsWith('/api/tree')) return response({entries:[]});
        if (path === '/api/git/snapshot') return response({available:false, reason:'demo'});
        if (path === '/api/attachments/upload') { demo.uploads++; return response({path:'C:/staging/'+decodeURIComponent(opts.headers['X-Filename']), name:decodeURIComponent(opts.headers['X-Filename']), bytes:2048}); }
        if (path === '/api/start') {
          demo.starts.push(JSON.parse(opts.body));
          return response({id:'j1', kind:'run', status:'running', started:at, project:project.path});
        }
        if (path.startsWith('/api/jobs/j1')) {
          const after = Number(new URL('http://x' + path).searchParams.get('after') || 0);
          return response({job:{id:'j1',kind:'run',status:demo.status,project:project.path,approval:null},
                           events: demo.events.filter(e => e.seq > after)});
        }
        throw new Error('Unexpected API: ' + path);
      };
    }""")
    page.add_script_tag(content=js)
    page.wait_for_function("document.getElementById('engine-label').textContent.includes('0.11.0') || document.getElementById('footer-status').textContent === 'Ready'")

    # Attachments: a dropped/pasted file becomes a removable chip and travels with Run task.
    page.evaluate("""async () => {
      const file = new File(['hello'], 'design.png', {type:'image/png'});
      const data = new DataTransfer(); data.items.add(file);
      document.querySelector('.composer').dispatchEvent(new DragEvent('drop', {dataTransfer:data, bubbles:true, cancelable:true}));
    }""")
    page.locator('#attachment-list .attachment-name', has_text='design.png').wait_for()
    # Chat mode refuses to silently drop files.
    page.fill('#prompt', 'Fix add()')
    page.click('#send')
    assert page.evaluate('window.__demo.starts.length') == 0
    assert page.locator('#attachment-list .attachment').count() == 1
    page.click('#mode-run')
    page.click('#send')
    page.wait_for_function('window.__demo.starts.length === 1')
    assert page.evaluate('window.__demo.starts[0].attachments') == ['C:/staging/design.png']
    assert page.locator('#attachment-list').is_hidden()

    # Agent flow with ownership: local brain vs provider brain vs tools.
    page.click('#tab-flow')
    page.locator('.agent-lane', has_text='Reviewer').wait_for()
    reviewer = page.locator('.agent-lane', has_text='Reviewer')
    assert 'Claude' in reviewer.locator('.owner-badge').inner_text()
    implementer = page.locator('.agent-lane', has_text='Implementer')
    assert 'Local brain' in implementer.locator('.owner-badge').inner_text()
    assert 'qwen2.5-coder:7b' in implementer.locator('.owner-badge').inner_text()
    assert 'Docker sandbox' in page.locator('.agent-lane', has_text='Test sandbox').inner_text()
    headings = page.locator('.owner-heading').all_inner_texts()
    assert any('Local brain' in h for h in headings) and any('Provider brains' in h for h in headings)
    # Progress is an estimate from completed stages, below 100 while running.
    percent = int(page.locator('#flow-percent').inner_text().rstrip('%'))
    assert 28 <= percent < 100, percent
    assert page.locator('#run-progress').is_visible()
    assert page.locator('.stage.state-completed', has_text='Planning').count() == 1
    # Actual engine activity is visible in the conversation, not only the side panel.
    card = page.locator('.inline-activity').first
    assert card.is_visible()
    assert 'running tests' in card.locator('.inline-activity-heading').inner_text()
    assert 'qwen2.5-coder:7b' in card.inner_text()
    assert card.locator('.inline-activity-log li').count() == 6
    assert page.locator('.typing-row').count() == 0
    card.locator('summary').click()
    assert not card.locator('.inline-activity-log').is_visible()
    card.locator('summary').click()
    assert card.locator('.inline-activity-log').is_visible()

    # Code display: fenced block with language, copy button and highlighting; inline code.
    block = page.locator('.message-content .code-block').first
    block.wait_for()
    assert block.locator('.code-lang').inner_text() == 'python'
    assert block.locator('.tok-k', has_text='def').count() == 1
    assert block.locator('.tok-c').count() == 1
    assert page.locator('.message-content .inline-code', has_text='add(1, 2)').count() == 1
    assert page.locator('.message-content script').count() == 0
    page.screenshot(path=str(output))

    # Heartbeats report waiting without inventing new steps; failures remain visible inline.
    page.evaluate("window.__demo.events.push({seq:8,kind:'heartbeat',message:'Request still in flight; no new output',at:1791662008,job_id:'j1'})")
    page.wait_for_function("document.querySelector('.inline-activity-signal').textContent.includes('no new output')")
    assert card.locator('.inline-activity-log li').count() == 6
    page.evaluate("window.__demo.events.push({seq:9,kind:'stage',message:'Requirements request failed: ReadTimeout',at:1791662009,job_id:'j1',data:{event_type:'model_response',phase:'requirements',status:'FAILED'}})")
    card.locator('.activity-failed', has_text='ReadTimeout').wait_for()

    # Completion fills the bar to 100 %.
    page.evaluate("window.__demo.events.push({seq:10,kind:'finish',message:'done',at:1791662010,job_id:'j1'}); window.__demo.status='completed'")
    page.wait_for_function("document.getElementById('flow-percent').textContent === '100%'")
    assert card.locator('.inline-activity-heading strong').inner_text() == 'Task completed'
    assert card.locator('.inline-activity-log li').count() == 8

    # Reconnect: losing the bridge shows a live state and recovers by itself.
    page.evaluate('window.__demo.online = false')
    page.wait_for_function("document.getElementById('connection-label').textContent.startsWith('Reconnecting')", timeout=15000)
    assert 'red' in page.locator('#connection-dot').get_attribute('class')
    page.evaluate('window.__demo.online = true')
    page.wait_for_function("document.getElementById('connection-label').textContent === 'Desktop connected'", timeout=15000)

    # An engine update is noticed without a manual refresh.
    page.evaluate("window.__demo.version = '0.12.0'")
    page.evaluate("document.dispatchEvent(new Event('visibilitychange'))")
    page.wait_for_function("document.getElementById('toast').textContent.includes('v0.12.0')", timeout=15000)

    assert not errors, errors
    browser.close()
print("Live flow browser PASS: attachments, local vs provider agents, progress, code blocks, reconnect, engine switch")
