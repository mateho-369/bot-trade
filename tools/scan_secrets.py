#!/usr/bin/env python3
"""Commit-time secret scanner.  Never prints a secret - excerpts are masked.

  python tools/scan_secrets.py --staged     # what a pre-commit hook runs
  python tools/scan_secrets.py --all        # audit every tracked file
  python tools/scan_secrets.py FILE [FILE]  # scan specific files

Exit code 0 = clean, 1 = probable secret found (the commit is blocked).
Add `secret-scan: allow` in a comment on a line to whitelist it (test fixtures).
Bypass once, deliberately:  git commit --no-verify
"""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core.secret_guard import scan_text  # noqa: E402

SKIP_SUFFIX = (".png", ".jpg", ".jpeg", ".gif", ".ico", ".pkl", ".pyc", ".svg",
               ".zip", ".pdf", ".xlsx")
# the scanner's own pattern file and its test would flag themselves
SELF = {"core/secret_guard.py", "tools/scan_secrets.py", "verify_secret_guard.py"}


def _git(*args):
    return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                          text=True, encoding="utf-8", errors="replace")


def _staged_added_lines():
    """{path: text of ADDED lines only} from the staged diff."""
    out = _git("diff", "--cached", "-U0", "--no-color", "--diff-filter=ACMR").stdout
    files, cur, buf = {}, None, []
    for line in out.splitlines():
        if line.startswith("+++ b/"):
            if cur:
                files[cur] = "\n".join(buf)
            cur, buf = line[6:], []
        elif line.startswith("+") and not line.startswith("+++"):
            buf.append(line[1:])
    if cur:
        files[cur] = "\n".join(buf)
    return files


def main(argv):
    if "--staged" in argv:
        targets = _staged_added_lines()
    elif "--all" in argv:
        names = _git("ls-files").stdout.splitlines()
        targets = {}
        for n in names:
            if n.lower().endswith(SKIP_SUFFIX):
                continue
            try:
                with open(os.path.join(ROOT, n), encoding="utf-8",
                          errors="replace") as f:
                    targets[n] = f.read()
            except OSError:
                pass
    else:
        targets = {}
        for n in [a for a in argv if not a.startswith("--")]:
            with open(n, encoding="utf-8", errors="replace") as f:
                targets[n] = f.read()

    bad = 0
    for path, text in sorted(targets.items()):
        norm = path.replace("\\", "/")
        if norm in SELF or norm.lower().endswith(SKIP_SUFFIX):
            continue
        if os.path.basename(norm) == ".env":
            print(f"BLOCKED  {norm}: a real .env file must never be committed")
            bad += 1
            continue
        for no, kind, excerpt in scan_text(text, norm):
            print(f"BLOCKED  {norm}:{no}  [{kind}]  {excerpt}")
            bad += 1
    if bad:
        print(f"\n{bad} probable secret(s). Remove them, rotate anything that was real,"
              f"\nand read them from .env / the environment instead."
              f"\n(false positive? add 'secret-scan: allow' to that line)")
        return 1
    print("secret scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
