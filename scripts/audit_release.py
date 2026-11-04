"""Check a source tree for common credentials and personal runtime artifacts.

Prints file names and line numbers, never matched credential values.
"""
from pathlib import Path
import os
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
SKIP_DIRS = {".git", ".venv", "venv", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
             "node_modules", "outputs", "test-results"}
PRIVATE_NAMES = {"key.dpapi", "settings.json", "runtime.json", "panel.json", "job.json", "management.json",
                 "storage-policy.json", "archive-policy.json", "local-sync.json", "routing.json", "app-settings.json",
                 ".nah-skill-install.json", ".env", "id_rsa", "id_ed25519"}
PRIVATE_SUFFIXES = {".log", ".pem", ".key", ".p12", ".pfx", ".sqlite", ".sqlite3", ".bak", ".dpapi", ".jsonl"}
PATTERNS = {
    "private-key block": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "API token": re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AIza[A-Za-z0-9_-]{30,}|gh[pousr]_[A-Za-z0-9]{30,})\b"),
    "personal Windows path": re.compile(r"(?i)\b[A-Z]:[\\/]+Users[\\/]+[^\\/\s\"'<>]+"),
    "literal API credential": re.compile(r"(?im)^\s*(?:api[_-]?key|AGENT_HUB_(?:GEMINI|CLAUDE|DEEPSEEK)_API_KEY|ANTHROPIC_API_KEY|DEEPSEEK_API_KEY|OPENAI_API_KEY|GEMINI_API_KEY)\s*[:=]\s*[\"'][A-Za-z0-9_./+\-=]{20,}[\"']"),
}


def source_paths(root: Path):
    # Prune local artifacts before descending into installed SDKs and browser test output.
    for directory, subdirs, files in os.walk(root):
        subdirs[:] = sorted(name for name in subdirs if name not in SKIP_DIRS)
        for name in subdirs + sorted(files):
            yield Path(directory) / name


def audit(root: Path = ROOT) -> tuple[int, list[str]]:
    issues = []
    scanned = 0
    for path in source_paths(root):
        relative = path.relative_to(root)
        if any(part in SKIP_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            issues.append(f"{relative.as_posix()}: symbolic link needs manual review")
            continue
        if not path.is_file():
            continue
        if (path.name in PRIVATE_NAMES or path.suffix.lower() in PRIVATE_SUFFIXES
                or re.fullmatch(r"result-[0-9a-f]{64}\.txt", path.name)
                or path.name.startswith(".env.") and path.name != ".env.example"
                or ".state" in relative.parts):
            issues.append(f"{relative.as_posix()}: local/private artifact")
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8-sig")
        except (UnicodeError, OSError):
            issues.append(f"{relative.as_posix()}: non-text artifact needs manual review")
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for label, pattern in PATTERNS.items():
                if pattern.search(line):
                    issues.append(f"{relative.as_posix()}:{number}: {label}")
    return scanned, issues


if __name__ == "__main__":
    count, problems = audit()
    if problems:
        print("Release audit failed:")
        print("\n".join(problems))
        sys.exit(1)
    print(f"Release audit passed: {count} source files checked; no matching private artifacts or credentials.")
