"""Headless Chromium integration exercise for the real UI with stubbed safe bridge replies."""
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
output = Path(os.environ.get('CB_DESKTOP_SCREENSHOT', '/mnt/data/CodingBrain-Desktop-Improved.png'))

with sync_playwright() as p:
    chromium = os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    options = {'headless': True, 'args': ['--no-sandbox', '--disable-dev-shm-usage']}
    if chromium:
        options['executable_path'] = chromium
    browser = p.chromium.launch(**options)
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
        else if(endpoint.startsWith('/api/engine/status')) data={engine:{available:true,version:'0.13.0',conversation:true,typed_api:true,new_project:true}, chat_available:true,model:{provider:'ollama',name:'qwen2.5-coder:7b',ready:true},next_action:null,title:'Engine ready',detail:'Verified simulated engine'};
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
    # Real chat rows have visually separate, accessible avatars and aligned bubbles.
    user = page.locator('#thread .message.user').first
    assistant = page.locator('#thread .message.assistant').first
    assert user.locator('.avatar-user svg').count() == 1
    assert assistant.locator('.avatar-assistant').count() == 1
    assert assistant.get_by_text('Coding Brain',exact=True).count() == 1
    assert assistant.locator('.message-time[datetime]').count() == 1
    user_pos = user.bounding_box()
    assistant_pos = assistant.bounding_box()
    composer_pos = page.locator('.composer').bounding_box()
    bubble_pos = assistant.locator('.message-bubble').bounding_box()
    assert abs(user_pos['x'] - assistant_pos['x']) < 2, (user_pos, assistant_pos)
    assert abs(composer_pos['x'] - bubble_pos['x']) < 2, (composer_pos, bubble_pos)
    assert bubble_pos['width'] > 400
    # A second common Windows window size keeps both the thread and composer anchored.
    page.set_viewport_size({'width':1366,'height':768})
    panel = page.locator('#thread .message.assistant').first.locator('.message-bubble').bounding_box()
    composer = page.locator('.composer').bounding_box()
    assert abs(panel['x'] - composer['x']) < 2, (panel, composer)
    assert page.evaluate('document.documentElement.scrollWidth <= innerWidth + 1')
    page.set_viewport_size({'width':1600,'height':970})
    # A real, keyboard-dismissable local profile menu (not a decorative avatar).
    page.locator('#profile-trigger').click()
    assert page.locator('#profile-trigger').get_attribute('aria-expanded') == 'true'
    assert page.locator('#profile-popover').is_visible()
    assert page.get_by_text('Private to this computer').count() == 1
    page.keyboard.press('Escape')
    assert not page.locator('#profile-popover').is_visible()
    assert page.locator('#profile-trigger').get_attribute('aria-expanded') == 'false'
    page.locator('#profile-trigger').click()
    page.locator('#profile-providers').click()
    assert page.locator('#control-dialog').is_visible()
    assert page.locator('#control-providers').is_visible()
    page.locator('#control-close').click()
    # Explorer must collapse and reopen, instead of a dead icon.
    page.locator('#collapse-explorer').click()
    assert not page.locator('#explorer').is_visible()
    assert page.locator('#open-explorer').is_visible()
    page.locator('#open-explorer').click()
    assert page.locator('#explorer').is_visible()
    # Capture an empty project state as separate screenshot evidence.
    page.screenshot(path=str(output.with_name('CodingBrain-Desktop-Empty-Explorer.png')))
    page.locator('#project-picker').click()
    page.get_by_text('sample-project',exact=True).last.click()
    page.get_by_text('backend',exact=True).wait_for(timeout=2000)
    page.get_by_text('main.py',exact=True).click()
    assert 'print("hello")' in page.locator('#preview-content').inner_text()
    page.locator('#preview-close').click()
    assert page.locator('#profile-project').inner_text() == 'sample-project'
    assert not errors, errors
    page.screenshot(path=str(output))
    # Narrow windows must not lose the explorer or trigger page-level horizontal overflow.
    page.set_viewport_size({'width': 650, 'height': 850})
    page.locator('#open-explorer').click()
    assert page.locator('#explorer').is_visible()
    page.locator('#collapse-explorer').click()
    assert not page.locator('#explorer').is_visible()
    dimensions = page.evaluate('''() => ({width:innerWidth,scrollWidth:document.documentElement.scrollWidth})''')
    assert dimensions['scrollWidth'] <= dimensions['width'] + 1, dimensions
    assert not errors, errors
    browser.close()
    print('Improved desktop browser smoke PASS: first-run, 7 providers, updates, chat, folder tree, preview, no JS errors')
