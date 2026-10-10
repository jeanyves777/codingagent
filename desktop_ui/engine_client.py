"""Client for the Coding Brain engine API (`codingbrain api --stdio`, API 1.0).

The desktop owns no orchestration: conversations, projects, tasks, approvals and live events all
go through this one typed interface, the same contract any other program uses. The engine runs
as a separate process from the installed Python environment; requests are JSON lines and may be
in flight concurrently (tasks.stop while tasks.approve is testing).
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

API_VERSION = "1.0"


class EngineError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class EngineClient:
    def __init__(self, command: list[str] | None = None, cwd: str | None = None, env: dict | None = None,
                 start_timeout: float = 120):
        # The installed engine's own launcher (Workspace.cli_command), never this UI process's
        # Python: readiness and behavior come from the engine the user actually has installed.
        self.command = list(command or [sys.executable, "-m", "brain.local"])
        self.cwd = cwd or str(Path.home())
        self.env = env
        self.start_timeout = start_timeout
        self.process: subprocess.Popen | None = None
        self.lock = threading.Lock()
        self.write_lock = threading.Lock()
        self.ids = itertools.count(1)
        self.pending: dict[int, dict] = {}
        self.ready = threading.Event()
        self.info: dict | None = None
        self.stderr_tail: list[str] = []

    # process -------------------------------------------------------------------------------------
    def _start(self):
        kwargs = {}
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        self.ready.clear()
        try:
            self.process = subprocess.Popen(
                [*self.command, "api", "--stdio"], cwd=self.cwd,
                env={**(self.env or os.environ), "PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8"},
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", bufsize=1, **kwargs)
        except OSError as error:
            raise EngineError("unavailable", f"the installed Coding Brain engine could not start ({type(error).__name__})") from error
        process = self.process
        threading.Thread(target=self._read, args=(process,), daemon=True).start()
        threading.Thread(target=self._read_stderr, args=(process,), daemon=True).start()
        deadline = time.monotonic() + self.start_timeout
        while not self.ready.wait(0.2):
            if process.poll() is not None or time.monotonic() > deadline:  # exited (e.g. no `api`) or hung
                self.close()
                raise EngineError("unavailable", "the Coding Brain engine did not start: " + "".join(self.stderr_tail)[-800:])

    def ensure(self):
        with self.lock:
            if self.process is None or self.process.poll() is not None:
                self._start()
                info = self._call("engine.info", {}, None, 60)
                if not str(info.get("api_version", "")).startswith(API_VERSION.split(".")[0] + "."):
                    raise EngineError("unsupported", f"engine API {info.get('api_version')} is not supported "
                                                     f"(this desktop speaks {API_VERSION})")
                self.info = info
        return self

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.stdin.close()
            process.wait(timeout=10)
        except Exception:
            process.kill()
        self._fail_pending("unavailable", "the engine stopped")

    def _read(self, process):
        for line in process.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if "event" in message:
                if message["event"].get("event_type") == "ready":
                    self.ready.set()
                    continue
                waiter = self.pending.get(message.get("op_id"))
                if waiter and waiter["on_event"]:
                    try:
                        waiter["on_event"](message["event"])
                    except Exception:
                        pass  # a display problem never breaks the protocol
                continue
            waiter = self.pending.get(message.get("id"))
            if waiter:
                waiter["response"] = message
                waiter["done"].set()
        if process is self.process or self.process is None:
            self._fail_pending("unavailable", "the engine stopped unexpectedly: " + "".join(self.stderr_tail)[-800:])

    def _read_stderr(self, process):
        for line in process.stderr:
            self.stderr_tail = (self.stderr_tail + [line])[-40:]

    def _fail_pending(self, code, message):
        for waiter in list(self.pending.values()):
            if not waiter["done"].is_set():
                waiter["response"] = {"ok": False, "error": {"code": code, "message": message}}
                waiter["done"].set()

    # requests ------------------------------------------------------------------------------------
    def call(self, op: str, params: dict | None = None, on_event=None, timeout: float | None = None):
        self.ensure()
        return self._call(op, params or {}, on_event, timeout)

    def _call(self, op, params, on_event, timeout):
        request_id = next(self.ids)
        waiter = {"done": threading.Event(), "response": None, "on_event": on_event}
        self.pending[request_id] = waiter
        try:
            with self.write_lock:
                self.process.stdin.write(json.dumps({"id": request_id, "op": op, "params": params}) + "\n")
                self.process.stdin.flush()
            if not waiter["done"].wait(timeout):
                raise EngineError("timeout", f"{op} did not answer within {timeout} s")
        except (OSError, ValueError) as error:
            raise EngineError("unavailable", f"the engine is not running ({type(error).__name__})") from error
        finally:
            self.pending.pop(request_id, None)
        response = waiter["response"]
        if not response.get("ok"):
            error = response.get("error") or {}
            raise EngineError(error.get("code", "engine_error"), error.get("message", "unknown error"))
        return response["result"]


_shared: EngineClient | None = None
_shared_lock = threading.Lock()


def shared() -> EngineClient:
    """One engine process for the desktop session."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = EngineClient()
        return _shared
