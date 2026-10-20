"""Check local links in maintained distribution docs, excluding pinned upstream docs."""
import re
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]


def main():
    files = [ROOT / name for name in ("README.md", "THIRD_PARTY.md")]
    files.extend(sorted((ROOT / "docs").rglob("*.md")))
    failures = []
    count = 0
    for path in files:
        fenced = False
        for line_number, line in enumerate(path.read_text().splitlines(), 1):
            if line.lstrip().startswith("```"):
                fenced = not fenced
                continue
            if fenced:
                continue
            for target in re.findall(r"\]\(([^)]+)\)", line):
                target = target.strip("<>")
                parsed = urlsplit(target)
                if parsed.scheme or not parsed.path:
                    continue
                count += 1
                if not (path.parent / unquote(parsed.path)).exists():
                    failures.append(f"{path.relative_to(ROOT)}:{line_number}: {target}")
    if failures:
        raise SystemExit("\n".join(failures))
    print(f"Maintained documentation: {len(files)} files, {count} local links checked")


if __name__ == "__main__":
    main()
