"""Exercise true pending/success/failure UI feedback with deferred backend replies in Chromium.

The test verifies that spinner and skeleton states correspond to a real unresolved request and
clear on completion. No fake percentages or simulated task-success labels are permitted.
"""
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / 'desktop_ui' / 'static'
html = (root / 'index.html').read_text(encoding='utf-8').replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root / 'app.css').read_text(encoding='utf-8') + '</style>'
).replace('<script defer src="/app.js"></script>', '')
js = (root / 'app.js').read_text(encoding='utf-8')
output = Path(os.environ.get('CB_DESKTOP_LOADING_SCREENSHOT', '/mnt/data/CodingBrain-Desktop-Loading.png'))

with sync_playwright() as p:
    chromium = os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    kwargs = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if chromium:
        kwargs['executable_path'] = chromium
    browser = p.chromium.launch(**kwargs)
    page = browser.new_page(viewport={'width': 1504, 'height': 1002})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    page.on('dialog', lambda dialog: dialog.accept())
    page.set_content(html)
    page.evaluate('''() => {
      location.hash = 'token=loading-browser-smoke';
      window.__demo = {
        setupPending: true, setupResponse: null, setupRelease: null,
        jobState: 'running', jobError: null, chatPending: false, chatRelease: null,
        checks: 0, refreshes: 0, actions: 0, chatStarted: 0
      };
      const data = window.__demo;
      const setup = {readiness:{onboarding_completed:true,engine_installed:true,full_installer_available:true,
        full_installer_note:'Official installer is available',components:[
          {id:'git',name:'Git',installed:true,detail:'Detected'},
          {id:'python',name:'Python',installed:true,detail:'Detected'},
          {id:'ollama',name:'Ollama',installed:true,detail:'Detected'},
          {id:'docker',name:'Docker',installed:true,detail:'Detected'},
          {id:'claude',name:'Claude',installed:true,detail:'Detected'},
          {id:'codex',name:'Codex',installed:false,detail:'Not in PATH'}
        ]},providers:[]};
      const reply = obj => ({ok:true,json:async()=>obj});
      window.fetch = async (url, opts) => {
        const target=String(url);
        if (target === '/api/state') return reply({project:null,projects:[],active:null});
        if (target === '/api/setup') {
          data.checks++;
          if (data.setupPending) return new Promise(resolve => data.setupRelease = () => resolve(reply(setup)));
          return reply(setup);
        }
        if (target === '/api/setup/completed') return reply({ok:true});
        if (target.startsWith('/api/system/action')) { data.actions++;return reply({job:{id:'maintenance'}}); }
        if (target.startsWith('/api/jobs/maintenance')) return reply({
          job:{id:'maintenance',status:data.jobState,error:data.jobError},
          events: data.jobState==='running'? [{seq:1,kind:'output',message:'Running official deep checks…'}] :
          [{seq:2,kind:data.jobState==='failed'?'error':'output',message:data.jobState==='failed'?'Docker sandbox did not start':'Update check finished'}]
        });
        if (target === '/api/start') {data.chatStarted++;return reply({id:'chat',kind:'chat',status:'running',started:Date.now()/1000}); }
        if (target.startsWith('/api/jobs/chat')) {
          if (data.chatPending) return new Promise(resolve => data.chatRelease = () => resolve(reply({
            job:{id:'chat',kind:'chat',status:'completed'},events:[{seq:1,kind:'output',message:'Hello from the brain',at:Date.now()/1000}]
          })));
          return reply({job:{id:'chat',kind:'chat',status:'completed'},events:[{seq:1,kind:'output',message:'Hello from the brain',at:Date.now()/1000}]});
        }
        throw Error('Unexpected mock API: ' + target);
      };
    }''')
    page.add_script_tag(content=js)
    # First-run read is intentionally held. Opening Settings reuses the same request.
    page.locator('#nav-settings').click()
    page.locator('#control-dialog').wait_for(state='visible')
    assert page.locator('#setup-components[aria-busy="true"] .skeleton-card').count() == 6
    assert page.locator('#control-feedback[data-state="busy"]').is_visible()
    assert page.locator('#setup-check.is-loading[aria-busy="true"]').count() == 1
    assert page.locator('#setup-check').is_disabled()
    assert page.locator('#control-feedback-title').inner_text() == 'Checking your environment'
    page.screenshot(path=str(output))
    page.evaluate('''() => {window.__demo.setupPending=false;window.__demo.setupRelease();}''')
    page.locator('#setup-components[aria-busy="false"] .component-card:not(.skeleton-card)').first.wait_for()
    assert page.locator('#setup-components .skeleton-card').count() == 0
    assert page.locator('#control-feedback[data-state="success"]').is_visible()
    assert page.locator('#setup-check').is_enabled()
    assert page.evaluate('window.__demo.checks') == 1, 'duplicate first-run probes'

    # A real backend job remains pending: control center shows an in-progress button/status.
    page.locator('#control-tab-updates').click()
    page.locator('#check-updates').click()
    page.locator('#control-feedback-title').get_by_text('Update check running').wait_for()
    assert page.locator('#check-updates.is-loading').is_disabled()
    assert page.locator('#control-feedback-elapsed').is_visible()
    assert page.locator('#maintenance-log').get_by_text('Running official deep checks…').count() == 1
    # No duplicate launches while this operation is running.
    page.locator('#repair-install').click()
    assert page.evaluate('window.__demo.actions') == 1
    # Backend reports failure; spinner must clear and the user can retry.
    page.evaluate('window.__demo.jobState="failed";window.__demo.jobError="Sandbox unavailable"')
    page.locator('#control-feedback[data-state="error"]').wait_for(timeout=3500)
    assert page.locator('#check-updates').is_enabled()
    assert 'Sandbox unavailable' in page.locator('#control-feedback-detail').inner_text()
    # Retry succeeds based on actual backend status, no fake success on launch.
    page.evaluate('window.__demo.jobState="completed"')
    page.locator('#check-updates').click()
    page.locator('#control-feedback[data-state="success"]').wait_for(timeout=3500)
    assert page.locator('#check-updates').is_enabled()
    page.locator('#control-close').click()

    # While the chat reply is pending, show the loading send button and a typing row.
    page.evaluate('window.__demo.chatPending=true')
    page.locator('#prompt').fill('hi')
    page.locator('#send').click()
    page.locator('#send.is-loading[aria-busy="true"]').wait_for()
    assert page.locator('#send').is_disabled()
    assert page.locator('#thread .typing-row').count() == 1
    # A second send must not launch another task.
    page.locator('#prompt').fill('second request')
    page.locator('#prompt').press('Enter')
    assert page.evaluate('window.__demo.chatStarted') == 1
    page.evaluate('window.__demo.chatPending=false;window.__demo.chatRelease()')
    page.locator('#thread .message.assistant').get_by_text('Hello from the brain').wait_for(timeout=3500)
    assert page.locator('#thread .typing-row').count() == 0
    assert page.locator('#send').is_enabled()
    assert page.locator('#send').get_attribute('aria-busy') is None
    assert not errors, errors
    browser.close()
    print('Desktop loading smoke PASS: skeleton, disabled buttons, progress, failure/retry, chat typing, no duplicate actions')
