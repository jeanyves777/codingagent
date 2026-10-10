"""Headless Chromium integration exercise for the real UI with stubbed safe bridge replies."""
from pathlib import Path
import os
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / 'desktop_ui' / 'static'
html = (root / 'index.html').read_text(encoding='utf-8').replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root / 'app.css').read_text(encoding='utf-8') + '</style'
).replace('<script defer src="/app.js"></script>', '')
js = (root / 'app.js').read_text(encoding='utf-8')
output = Path(os.environ.get('CB_DESKTOP_SCREENSHOT', '/mnt/data/CodingBrain-Desktop-Improved.png'))

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, executable_path='/usr/bin/chromium', args=['--no-sandbox','--disable-dev-shm-usage'])
    page = browser.new_page(viewport={'width':1600,'height':970}, device_scale_factor=1)
    errors=[]
    page.on('pageerror', lambda error:errors.append(str(error)))
    page.set_content(html)
    page.evaluate('''() => {
      location.hash = 'token=browser-smoke';
      window.fetch = async (url, options) => {
        const endpoint=String(url);
        let data={};
        if(endpoint==='/api/state') data={project:null,projects:[{name:'sample-project',path:'/tmp/sample-project'}],active:null};
        else if(endpoint==='/api/setup') data={readiness:{onboarding_completed:false,engine_installed:true,full_installer_available:true,full_installer_note:'Official guided installer ready',components:[{id:'git',name:'Git',installed:true,detail:'Found on your computer'},{id:'ollama',name:'Ollama',installed:true,detail:'Found on your computer'},{id:'docker',name:'Docker',installed:true,detail:'Found on your computer'},{id:'claude',name:'Claude',installed:true,detail:'Found on your computer'},{id:'codex',name:'Codex',installed:true,detail:'Found on your computer'},{id:'python',name:'Python',installed:true,detail:'Found on your computer'}]}, providers:[{id:'ollama',name:'Ollama / Qwen',description:'Local primary coding model',status:'installed',core_enabled:true,docs:'',sign_in_available:false},{id:'claude',name:'Claude Code',description:'Subscription-based supervisor',status:'authenticated',core_enabled:true,docs:'',sign_in_available:true},{id:'codex',name:'OpenAI Codex',description:'ChatGPT subscription supervisor',status:'authenticated',core_enabled:true,docs:'',sign_in_available:true},{id:'gemini',name:'Google Gemini CLI',description:'Additional connected coding provider',status:'not-installed',core_enabled:false,docs:'',sign_in_available:false},{id:'grok',name:'xAI Grok',description:'API-key only; cost approval required',status:'api-key-needed',core_enabled:false,docs:'',sign_in_available:false},{id:'meta',name:'Meta Llama',description:'Local through Ollama',status:'local-capability',core_enabled:false,docs:'',sign_in_available:false},{id:'muse',name:'Meta Muse / Mouse',description:'Future provider',status:'planned',core_enabled:false,docs:'',sign_in_available:false}]};
        else if(endpoint==='/api/setup/completed') data={ok:true,onboarding_completed:true};
        else if(endpoint==='/api/project') data={name:'sample-project',path:'/tmp/sample-project',git:'main'};
        else if(endpoint.startsWith('/api/tree')) data={entries:[{name:'backend',path:'backend',dir:true},{name:'main.py',path:'main.py',dir:false}]};
        else if(endpoint.startsWith('/api/file')) data={path:'main.py',content:'print("hello")',bytes:14};
        else if(endpoint==='/api/start') data={id:'testjob',kind:'chat',status:'running',started:Date.now()/1000};
        else if(endpoint.startsWith('/api/jobs/testjob')) data={job:{id:'testjob',kind:'chat',status:'completed'},events:[{seq:1,kind:'output',message:'Hello! What would you like to build?',at:Date.now()/1000,job_id:'testjob'}]};
        return {ok:true,json:async()=>data};
      };
    }''')
    page.add_script_tag(content=js)
    page.locator('#control-dialog').wait_for(state='visible',timeout=4000)
    assert page.locator('#setup-components .component-card').count()==6
    page.locator('#control-tab-providers').click()
    assert page.locator('#provider-list .provider-row').count()==7
    assert page.locator('#provider-list').get_by_text('Claude Code').count()==1
    page.screenshot(path=str(output.with_name('CodingBrain-Desktop-Providers.png')))
    page.locator('#control-tab-updates').click()
    assert page.get_by_text('Install latest stable').count()==1
    page.locator('#control-tab-setup').click()
    page.locator('#setup-continue').click()
    page.locator('#control-dialog').wait_for(state='hidden',timeout=2000)
    page.get_by_text('Start a conversation').click()
    assert page.locator('#prompt').input_value()=='Hello'
    page.locator('#send').click()
    page.locator('#thread .message.assistant').get_by_text('Hello! What would you like to build?',exact=True).wait_for(timeout=3000)
    # Completed chats shouldn't fill the transcript with fake task-finished bubbles.
    assert page.get_by_text('Session finished.').count()==0
    page.locator('#project-picker').click()
    page.get_by_text('sample-project',exact=True).last.click()
    page.get_by_text('backend',exact=True).wait_for(timeout=2000)
    page.get_by_text('main.py',exact=True).click()
    assert 'print("hello")' in page.locator('#preview-content').inner_text()
    page.locator('#preview-close').click()
    assert not errors, errors
    page.screenshot(path=str(output))
    browser.close()
    print('Improved desktop browser smoke PASS: first-run, 7 providers, updates, chat, folder tree, preview, no JS errors')
