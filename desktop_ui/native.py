"""Native WebView2 desktop shell for the installed Coding Brain environment.

Runs the UI service in the active Coding Brain Python interpreter and opens a
native window. It does NOT bundle a second Brain/model/router, and the UI binary
continues to work when the installed brain version is updated.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from urllib.parse import quote


def source_root() -> Path:
    """Bundled assets from PyInstaller, or the editable source checkout."""
    packaged = getattr(sys, "_MEIPASS", None)
    root = Path(packaged) if packaged else Path(__file__).resolve().parents[1]
    if not (root / "desktop_ui" / "server.py").is_file():
        raise RuntimeError("Coding Brain Desktop files are missing from this installation")
    return root


def installed_brain_python() -> Path:
    """Always use the installed Coding Brain environment, not a bundled copy."""
    override = os.environ.get("CODINGBRAIN_PYTHON")
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise RuntimeError(f"CODINGBRAIN_PYTHON does not exist: {candidate}")
        return candidate.resolve()
    if os.name != "nt":
        return Path(sys.executable)
    home = Path(os.environ.get("CODINGBRAIN_HOME") or (Path(os.environ["LOCALAPPDATA"]) / "CodingBrain"))
    current = home / "app" / "current.json"
    try:
        version = json.loads(current.read_text(encoding="utf-8"))["version"]
    except (OSError, KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Install Coding Brain first; app/current.json is missing or invalid") from exc
    # Version validation avoids treating current.json as an arbitrary path.
    import re
    if not isinstance(version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){1,3}(?:[a-z0-9.+-]+)?", version):
        raise RuntimeError("Invalid installed Coding Brain version")
    candidate = home / "app" / "versions" / version / "venv" / "Scripts" / "python.exe"
    if not candidate.is_file():
        raise RuntimeError(f"Coding Brain Python not found: {candidate}")
    return candidate


class BridgeProcess:
    # A bridge that keeps crashing is reported, not restarted forever.
    MAX_RESTARTS = 3
    RESTART_WINDOW = 120.0

    def __init__(self, python: Path | None = None, root: Path | None = None):
        self._python_override = python
        self.python = python or installed_brain_python()
        self.root = root or source_root()
        self.child: subprocess.Popen | None = None
        self.address: str | None = None
        self.stderr_tail: list[str] = []
        self.stopping = threading.Event()
        self.restarts: list[float] = []

    @staticmethod
    def _drain(stream, sink=None):
        # The bridge must never block on a full pipe: uvicorn warnings and tracebacks keep coming
        # after the handshake line, and nothing else reads them.
        try:
            for line in stream:
                if sink is not None:
                    sink(line)
        except (OSError, ValueError):
            pass

    def _keep_stderr(self, line: str):
        self.stderr_tail = (self.stderr_tail + [line])[-40:]

    def start(self, timeout: int = 30) -> str:
        if self.child is not None:
            raise RuntimeError("Desktop service is already running")
        if not getattr(self, "_restarting", False):
            self.stopping.clear()  # a restart must never undo a stop() that came in meanwhile
        env = os.environ.copy()
        env["PYTHONPATH"] = str(self.root) + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
        kwargs = {"cwd": str(Path.home()), "stdin": subprocess.DEVNULL,
                  "stdout": subprocess.PIPE, "stderr": subprocess.PIPE,
                  "text": True, "encoding": "utf-8", "errors": "replace", "bufsize": 1,
                  "env": env}
        if os.name == "nt":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.child = subprocess.Popen([str(self.python), "-u", "-m", "desktop_ui.server",
                                        "--no-open", "--port", "0", "--handshake-stdout"], **kwargs)
        output: queue.Queue[str] = queue.Queue(maxsize=1)
        def await_first_line():
            try:
                output.put(self.child.stdout.readline())
            except (OSError, ValueError):
                output.put("")
        threading.Thread(target=await_first_line, daemon=True).start()
        try:
            line = output.get(timeout=timeout).rstrip("\r\n")
            if not line.startswith("CBUI_READY "):
                raise RuntimeError("The desktop service failed to start; check Coding Brain installation")
            payload = json.loads(line[len("CBUI_READY "):])
            port = payload["port"]
            token = payload["token"]
            if type(port) is not int or not 1024 <= port <= 65535 or not isinstance(token, str) or len(token) < 32:
                raise RuntimeError("Invalid desktop bridge handshake")
            self.address = f"http://127.0.0.1:{port}/#token={quote(token, safe='')}"
            threading.Thread(target=self._drain, args=(self.child.stdout,), daemon=True).start()
            threading.Thread(target=self._drain, args=(self.child.stderr, self._keep_stderr), daemon=True).start()
            return self.address
        except (queue.Empty, RuntimeError, KeyError, TypeError, ValueError) as exc:
            self.stop()
            raise RuntimeError("Could not start Coding Brain Desktop bridge: " + str(exc)) from exc

    def restart(self) -> str:
        """Start a fresh bridge after an unexpected exit, from the engine installed *now*."""
        now = time.monotonic()
        self.restarts = [at for at in self.restarts if now - at < self.RESTART_WINDOW] + [now]
        if len(self.restarts) > self.MAX_RESTARTS:
            raise RuntimeError("The desktop service stopped repeatedly: " + "".join(self.stderr_tail)[-600:])
        child, self.child = self.child, None
        if child is not None:
            for stream in (child.stdout, child.stderr):
                if stream:
                    stream.close()
        if self._python_override is None:
            # An update may have switched app/current.json to another version while we ran.
            self.python = installed_brain_python()
        if self.stopping.is_set():
            raise RuntimeError("The desktop is closing")
        self._restarting = True
        try:
            address = self.start()
        finally:
            self._restarting = False
        if self.stopping.is_set():  # the window closed while the new bridge was starting
            self.stop()
            raise RuntimeError("The desktop is closing")
        return address

    def watch(self, on_restart, on_failure, poll: float = 1.0):
        """Supervise the bridge until stop(): restart it if it exits and report the new address."""
        def loop():
            while not self.stopping.wait(poll):
                child = self.child
                if child is None or child.poll() is None:
                    continue
                if self.stopping.is_set():
                    return
                try:
                    address = self.restart()
                except RuntimeError as error:
                    if not self.stopping.is_set():
                        on_failure(str(error))
                    return
                on_restart(address)
        thread = threading.Thread(target=loop, daemon=True)
        thread.start()
        return thread

    def stop(self):
        self.stopping.set()
        child, self.child = self.child, None
        self.address = None
        if child is None:
            return
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=5)
        if child.stdout:
            child.stdout.close()
        if child.stderr:
            child.stderr.close()


def failure_page(reason: str) -> str:
    from html import escape
    return ("<!doctype html><meta charset='utf-8'><body style='background:#111315;color:#e6e6e6;"
            "font:15px system-ui;padding:48px'><h2>Coding Brain Desktop stopped</h2>"
            "<p>The local desktop service could not be restarted. Close and reopen Coding Brain; "
            "if this repeats, run <code>codingbrain doctor</code>.</p>"
            f"<pre style='white-space:pre-wrap;color:#9aa'>{escape(reason)}</pre></body>")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Coding Brain native desktop workspace")
    parser.add_argument("--self-test", action="store_true", help="Check packaged files without launching GUI")
    parser.add_argument("--url", help="Developer mode: open an existing local bridge")
    args = parser.parse_args(argv)
    try:
        root = source_root()
        if args.self_test:
            # Verify bundled assets and pywebview without starting a GUI.
            import importlib.util
            assets = [root / "desktop_ui" / "static" / name for name in ("index.html", "app.js", "app.css")]
            if not all(p.is_file() and p.stat().st_size > 0 for p in assets):
                raise RuntimeError("Required desktop assets are missing")
            webview_available = importlib.util.find_spec("webview") is not None
            if getattr(sys, "frozen", False) and not webview_available:
                raise RuntimeError("Native WebView dependency is missing from packaged executable")
            result = json.dumps({"native_assets": True, "root": str(root),
                                 "webview_available": webview_available})
            # A PyInstaller --windowed EXE has no stdout on Windows.
            if sys.stdout is not None:
                print(result)
            return 0
        import webview
        bridge = None
        try:
            if args.url:
                # Only accept a local URL for development; token is not checked here.
                from urllib.parse import urlsplit
                if urlsplit(args.url).hostname not in {"127.0.0.1", "localhost"}:
                    raise RuntimeError("Only loopback URLs are supported")
                address = args.url
            else:
                bridge = BridgeProcess()
                address = bridge.start()
            window = webview.create_window("Coding Brain", address, width=1360, height=850,
                                           min_size=(850, 560), background_color="#111315", text_select=True)
            def confirm_close():
                try:
                    if window.evaluate_js("window.__codingBrainDirty ? window.__codingBrainDirty() : false"):
                        return bool(window.create_confirmation_dialog(
                            "Unsaved edits", "Some open files have unsaved edits. Close Coding Brain and discard them?"))
                except Exception:
                    pass  # never block closing because the page could not be asked
                return True
            window.events.closing += confirm_close
            if bridge:
                # A crashed bridge comes back on a new port and token; the window follows it.
                bridge.watch(on_restart=window.load_url,
                             on_failure=lambda reason: window.load_html(failure_page(reason)))
            # WebView2 is required on Windows; never silently fall back to legacy MSHTML.
            webview.start(gui="edgechromium" if os.name == "nt" else None, debug=False)
            return 0
        finally:
            if bridge:
                bridge.stop()
    except (ImportError, RuntimeError, OSError) as exc:
        # GUI apps have no console. The Windows launcher/installer can surface this error.
        error = "Coding Brain Desktop: " + str(exc)
        if sys.stderr is not None:
            sys.stderr.write(error + "\n")
        elif os.name == "nt":
            try:
                import ctypes
                ctypes.windll.user32.MessageBoxW(None, error, "Coding Brain Desktop", 0x10)
            except OSError:
                pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
