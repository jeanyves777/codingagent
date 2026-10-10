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
    def __init__(self, python: Path | None = None, root: Path | None = None):
        self.python = python or installed_brain_python()
        self.root = root or source_root()
        self.child: subprocess.Popen | None = None
        self.address: str | None = None

    def start(self, timeout: int = 30) -> str:
        if self.child is not None:
            raise RuntimeError("Desktop service is already running")
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
            return self.address
        except (queue.Empty, RuntimeError, KeyError, TypeError, ValueError) as exc:
            self.stop()
            raise RuntimeError("Could not start Coding Brain Desktop bridge: " + str(exc)) from exc

    def stop(self):
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


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Coding Brain native desktop workspace")
    parser.add_argument("--self-test", action="store_true", help="Check packaged files without launching GUI")
    parser.add_argument("--url", help="Developer mode: open an existing local bridge")
    args = parser.parse_args(argv)
    try:
        root = source_root()
        if args.self_test:
            # Portable Windows CI can verify packaging without a signed-in model or GUI session.
            import importlib.util
            result = json.dumps({"native_assets": True, "root": str(root),
                                 "webview_available": importlib.util.find_spec("webview") is not None})
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
            webview.create_window("Coding Brain", address, width=1360, height=850,
                                  min_size=(850, 560), background_color="#111315", text_select=True)
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
