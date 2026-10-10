"""Live Git tab must update branch/commits/working tree without a manual refresh."""
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
output = Path(os.environ.get("CB_DESKTOP_GIT_SCREENSHOT", "/mnt/data/CodingBrain-Desktop-Git.png"))

with sync_playwright() as p:
    chromium = os.environ.get("CB_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage"]}
    if chromium:
        options["executable_path"] = chromium
    browser = p.chromium.launch(**options)
    page = browser.new_page(viewport={"width": 1480, "height": 940})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("dialog", lambda dialog: (_ for _ in ()).throw(
        AssertionError("Browser alert/confirm is forbidden: " + dialog.message)))
    page.set_content(html)
    page.evaluate("""() => {
      location.hash = 'token=git-smoke';
      const state = window.__repo = {
        branch:'main', head:'a'.repeat(40), changes:[], commits:[
          {sha:'a'.repeat(40),short:'aaaaaaa',subject:'Initial repository',
           author:'Local developer',date:'2026-10-10T12:00:00+00:00'}
        ],calls:0
      };
      const project = {name:'sample',path:'C:/projects/sample',git:'main'};
      const response = x => ({ok:true,json:async()=>x});
      window.fetch = async (url, opts) => {
        const path = String(url);
        if (path === '/api/state')
          return response({project,projects:[project],active:null});
        if (path.startsWith('/api/engine/status'))
          return response({engine:{available:true,version:'0.13.0.dev9',conversation:true,
              typed_api:true,new_project:true},chat_available:true,
              model:{provider:'ollama',name:'qwen3:4b',ready:true},
              next_action:null,title:'Local engine ready',detail:'Model verified'});
        if (path === '/api/setup')
          return response({readiness:{onboarding_completed:true,engine_installed:true,
                full_installer_available:false,full_installer_note:'Already installed',
                components:[]},providers:[]});
        if (path.startsWith('/api/tree'))
          return response({entries:[]});
        if (path === '/api/git/snapshot') {
          state.calls++;
          return response({available:true,branch:state.branch,detached:false,
            head:state.head,branches:['main','feature/live'],commits:state.commits,
            changes:state.changes,
            changes_truncated:false,
            counts:{staged:0,modified:state.changes.length,untracked:0,conflicted:0}});
        }
        throw new Error('Unexpected API: ' + path);
      };
    }""")
    page.add_script_tag(content=js)
    page.locator("#branch-name").click()
    page.locator("#git-panel").wait_for(state="visible")
    page.wait_for_function("document.getElementById('git-current-branch').textContent === 'main'", timeout=9000)
    assert page.locator("#git-commits .git-commit-row").count() == 1
    assert "Initial repository" in page.locator("#git-commits").inner_text()
    assert page.locator("#git-changes").get_by_text("Working tree clean").count() == 1
    initial_calls = page.evaluate("window.__repo.calls")

    # Change the state behind the local API. The UI has to pick it up by its
    # automatic ~3-second timer, without a Refresh click or a tab change.
    page.evaluate("""() => {
      const state=window.__repo;
      state.branch='feature/live';
      state.head='b'.repeat(40);
      state.changes=[{path:'src/changed.py',status:' M',staged:false,untracked:false,conflicted:false}];
      state.commits.unshift({sha:state.head,short:'bbbbbbb',
        subject:'<img src=x onerror=alert(1)> safely shown as text',
        author:'Local developer',date:'2026-10-10T12:04:00+00:00'});
    }""")
    page.wait_for_function(
        "document.getElementById('git-current-branch').textContent === 'feature/live'",
        timeout=9000,
    )
    assert "feature/live" in page.locator("#branch-name").inner_text()
    assert "src/changed.py" in page.locator("#git-changes").inner_text()
    assert page.locator("#git-commits .git-commit-row").count() == 2
    assert page.locator("#git-commits img").count() == 0, "Commit messages must never be HTML"
    assert "<img src=x onerror=alert(1)>" in page.locator("#git-commits").inner_text()
    assert page.evaluate("window.__repo.calls") > initial_calls
    assert page.locator("#git-updated").inner_text().startswith("Live · Checked")

    page.screenshot(path=str(output))
    assert not errors, errors
    browser.close()
    print("Live Git UI PASS: initial history, branch switch, auto-refresh, change list, safe commit text")
