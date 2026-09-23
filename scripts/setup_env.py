"""Create a private local configuration once, without displaying any secret."""

import argparse
import os
import secrets
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def create_env(root: Path = ROOT) -> bool:
    root = root.resolve()
    target = root / ".env"
    if target.exists() or target.is_symlink():
        return False
    template_path = root / ".env.example"
    if not template_path.is_file():
        template_path = ROOT / ".env.example"
    template = template_path.read_text(encoding="utf-8")
    placeholder = "BEESMART_API_TOKEN="
    lines = template.splitlines()
    if lines.count(placeholder) != 1:
        raise ValueError("The example must contain one empty BEESMART_API_TOKEN entry")
    lines[lines.index(placeholder)] = placeholder + secrets.token_urlsafe(48)
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("\n".join(lines) + "\n")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return True


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT, help="Project directory receiving the private .env")
    args = parser.parse_args()
    created = create_env(args.root)
    print("Created private .env (mode 0600); token was generated and not displayed." if created
          else "Existing .env preserved; no values were changed or displayed.")
