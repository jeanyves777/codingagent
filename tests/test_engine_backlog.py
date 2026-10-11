"""The continuous-orchestration backlog through the typed engine API: backlog.status and
backlog.ledger are reads that never start the Brain service or a run and match Conductor.status;
backlog.add uses the same checks as `codingbrain backlog add` and starts nothing. There is no
operation that works the backlog."""
import hashlib
import sqlite3

import pytest

from brain.conductor import Backlog, Conductor, Resources
from brain.local.engine import OPERATIONS, ApiError, describe
from test_engine import tmp_path, world  # noqa: F401  (fixtures)

ACCEPT = "from alpha import VALUE\n\n\ndef test_value():\n    assert VALUE == 'alpha'\n"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def no_service(monkeypatch):
    """Building the Brain service (or anything that would work the backlog) fails the test."""
    def refuse(*args, **kwargs):
        raise AssertionError("a backlog read must not build the Brain service")
    monkeypatch.setattr("brain.factory.build_brain_from_env", refuse)
    monkeypatch.setattr("brain.conductor.BrainExecutor.__init__", refuse)
    monkeypatch.setattr("brain.conductor.Conductor.tick", refuse)


def add_items(world):
    engine, project = world["engine"], world["project"]
    (world["root"] / "acceptance").mkdir()
    (world["root"] / "acceptance" / "test_alpha.py").write_text(ACCEPT)
    first = engine.call("backlog.add", {"project_id": project["id"], "goal": "Create alpha.py", "priority": "P0",
                                        "accept_files": ["acceptance/test_alpha.py"],
                                        "accept_nodes": ["acceptance/test_alpha.py::test_value"],
                                        "criteria": "VALUE is 'alpha'"})
    second = engine.call("backlog.add", {"project_id": project["id"], "goal": "Use alpha in main.py",
                                         "depends_on": [first["id"][:6]]})
    return first, second


def test_schema_declares_backlog_reads_and_one_action_but_no_run():
    schema = describe()["operations"]
    assert schema["backlog.status"]["kind"] == "read" and schema["backlog.ledger"]["kind"] == "read"
    assert schema["backlog.add"]["kind"] == "action"
    assert schema["backlog.add"]["params"]["accept_files"] == {"type": "list[str]", "required": False}
    assert not {op for op in OPERATIONS if op.startswith("backlog.")} - {"backlog.status", "backlog.ledger", "backlog.add"}


def test_status_of_a_project_without_a_backlog_creates_nothing(world, monkeypatch):
    no_service(monkeypatch)
    engine, project = world["engine"], world["project"]
    status = engine.call("backlog.status", {"project_id": project["id"]})
    assert status == {"counts": {}, "agents": {"local_slots": 2, "busy": []}, "items": [], "finished": True,
                      "waiting_on_people_only": False, "ledger_intact": True,
                      "integration": None}
    assert engine.call("backlog.ledger", {"project_id": project["id"]}) == {"intact": True, "entries": []}
    data = engine.context(project["id"]).data
    assert not (data / "backlog.sqlite3").exists() and not (data / "brain.sqlite3").exists()
    with pytest.raises(ApiError) as error:
        engine.call("backlog.status", {"project_id": "nope"})
    assert error.value.code == "not_found"


def test_status_is_read_only_and_matches_the_conductor(world, monkeypatch):
    engine, project = world["engine"], world["project"]
    first, second = add_items(world)
    path = engine.context(project["id"]).data / "backlog.sqlite3"
    backlog = Backlog(path)
    item = backlog.get(first["id"])
    attempt = backlog.start_attempt(item, "local", "local", "task-1234567890")
    backlog.record(item["id"], attempt, "verification", "passed", {"exit_code": 0})
    no_service(monkeypatch)
    before = digest(path)
    with sqlite3.connect(path) as db:
        rows_before = [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                       for table in ("items", "attempts", "ledger", "decisions")]

    status = engine.call("backlog.status", {"project_id": project["id"]})
    ledger = engine.call("backlog.ledger", {"project_id": project["id"]})

    assert digest(path) == before  # not a single byte written
    with sqlite3.connect(path) as db:
        assert rows_before == [db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                               for table in ("items", "attempts", "ledger", "decisions")]
    assert engine.context(project["id"])._brain is None
    assert not (engine.context(project["id"]).data / "brain.sqlite3").exists()
    conductor = Conductor(backlog, None, resources=Resources(world["engine"].system.facts, max_local=4))
    assert status == conductor.status()
    assert status["counts"] == {"running": 1, "waiting": 1} and status["ledger_intact"]
    assert status["agents"]["busy"] == [{"item": first["id"], "strategy": "local", "task": "task-1234567890"}]
    assert [entry["verdict"] for entry in ledger["entries"]] == ["passed"] and ledger["intact"]
    assert engine.call("backlog.status", {"project_id": project["id"], "max_agents": 1})["agents"]["local_slots"] == 1


def test_waiting_for_people_and_a_broken_ledger_are_reported(world):
    engine, project = world["engine"], world["project"]
    first, _ = add_items(world)
    backlog = Backlog(engine.context(project["id"]).data / "backlog.sqlite3")
    backlog.update(first["id"], state="awaiting_approval", reason="the proposal waits for your approval")
    backlog.record(first["id"], None, "verification", "passed", {"exit_code": 0})
    status = engine.call("backlog.status", {"project_id": project["id"]})
    assert status["waiting_on_people_only"] and not status["finished"]
    assert status["items"][0]["reason"] == "the proposal waits for your approval"
    with sqlite3.connect(backlog.path) as db:
        db.execute("UPDATE ledger SET verdict='failed'")
    assert not engine.call("backlog.status", {"project_id": project["id"]})["ledger_intact"]
    assert not engine.call("backlog.ledger", {"project_id": project["id"]})["intact"]
    one = engine.call("backlog.ledger", {"project_id": project["id"], "item_id": first["id"][:5]})
    assert len(one["entries"]) == 1
    with pytest.raises(ApiError) as error:
        engine.call("backlog.ledger", {"project_id": project["id"], "item_id": "zzzz"})
    assert error.value.code == "not_found"


def test_add_freezes_acceptance_files_and_starts_nothing(world, monkeypatch):
    no_service(monkeypatch)
    engine, project = world["engine"], world["project"]
    first, second = add_items(world)
    assert first["priority"] == "P0" and first["state"] == "ready" and first["attempts"] == 0
    assert first["acceptance"] == {"files": ["acceptance/test_alpha.py"],
                                   "nodes": ["acceptance/test_alpha.py::test_value"], "criteria": "VALUE is 'alpha'"}
    assert second["state"] == "waiting" and second["depends_on"] == [first["id"]] and second["acceptance"] is None
    (world["root"] / "acceptance" / "test_alpha.py").write_text("def test_value():\n    pass\n")  # edited later
    backlog = Backlog(engine.context(project["id"]).data / "backlog.sqlite3")
    assert backlog.get(first["id"])["acceptance"]["files"]["acceptance/test_alpha.py"] == ACCEPT
    assert backlog.attempts() == [] and engine.context(project["id"])._brain is None


@pytest.mark.parametrize("params,code,message", [
    ({"goal": "x", "accept_files": ["../../outside.py"], "accept_nodes": ["x.py::t"]}, "refused", "inside the project"),
    ({"goal": "x", "accept_files": ["main.py"], "accept_nodes": ["other.py::t"]}, "refused", "must name a test"),
    ({"goal": "x", "accept_files": ["main.py"]}, "refused", "node ids"),
    ({"goal": "x", "depends_on": ["nope"]}, "refused", "no single backlog item"),
    ({"goal": "x", "priority": "P9"}, "refused", "priority"),
    ({"goal": "x", "max_attempts": 9}, "refused", "max_attempts"),
    ({"goal": " "}, "refused", "needs a goal"),
    ({"goal": "x", "accept_files": "main.py"}, "bad_params", "list[str]"),
    ({"goal": "x", "depends_on": [3]}, "bad_params", "list[str]"),
    ({"goal": "x", "run": True}, "bad_params", "unknown parameter"),
])
def test_add_uses_the_cli_checks(world, params, code, message):
    engine, project = world["engine"], world["project"]
    with pytest.raises(ApiError) as error:
        engine.call("backlog.add", {"project_id": project["id"], **params})
    assert error.value.code == code and message in str(error.value)
    path = engine.context(project["id"]).data / "backlog.sqlite3"
    assert not path.exists() or Backlog(path).list() == []
