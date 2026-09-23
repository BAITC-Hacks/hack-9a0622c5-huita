#!/usr/bin/env python3
"""Install this repository's tracked hooks; requires only Python and Git."""

from pathlib import Path
import subprocess


def main() -> None:
    root = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    for name in ("pre-commit", "pre-push"):
        hook = root / ".githooks" / name
        if not hook.is_file():
            raise SystemExit("Tracked hooks are missing; installation stopped.")
        hook.chmod(hook.stat().st_mode | 0o111)
    subprocess.run(["git", "config", "--local", "core.hooksPath", ".githooks"], cwd=root, check=True)
    print("Installed pre-commit and pre-push repository guards.")


if __name__ == "__main__":
    main()
