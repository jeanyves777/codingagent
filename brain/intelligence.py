"""Structural repository index and call graph for Python, JavaScript, TypeScript, and TSX."""
from dataclasses import dataclass, asdict
from pathlib import Path
from tree_sitter import Language, Parser
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript
from .repository import EXCLUDED, MAX_FILE


LANGUAGES = {
    ".py": ("python", Language(tree_sitter_python.language())),
    ".js": ("javascript", Language(tree_sitter_javascript.language())),
    ".jsx": ("javascript", Language(tree_sitter_javascript.language())),
    ".ts": ("typescript", Language(tree_sitter_typescript.language_typescript())),
    ".tsx": ("tsx", Language(tree_sitter_typescript.language_tsx())),
}
SYMBOL_NODES = {
    "function_definition": "function",
    "class_definition": "class",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
    "interface_declaration": "interface",
    "type_alias_declaration": "type",
    "enum_declaration": "enum",
}
IMPORT_NODES = {"import_statement", "import_from_statement"}
CALL_NODES = {"call", "call_expression", "new_expression"}
MAX_CALLS = 5000


@dataclass(frozen=True)
class Symbol:
    path: str
    language: str
    kind: str
    name: str
    line: int


@dataclass(frozen=True)
class Dependency:
    path: str
    language: str
    statement: str
    line: int


@dataclass(frozen=True)
class Call:
    path: str
    caller: str
    callee: str
    line: int


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _callee(node, source: bytes) -> str | None:
    """Return the final name of a call target: foo(), obj.foo(), new Foo()."""
    target = node.child_by_field_name("function") or node.child_by_field_name("constructor")
    while target is not None and target.type in {"attribute", "member_expression"}:
        target = target.child_by_field_name("attribute") or target.child_by_field_name("property")
    if target is None or target.type not in {"identifier", "property_identifier"}:
        return None
    return source[target.start_byte:target.end_byte].decode(errors="replace")


def _caller(node, source: bytes) -> str:
    """Return the nearest enclosing named declaration, or <module>."""
    parent = node.parent
    while parent is not None:
        if parent.type in SYMBOL_NODES and parent.type not in {"class_definition", "class_declaration"}:
            name = parent.child_by_field_name("name")
            if name:
                return source[name.start_byte:name.end_byte].decode(errors="replace")
        parent = parent.parent
    return "<module>"


def build_index(root: Path) -> dict:
    symbols, dependencies, calls, errors = [], [], [], []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or path.suffix not in LANGUAGES:
            continue
        relative = path.relative_to(root)
        if any(part.startswith(".") or part in EXCLUDED for part in relative.parts):
            continue
        if path.stat().st_size > MAX_FILE:
            continue
        language_name, language = LANGUAGES[path.suffix]
        source = path.read_bytes()
        tree = Parser(language).parse(source)
        if tree.root_node.has_error:
            errors.append(relative.as_posix())
        for node in _walk(tree.root_node):
            if node.type in SYMBOL_NODES:
                name_node = node.child_by_field_name("name")
                if name_node:
                    symbols.append(Symbol(relative.as_posix(), language_name,
                                          SYMBOL_NODES[node.type], source[name_node.start_byte:name_node.end_byte].decode(),
                                          node.start_point.row + 1))
            elif node.type in IMPORT_NODES:
                statement = source[node.start_byte:node.end_byte].decode(errors="replace")[:500]
                dependencies.append(Dependency(relative.as_posix(), language_name, statement,
                                               node.start_point.row + 1))
            elif node.type in CALL_NODES and len(calls) < MAX_CALLS:
                callee = _callee(node, source)
                if callee:
                    calls.append(Call(relative.as_posix(), _caller(node, source), callee,
                                      node.start_point.row + 1))
    return {"symbols": [asdict(item) for item in symbols],
            "dependencies": [asdict(item) for item in dependencies],
            "calls": [asdict(item) for item in calls], "parse_errors": errors}


def call_graph(index: dict, names: set[str], limit: int = 40) -> dict:
    """Return direct callers and callees of the named symbols."""
    callers, callees = [], []
    for call in index.get("calls", []):
        if call["callee"] in names and len(callers) < limit:
            callers.append(call)
        if call["caller"] in names and len(callees) < limit:
            callees.append(call)
    return {"callers": callers, "callees": callees}


def relevant_context(index: dict, query: str, limit: int = 40) -> dict:
    words = {word.lower() for word in query.replace("_", " ").split() if len(word) > 2}
    def score(item):
        text = " ".join(str(value) for value in item.values()).lower()
        return sum(word in text for word in words)
    symbols = sorted(index.get("symbols", []), key=score, reverse=True)
    dependencies = sorted(index.get("dependencies", []), key=score, reverse=True)
    focus = {item["name"] for item in symbols
             if any(word in item["name"].lower() for word in words)}
    return {"symbols": symbols[:limit], "dependencies": dependencies[:limit],
            "call_graph": call_graph(index, focus, limit),
            "parse_errors": index.get("parse_errors", [])[:20]}


def _terms(text: str) -> set[str]:
    """Lowercase terms, splitting snake_case and camelCase so `wordCount` matches `word count`."""
    import re
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text).replace("_", " ").replace(".", " ")
    return {word.lower() for word in re.findall(r"[A-Za-z][A-Za-z0-9]+", spaced) if len(word) > 2}


def ranked_context(index: dict, query: str, max_symbols: int = 15, max_chars: int = 3000) -> dict:
    """Scored, bounded code context: only symbols that match the goal, the files that hold them,
    their imports, and their direct callers and callees, compacted to fit a character budget."""
    words = _terms(query)

    def score(symbol):
        name_terms = _terms(symbol["name"])
        exact = 5 if symbol["name"].lower() in query.lower() else 0
        return exact + 3 * len(words & name_terms) + len(words & _terms(symbol["path"]))
    ranked = sorted(((score(item), item) for item in index.get("symbols", [])),
                    key=lambda pair: -pair[0])
    chosen = [item for points, item in ranked if points > 0][:max_symbols]
    if not chosen:
        chosen = [item for _, item in ranked[:min(8, max_symbols)]]
    files = list(dict.fromkeys(item["path"] for item in chosen))[:6]
    names = {item["name"] for item in chosen}
    graph = call_graph(index, names, 10)
    context = {
        "files": files,
        "symbols": [f"{item['path']}:{item['line']} {item['kind']} {item['name']}" for item in chosen],
        "imports": [f"{item['path']}:{item['line']} {item['statement'][:120]}"
                    for item in index.get("dependencies", []) if item["path"] in files][:10],
        "callers": [f"{call['path']}:{call['line']} {call['caller']} -> {call['callee']}"
                    for call in graph["callers"]],
        "callees": [f"{call['path']}:{call['line']} {call['caller']} -> {call['callee']}"
                    for call in graph["callees"]],
        "parse_errors": index.get("parse_errors", [])[:5],
    }
    import json
    while len(json.dumps(context)) > max_chars:
        longest = max(("symbols", "imports", "callers", "callees"), key=lambda key: len(context[key]))
        if not context[longest]:
            break
        context[longest].pop()
    return context
