"""Check maintained documentation links, anchors, assets and Node entry points."""

import json
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt

ROOT = Path(__file__).resolve().parents[1]
MARKDOWN = MarkdownIt()


class HTMLLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.targets = []
        self.anchors = set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        for name in ("href", "src"):
            if target := values.get(name):
                self.targets.append(target)
        if anchor := values.get("id") or (values.get("name") if tag == "a" else None):
            self.anchors.add(anchor)


def document(path):
    targets, anchors, headings = [], set(), Counter()
    tokens = MARKDOWN.parse(path.read_text())
    for index, token in enumerate(tokens):
        line = token.map[0] + 1 if token.map else 1
        if token.type == "heading_open":
            text = "".join(child.content for child in tokens[index + 1].children or ()
                           if child.type in {"text", "code_inline"})
            slug = re.sub(r"[^\w\- ]", "", text.lower()).replace(" ", "-")
            anchors.add(slug if not headings[slug] else f"{slug}-{headings[slug]}")
            headings[slug] += 1
        children = token.children or ()
        for child in children:
            target = child.attrGet("href") if child.type == "link_open" else child.attrGet("src") if child.type == "image" else None
            if target:
                targets.append((line, target))
        for html in [token, *children]:
            if html.type in {"html_block", "html_inline"}:
                parser = HTMLLinks()
                parser.feed(html.content)
                targets.extend((line, target) for target in parser.targets)
                anchors.update(parser.anchors)
    return targets, anchors


def main():
    files = [ROOT / name for name in (
        "README.md", "README-cn.md", "THIRD_PARTY.md", "eval/README.md", "scripts/README.md",
        "sdk/python/README.md", "migrations/README.md", "frontend/design.md",
        "frontend/theme/assets/fonts/README.md", "frontend/theme/src/fonts/README.md",
        "frontend/theme/src/fonts/reading-serif.md",
    )]
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    parsed = {path.resolve(): document(path) for path in files}
    failures, count, fragments = [], 0, 0
    for path in files:
        for line, target in parsed[path.resolve()][0]:
            link = urlsplit(target)
            if link.scheme or link.netloc:
                continue
            destination = (path.parent / unquote(link.path)).resolve() if link.path else path.resolve()
            count += 1
            if not destination.exists():
                failures.append(f"{path.relative_to(ROOT)}:{line}: missing {target}")
            elif link.fragment and destination.suffix == ".md":
                fragments += 1
                if destination not in parsed:
                    parsed[destination] = document(destination)
                if unquote(link.fragment) not in parsed[destination][1]:
                    failures.append(f"{path.relative_to(ROOT)}:{line}: missing anchor {target}")
    node_entries = 0
    for name, command in json.loads((ROOT / "package.json").read_text())["scripts"].items():
        for entry in re.findall(r"(?:^|&&\s*)node\s+([^\s]+)", command):
            node_entries += 1
            if not (ROOT / entry).is_file():
                failures.append(f"package.json: {name} points to missing {entry}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Maintained documentation: {len(files)} files, {count} local links/assets, "
          f"{fragments} anchors, {node_entries} Node entry points checked")


if __name__ == "__main__":
    main()
