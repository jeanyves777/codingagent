from pipeline import Registry, Runner, Stage


def test_stages_run_in_dependency_order():
    registry = Registry()
    registry.add(Stage("total", lambda c: c["load"] * 2, depends_on=("load",)))
    registry.add(Stage("load", lambda c: 21))
    report = Runner(registry).run({})
    assert report.results["total"] == {"status": "succeeded", "value": 42}


def test_a_failing_stage_is_recorded_not_raised():
    def broken(context):
        raise RuntimeError("disk full")
    registry = Registry()
    registry.add(Stage("save", broken))
    report = Runner(registry, sleep=lambda seconds: None).run({})
    assert report.results["save"] == {"status": "failed", "error": "disk full"}
