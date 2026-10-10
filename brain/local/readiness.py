"""Readiness levels instead of a single "Healthy".

  Core ready     Python, Git, Coding Brain's state and a local model that has generated text
  Sandbox ready  Core + the Docker engine (Linux containers) + sandbox images that started offline
  Hybrid ready   Sandbox + Claude Code or Codex signed in with a subscription and turned on
  Full ready     Hybrid + knowledge library + OCR, a vision model and a browser for visual checks
  Degraded       something you set up or turned on is not working now (the level reached is still given)
  Blocked        a core requirement is missing or failing; Coding Brain cannot work on goals yet

A level counts only verified evidence: a downloaded model that never generated text, or sandbox
images that never started, leave the level unverified and say which check proves it.
"""
from .components import BY_ID, Status

LEVELS = [("core", "Core ready", ("python", "git", "ollama", "model")),
          ("sandbox", "Sandbox ready", ("docker", "sandbox")),
          ("hybrid", "Hybrid ready", ()),  # one subscription CLI, checked below
          ("full", "Full ready", ("knowledge", "ocr", "vision", "browser"))]
LABELS = {"core": "Core ready", "sandbox": "Sandbox ready", "hybrid": "Hybrid ready", "full": "Full ready",
          "degraded": "Degraded", "blocked": "Blocked", "none": "Blocked"}
CAPABILITIES = {
    "core": "plan and write code with the local model",
    "sandbox": "run your project's tests in an offline Docker sandbox before you accept anything",
    "hybrid": "ask Claude or Codex (your subscription, within your budgets) to plan and diagnose hard tasks",
    "full": "use the knowledge library, read screenshots and documents, and check UIs visually",
}


def premium_ready(results: dict) -> list[str]:
    return [name for name in ("claude", "codex")
            if name in results and results[name].ready and (results[name].data or {}).get("auth") == "authenticated"]


def assess(results: dict[str, Status], app_ok: bool = True, app_problems=(), declined=(), config: dict | None = None,
           profile: str | None = None, expected=()) -> dict:
    """The level reached, what each level still needs, and what is degraded."""
    config = config or {}
    levels, reached, unverified = {}, "none", []
    for key, label, needs in LEVELS:
        missing = [component for component in needs if not (results.get(component) or Status("missing")).ready]
        if key == "core" and not app_ok:
            missing = list(app_problems) + missing
        if key == "hybrid" and not premium_ready(results):
            missing.append("claude or codex (signed in with a subscription and turned on)")
        pending = [component for component in needs if component in results and results[component].ready
                   and (results[component].data or {}).get("unverified")]
        levels[key] = {"label": label, "ready": not missing and not pending, "missing": missing, "unverified": pending,
                       "capability": CAPABILITIES[key]}
        unverified += pending
        if levels[key]["ready"] and (reached == {"core": "none", "sandbox": "core", "hybrid": "sandbox",
                                                  "full": "hybrid"}[key]):
            reached = key
    degraded = []
    for name, status in results.items():
        if status.ready and not (status.data or {}).get("unverified"):
            continue
        is_expected = name in expected and name not in declined
        turned_on = (name in ("claude", "codex") and config.get("supervisors", {}).get(name, {}).get("enabled")) or \
            (name == "vision" and config.get("vision", {}).get("model"))
        if status.state in {"stopped", "failed", "unavailable"} or (status.state == "needs_sign_in" and (turned_on or is_expected)) \
                or (turned_on and not status.ready) or (is_expected and status.state == "needs_restart"):
            degraded.append({"component": name, "title": BY_ID[name].title if name in BY_ID else name,
                             "state": status.state, "detail": status.detail})
    if not levels["core"]["ready"]:
        level = "blocked"
    elif degraded:
        level = "degraded"
    else:
        level = reached
    return {"level": level, "label": LABELS[level], "reached": reached,
            "reached_label": LABELS[reached] if reached != "none" else "none", "verified": not unverified,
            "unverified": unverified, "levels": levels, "degraded": degraded, "profile": profile,
            "next_steps": next_steps(results, levels, unverified)}


def next_steps(results, levels, unverified) -> list[str]:
    steps = []
    if unverified:
        steps.append("codingbrain doctor --full   # proves the model generates and the sandbox starts")
    for key in ("core", "sandbox", "hybrid", "full"):
        if not levels[key]["ready"] and levels[key]["missing"]:
            profile = "--full" if key != "core" else "--profile local"
            steps.append(f"codingbrain install {profile}   # {levels[key]['label']}: needs "
                         + ", ".join(levels[key]["missing"]))
            break
    for name, status in results.items():
        if status.state == "needs_sign_in" and name in ("claude", "codex"):
            steps.append(f"codingbrain install --only {name}   # official {name} sign-in")
    return steps


def render(report: dict) -> str:
    lines = [f"Readiness: {report['label']}" + (f" (reached {report['reached_label']})"
                                                if report["level"] == "degraded" else "")]
    if not report["verified"]:
        lines[0] += " — not fully verified: " + ", ".join(report["unverified"]) + " (codingbrain doctor --full)"
    for key, item in report["levels"].items():
        mark = "ok " if item["ready"] else "-- "
        lines.append(f"  [{mark}] {item['label']:<14} {item['capability']}"
                     + ("" if item["ready"] else f"\n          needs: {', '.join(item['missing'] + item['unverified'])}"))
    for item in report["degraded"]:
        lines.append(f"  degraded: {item['title']}: {item['detail']}")
    if report["next_steps"]:
        lines.append("Next:")
        lines.extend(f"  {step}" for step in report["next_steps"])
    return "\n".join(lines)
