"""UI bridge online is not engine ready; show Ollama and premium readiness truthfully."""
from pathlib import Path
import os, shutil
from playwright.sync_api import sync_playwright

root = Path(__file__).resolve().parents[1] / 'desktop_ui' / 'static'
html = (root/'index.html').read_text(encoding='utf-8').replace(
    '<link rel="stylesheet" href="/app.css">',
    '<style>' + (root/'app.css').read_text(encoding='utf-8') + '</style>'
).replace('<script defer src="/app.js"></script>', '')
js = (root/'app.js').read_text(encoding='utf-8')

with sync_playwright() as p:
    executable=os.environ.get('CB_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')
    options={'headless':True,'args':['--no-sandbox','--disable-dev-shm-usage']}
    if executable:options['executable_path']=executable
    browser=p.chromium.launch(**options)
    page=browser.new_page(viewport={'width':1440,'height':900})
    errors=[]; native_dialogs=[]
    page.on('pageerror',lambda x:errors.append(str(x)))
    page.on('dialog',lambda d:(native_dialogs.append(d.type),d.dismiss()))
    page.set_content(html)
    page.evaluate('''() => {
      location.hash='token=diagnostics-test';
      window.testStatus={modern:false, model:true, setupCalls:0};
      const reply=obj=>({ok:true,json:async()=>obj});
      window.fetch=async url=>{
        const path=String(url);
        if(path==='/api/state')return reply({project:null,projects:[],active:null});
        if(path.startsWith('/api/engine/status'))return reply({
          bridge_online:true,
          engine:{available:true,version:window.testStatus.modern?'0.12.0':'0.9.0',
                  typed_api:window.testStatus.modern,conversation:window.testStatus.modern},
          chat_available:window.testStatus.modern,
          model:{provider:'ollama',name:'qwen2.5-coder:7b',ready:window.testStatus.model,
                 ollama_running:true,installed_models:window.testStatus.model?['qwen2.5-coder:7b']:[]},
          title:window.testStatus.modern?'Engine and chat interface available':'Engine v0.9.0 needs the conversational release',
          detail:'The desktop bridge is online but chat requires an updated engine.',
          next_action:window.testStatus.modern?null:'updates'});
        if(path==='/api/setup'){
          window.testStatus.setupCalls++;
          return reply({readiness:{onboarding_completed:true,full_installer_available:false,
              full_installer_note:'Install newer engine',components:[],engine_installed:true},
            providers:[
            {id:'ollama',name:'Ollama / Qwen',status:'model-detected',model_name:'qwen2.5-coder:7b',
             model_ready:true,ollama_running:true,detail:'Qwen detected',description:'Local worker',core_enabled:true,docs:''},
            {id:'claude',name:'Claude Code',status:'authenticated',authenticated:true,supervisor_enabled:true,
             description:'Subscription supervisor',core_enabled:true,docs:''},
            {id:'codex',name:'OpenAI Codex',status:'not-installed',authenticated:false,supervisor_enabled:false,
             description:'Subscription supervisor',core_enabled:true,docs:''}]});
        }
        throw Error('Unexpected API request '+path);
      };
    }''')
    page.add_script_tag(content=js)
    page.get_by_text('Engine v0.9.0 needs the conversational release').wait_for()
    assert page.locator('#engine-dot.green').count()==0, 'Legacy engine should never render a green indicator'
    assert 'Chat update needed' in page.locator('#engine-label').inner_text()
    assert 'qwen2.5-coder:7b' in page.locator('#model-label').inner_text()
    page.locator('#readiness-claude').get_by_text('Claude: connected').wait_for()
    page.locator('#readiness-codex').get_by_text('Codex: CLI not found').wait_for()
    assert 'detected' in page.locator('#readiness-local').inner_text()
    page.screenshot(path=os.environ.get('CB_DESKTOP_ENGINE_SCREENSHOT','/mnt/data/CodingBrain-Engine-Status.png'))
    page.locator('#engine-banner-action').click()
    assert page.locator('#control-updates').is_visible()
    page.locator('#control-close').click()
    page.evaluate('window.testStatus.modern=true')
    page.locator('#refresh').click()
    page.locator('#engine-dot.green').wait_for()
    assert 'Chat available' in page.locator('#engine-label').inner_text()
    assert not page.locator('#engine-banner').is_visible()
    assert not native_dialogs, native_dialogs
    assert not errors, errors
    browser.close()
    print('Desktop engine status browser PASS: legacy warning, Qwen model, Claude verified, Codex missing, updated API ready')
