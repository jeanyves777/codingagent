"""Agent Skills (SKILL.md) and Markdown reference parsing. Text only: scripts are never run."""
import re

FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.S)
HEADING = re.compile(r"^(#{1,3})\s+(.+?)\s*#*\s*$", re.M)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Parse simple `key: value` frontmatter (the subset SKILL.md metadata uses)."""
    match = FRONTMATTER.match(text)
    if not match:
        return {}, text
    metadata, key = {}, None
    for line in match.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        pair = re.match(r"^([A-Za-z0-9_-]+):\s*(.*)$", line)
        if pair and not line.startswith((" ", "\t")):
            key, value = pair.group(1), pair.group(2).strip()
            metadata[key] = value.strip("'\"") if value not in {">", "|", ">-", "|-"} else ""
        elif key and line.startswith((" ", "\t")):
            metadata[key] = (metadata[key] + " " + line.strip()).strip()
    return metadata, text[match.end():]


def parse_skill(text: str, fallback_name: str) -> dict:
    metadata, body = parse_frontmatter(text)
    name = metadata.get("name") or fallback_name
    description = metadata.get("description") or first_paragraph(body)
    return {"name": name[:100], "description": description[:1000], "body": body.strip()}


def first_paragraph(text: str) -> str:
    for block in re.split(r"\n\s*\n", text):
        cleaned = " ".join(line.strip() for line in block.splitlines() if not line.startswith("#"))
        if cleaned:
            return cleaned
    return ""


def sections(text: str, max_chars: int = 4000) -> list[tuple[str, str]]:
    """Split Markdown into (heading, body) chunks so retrieval returns focused passages."""
    positions = [(match.start(), match.group(2)) for match in HEADING.finditer(text)]
    if not positions:
        return [("", text[:max_chars])] if text.strip() else []
    chunks = []
    if positions[0][0] > 0 and text[:positions[0][0]].strip():
        chunks.append(("", text[:positions[0][0]]))
    for index, (start, title) in enumerate(positions):
        end = positions[index + 1][0] if index + 1 < len(positions) else len(text)
        body = text[start:end].strip()
        while body:
            chunks.append((title, body[:max_chars]))
            body = body[max_chars:]
    return [(title, body) for title, body in chunks if len(body.strip()) > 40]


def excerpt(body: str, words: set[str], max_chars: int) -> str:
    """Return the paragraphs of a document that best match the query words."""
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", body) if item.strip()]
    scored = sorted(enumerate(paragraphs), key=lambda pair: (
        -sum(word in pair[1].lower() for word in words), pair[0]))
    chosen, used = [], 0
    for index, paragraph in scored:
        if used + len(paragraph) > max_chars and chosen:
            break
        chosen.append((index, paragraph[:max_chars]))
        used += len(paragraph)
    return "\n\n".join(text for _, text in sorted(chosen))[:max_chars]
