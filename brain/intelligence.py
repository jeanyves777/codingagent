"""Structural repository index for Python, JavaScript, TypeScript, and TSX."""
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


def _walk(node):
    yield node
    for child in node.children:
        yield from _walk(child)


def build_index(root: Path) -> dict:
    symbols, dependencies, errors = [], [], []
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
    return {"symbols": [asdict(item) for item in symbols],
            "dependencies": [asdict(item) for item in dependencies], "parse_errors": errors}


def relevant_context(index: dict, query: str, limit: int = 40) -> dict:
    words = {word.lower() for word in query.replace("_", " ").split() if len(word) > 2}
    def score(item):
        text = " ".join(str(value) for value in item.values()).lower()
        return sum(word in text for word in words)
    symbols = sorted(index.get("symbols", []), key=score, reverse=True)
    dependencies = sorted(index.get("dependencies", []), key=score, reverse=True)
    return {"symbols": symbols[:limit], "dependencies": dependencies[:limit],
            "parse_errors": index.get("parse_errors", [])[:20]}
