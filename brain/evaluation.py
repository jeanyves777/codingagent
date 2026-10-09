"""Evidence-based runtime metrics and verified learning-data export."""
import json


TERMINAL = {"accepted", "failed", "blocked", "cancelled", "integration_conflict"}


def report(tasks: list[dict]) -> dict:
    coding = [task for task in tasks if task.get("kind") == "task"]
    terminal = [task for task in coding if task.get("status") in TERMINAL]
    accepted = [task for task in terminal if task["status"] == "accepted"]
    tested = [task for task in terminal if "test_evidence" in task]
    review_events = sum(event.get("kind") == "reviewer" for task in coding
                        for event in task.get("events", []))
    test_attempts = sum(event.get("kind") == "tester" for task in coding
                        for event in task.get("events", []))
    return {
        "tasks": len(coding), "terminal_tasks": len(terminal), "accepted": len(accepted),
        "acceptance_rate": len(accepted) / len(terminal) if terminal else None,
        "tested_tasks": len(tested), "review_calls": review_events, "test_attempts": test_attempts,
        "average_test_attempts": test_attempts / len(tested) if tested else None,
        "integration_conflicts": sum(task.get("status") == "integration_conflict" for task in terminal),
    }


def learning_examples(tasks: list[dict]) -> list[dict]:
    examples = []
    for task in tasks:
        evidence = task.get("test_evidence", {})
        accepted = [event for event in task.get("events", []) if event.get("kind") == "accepted"]
        if task.get("kind") != "task" or task.get("status") != "accepted" or not evidence.get("passed") or not accepted:
            continue
        examples.append({
            "task_id": task["id"], "repository": task["repository"], "goal": task["goal"],
            "plan": task.get("proposal", {}).get("plan"),
            "changes": task.get("proposal", {}).get("changes", []), "diff": task.get("diff"),
            "review": task.get("review"), "test_evidence": evidence,
            "acceptance_summary": accepted[-1]["detail"], "commit": task.get("commit"),
        })
    return examples


def jsonl(tasks: list[dict]) -> str:
    return "".join(json.dumps(example, separators=(",", ":")) + "\n"
                   for example in learning_examples(tasks))
