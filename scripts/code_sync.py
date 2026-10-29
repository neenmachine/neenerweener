"""Snapshot the committed files of a git repo into code/<name>/ for the site's code browser.

Usage: python scripts/code_sync.py <repo> [--name <name>]
"""

import argparse
import io
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

CODE_DIR = Path(os.environ.get("CODE_DIR", Path(__file__).resolve().parent.parent / "code"))
MAX_BYTES = 1024 * 1024
NAME = re.compile(r"[a-z0-9-]+")
PERSONAL = re.compile(
    r"github\.com/hwang2409|hwang2409|henryuni|henry wang|git@|[\w.+-]+@[\w-]+\.[a-z]{2,}\b", re.I
)
SECRET = re.compile(
    r"\bsk-[\w-]{16,}|BEGIN [A-Z ]*PRIVATE KEY|(api_key|secret|token|password)\s*[:=]\s*[\"'][^\"']{12,}", re.I
)


def git(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True).stdout


def scan(root: Path) -> list[str]:
    """Print personal-info matches as warnings; return secret matches."""
    secrets = []
    for path in sorted(p for p in root.rglob("*") if p.is_file() and not p.is_symlink()):
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(lines, 1):
            where = f"{path.relative_to(root)}:{number}: {line.strip()[:160]}"
            if SECRET.search(line):
                secrets.append(where)
            elif PERSONAL.search(line):
                print(f"warning: personal info: {where}")
    return secrets


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("repo")
    parser.add_argument("--name")
    args = parser.parse_args()
    repo = Path(args.repo).expanduser().resolve()
    name = args.name or repo.name
    if not NAME.fullmatch(name):
        print(f"error: name {name!r} must match [a-z0-9-]+", file=sys.stderr)
        return 2

    archive = git(repo, "archive", "--format=tar", "HEAD")
    if dirty := git(repo, "status", "--porcelain").decode():
        print("warning: uncommitted changes are not included:\n" + dirty.rstrip())

    CODE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=CODE_DIR, prefix=".sync-"))
    dest = CODE_DIR / name
    try:
        with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
            for member in tar:
                if member.isfile() and member.size > MAX_BYTES:
                    print(f"skipped: {member.name} ({member.size / MAX_BYTES:.1f} MB)")
                    continue
                tar.extract(member, tmp, filter="data")
        if secrets := scan(tmp):
            print("error: secret-looking lines, snapshot not written:", *secrets, sep="\n", file=sys.stderr)
            return 1
        # Replace the whole directory so files deleted upstream disappear.
        if dest.exists():
            shutil.rmtree(dest)
        tmp.rename(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    count = sum(1 for p in dest.rglob("*") if p.is_file())
    print(f"wrote {count} files to {dest}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
