"""The desktop consent surface must be in-app, keyboard-safe and non-approving by default.

Browser alerts, confirmations, and prompts are always a regression. This test runs the
actual shipped HTML/CSS/JS with mocked loopback operations and proves a refused action
makes no request. It also covers the legacy doctor --full fallback label.
"""
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / "desktop_ui" / "static"
html = (root / "index.html").read_text(encoding="utf-8").replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root / "app.css").read_text(encoding="utf-8") + '</style>'
).replace('<script defer src="/app.js"></script>', '')
js = (root / "app.js").read_text(encoding="utf-8")

with sync_playwright() as p:
    chromium = os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    options = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if chromium:
        options['executable_path'] = chromium
    browser = p.chromium.launch(**options)
    page = browser.new_page(viewport={'width': 1400, 'height': 950})
    errors = []
    native_dialogs = []
    page.on('pageerror', lambda err: errors.append(str(err)))
    page.on('dialog', lambda dialog: (native_dialogs.append(dialog.type), dialog.dismiss()))
    page.set_content(html)
    page.evaluate('''() => {
        location.hash = 'token=confirm-browser-test';
        window.testApp = { actions: [], signins: [], configures: [] };
        const ready = {readiness:{onboarding_completed:true,engine_installed:true,
          full_installer_available:false,deep_doctor_available:false,
          full_installer_note:'Update backend for full installer',components:[
            {id:'python',name:'Python',installed:true,detail:'Found'},
            {id:'git',name:'Git',installed:true,detail:'Found'}
          ]},providers:[{id:'claude',name:'Claude Code',description:'Official CLI',status:'authenticated',
            core_enabled:true,docs:'',sign_in_available:true,supervisor_enabled:false}]};
        const reply = obj => ({ok:true,json:async()=>obj});
        window.fetch = async (url, request={}) => {
          const path=String(url);
          if(path==='/api/state') return reply({project:null,projects:[],active:null});
          if(path==='/api/setup') return reply(ready);
          if(path.startsWith('/api/system/action')) {
            const name=new URL(path,'http://127.0.0.1').searchParams.get('action');
            window.testApp.actions.push(name);
            return reply({opened_terminal:false,diagnostic_level:name==='doctor'?'basic':null,
                          job:{id:'job'+window.testApp.actions.length,status:'starting'}});
          }
          if(path.startsWith('/api/jobs/')) return reply({job:{id:'one',status:'completed'},events:[
            {seq:1,kind:'output',message:'Health checks finished',at:Date.now()/1000}
          ]});
          if(path.endsWith('/signin')) {window.testApp.signins.push(path);return reply({opened_terminal:true,message:'Sign-in launched'});}
          if(path.endsWith('/configure')) {window.testApp.configures.push(path);return reply({message:'Updated'});}
          throw Error('Unexpected request: ' + path);
        };
    }''')
    page.add_script_tag(content=js)
    page.locator('#nav-settings').click()
    page.locator('#control-dialog').wait_for(state='visible')
    page.locator('#deep-check').get_by_text('Basic system check').wait_for()

    # Opening consent must never start the action. Escape refuses it.
    page.locator('#deep-check').click()
    page.locator('#action-dialog').wait_for(state='visible')
    assert page.locator('#action-description').inner_text().find('does not support doctor --full') != -1
    assert page.evaluate('window.testApp.actions.length') == 0
    page.keyboard.press('Escape')
    page.locator('#action-dialog').wait_for(state='hidden')
    assert page.evaluate('window.testApp.actions.length') == 0

    # Cancel button refuses the update check as well.
    page.locator('#control-tab-updates').click()
    page.locator('#check-updates').click()
    page.locator('#action-cancel').click()
    assert page.evaluate('window.testApp.actions.length') == 0

    # Only affirmative button authorizes a request, and the UI labels basic verification honestly.
    page.locator('#control-tab-setup').click()
    page.locator('#deep-check').click()
    page.locator('#action-approve').click()
    page.locator('#action-dialog').wait_for(state='hidden')
    page.locator('#control-feedback[data-state="success"]').wait_for(timeout=4000)
    page.wait_for_function('window.testApp.actions.length === 1')
    page.locator('#control-feedback-title').get_by_text('Basic system check completed').wait_for(timeout=4000)
    assert page.evaluate('window.testApp.actions') == ['doctor']
    assert 'Basic' in page.locator('#control-feedback-title').inner_text()
    assert 'Deep model' in page.locator('#control-feedback-detail').inner_text()

    # Provider sign-in follows the same contract: no action on Escape.
    page.locator('#control-tab-providers').click()
    page.locator('#provider-list button').get_by_text('Sign in / reconnect').click()
    page.locator('#action-dialog').wait_for(state='visible')
    page.keyboard.press('Escape')
    assert page.evaluate('window.testApp.signins.length') == 0
    page.locator('#provider-list button').get_by_text('Sign in / reconnect').click()
    page.locator('#action-approve').click()
    page.wait_for_function('window.testApp.signins.length === 1')

    assert not native_dialogs, native_dialogs
    assert not errors, errors
    browser.close()
    print('Desktop confirmation smoke PASS: no browser alerts, Escape/Cancel refuse, explicit approve, legacy doctor basic check')
