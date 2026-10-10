"""Regression: submitting chat while engine startup is unresolved must not fake Thinking.

Real Chromium verifies that the desktop waits for the installed engine's capability
before creating a chat job. A v0.9.0 backend yields an update prompt, not a canned
reply, hanging spinner or code execution. A newer API-capable backend can proceed.
All responses are simulated; this test is NOT proof of live model integration.
"""
from pathlib import Path
import shutil
import os
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / 'desktop_ui' / 'static'
html = (root / 'index.html').read_text(encoding='utf-8').replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root / 'app.css').read_text(encoding='utf-8') + '</style>'
).replace('<script defer src="/app.js"></script>', '')
js = (root / 'app.js').read_text(encoding='utf-8')

with sync_playwright() as p:
    executable = os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    launch = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if executable:
        launch['executable_path'] = executable
    browser = p.chromium.launch(**launch)
    page = browser.new_page(viewport={'width': 1440, 'height': 900})
    errors = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.on('dialog', lambda dialog: (_ for _ in ()).throw(AssertionError('Native dialog: ' + dialog.message)))
    page.set_content(html)
    page.evaluate("""() => {
      location.hash = 'token=chat-gate-test';
      const state = window.__demo = {
        deferred: true, releaseEngine: null, modern: false,
        starts: 0, checks: 0, lastStart: null
      };
      const reply = obj => ({ok:true,json:async()=>obj});
      const status = () => ({
        engine:{available:true,version:state.modern?'0.13.0':'0.9.0',
          conversation:state.modern,typed_api:state.modern,new_project:state.modern},
        chat_available:state.modern,
        model:{provider:'ollama',name:'qwen2.5-coder:7b',ready:true},
        title:state.modern?'Engine and chat interface available':'Engine v0.9.0 needs the conversational release',
        detail:state.modern?'Verified capability in mock':'Desktop UI is online but installed engine lacks conversation.',
        next_action:state.modern?null:'updates'
      });
      window.fetch = async (url, opts) => {
        const endpoint = String(url);
        if(endpoint==='/api/state') return reply({project:null,projects:[],active:null});
        if(endpoint.startsWith('/api/engine/status')) {
          state.checks++;
          if(state.deferred) return new Promise(resolve => state.releaseEngine = () => resolve(reply(status())));
          return reply(status());
        }
        if(endpoint==='/api/setup') return reply({readiness:{
          onboarding_completed:true,engine_installed:true,components:[],
          full_installer_available:false,full_installer_note:'Upgrade required'
        },providers:[]});
        if(endpoint==='/api/start') {
          state.starts++;
          state.lastStart = JSON.parse(opts.body);
          return reply({id:'chat-test',kind:'chat',status:'running',started:Date.now()/1000});
        }
        if(endpoint.startsWith('/api/jobs/chat-test')) return reply({
          job:{id:'chat-test',kind:'chat',status:'completed'},
          events:[{seq:1,kind:'output',message:'Reply from simulated API-capable brain'}]
        });
        throw Error('Unexpected API: '+endpoint);
      };
    }""")
    page.add_script_tag(content=js)
    page.wait_for_function('window.__demo.releaseEngine !== null')
    page.locator('#prompt').fill('Hello')
    page.locator('#send').click()
    page.get_by_text('Checking installed Coding Brain engine', exact=False).wait_for()
    assert page.locator('#send').is_disabled()
    assert page.locator('#thread .typing-row').count() == 0, 'Never say Thinking while readiness is unknown'
    assert page.evaluate('window.__demo.starts') == 0, 'No job before capability check'
    page.evaluate('window.__demo.deferred=false;window.__demo.releaseEngine()')
    page.get_by_text('Your installed Coding Brain engine does not support conversational chat', exact=False).wait_for(timeout=5000)
    assert page.evaluate('window.__demo.starts') == 0
    assert page.locator('#prompt').input_value() == 'Hello', 'Unsent prompt must be preserved'
    assert page.locator('#send').is_enabled()
    assert page.locator('#thread .typing-row').count() == 0
    assert page.get_by_text('Hello! I\'m Coding Brain').count() == 0, 'No scripted greeting'

    # A subsequent engine update changes the actual capability. Recheck and
    # let the normal chat job flow proceed only after its successful verification.
    page.evaluate('window.__demo.modern=true')
    page.locator('#refresh').click()
    page.get_by_text('Chat available', exact=False).first.wait_for(timeout=5000)
    page.locator('#send').click()
    page.get_by_text('Reply from simulated API-capable brain', exact=False).wait_for(timeout=5000)
    assert page.evaluate('window.__demo.starts') == 1
    assert page.evaluate('window.__demo.lastStart.mode') == 'chat'
    assert page.locator('#thread .typing-row').count() == 0
    assert not errors, errors
    browser.close()
    print('Desktop chat gating PASS: pending engine, v0.9.0 blocked without fake thinking, recheck, later supported chat')
