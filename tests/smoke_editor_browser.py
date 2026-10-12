"""Monaco editor, save with conflict guard, side-by-side proposal review and streaming replies,
against the real desktop bridge (uvicorn) with the bundled editor files.

    python scripts/fetch_monaco.py && python tests/smoke_editor_browser.py
"""
from pathlib import Path
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from desktop_ui.server import Job, Workspace, create_app  # noqa: E402

if not (Path(__file__).resolve().parents[1] / "desktop_ui/static/vendor/monaco/vs/loader.js").is_file():
    raise SystemExit("Run scripts/fetch_monaco.py first")

home = Path(tempfile.mkdtemp(prefix="cb-editor-"))
project = home / "calc"
project.mkdir()
(project / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
(project / ".env").write_text("SECRET=1\n", encoding="utf-8")
subprocess.run(["git", "init", "-q", str(project)], check=True)

workspace = Workspace(home=home, executable="not-a-real-binary")
workspace.choose(str(project))
# A task waiting for approval of a proposal, as the engine would leave it.
job = Job("run", str(project), ["codingbrain", "run", "add subtract"])
job.status, job.core, job.awaiting_approval = "approval_required", True, True
job.pending_id = "approval-1"
job.pending_payload = {"kind": "proposal", "summary": "Add subtract()", "files": ["calc.py", "test_calc.py"],
                       "diff": "diff --git a/calc.py b/calc.py\n--- a/calc.py\n+++ b/calc.py\n@@ -1,2 +1,5 @@\n"
                               " def add(a, b):\n     return a + b\n+\n+def subtract(a, b):\n+    return a - b\n"
                               "diff --git a/test_calc.py b/test_calc.py\n--- /dev/null\n+++ b/test_calc.py\n"
                               "@@ -0,0 +1,2 @@\n+def test_subtract():\n+    assert subtract(3, 1) == 2\n"}
job.emit("approval", "proposal: Add subtract()")
workspace.jobs[job.id] = job
workspace.active = job.id
# The engine is not running here: report it ready without starting anything.
workspace.engine_status = lambda refresh=False: {
    "bridge_online": True, "chat_available": True, "title": "Engine ready", "detail": "", "next_action": None,
    "engine": {"available": True, "version": "0.13.0", "conversation": True, "typed_api": True, "new_project": True},
    "model": {"provider": "ollama", "name": "qwen2.5-coder:7b", "ready": True, "ollama_running": True, "installed_models": []}}
workspace.installation_status = lambda: {"onboarding_completed": True, "components": []}
workspace.configured_providers = lambda: []

sock = socket.socket(); sock.bind(("127.0.0.1", 0)); sock.listen(16)
port = sock.getsockname()[1]
server = uvicorn.Server(uvicorn.Config(create_app(workspace), log_level="warning", access_log=False))
threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True).start()
time.sleep(1)

output = Path(os.environ.get("CB_DESKTOP_EDITOR_SCREENSHOT", str(home / "editor.png")))
try:
    with sync_playwright() as p:
        chromium = os.environ.get("CB_CHROMIUM_PATH") or shutil.which("chromium")
        browser = p.chromium.launch(headless=True, **({"executable_path": chromium} if chromium else {}))
        page = browser.new_page(viewport={"width": 1480, "height": 940})
        errors = []
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: (_ for _ in ()).throw(AssertionError("Browser dialogs are forbidden")))
        page.goto(f"http://127.0.0.1:{port}/#token={workspace.secret}")

        # 1. Clicking a file opens it in Monaco, read-only.
        page.locator('.tree-row', has_text='calc.py').first.click()
        page.locator('#editor-host .monaco-editor').wait_for(timeout=20000)
        assert page.locator('#editor-path').inner_text() == 'calc.py'
        assert page.locator('#editor-state').inner_text() == 'Read-only'
        assert page.locator('.editor-tab', has_text='calc.py').count() == 1
        assert page.locator('.tree-row', has_text='.env').count() == 0  # secrets are never listed

        # 2. Edit, then Ctrl+S writes the file atomically.
        page.click('#editor-edit')
        page.locator('#editor-host .monaco-editor textarea').focus()
        page.keyboard.press('Control+End')
        page.keyboard.type('\ndef double(x):\n    return 2 * x\n')
        page.locator('.editor-tab .dirty').wait_for()
        page.keyboard.press('Control+s')
        page.locator('#editor-state', has_text='Saved').wait_for()  # locator waits: the page's CSP forbids eval
        assert 'def double(x):' in (project / 'calc.py').read_text(encoding='utf-8')

        # 3. A change on disk after opening is never overwritten.
        (project / 'calc.py').write_text('# changed elsewhere\n', encoding='utf-8')
        page.locator('#editor-host .monaco-editor textarea').focus()
        page.keyboard.type('# my edit\n')
        page.locator('.editor-tab .dirty').wait_for()  # the edit reached the editor
        page.keyboard.press('Control+s')
        page.locator('#editor-state', has_text='changed on disk').wait_for()
        assert (project / 'calc.py').read_text(encoding='utf-8') == '# changed elsewhere\n'
        assert page.locator('#editor-reload').is_visible()

        # 3b. Review B2: a tab never saves into another project, even a byte-identical file.
        page.click('#editor-reload')
        page.locator('#action-approve').click()  # the app asks before replacing unsaved edits
        page.locator('#editor-state', has_text='Editing').wait_for()
        other = home / 'other'
        other.mkdir()
        (other / 'calc.py').write_text('# changed elsewhere\n', encoding='utf-8')
        subprocess.run(['git', 'init', '-q', str(other)], check=True)
        workspace.choose(str(other))  # the selected project changes behind the open tab
        page.locator('#editor-host .monaco-editor textarea').focus()
        page.keyboard.type('# into B?\n')
        page.locator('.editor-tab .dirty').wait_for()
        page.keyboard.press('Control+s')
        page.locator('#editor-state', has_text='no longer selected').wait_for()
        assert (other / 'calc.py').read_text(encoding='utf-8') == '# changed elsewhere\n'
        workspace.choose(str(project))

        # 4. The waiting proposal opens side by side, file by file.
        page.locator('.editor-tab', has_text='Chat').click()
        page.get_by_text('Review side by side').click()
        page.locator('#editor-host .monaco-diff-editor').wait_for(timeout=20000)
        assert page.locator('.diff-entry').count() == 2
        assert 'test_calc.py' in page.locator('.diff-entry').nth(1).inner_text()
        page.locator('.diff-entry').nth(1).click()
        page.locator('#editor-state', has_text='added').wait_for()
        page.screenshot(path=str(output))

        assert not errors, errors
        browser.close()
finally:
    server.should_exit = True
print("Editor browser PASS: Monaco opens files read-only, edit + Ctrl+S saves, disk conflicts refused, side-by-side proposal")
