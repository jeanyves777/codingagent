"""Phase 2 API and dependency intelligence: OpenAPI discovery and summaries, contract and
data-integrity validation, upstream change detection, endpoint-usage checks, and SDK/version
checks. All network access goes through the Web Verification Broker."""
import json
import re
import sqlite3
import time
from pathlib import Path
from urllib.parse import urlparse
import yaml
from jsonschema import Draft202012Validator
from packaging.version import InvalidVersion, Version

METHODS = ("get", "put", "post", "delete", "patch", "head", "options")
WELL_KNOWN = ("/openapi.json", "/openapi.yaml", "/swagger.json", "/v3/api-docs", "/api-docs",
              "/.well-known/openapi.json", "/api/openapi.json")
DEPRECATIONS = json.loads((Path(__file__).parent / "data" / "deprecations.json").read_text(encoding="utf-8"))


def parse_spec(body: bytes | str) -> dict:
    text = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
    spec = json.loads(text) if text.lstrip().startswith("{") else yaml.safe_load(text)
    if not isinstance(spec, dict) or not ("openapi" in spec or "swagger" in spec) or "paths" not in spec:
        raise ValueError("Document is not an OpenAPI/Swagger specification")
    return spec


async def discover_openapi(broker, base_url: str) -> tuple[dict | None, list[dict]]:
    """Try the base URL itself, then well-known locations. Returns (spec, evidence list)."""
    parsed = urlparse(base_url)
    root = f"{parsed.scheme}://{parsed.netloc}"
    evidence = []
    for url in [base_url] + [root + path for path in WELL_KNOWN]:
        result, body = await broker.fetch(url, crawl=False)
        evidence.append(result.as_dict())
        if result.outcome == "verified":
            try:
                spec = parse_spec(body)
                spec["x-coding-brain-source"] = result.as_dict()
                return spec, evidence
            except (ValueError, yaml.YAMLError):
                continue
    return None, evidence


def _resolve(spec: dict, node, depth=0):
    """Inline local $refs (bounded) and translate OpenAPI 3.0 `nullable` for JSON Schema."""
    if depth > 25:
        return {}
    if isinstance(node, list):
        return [_resolve(spec, item, depth + 1) for item in node]
    if not isinstance(node, dict):
        return node
    if "$ref" in node and isinstance(node["$ref"], str) and node["$ref"].startswith("#/"):
        target = spec
        for part in node["$ref"][2:].split("/"):
            target = target.get(part.replace("~1", "/").replace("~0", "~"), {}) if isinstance(target, dict) else {}
        return _resolve(spec, target, depth + 1)
    resolved = {key: _resolve(spec, value, depth + 1) for key, value in node.items()
                if not key.startswith("x-")}
    if resolved.pop("nullable", False) and "type" in resolved:
        resolved["type"] = [resolved["type"], "null"] if isinstance(resolved["type"], str) else \
            list(resolved["type"]) + ["null"]
    return resolved


def endpoints(spec: dict) -> list[dict]:
    result = []
    global_security = spec.get("security")
    for path, item in (spec.get("paths") or {}).items():
        shared = item.get("parameters", []) if isinstance(item, dict) else []
        for method in METHODS:
            operation = item.get(method) if isinstance(item, dict) else None
            if not isinstance(operation, dict):
                continue
            parameters = [_resolve(spec, parameter) for parameter in shared + operation.get("parameters", [])]
            body = _resolve(spec, operation.get("requestBody", {}))
            ok = next((code for code in operation.get("responses", {}) if str(code).startswith("2")), None)
            schema = None
            if ok:
                content = _resolve(spec, operation["responses"][ok]).get("content", {})
                schema = next((value.get("schema") for key, value in content.items() if "json" in key), None)
            result.append({
                "method": method.upper(), "path": path, "operation_id": operation.get("operationId"),
                "summary": (operation.get("summary") or operation.get("description") or "")[:200],
                "deprecated": bool(operation.get("deprecated")),
                "parameters": [{"name": parameter.get("name"), "in": parameter.get("in"),
                                "required": bool(parameter.get("required")),
                                "type": (parameter.get("schema") or {}).get("type")} for parameter in parameters],
                "request_body_required": bool(body.get("required")),
                "security": operation.get("security", global_security),
                "success_status": ok, "response_schema": schema})
    return result


def summarize(spec: dict, query: str, limit: int = 8) -> dict:
    """Only the endpoints relevant to the query, with parameters and top-level response fields."""
    from .intelligence import _terms
    words = _terms(query)
    ranked = sorted(endpoints(spec), key=lambda item: -len(words & _terms(
        f"{item['path']} {item['operation_id'] or ''} {item['summary']}")))
    servers = [server.get("url") for server in spec.get("servers", []) if isinstance(server, dict)]
    selected = []
    for item in ranked[:limit]:
        fields = sorted(((item["response_schema"] or {}).get("properties") or {}).keys())[:20]
        selected.append({key: item[key] for key in ("method", "path", "operation_id", "summary", "deprecated",
                                                     "parameters", "security", "success_status")}
                        | {"response_fields": fields})
    return {"title": spec.get("info", {}).get("title"), "version": spec.get("info", {}).get("version"),
            "servers": servers[:3], "endpoints": selected,
            "source": spec.get("x-coding-brain-source")}


def diff_specs(old: dict, new: dict) -> dict:
    """Breaking and notable differences between two versions of an API description."""
    before = {(item["method"], item["path"]): item for item in endpoints(old)}
    after = {(item["method"], item["path"]): item for item in endpoints(new)}
    changes = []
    for key in sorted(before.keys() & after.keys()):
        a, b = before[key], after[key]
        notes = []
        old_params = {(p["name"], p["in"]): p for p in a["parameters"]}
        new_params = {(p["name"], p["in"]): p for p in b["parameters"]}
        notes += [f"parameter removed: {name}" for name, _ in old_params.keys() - new_params.keys()]
        notes += [f"new required parameter: {name}" for (name, where), p in new_params.items()
                  if p["required"] and (name, where) not in old_params]
        notes += [f"parameter became required: {name}" for key2, p in new_params.items()
                  if p["required"] and key2 in old_params and not old_params[key2]["required"]]
        old_fields = (a["response_schema"] or {}).get("properties") or {}
        new_fields = (b["response_schema"] or {}).get("properties") or {}
        notes += [f"response field removed: {name}" for name in old_fields.keys() - new_fields.keys()]
        notes += [f"response field type changed: {name} {old_fields[name].get('type')} -> {new_fields[name].get('type')}"
                  for name in old_fields.keys() & new_fields.keys()
                  if old_fields[name].get("type") != new_fields[name].get("type")]
        if b["deprecated"] and not a["deprecated"]:
            notes.append("endpoint deprecated")
        if a["security"] != b["security"]:
            notes.append("authentication requirements changed")
        if notes:
            changes.append({"endpoint": f"{key[0]} {key[1]}", "changes": notes})
    return {"removed": [f"{m} {p}" for m, p in sorted(before.keys() - after.keys())],
            "added": [f"{m} {p}" for m, p in sorted(after.keys() - before.keys())],
            "changed": changes,
            "version": [old.get("info", {}).get("version"), new.get("info", {}).get("version")]}


def _match_path(template: str, path: str) -> bool:
    pattern = "^" + re.sub(r"\\\{[^/]+?\\\}", "[^/]+", re.escape(template)) + "/?$"
    return re.match(pattern, path) is not None


def verify_endpoints(spec: dict, used: list[tuple[str, str]]) -> list[dict]:
    """Check the (method, path) pairs code uses against the specification."""
    available = endpoints(spec)
    results = []
    for method, path in used:
        clean = urlparse(path).path if path.startswith("http") else path.split("?")[0]
        for server in spec.get("servers", []):
            prefix = urlparse(server.get("url", "")).path.rstrip("/")
            if prefix and clean.startswith(prefix + "/"):
                clean = clean[len(prefix):]
        matches = [item for item in available if _match_path(item["path"], clean)]
        same_method = [item for item in matches if item["method"] == method.upper()]
        if same_method:
            outcome = "verified" if not same_method[0]["deprecated"] else "inconclusive"
            reason = "documented" if outcome == "verified" else "documented but deprecated"
        elif matches:
            outcome, reason = "failed", f"path exists but not for {method.upper()}"
        else:
            outcome, reason = "failed", "not in the API description (moved or removed?)"
        results.append({"method": method.upper(), "path": path, "outcome": outcome, "reason": reason})
    return results


CALL = re.compile(r"""\.(get|post|put|patch|delete)\(\s*f?["']((?:https://[^"'/]+)?/[^"'\s{]*(?:\{[^}"']*\}[^"'\s{]*)*)["']""")


def used_endpoints(root: Path, max_files: int = 400) -> list[tuple[str, str, str]]:
    """(method, path, location) for literal HTTP calls such as client.get("/v1/items")."""
    found = []
    for number, path in enumerate(sorted(root.rglob("*"))):
        if number > max_files * 10 or len(found) > 200:
            break
        relative = path.relative_to(root).as_posix()
        if (path.suffix not in {".py", ".js", ".ts", ".tsx", ".jsx"} or not path.is_file()
                or any(part.startswith(".") or part in {"node_modules", ".venv", "venv"} for part in path.parts)):
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), 1):
            for match in CALL.finditer(line):
                found.append((match.group(1), re.sub(r"\{[^}]*\}", "{x}", match.group(2)),
                              f"{relative}:{line_number}"))
    return found


def validate_response(spec: dict, method: str, path: str, status: int, body) -> dict:
    """Validate a real or recorded response body against the documented schema."""
    operation = next((item for item in endpoints(spec)
                      if item["method"] == method.upper() and _match_path(item["path"], path)), None)
    if operation is None:
        return {"outcome": "failed", "errors": [f"{method.upper()} {path} is not documented"]}
    if str(status) != str(operation["success_status"]):
        return {"outcome": "failed", "errors": [f"status {status}, documented {operation['success_status']}"]}
    schema = operation["response_schema"]
    if not schema:
        return {"outcome": "inconclusive", "errors": ["No JSON response schema documented"]}
    errors = sorted(Draft202012Validator(schema).iter_errors(body), key=lambda error: list(error.path))
    return {"outcome": "failed" if errors else "verified",
            "errors": [f"{'/'.join(str(part) for part in error.path) or '<root>'}: {error.message}"[:300]
                       for error in errors[:20]]}


def integrity(records: list[dict], unique: list[str] = (), not_null: list[str] = (),
              max_age: dict | None = None, schema: dict | None = None, now: float | None = None) -> dict:
    """Configurable data invariants: schema, required non-null fields, uniqueness, freshness."""
    issues, now = [], now or time.time()
    validator = Draft202012Validator(schema) if schema else None
    seen = {field: {} for field in unique}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            issues.append(f"record {index}: not an object")
            continue
        if validator:
            issues += [f"record {index}: {'/'.join(map(str, error.path)) or '<root>'}: {error.message}"[:300]
                       for error in validator.iter_errors(record)]
        issues += [f"record {index}: {field} is null or missing" for field in not_null if record.get(field) is None]
        for field in unique:
            value = record.get(field)
            if value is not None and value in seen[field]:
                issues.append(f"record {index}: duplicate {field}={value!r} (first at {seen[field][value]})")
            seen[field].setdefault(value, index)
        for field, seconds in (max_age or {}).items():
            stamp = record.get(field)
            moment = _timestamp(stamp)
            if stamp is not None and moment is None:
                issues.append(f"record {index}: {field} is not a timestamp")
            elif moment is not None and now - moment > seconds:
                issues.append(f"record {index}: {field} is stale ({int(now - moment)}s old)")
    return {"outcome": "failed" if issues else "verified", "records": len(records), "issues": issues[:50]}


def _timestamp(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        from datetime import datetime
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return None
    return None


class SpecTracker:
    """Snapshots of API descriptions, so upstream changes are detected and explained."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with sqlite3.connect(path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS specs (url TEXT, sha256 TEXT, fetched_at REAL, spec TEXT, "
                       "PRIMARY KEY (url, sha256))")

    async def check(self, broker, url: str) -> dict:
        evidence, body = await broker.fetch(url, max_age=0, crawl=False)
        if evidence.outcome != "verified":
            return {"outcome": evidence.outcome, "evidence": evidence.as_dict(), "diff": None}
        spec = parse_spec(body)
        with sqlite3.connect(self.path) as db:
            previous = db.execute("SELECT sha256, spec FROM specs WHERE url=? ORDER BY fetched_at DESC LIMIT 1",
                                  (url,)).fetchone()
            if not previous or previous[0] != evidence.sha256:
                db.execute("INSERT OR REPLACE INTO specs VALUES (?,?,?,?)",
                           (url, evidence.sha256, time.time(), json.dumps(spec)))
        if not previous:
            return {"outcome": "verified", "evidence": evidence.as_dict(), "diff": None, "first_snapshot": True}
        if previous[0] == evidence.sha256:
            return {"outcome": "verified", "evidence": evidence.as_dict(), "diff": None, "changed": False}
        return {"outcome": "verified", "evidence": evidence.as_dict(), "changed": True,
                "diff": diff_specs(json.loads(previous[1]), spec)}


def declared_dependencies(root: Path) -> dict:
    """{ecosystem: {package: version specifier}} from common manifest files."""
    found = {"pypi": {}, "npm": {}}
    for name in ("requirements.txt", "requirements-dev.txt"):
        path = root / name
        if path.exists():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                match = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*([<>=!~].*?)?\s*(?:#.*)?$", line)
                if match and not line.strip().startswith(("-", "#")):
                    found["pypi"][match.group(1).lower()] = (match.group(2) or "").strip()
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        import tomllib
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError:
            data = {}
        for requirement in data.get("project", {}).get("dependencies", []):
            match = re.match(r"^\s*([A-Za-z0-9_.-]+)(?:\[[^\]]*\])?\s*(.*)$", requirement)
            if match:
                found["pypi"][match.group(1).lower()] = match.group(2).strip()
    package = root / "package.json"
    if package.exists():
        try:
            data = json.loads(package.read_text(encoding="utf-8"))
        except ValueError:
            data = {}
        for section in ("dependencies", "devDependencies"):
            found["npm"].update({name.lower(): str(spec) for name, spec in (data.get(section) or {}).items()})
    return found


async def latest_version(broker, ecosystem: str, package: str) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_.@/-]{1,100}", package):
        raise ValueError("Invalid package name")
    url = (f"https://pypi.org/pypi/{package}/json" if ecosystem == "pypi"
           else f"https://registry.npmjs.org/{package}")
    evidence, data = await broker.json(url)
    if not data:
        return {"package": package, "ecosystem": ecosystem, "latest": None, "outcome": evidence.outcome,
                "evidence": evidence.as_dict()}
    latest = data["info"]["version"] if ecosystem == "pypi" else data.get("dist-tags", {}).get("latest")
    return {"package": package, "ecosystem": ecosystem, "latest": latest, "outcome": "verified",
            "evidence": evidence.as_dict()}


def _version_floor(specifier: str) -> Version | None:
    match = re.search(r"(==|>=|~=|\^|~)?\s*v?(\d+(?:\.\d+){0,2})", specifier or "")
    try:
        return Version(match.group(2)) if match else None
    except InvalidVersion:
        return None


async def sdk_findings(broker, root: Path, packages: list[str] | None = None) -> list[dict]:
    """Outdated usage patterns from a curated, versioned list, confirmed against live registries."""
    declared = declared_dependencies(root)
    findings, checked = [], {}
    sources = [path for path in sorted(root.rglob("*")) if path.is_file() and path.suffix in {".py", ".js", ".ts"}
               and not any(part.startswith(".") or part in {"node_modules", "venv"} for part in path.parts)][:400]
    for rule in DEPRECATIONS:
        if packages is not None and rule["package"] not in packages:
            continue
        pattern = re.compile(rule["pattern"])
        hits = []
        for path in sources:
            text = path.read_text(encoding="utf-8", errors="replace")
            if rule.get("requires_import") and not re.search(rule["requires_import"], text):
                continue
            hits += [f"{path.relative_to(root).as_posix()}:{number}" for number, line in
                     enumerate(text.splitlines(), 1) if pattern.search(line)]
        if not hits:
            continue
        key = (rule["ecosystem"], rule["package"])
        if key not in checked:
            checked[key] = await latest_version(broker, *key)
        latest, removed = checked[key]["latest"], Version(rule["changed_in"])
        floor = _version_floor(declared[rule["ecosystem"]].get(rule["package"], ""))
        if latest is None:
            outcome = "inconclusive"
        else:
            outcome = "failed" if Version(latest) >= removed else "verified"
        findings.append({"package": rule["package"], "locations": hits[:10], "problem": rule["problem"],
                         "replacement": rule["replacement"], "changed_in": rule["changed_in"],
                         "latest": latest, "declared": declared[rule["ecosystem"]].get(rule["package"]),
                         "pinned_below_change": bool(floor and floor < removed and "==" in
                                                     declared[rule["ecosystem"]].get(rule["package"], "")),
                         "outcome": outcome, "source": rule["source"],
                         "registry_evidence": checked[key]["evidence"]})
    return findings
