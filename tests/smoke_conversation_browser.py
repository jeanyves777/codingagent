"""Directing work from the conversation: the saved conversation (with task milestones) loads for the
selected project, the context bar says what the model saw, proposed work becomes a button, replying
"yes" starts it through the normal reviewed flow, and New conversation starts fresh."""
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
      location.hash = 'token=conversation-smoke';
      const at = 1791662000;
      const project = {name: 'calc', path: 'C:/p/calc', git: 'master'};
      const demo = window.__demo = {starts: [], resets: 0, jobs: {}, turns: [
        {role: 'user', content: 'add a subtract function'},
        {role: 'assistant', content: 'I can do that as a task.'},
        {role: 'assistant', content: '[Task] Accepted "add subtract" on branch codingbrain/add-subtract.'}]};
      const response = x => ({ok: true, status: 200, json: async () => x});
      window.fetch = async (url, opts = {}) => {
        const path = String(url);
        if (path === '/api/health') return response({ok: true, boot: 'b', installed_version: '0.13.0', active: null});
        if (path === '/api/state') return response({project, projects: [project], active: null});
        if (path.startsWith('/api/engine/status')) return response({engine: {available: true, version: '0.13.0', conversation: true, typed_api: true, new_project: true},
          chat_available: true, model: {provider: 'ollama', name: 'qwen2.5-coder:3b', ready: true}, next_action: null, title: 'ok', detail: ''});
        if (path === '/api/setup') return response({readiness: {onboarding_completed: true, components: []}, providers: []});
        if (path.startsWith('/api/tree')) return response({entries: []});
        if (path === '/api/git/snapshot') return response({available: false, reason: 'demo'});
        if (path === '/api/conversation') return response({project: 'calc', turns: demo.turns, available: true});
        if (path === '/api/conversation/reset') { demo.resets++; demo.turns = []; return response({reset: true}); }
        if (path === '/api/project') return response(project);
        if (path.startsWith('/api/documents/open')) { demo.opened = [JSON.parse(opts.body).path, path.includes('reveal')]; return response({opened: true}); }
        if (path === '/api/start') {
          const body = JSON.parse(opts.body); demo.starts.push(body);
          const id = 'j' + demo.starts.length;
          const ev = (seq, kind, message) => ({seq, kind, message, at: at + seq, job_id: id});
          if (body.mode === 'chat' && body.message.includes('pdf')) {  // chat only proposes the document
            demo.jobs[id] = [ev(1, 'output', 'I will write a PDF: A two-page history of Rome. Confirm to create it.'),
              ev(2, 'action', JSON.stringify({kind: 'document', request: 'A two-page history of Rome', format: 'pdf', language: null})),
              ev(3, 'finish', 'Chat completed')];
            return response({id, kind: 'chat', status: 'running', started: at});
          }
          if (body.mode === 'document') {  // written only after the user confirmed
            demo.documents = (demo.documents || 0) + 1;
            demo.jobs[id] = [ev(1, 'step', 'Writing the document'), ev(2, 'output', 'Created PDF "Rome": C:/Users/me/Documents/Coding Brain/rome.pdf'),
              ev(3, 'file', JSON.stringify({path: 'C:/Users/me/Documents/Coding Brain/rome.pdf', format: 'pdf', title: 'Rome'})), ev(4, 'finish', 'Document completed')];
            return response({id, kind: 'document', status: 'running', started: at});
          }
          demo.jobs[id] = body.mode === 'chat' ? [
            ev(1, 'output', 'Yes — I can add that as a task in calc.'),
            ev(2, 'context', JSON.stringify({project: 'calc', history_turns: 3, history_left_out: 2, project_context: 900, project_context_trimmed: false, window: 8192})),
            ev(3, 'action', JSON.stringify({kind: 'task', goal: 'add a multiply function with a test', project: 'calc', root: 'C:/p/calc'})),
            ev(4, 'finish', 'Chat completed')] : [ev(1, 'stage', 'Planning'), ev(2, 'finish', 'done')];
          return response({id, kind: body.mode, status: 'running', started: at});
        }
        const match = path.match(/^\\/api\\/jobs\\/(j\\d+)/);
        if (match) return response({job: {id: match[1], kind: demo.starts[Number(match[1].slice(1)) - 1].mode, status: 'completed'}, events: demo.jobs[match[1]]});
        throw new Error('Unexpected API: ' + path);
      };
    }""")
    page.add_script_tag(content=js)

    # 1. The saved conversation of the selected project loads, task milestones included.
    page.locator('.message.user', has_text='add a subtract function').wait_for()
    assert page.locator('.message.system', has_text='Accepted "add subtract" on branch').count() == 1
    assert page.locator('.message.history time', has_text='earlier').count() == 3
    assert 'calc' in page.locator('#context-text').inner_text()

    # 2. Asking for work in chat: the reply proposes it as a button; the context bar shows what was sent.
    page.fill('#prompt', 'can u add multiply too')
    page.click('#send')
    page.locator('.action-buttons button', has_text='Run as task in calc').wait_for()
    context = page.locator('#context-text').inner_text()
    assert '3 earlier messages' in context and '2 older left out' in context and '8K window' in context, context

    # 3. Replying "yes" starts it in Run task mode, through the normal reviewed flow.
    page.fill('#prompt', 'yes')
    page.click('#send')
    page.wait_for_function("window.__demo.starts.length === 2")
    second = page.evaluate("window.__demo.starts[1]")
    assert second == {"message": "add a multiply function with a test", "mode": "run", "attachments": []}, second

    # 4. New conversation clears what the model will see, after confirmation.
    page.locator('#footer-status', has_text='Task').wait_for()
    page.wait_for_function("!document.getElementById('send').disabled")
    page.click('#new-conversation')
    page.locator('#action-approve').click()
    page.wait_for_function("window.__demo.resets === 1")
    page.locator('#welcome:not([hidden])').wait_for()

    # 5. A document request returns the file, with Open and Show in folder.
    page.click('#mode-chat')
    page.fill('#prompt', 'make me a pdf about the history of Rome in two pages')
    page.click('#send')
    page.locator('.action-buttons button', has_text='Create the PDF').wait_for()
    assert page.evaluate("window.__demo.documents || 0") == 0  # nothing written from chat alone
    page.locator('.action-buttons button', has_text='Create the PDF').click()
    page.locator('.action-buttons button', has_text='Open PDF').click()
    assert page.evaluate("window.__demo.documents") == 1
    page.wait_for_function("window.__demo.opened && window.__demo.opened[1] === false")
    page.locator('.action-buttons button', has_text='Show in folder').click()
    page.wait_for_function("window.__demo.opened[1] === true")

    assert not errors, errors
    browser.close()
print("Conversation browser PASS: saved conversation + task milestones load, context bar, action button, 'yes' starts the task, new conversation, document proposed, created only after confirmation, Open / Show in folder")
