"""The conversational assistant: conversation by default, coding work only when asked and confirmed.

The model is a scripted fake here (it returns the intents and replies each test sets), so these
tests check routing, safety and lifecycle, not the model's judgement. test_real_* uses a real
local model, and the Windows workflow drives `codingbrain` through a real console pipe.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from brain.local import config as settings
from brain.local.assistant import Assistant, ConversationLog, ModelUnavailable, registry
from brain.local.paths import Layout


class FakeModel:
    def __init__(self, routes=None, replies=None, unavailable=False):
        self.routes, self.replies, self.unavailable = dict(routes or {}), list(replies or []), unavailable
        self.calls = []

    def chat(self, messages, schema=None, max_tokens=600):
        self.calls.append({"messages": messages, "schema": schema})
        if self.unavailable:
            raise ModelUnavailable("the local model at http://localhost:11434 did not answer (ConnectError)")
        text = messages[-1]["content"]
        if schema:
            return json.dumps(self.routes.get(text, {"intent": "chat", "goal": text}))
        return self.replies.pop(0) if self.replies else f"(answer to: {text})"


@pytest.fixture
def world(tmp_path, monkeypatch):
    home = tmp_path / "Users" / "Kkoff"
    home.mkdir(parents=True)
    layout = Layout(home / "AppData" / "Local" / "CodingBrain").ensure()
    monkeypatch.setenv("CODINGBRAIN_HOME", str(layout.home))
    config = settings.load(layout)
    config["models"]["model"] = "qwen2.5-coder:7b"
    settings.save(layout, config)
    projects = {}
    for name, files in (("calculator", {"calculator.py": "def add(a, b):\n    return a - b\n"}),
                        ("recipes", {"recipes/search.py": "def search(q):\n    return []\n"})):
        root = home / "Projects" / name
        for path, text in files.items():
            (root / path).parent.mkdir(parents=True, exist_ok=True)
            (root / path).write_text(text)
        subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
        subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "init"],
                       check=True)
        from brain.local.cli import Context
        Context(layout, root).register()
        projects[name] = root
    return {"home": home, "layout": layout, "projects": projects}


def assistant(world, model=None, answers=(), cwd=None, interactive=True):
    out, replies = [], list(answers)

    def read(prompt=""):
        out.append(prompt)
        if not replies:
            raise EOFError
        return replies.pop(0)
    bot = Assistant(world["layout"], cwd or world["home"], out=lambda text="": out.append(str(text)), read=read,
                    model=model or FakeModel(), interactive=interactive)
    bot.output = out
    return bot


def no_tasks_anywhere(world):
    stores = list(world["layout"].projects.glob("*/brain.sqlite3"))
    worktrees = [path for path in world["layout"].projects.glob("*/tasks/*")]
    return not stores and not worktrees


def text_of(bot):
    return "\n".join(bot.output)


# Conversation is the default -------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["hello", "Hi!", "hey there", "Good morning", "bonjour", "hello coding brain",
                                     "\ufeffhello", "ï»¿hello", "hel\u200blo"])  # piped by PowerShell 5.1, pasted
def test_greetings_get_a_greeting_without_a_model_call_or_a_task(world, message):
    model = FakeModel()
    bot = assistant(world, model)
    bot.handle(message)
    assert "Hello! I'm Coding Brain" in text_of(bot)
    assert model.calls == [] and no_tasks_anywhere(world)


def test_general_question_is_answered_in_conversation(world):
    model = FakeModel(routes={"How do Python decorators work?": {"intent": "question", "goal": "explain decorators"}},
                      replies=["A decorator wraps a function..."])
    bot = assistant(world, model)
    bot.handle("How do Python decorators work?")
    assert "A decorator wraps a function" in text_of(bot)
    assert no_tasks_anywhere(world)
    assert "Never claim to have changed files" in model.calls[-1]["messages"][0]["content"]


def test_what_projects_reads_the_registry_without_a_task(world):
    bot = assistant(world)
    bot.handle("What projects am I working on?")
    text = text_of(bot)
    assert "calculator" in text and "recipes" in text and "Projects on this computer (2)" in text
    assert no_tasks_anywhere(world)


def test_coding_discussion_is_read_only(world):
    model = FakeModel(routes={"How should the tests in calculator be organised?":
                              {"intent": "plan", "goal": "organise calculator tests", "project": "calculator"}},
                      replies=["Put them in tests/test_calculator.py ..."])
    bot = assistant(world, model, answers=["no"])
    bot.handle("How should the tests in calculator be organised?")
    assert "tests/test_calculator.py" in text_of(bot) and "do it" in text_of(bot)
    context = model.calls[-1]["messages"][0]["content"]
    assert "calculator.py" in context and "search.py" not in context  # only that project's files
    bot.handle("no")
    assert no_tasks_anywhere(world)
    assert subprocess.run(["git", "-C", str(world["projects"]["calculator"]), "status", "--porcelain"],
                          capture_output=True, text=True).stdout == ""


def test_ambiguous_request_gets_a_clarifying_question(world):
    model = FakeModel(routes={"make it better": {"intent": "implement", "goal": "make it better",
                                                 "clarification": "What should I improve, and in which project?"}})
    bot = assistant(world, model)
    bot.handle("make it better")
    assert "What should I improve" in text_of(bot) and no_tasks_anywhere(world)


def test_model_cannot_turn_a_question_into_work(world):
    model = FakeModel(routes={"what is a closure": {"intent": "implement", "goal": "closures"}}, replies=["A closure..."])
    bot = assistant(world, model)
    bot.handle("what is a closure")
    assert "A closure" in text_of(bot) and no_tasks_anywhere(world)


# Explicit work, after confirmation ------------------------------------------------------------------

def test_fix_request_finds_the_project_and_runs_only_after_confirmation(world, monkeypatch):
    started = []
    monkeypatch.setattr("brain.local.cli.run_goal", lambda context, goal, *a, **k: started.append(
        (context.root, goal)) or {"id": "t1", "status": "proposed"})
    model = FakeModel(routes={"Fix the calculator bug": {"intent": "implement", "goal": "Fix the add bug in calculator"}})
    declined = assistant(world, model, answers=["n"])
    declined.handle("Fix the calculator bug")
    assert started == [] and "nothing was started" in text_of(declined)
    accepted = assistant(world, model, answers=["y"])
    accepted.handle("Fix the calculator bug")
    assert started == [(world["projects"]["calculator"].resolve(), "Fix the add bug in calculator")]


def test_unclear_project_is_asked_and_the_answer_continues(world, monkeypatch):
    started = []
    monkeypatch.setattr("brain.local.cli.run_goal", lambda context, goal, *a, **k: started.append(context.root.name) or {})
    model = FakeModel(routes={"Add input validation": {"intent": "implement", "goal": "Add input validation"}})
    bot = assistant(world, model, answers=["y"])
    bot.handle("Add input validation")
    assert "Which project do you mean?" in text_of(bot) and started == []
    number = 1 + [line for line in text_of(bot).splitlines() if ". " in line and "(" in line].index(
        next(line for line in text_of(bot).splitlines() if "recipes" in line and ". " in line))
    bot.handle(str(number))
    assert started == ["recipes"]


def test_file_names_identify_the_project(world, monkeypatch):
    monkeypatch.setattr("brain.local.cli.run_goal", lambda *a, **k: {})
    model = FakeModel(routes={"Fix the search returning nothing": {"intent": "implement", "goal": "Fix search"}})
    bot = assistant(world, model, answers=["n"])
    bot.handle("Fix the search returning nothing")
    assert "This looks like recipes" in text_of(bot)


def test_create_application_enters_goal_first_creation(world, monkeypatch):
    created = []
    monkeypatch.setattr("brain.local.create.create_project", lambda layout, goal, **k: created.append(goal) or {
        "path": str(world["home"] / "Projects" / "task-management"), "name": "task-management", "stack": "web",
        "goal": goal})
    monkeypatch.setattr("brain.local.create.build", lambda *a, **k: {"status": "passed", "id": "abcdef12"})
    (world["home"] / "Projects" / "task-management").mkdir()
    model = FakeModel(routes={"Create a task management application":
                              {"intent": "create_project", "goal": "Create a task management application"}})
    bot = assistant(world, model, answers=["y"])
    bot.handle("Create a task management application")
    assert created == ["Create a task management application"]
    assert "codingbrain accept" in text_of(bot)


def test_end_of_input_or_empty_piped_answer_is_never_consent(world, monkeypatch):
    started = []
    monkeypatch.setattr("brain.local.cli.run_goal", lambda *a, **k: started.append(1) or {})
    model = FakeModel(routes={"Fix the calculator bug": {"intent": "implement", "goal": "fix"}})
    assistant(world, model, answers=[]).handle("Fix the calculator bug")  # EOF
    assistant(world, model, answers=[""], interactive=False).handle("Fix the calculator bug")
    assert started == []


# Without a model, conversation stays the default ----------------------------------------------------------

@pytest.mark.parametrize("message,intent", [
    ("Create a task management application", "create_project"),
    ("Fix the calculator bug", "implement"),
    ("how do I write a unit test?", "question"),
    ("I had a long day", "chat"),
    ("the build is weird", "chat"),
])
def test_fallback_routing_without_a_model(world, message, intent):
    bot = assistant(world, FakeModel(unavailable=True))
    assert bot.route(message).intent == intent


# Context, privacy, lifecycle ------------------------------------------------------------------------

def test_follow_up_turns_carry_context(world):
    model = FakeModel(replies=["Python is a language.", "It was created by Guido van Rossum."])
    bot = assistant(world, model)
    bot.handle("tell me about python")
    bot.handle("who created it?")
    sent = [message["content"] for message in model.calls[-1]["messages"]]
    assert "tell me about python" in sent and "Python is a language." in sent and sent[-1] == "who created it?"


def test_conversations_do_not_leak_between_projects(world):
    log = ConversationLog(world["layout"])
    log.add("user", "secret plans for project A", "project-a")
    log.add("user", "global hello", None)
    log.add("user", "about project B", "project-b")
    assert [turn["content"] for turn in log.recent("project-b", 10)] == ["global hello", "about project B"]
    assert "secret plans" not in json.dumps(log.recent(None, 10))


def test_secrets_are_redacted_in_the_conversation_log(world):
    bot = assistant(world, FakeModel())
    bot.handle("my token is ghp_abcdefghijklmnopqrstuvwxyz0123456789 keep it")
    stored = (world["layout"].data / "conversations" / "turns.jsonl").read_text()
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in stored


def test_ctrl_c_while_thinking_starts_nothing(world):
    class Interrupting(FakeModel):
        def chat(self, messages, schema=None, max_tokens=600):
            if not schema:
                raise KeyboardInterrupt
            return json.dumps({"intent": "question", "goal": "x"})
    messages = iter(["how do generators work?", "/exit"])
    out = []
    bot = Assistant(world["layout"], world["home"], out=lambda text="": out.append(str(text)),
                    read=lambda prompt="": next(messages), model=Interrupting(), interactive=True)
    assert bot.repl() == 0
    assert any("Stopped. Nothing was started." in line for line in out)
    assert bot.pending is None and no_tasks_anywhere(world)


def test_works_outside_repositories_without_scanning_the_home_folder(world, monkeypatch):
    import brain.local.project as project
    original = project._files

    def guarded(root, limit=5000):
        assert Path(root).resolve() != world["home"].resolve(), "the home folder must not be scanned"
        return original(root, limit)
    monkeypatch.setattr(project, "_files", guarded)
    bot = assistant(world, FakeModel())
    assert "not a project" in bot.folder_description()
    bot.handle("hello")
    bot.handle("what projects am I working on?")
    assert "calculator" in text_of(bot)


def test_inside_a_repository_it_is_the_default_project(world, monkeypatch):
    started = []
    monkeypatch.setattr("brain.local.cli.run_goal", lambda context, goal, *a, **k: started.append(context.root.name) or {})
    model = FakeModel(routes={"Add a subtract function": {"intent": "implement", "goal": "Add subtract"}})
    bot = assistant(world, model, answers=["y"], cwd=world["projects"]["calculator"])
    bot.handle("Add a subtract function")
    assert started == ["calculator"]


def test_registry_and_tasks_are_read_without_creating_stores(world):
    items = registry(world["layout"])
    assert {item["name"] for item in items} == {"calculator", "recipes"} and all(item["tasks"] == {} for item in items)
    bot = assistant(world)
    bot.handle("/tasks")
    assert "conversations are not tasks" in text_of(bot) and no_tasks_anywhere(world)


def test_cli_chat_answers_one_message(world, tmp_path):
    result = subprocess.run([sys.executable, "-m", "brain.local", "chat", "hello"], capture_output=True, text=True,
                            cwd=world["home"], env={**os.environ, "CODINGBRAIN_HOME": str(world["layout"].home),
                                                    "PYTHONPATH": str(Path(__file__).resolve().parents[1])}, timeout=120)
    assert result.returncode == 0 and "Hello! I'm Coding Brain" in result.stdout
    assert no_tasks_anywhere(world)


@pytest.mark.skipif(not os.environ.get("CODINGBRAIN_TEST_CODING_MODEL"), reason="needs a real Ollama model")
def test_real_model_conversation_and_routing(world):
    """Real local model: answers a question, and recognizes requests to build and to fix."""
    config = settings.load(world["layout"])
    config["models"]["model"] = os.environ["CODINGBRAIN_TEST_CODING_MODEL"]
    settings.save(world["layout"], config)
    bot = assistant(world, model=None)
    from brain.local.assistant import LocalModel
    bot.model = LocalModel(settings.load(world["layout"]))
    bot.handle("hello")
    question = bot.route("What is a Python list comprehension?")
    bot.handle("What is a Python list comprehension?")
    create = bot.route("Create a task management application")
    fix = bot.route("Fix the calculator bug")
    print(json.dumps({"question": question.__dict__, "create": create.__dict__, "fix": fix.__dict__}, indent=2))
    print(text_of(bot)[-1500:])
    assert question.intent in {"question", "chat"} and create.intent == "create_project"
    assert fix.intent == "implement" and no_tasks_anywhere(world)
    assert "comprehension" in text_of(bot).lower()
