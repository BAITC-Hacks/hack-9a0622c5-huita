"""Generate a data-free OpenAPI contract; --check fails on schema drift."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from beesmart.config import Settings
from beesmart.web import create_app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    schema = create_app(Settings()).openapi()
    rendered = json.dumps(schema, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    path = ROOT / "open.json"
    if args.check:
        if not path.is_file() or path.read_text(encoding="utf-8") != rendered:
            raise SystemExit("open.json is stale; run python scripts/export_openapi.py")
        print("OpenAPI contract matches application")
    else:
        path.write_text(rendered, encoding="utf-8")
        print("Updated open.json (schema only; no dataset or credentials)")


if __name__ == "__main__":
    main()
