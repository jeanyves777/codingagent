"""Deterministic verification gates that run before any model reviews a proposal.

Nothing here executes proposed code. Python is compiled to bytecode in memory (parse and
compile only, never run), JSON and TOML are parsed, and JavaScript/TypeScript are parsed with
Tree-sitter. Failures become short, structured diagnostics that a small model can act on.
"""
import json
import re
import tomllib
from dataclasses import dataclass, asdict


@dataclass(frozen=True)
class Diagnostic:
    category: str
    file: str
    line: int | None
    reason: str
    action: str

    def as_dict(self) -> dict:
        return asdict(self)


SYNTAX_ACTION = ("Correct the generated syntax. Preserve the intended behavior. "
                 "Return the complete corrected file in the proposal.")


def _python(path: str, content: str) -> list[Diagnostic]:
    try:
        compile(content, path, "exec", dont_inherit=True)
    except SyntaxError as error:
        reason = error.msg or "invalid syntax"
        if error.text:
            reason += f" near: {error.text.strip()[:120]}"
        return [Diagnostic("Python syntax", path, error.lineno, reason, SYNTAX_ACTION)]
    except (ValueError, RecursionError, MemoryError) as error:
        return [Diagnostic("Python syntax", path, None, str(error)[:200], SYNTAX_ACTION)]
    return []


def _json(path: str, content: str) -> list[Diagnostic]:
    try:
        json.loads(content)
    except json.JSONDecodeError as error:
        return [Diagnostic("JSON syntax", path, error.lineno, error.msg, SYNTAX_ACTION)]
    return []


def _toml(path: str, content: str) -> list[Diagnostic]:
    try:
        tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        line = re.search(r"line (\d+)", str(error))
        return [Diagnostic("TOML syntax", path, int(line.group(1)) if line else None,
                           str(error)[:200], SYNTAX_ACTION)]
    return []


def _tree_sitter(path: str, content: str) -> list[Diagnostic]:
    from tree_sitter import Parser
    from .intelligence import LANGUAGES, _walk
    suffix = "." + path.rsplit(".", 1)[-1]
    language_name, language = LANGUAGES[suffix]
    tree = Parser(language).parse(content.encode())
    if not tree.root_node.has_error:
        return []
    node = next((item for item in _walk(tree.root_node) if item.type == "ERROR" or item.is_missing),
                tree.root_node)
    what = f"missing {node.type}" if node.is_missing else "unexpected or malformed code"
    return [Diagnostic(f"{language_name.capitalize()} syntax", path, node.start_point.row + 1,
                       what, SYNTAX_ACTION)]


VALIDATORS = {".py": _python, ".json": _json, ".toml": _toml, ".js": _tree_sitter,
              ".jsx": _tree_sitter, ".ts": _tree_sitter, ".tsx": _tree_sitter}


def validate_change(path: str, content: str, before: str | None = None) -> list[Diagnostic]:
    """Validate one proposed file. `before` is the baseline content, used for scope checks."""
    if before is not None and content == before:
        return [Diagnostic("Scope", path, None, "Proposed content is identical to the current file",
                           "Make the change the goal requires, or drop this file from the proposal.")]
    suffix = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    validator = VALIDATORS.get(suffix)
    return validator(path, content) if validator else []


def mechanical_repair(path: str, content: str) -> str | None:
    """Fix double-escaped line breaks, the most common small-model output defect.

    Used only when the file does not compile; the repaired text is returned only if it compiles.
    Tests remain the authority on whether the result is correct."""
    if not path.endswith(".py") or "\\n" not in content or not _python(path, content):
        return None
    candidate = content.replace("\\r\\n", "\n").replace("\\n", "\n").replace("\\t", "    ")
    return candidate if not _python(path, candidate) else None


def format_diagnostics(diagnostics: list[Diagnostic]) -> str:
    """Compact, model-facing report."""
    blocks = []
    for item in diagnostics[:5]:
        blocks.append("\n".join([
            "Validation: FAILED", f"Category: {item.category}", f"File: {item.file}",
            f"Line: {item.line if item.line is not None else 'n/a'}", f"Reason: {item.reason}",
            "Required action:", item.action]))
    return "\n\n".join(blocks)


class ProposalInvalid(ValueError):
    def __init__(self, diagnostics: list[Diagnostic], contents: dict[str, str] | None = None):
        self.diagnostics, self.contents = diagnostics, contents or {}
        super().__init__(format_diagnostics(diagnostics))

    @property
    def excerpt(self) -> str:
        """The few rejected lines around each error, so a correction needs little context."""
        parts = []
        for item in self.diagnostics[:3]:
            content = self.contents.get(item.file)
            if content is None or item.line is None:
                continue
            lines = content.splitlines()
            start = max(0, item.line - 4)
            window = lines[start:item.line + 3]
            numbered = "\n".join(f"{start + index + 1}: {text}" for index, text in enumerate(window))
            parts.append(f"\nRejected {item.file} around line {item.line}:\n{numbered}")
        return "".join(parts)


def classify_test_failure(evidence: dict) -> dict:
    """Classify sandbox evidence and keep only the lines a model needs."""
    output, code = evidence.get("output", ""), evidence.get("exit_code")
    if evidence.get("cancelled"):
        category = "cancelled"
    elif code is None:
        category = "timeout" if "timed out" in output else "infrastructure"
    elif code in (125, 126, 127):
        category = "infrastructure"
    elif code == 5:
        category = "no_tests"
    elif "SyntaxError" in output or "IndentationError" in output:
        category = "syntax"
    elif code in (2, 3, 4) or "ERROR collecting" in output:
        category = "collection"
    else:
        category = "test_failure"
    keep = [line for line in output.splitlines()
            if line.startswith(("FAILED", "ERROR", "E ", ">")) or "Error" in line
            or re.match(r"^\S+\.py:\d+", line) or re.search(r"\d+ (passed|failed)", line)]
    summary = "\n".join(dict.fromkeys(keep))[-3000:] or output[-1500:]
    return {"category": category, "exit_code": code, "summary": summary}


STATIC_ACTION = ("Define or import every name you use, and import only names that exist in the module "
                 "you import from. Return the complete corrected files.")


def _module_names(source: str) -> set[str] | None:
    """Top-level names a module defines; None when it cannot be determined (star import, __getattr__)."""
    import ast
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
            if node.name == "__getattr__":
                return None
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                names.update(item.id for item in ast.walk(target) if isinstance(item, ast.Name))
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                if alias.name == "*":
                    return None
                names.add((alias.asname or alias.name).split(".")[0])
        elif isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
            names.update(item.id for item in ast.walk(node) if isinstance(item, ast.Name)
                         and isinstance(item.ctx, ast.Store))
            names.update((alias.asname or alias.name).split(".")[0] for item in ast.walk(node)
                         if isinstance(item, (ast.Import, ast.ImportFrom)) for alias in item.names)
    return names


def static_issues(path: str, content: str, read_module) -> list[Diagnostic]:
    """Deterministic static analysis for Python: undefined names (pyflakes) and names imported
    from repository modules that those modules do not define. read_module(dotted) returns the
    module's final source (proposal or workspace) or None for modules outside the repository."""
    import ast
    from pyflakes import checker, messages
    try:
        tree = ast.parse(content, filename=path)
    except SyntaxError:
        return []
    found = []
    for message in checker.Checker(tree, filename=path).messages:
        if isinstance(message, (messages.UndefinedName, messages.UndefinedLocal, messages.UndefinedExport)):
            found.append(Diagnostic("Undefined name", path, message.lineno,
                                    (message.message % message.message_args)[:200], STATIC_ACTION))
    package = path.split("/")[:-1]
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or any(alias.name == "*" for alias in node.names):
            continue
        base = package[:len(package) - (node.level - 1)] if node.level > 1 else (package if node.level else [])
        module = ".".join(base + ([node.module] if node.module else []))
        source = read_module(module) if module else None
        if source is None:
            continue
        defined = _module_names(source)
        if defined is None:
            continue
        for alias in node.names:
            if alias.name not in defined and read_module(f"{module}.{alias.name}") is None:
                found.append(Diagnostic("Missing import", path, node.lineno,
                                        f"{module} does not define {alias.name}", STATIC_ACTION))
    return found
