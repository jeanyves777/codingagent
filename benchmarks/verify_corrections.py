"""Reproducible evidence for the six post-pilot corrections.

For each correction, copy the repository to a temporary directory, disable that one correction
with a minimal source edit, and run its regression tests: they must fail. Then run them on the
unmodified code: they must pass. Usage: python benchmarks/verify_corrections.py
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = "tests/test_corrections.py"
# correction -> (file, original text, text that disables it, tests that must then fail); a list of
# (file, original, disabled) edits replaces the first three when a correction spans several places.
MUTATIONS = {
    "1 completion verification": (
        "brain/service.py", "        if not self.requirement_checks or task.get(\"requirement_tests\") is not None:",
        "        if True:",
        ["test_requirement_checks_catch_a_false_success_and_drive_a_repair",
         "test_unmet_requirement_checks_never_leave_the_task_worse"]),
    "1a generated checks validated before repairs": (
        "brain/service.py", "            if not assessment[\"invalid\"]:", "            if True:",
        ["test_invalid_checks_are_rejected_without_using_the_repair_budget"]),
    "1b no repeated repairs against an unchanged failing check": (
        "brain/service.py", "        if set(signatures) <= set(seen):", "        if False:",
        ["test_no_repeated_repairs_against_an_unchanged_failing_check"]),
    "1c rejected checks preserved for audit": (
        "brain/service.py", "        entries.append(entry)\n", "        pass\n",
        ["test_rejected_checks_are_preserved_with_reasons_and_checksums"]),
    "2 escalation on proposal failures": (
        "brain/service.py", "            if not self.supervision or proposals < self.supervision.escalate_after:",
        "            if True:",
        ["test_rejected_initial_proposals_escalate_within_budget"]),
    "3 relevant knowledge retrieval": ([
        ("brain/knowledge.py", "if hit[\"id\"] not in seen and wanted & about:", "if hit[\"id\"] not in seen:"),
        ("brain/knowledge.py", "kinds=(\"skill\",), metadata_only=True)", "kinds=(\"skill\",), metadata_only=False)"),
        ("brain/knowledge.py", "include_references: bool = False", "include_references: bool = True")],
        ["test_packet_keeps_relevant_skills_and_drops_unrelated_ones"]),
    "4 static analysis": (
        "brain/service.py", "                if not found:\n                    found = static_issues(",
        "                if False:\n                    found = static_issues(",
        ["test_undefined_name_is_rejected_before_review", "test_missing_cross_file_import_is_rejected"]),
    "5 failure classification": (
        "brain/gauntlet.py", "    if model_failure_evidence(events):\n        return \"model\"",
        "    if False:\n        return \"model\"",
        ["test_upstream_model_failure_is_the_root_cause_not_orchestration",
         "test_pilot_rescoring_changes_only_the_mislabeled_record"]),
    "6 model-level accounting": (
        "brain/accounting.py", "    if log is not None:\n        log.append(", "    if False:\n        log.append(",
        ["test_every_inference_and_fallback_is_attributed_to_the_task",
         "test_adapters_record_the_model_that_actually_served",
         "test_escalations_record_each_supervisor_attempt_and_served_model"]),
}


def pytest(root: Path, tests: list[str]) -> dict:
    selected = [f"{TESTS}::{name}" for name in tests]
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *selected],
                            cwd=root, capture_output=True, text=True, timeout=600)
    failed = sorted({line.split("::")[1].split(" ")[0] for line in result.stdout.splitlines()
                     if line.startswith("FAILED")})
    return {"exit_code": result.returncode, "failed": failed}


def main() -> int:
    report, ok = {}, True
    for correction, (*edits, tests) in MUTATIONS.items():
        edits = edits[0] if isinstance(edits[0], list) else [tuple(edits)]
        with tempfile.TemporaryDirectory(prefix="verify-correction-") as scratch:
            copy = Path(scratch) / "repo"
            shutil.copytree(ROOT, copy, ignore=shutil.ignore_patterns(".git", ".venv", "__pycache__", "*.egg-info"))
            with_fix = pytest(copy, tests)
            for path, original, disabled in edits:
                source = copy / path
                text = source.read_text(encoding="utf-8")
                if text.count(original) != 1:
                    raise SystemExit(f"{correction}: anchor not found exactly once in {path}")
                source.write_text(text.replace(original, disabled), encoding="utf-8")
            without_fix = pytest(copy, tests)
        passed = with_fix["exit_code"] == 0 and sorted(tests) == without_fix["failed"]
        ok &= passed
        report[correction] = {"tests": tests, "pass_with_correction": with_fix["exit_code"] == 0,
                              "fail_without_correction": without_fix["failed"], "verified": passed}
    print(json.dumps(report, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
