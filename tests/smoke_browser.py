"""Static UI interaction smoke with mocked bridge (no live model)."""
from pathlib import Path
from playwright.sync_api import sync_playwright
root=Path(__file__).resolve().parents[1] / 'desktop_ui/static'
html=(root/'index.html').read_text().replace('<link rel="stylesheet" href="/app.css">','<style>'+ (root/'app.css').read_text() + '</style>').replace('<script defer src="/app.js"></script>','')
js=(root/'app.js').read_text()
with sync_playwright() as p:
    browser=p.chromium.launch(headless=True,executable_path='/usr/bin/chromium',args=['--no-sandbox','--disable-dev-shm-usage'])
    page=browser.new_page(viewport={'width':1440,'height':900})
    errors=[]
    page.on('pageerror',lambda err:errors.append(str(err)))
    page.set_content(html)
    page.evaluate('''() => {
      location.hash = 'token=browser-smoke';
      window.fetch = async (url, options) => {
        const endpoint=String(url);
        let data={};
        if(endpoint==='/api/state') data={project:null,projects:[{name:'sample-project',path:'/tmp/sample-project'}],active:null};
        else if(endpoint==='/api/project') data={name:'sample-project',path:'/tmp/sample-project',git:'main'};
        else if(endpoint.startsWith('/api/tree')) data={entries:[{name:'backend',path:'backend',dir:true},{name:'main.py',path:'main.py',dir:false}]};
        else if(endpoint.startsWith('/api/file')) data={path:'main.py',content:'print("hello")',bytes:14};
        else if(endpoint==='/api/start') data={id:'testjob',kind:'chat',status:'running',started:Date.now()/1000};
        else if(endpoint.startsWith('/api/jobs/testjob')) data={job:{id:'testjob',kind:'chat',status:'completed'},events:[{seq:1,kind:'output',message:'Hello! What would you like to build?',at:Date.now()/1000,job_id:'testjob'}]};
        return {ok:true,json:async()=>data};
      };
    }''')
    page.add_script_tag(content=js)
    page.get_by_text('Say hello').click()
    assert page.locator('#prompt').input_value()=='Hello'
    page.locator('#send').click()
    page.locator('#thread .message.assistant').get_by_text('Hello! What would you like to build?',exact=True).wait_for(timeout=2000)
    page.locator('#project-picker').click()
    page.get_by_text('sample-project',exact=True).last.click()
    page.get_by_text('backend',exact=True).wait_for(timeout=2000)
    page.get_by_text('main.py',exact=True).click()
    assert 'print("hello")' in page.locator('#preview-content').inner_text()
    assert not errors,errors
    page.screenshot(path='/mnt/data/codingbrain-ui-interactive-preview.png')
    browser.close()
    print('Mocked browser smoke: PASS. No JS errors, folder browsing and conversation rendered.')
