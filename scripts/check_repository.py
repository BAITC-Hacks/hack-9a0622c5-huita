#!/usr/bin/env python3
"""Reject private datasets and credentials before they enter or leave Git.

Uses Python's standard library and Git only. Diagnostics contain paths and
finding categories, never matching values or source lines. A credential that
has already been exposed must still be revoked separately.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath


MAX_BLOB_BYTES = 10 * 1024 * 1024
DATA_SUFFIXES = {
    ".csv", ".tsv", ".parquet", ".pq", ".feather", ".arrow", ".xls", ".xlsx",
    ".xlsm", ".xlsb", ".ods", ".sqlite", ".sqlite3", ".db", ".db3", ".duckdb",
    ".h5", ".hdf5", ".jsonl", ".ndjson",
}
ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".tar", ".gz", ".tgz", ".bz2", ".xz"}
EXPORT_SUFFIXES = {".pdf", ".doc", ".docx", ".ppt", ".pptx"}
KEY_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks", ".keystore"}
PRIVATE_DIRECTORIES = {
    "data", "datasets", "uploads", "work", "outputs", "reports", "exports",
    "private", "secrets", "backups", "logs", "runtime", ".venv", "venv",
    "__pycache__", ".pytest_cache", "node_modules",
}
SENSITIVE_NAME = re.compile(r"(?:api[_-]?key|secret|token|password|passwd|credential)", re.I)
TOKEN_PATTERNS = (
    ("OpenAI credential", re.compile(rb"(?<![A-Za-z0-9])sk-(?:(?:proj|svcacct)[-_])?[A-Za-z0-9_-]{20,}")),
    ("GitHub credential", re.compile(rb"(?<![A-Za-z0-9])(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})")),
    ("NVIDIA credential", re.compile(rb"(?<![A-Za-z0-9])nvapi-[A-Za-z0-9_-]{16,}")),
    ("private key material", re.compile(rb"-----BEGIN (?:[A-Z0-9]+ )?PRIVATE KEY-----")),
)
ASSIGNMENT = re.compile(
    r"(?m)(?P<keyquote>[\"']?)(?P<name>[A-Za-z_][A-Za-z0-9_-]*)(?P=keyquote)"
    r"[ \t]*[:=][ \t]*(?:[\"'](?P<quoted>[^\"'\r\n]*)[\"']|(?P<bare>[A-Za-z0-9_+/.=-]+))"
)
PLACEHOLDERS = {
    "", "none", "null", "false", "true", "example", "placeholder", "redacted",
    "changeme", "change-me", "change_me", "replace-me", "replace_me", "replace-with-your-key",
    "your-api-key", "your_api_key", "your-api-key-here", "your_api_key_here",
    "your-secret", "your_secret", "your-secret-key", "your_secret_key",
    "your-token", "your_token", "your-token-here", "your_token_here",
    "your-password", "your_password", "not-a-real-key", "not_a_real_key",
}


class GitFailure(RuntimeError):
    pass


def git(*arguments: str, cwd: Path | None = None, allow_failure: bool = False) -> bytes:
    result = subprocess.run(
        ["git", *arguments], cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode and not allow_failure:
        raise GitFailure("Git could not inspect the requested repository objects")
    return result.stdout if result.returncode == 0 else b""


def path_problem(path: str) -> str | None:
    normalized = PurePosixPath(path.lower())
    name = normalized.name
    if any(part in PRIVATE_DIRECTORIES for part in normalized.parts[:-1]):
        return "private data or runtime directory"
    if normalized.parts[:2] == ("docs", "reference"):
        return "private reference documentation"
    if name != ".env.example" and (name == ".env" or name.startswith(".env.")):
        return "private environment file"
    if (normalized.suffix in KEY_SUFFIXES or name.startswith(("id_rsa", "id_ed25519"))
            or name in {"credentials.json", "secrets.json"}):
        return "credential file"
    if normalized.suffix in DATA_SUFFIXES:
        return "dataset or database file"
    if normalized.suffix in ARCHIVE_SUFFIXES:
        return "archive that may contain private data"
    if normalized.suffix in EXPORT_SUFFIXES:
        return "document export that may contain private data"
    return None


def placeholder(value: str) -> bool:
    value = value.strip().lower()
    if value in PLACEHOLDERS or (value.startswith("${") and value.endswith("}")):
        return True
    if value.startswith("<") and value.endswith(">"):
        return True
    if len(value) >= 8 and set(value) <= {"x", "*", "0", "_", "-"}:
        return True
    for prefix in ("sk-proj-", "sk-proj_", "sk-svcacct-", "sk-", "ghp_", "gho_", "github_pat_", "nvapi-"):
        if value.startswith(prefix):
            return placeholder(value[len(prefix):])
    return False


def content_problems(content: bytes, path: str) -> set[str]:
    findings = set()
    for category, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(content):
            if category == "private key material" or not placeholder(match.group().decode("ascii")):
                findings.add(category)
    text = content.decode("utf-8", errors="replace")
    example_file = PurePosixPath(path.lower()).name == ".env.example"
    python_file = PurePosixPath(path.lower()).suffix == ".py"
    for match in ASSIGNMENT.finditer(text):
        if not SENSITIVE_NAME.search(match["name"]):
            continue
        # Bare Python values are identifiers/expressions, not embedded string
        # credentials (for example a secure token generator or env lookup).
        # Provider-shaped strings still have independent raw-byte checks above.
        if python_file and match["quoted"] is None:
            continue
        value = match["quoted"] if match["quoted"] is not None else match["bare"]
        if placeholder(value):
            continue
        if example_file or (len(value) >= 16 and re.fullmatch(r"[A-Za-z0-9_+/.=-]+", value)):
            findings.add("non-placeholder credential assignment")
    return findings


class RepositoryGuard:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.findings: set[tuple[str, str]] = set()
        self.blob_cache: dict[str, set[str]] = {}
        self.seen_objects: set[tuple[str, str, str]] = set()

    def add(self, path: str, category: str) -> None:
        self.findings.add((path, category))

    def blob(self, path: str, mode: str, oid: str) -> None:
        key = (path, mode, oid)
        if key in self.seen_objects:
            return
        self.seen_objects.add(key)
        problem = path_problem(path)
        if problem:
            self.add(path, problem)
            return
        if mode not in ("100644", "100755"):
            self.add(path, "symlink or submodule cannot be safely scanned")
            return
        cache_key = oid + (":env-example" if PurePosixPath(path.lower()).name == ".env.example" else "")
        if cache_key not in self.blob_cache:
            size = int(git("cat-file", "-s", oid, cwd=self.root))
            if size > MAX_BLOB_BYTES:
                self.blob_cache[cache_key] = {"file exceeds repository size limit"}
            else:
                self.blob_cache[cache_key] = content_problems(git("cat-file", "blob", oid, cwd=self.root), path)
        for category in self.blob_cache[cache_key]:
            self.add(path, category)

    def index(self, *, working_tree: bool = False) -> None:
        entries = git("ls-files", "--stage", "-z", cwd=self.root).split(b"\0")
        for entry in entries:
            if not entry:
                continue
            metadata, raw_path = entry.split(b"\t", 1)
            mode, oid, stage = metadata.decode("ascii").split()
            path = os.fsdecode(raw_path)
            if stage != "0":
                self.add(path, "unresolved index conflict")
                continue
            self.blob(path, mode, oid)
            if working_tree and not path_problem(path) and mode in ("100644", "100755"):
                self.working_file(path)

    def working_file(self, path: str) -> None:
        filename = self.root / path
        if any(part.is_symlink() for part in (filename, *filename.parents) if part != self.root):
            self.add(path, "tracked path contains a symlink")
            return
        if not filename.exists():
            return
        if not filename.resolve().is_relative_to(self.root) or not filename.is_file():
            self.add(path, "tracked path is outside the repository or not a file")
            return
        try:
            with filename.open("rb") as stream:
                content = stream.read(MAX_BLOB_BYTES + 1)
        except OSError:
            self.add(path, "tracked file could not be read")
            return
        if len(content) > MAX_BLOB_BYTES:
            self.add(path, "file exceeds repository size limit")
            return
        for category in content_problems(content, path):
            self.add(path, category)

    def history(self, revision: str | None = None) -> None:
        arguments = ("rev-list", "--all") if revision is None else ("rev-list", "--end-of-options", revision)
        commits = git(*arguments, cwd=self.root).splitlines()
        for commit in commits:
            for entry in git("ls-tree", "-r", "-z", "--full-tree", commit.decode("ascii"), cwd=self.root).split(b"\0"):
                if not entry:
                    continue
                metadata, raw_path = entry.split(b"\t", 1)
                mode, _kind, oid = metadata.decode("ascii").split()
                self.blob(os.fsdecode(raw_path), mode, oid)

    def pre_push(self, lines: list[str]) -> None:
        self.index(working_tree=True)
        for line in lines:
            parts = line.split()
            if len(parts) != 4 or not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", parts[i]) for i in (1, 3)):
                raise GitFailure("Invalid pre-push revision metadata")
            _, local_oid, _, remote_oid = parts
            if set(local_oid) == {"0"}:
                continue
            commit = git("rev-parse", "--verify", local_oid + "^{commit}", cwd=self.root, allow_failure=True).strip()
            if not commit:
                raise GitFailure("Only commits and tags pointing to commits can be pushed")
            local_commit = commit.decode("ascii")
            remote_commit = b""
            if set(remote_oid) != {"0"}:
                remote_commit = git("rev-parse", "--verify", remote_oid + "^{commit}", cwd=self.root, allow_failure=True).strip()
            revision = f"{remote_commit.decode('ascii')}..{local_commit}" if remote_commit else local_commit
            self.history(revision)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--staged", action="store_true", help="scan the complete resulting Git index")
    mode.add_argument("--all", action="store_true", help="scan the index and current tracked working files")
    mode.add_argument("--history", nargs="?", const="", metavar="RANGE", help="scan commit trees in RANGE, or every ref")
    mode.add_argument("--pre-push", action="store_true", help="scan tracked files and revisions supplied by Git on stdin")
    args = parser.parse_args(argv)
    try:
        root = Path(os.fsdecode(git("rev-parse", "--show-toplevel").strip()))
        guard = RepositoryGuard(root)
        if args.staged:
            guard.index()
        elif args.all:
            guard.index(working_tree=True)
        elif args.history is not None:
            guard.history(args.history or None)
        else:
            guard.pre_push([line for line in sys.stdin.read().splitlines() if line.strip()])
        if guard.findings:
            print("Repository guard blocked private material:", file=sys.stderr)
            for path, category in sorted(guard.findings):
                print(f"  {json.dumps(path, ensure_ascii=True)}: {category}", file=sys.stderr)
            return 1
        print("Repository guard: no blocked paths or credential signatures found.")
        return 0
    except (GitFailure, OSError, ValueError):
        print("Repository guard could not complete Git inspection; operation blocked.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
