"""The visible provider badge must reflect verified auth AND supervisor config.

The exact report in the user's screenshot is a regression case: Claude is
already authenticated and enabled, but the UI offered 'Sign in / reconnect'
instead of confirming the connection. Also exercise manual rechecks and the
sign-in handoff without treating terminal launch as verified authentication.
"""
from pathlib import Path
import os
import shutil
from playwright.sync_api import sync_playwright

root=Path(__file__).resolve().parents[1] / 'desktop_ui' / 'static'
html=(root/'index.html').read_text(encoding='utf-8').replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root/'app.css').read_text(encoding='utf-8') + '</style>'
).replace('<script defer src="/app.js"></script>', '')
js=(root/'app.js').read_text(encoding='utf-8')

with sync_playwright() as p:
    executable=os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    options={'headless':True,'args':['--no-sandbox','--disable-dev-shm-usage']}
    if executable:options['executable_path']=executable
    browser=p.chromium.launch(**options)
    page=browser.new_page(viewport={'width':1460,'height':900})
    errors=[];native_dialogs=[]
    page.on('pageerror',lambda e: errors.append(str(e)))
    page.on('dialog',lambda d:(native_dialogs.append(d.type),d.dismiss()))
    page.set_content(html)
    page.evaluate('''() => {
      location.hash='token=provider-test';
      window.providerTest={signins:0, rechecks:0, verified:true, enabled:true};
      const app=window.providerTest;
      const reply=obj=>({ok:true,json:async()=>obj});
      window.fetch=async url=>{
        const path=String(url);
        if(path==='/api/state')return reply({project:null,projects:[],active:null});
        if(path==='/api/setup'){
          app.rechecks++;
          return reply({
            readiness:{onboarding_completed:true,engine_installed:true,full_installer_available:false,
            full_installer_note:'Update backend',components:[]},
            providers:[
              {id:'claude',name:'Claude Code',status:app.verified?'authenticated':'sign-in-needed',
              description:'Official Claude Code CLI',detail:app.verified?'CLI verified':'Sign-in needed',
              authenticated:app.verified,supervisor_enabled:app.enabled,core_enabled:true,
              sign_in_available:true,docs:''},
              {id:'codex',name:'OpenAI Codex',status:'not-installed',
              description:'Official Codex CLI',detail:'CLI not found in desktop PATH or user launcher folders.',
              authenticated:false,supervisor_enabled:false,core_enabled:true,sign_in_available:false,docs:''}
            ]});
        }
        if(path==='/api/providers/claude/signin'){
          app.signins++;return reply({opened_terminal:true,message:'Claude sign-in terminal opened'});
        }
        throw Error('Unexpected API request '+path);
      };
    }''')
    page.add_script_tag(content=js)
    page.locator('#nav-providers').click()
    page.locator('#control-dialog').wait_for(state='visible')
    claude=page.locator('.provider-row[data-provider="claude"]')
    codex=page.locator('.provider-row[data-provider="codex"]')
    claude.locator('.provider-badge.connected').get_by_text('Connected').wait_for()
    assert claude.get_by_text('Supervisor enabled').count()==1
    assert claude.get_by_text('Recheck connection').count()==1
    page.screenshot(path=os.environ.get('CB_DESKTOP_PROVIDER_SCREENSHOT', '/mnt/data/CodingBrain-Provider-Connected.png'))
    assert claude.get_by_text('Sign in with Claude Code').count()==0
    assert codex.locator('.provider-badge').get_by_text('CLI not found').count()==1
    assert codex.get_by_text('Enable supervisor').is_disabled()

    # This is read-only: the button must not launch sign-in nor change routing.
    count=page.evaluate('window.providerTest.rechecks')
    claude.get_by_text('Recheck connection').click()
    page.wait_for_function('window.providerTest.rechecks > '+str(count))
    assert page.evaluate('window.providerTest.signins')==0

    # Expired auth: cannot show connected just because supervisor is enabled.
    page.evaluate('window.providerTest.verified=false')
    page.locator('#refresh-providers').click()
    claude.locator('.provider-badge.unverified').wait_for()
    assert claude.get_by_text('Sign in with Claude Code').count()==1
    assert claude.get_by_text('Connected').count()==0

    claude.get_by_text('Sign in with Claude Code').click()
    page.locator('#action-dialog').wait_for(state='visible')
    page.keyboard.press('Escape')
    assert page.evaluate('window.providerTest.signins')==0
    claude.get_by_text('Sign in with Claude Code').click()
    page.locator('#action-approve').click()
    page.wait_for_function('window.providerTest.signins === 1')
    assert 'opened' in page.locator('#control-feedback-title').inner_text().lower()
    assert claude.locator('.provider-badge.connected').count()==0

    # Official CLI eventually succeeds. Manual refresh must convert the card
    # into a connected state, without asking for another sign-in.
    page.evaluate('window.providerTest.verified=true')
    page.locator('#refresh-providers').click()
    claude.locator('.provider-badge.connected').wait_for()
    assert claude.get_by_text('Sign in with Claude Code').count()==0
    assert page.evaluate('window.providerTest.signins')==1
    assert not native_dialogs,native_dialogs
    assert not errors,errors
    browser.close()
    print('Provider connection browser PASS: verified active, rechecks, expired auth, sign-in launch != success')
