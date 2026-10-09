from pipeline import Registry, Runner, Stage


def flaky(failures, value):
    calls = {"n": 0}

    def run(context):
        calls["n"] += 1
        if calls["n"] <= failures:
            raise ConnectionError(f"attempt {calls['n']} failed")
        return value
    return run


def test_retries_with_exponential_backoff_then_succeeds():
    waits = []
    registry = Registry()
    registry.add(Stage("fetch", flaky(2, "data")))
    report = Runner(registry, sleep=waits.append).run({})
    assert report.results["fetch"] == {"status": "succeeded", "value": "data"}
    assert waits == [0.5, 1.0] and report.attempts == {"fetch": 3} and report.ok


def test_no_sleep_after_last_attempt_and_custom_policy():
    waits = []
    registry = Registry()
    registry.add(Stage("fetch", flaky(10, None)))
    report = Runner(registry, max_attempts=4, sleep=waits.append, backoff=1).run({})
    assert waits == [1, 2, 4] and report.attempts["fetch"] == 4
    assert report.results["fetch"] == {"status": "failed", "error": "attempt 4 failed"} and not report.ok


def test_dependents_are_blocked_transitively_and_independent_stages_run():
    ran = []
    registry = Registry()
    registry.add(Stage("extract", flaky(99, None)))
    registry.add(Stage("transform", lambda c: ran.append("transform"), depends_on=("extract",)))
    registry.add(Stage("load", lambda c: ran.append("load"), depends_on=("transform",)))
    registry.add(Stage("audit", lambda c: ran.append("audit") or "ok"))
    report = Runner(registry, sleep=lambda seconds: None).run({})
    assert [report.status(name) for name in ("extract", "transform", "load", "audit")] == \
        ["failed", "blocked", "blocked", "succeeded"]
    assert ran == ["audit"] and "transform" not in report.attempts and report.attempts["audit"] == 1
