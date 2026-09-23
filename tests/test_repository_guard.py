"""Exercise real Git objects using disposable repositories and fabricated keys."""

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "scripts" / "check_repository.py"
INSTALLER = ROOT / "scripts" / "install_hooks.py"
ZERO = "0" * 40


def synthetic_key(prefix):
    # The test source itself contains no complete provider-shaped credentials.
    return "".join(prefix) + "C7mR9qL2aP8vN4dS6fH3wK5zT1bE0uJ9"


class RepositoryGuardTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.environment = {key: value for key, value in os.environ.items()
                            if not key.startswith("GIT_")}
        self.environment.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_SYSTEM=os.devnull,
                                GIT_AUTHOR_NAME="Guard test", GIT_COMMITTER_NAME="Guard test",
                                GIT_AUTHOR_EMAIL="guard@example.invalid", GIT_COMMITTER_EMAIL="guard@example.invalid")
        self.git("init", "-q")

    def tearDown(self):
        self.temporary.cleanup()

    def git(self, *args, check=True):
        result = subprocess.run(["git", *args], cwd=self.root, env=self.environment,
                                capture_output=True, text=True, check=False)
        if check:
            self.assertEqual(result.returncode, 0, "A temporary Git fixture operation failed")
        return result

    def write(self, path, text):
        filename = self.root / path
        filename.parent.mkdir(parents=True, exist_ok=True)
        filename.write_text(text, encoding="utf-8")

    def commit(self, message="fixture"):
        self.git("add", "--all")
        self.git("commit", "-qm", message)
        return self.git("rev-parse", "HEAD").stdout.strip()

    def scan(self, *args, input=None):
        return subprocess.run([sys.executable, str(GUARD), *args], cwd=self.root,
                              env=self.environment, input=input, capture_output=True, text=True)

    def test_data_and_private_paths_blocked_even_when_forced_into_index(self):
        cases = ["customers.csv", "DATA/table.TSV", "result.parquet", "report.xlsx", "local.sqlite",
                 ".env", ".env.production", "bundle.zip", "report.pdf", "private.key",
                 "docs/reference/notes.md", "work/result.json", "uploads/raw.txt"]
        for path in cases:
            self.write(path, "fabricated fixture\n")
        self.git("add", "-f", "--all")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        for path in cases:
            self.assertIn(path, result.stderr)
        self.assertNotIn("fabricated fixture", result.stderr)

    def test_complete_index_is_scanned_not_only_the_diff(self):
        self.write("already.csv", "id\n1\n")
        self.commit()
        self.assertEqual(self.scan("--staged").returncode, 1)

    def test_real_shaped_provider_keys_and_pem_are_blocked_without_disclosure(self):
        values = [synthetic_key(("sk", "-", "proj", "-")),
                  synthetic_key(("sk", "-", "proj", "_")),
                  synthetic_key(("sk", "-")), synthetic_key(("gh", "p", "_")),
                  synthetic_key(("gh", "o", "_")), synthetic_key(("github", "_pat", "_")),
                  synthetic_key(("nv", "api", "-")),
                  "".join(("-----BEGIN ", "RSA ", "PRIVATE KEY", "-----"))]
        for number, value in enumerate(values):
            self.write(f"fixture{number}.txt", value)
        self.git("add", "--all")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        for number, value in enumerate(values):
            self.assertIn(f"fixture{number}.txt", result.stderr)
            self.assertNotIn(value, result.stdout + result.stderr)

    def test_generic_secret_assignment_is_blocked(self):
        self.write("config.py", 'SERVICE_TOKEN = "' + synthetic_key(()) + '"\n')
        self.git("add", "config.py")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("non-placeholder credential assignment", result.stderr)

    def test_python_dynamic_secret_expressions_are_not_literal_credentials(self):
        self.write("config.py", "api_token=secrets.token_urlsafe(48)\n"
                   "service_token=settings.authentication_token\n"
                   "password = os.environ.get('SERVICE_PASSWORD', '')\n")
        self.git("add", "config.py")
        self.assertEqual(self.scan("--staged").returncode, 0)

    def test_bare_config_secret_and_quoted_python_secret_still_block(self):
        self.write("settings.ini", "SERVICE_TOKEN=" + synthetic_key(()) + "\n")
        self.write("config.py", 'api_token="' + synthetic_key(()) + '"\n')
        self.git("add", "--all")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertIn("settings.ini", result.stderr)
        self.assertIn("config.py", result.stderr)

    def test_placeholder_environment_and_public_json_are_allowed(self):
        self.write(".env.example", 'OPENAI_API_KEY=\nNVIDIA_API_KEY=your_api_key_here\nAPP_PASSWORD="<password>"\nPORT=8000\n')
        self.write("frozen_policy.json", '{"source":"deterministic_baseline"}')
        self.write("openapi.json", '{"openapi":"3.1.0"}')
        self.git("add", "--all")
        self.assertEqual(self.scan("--staged").returncode, 0)

    def test_environment_example_rejects_non_placeholder_even_if_short(self):
        self.write(".env.example", "APP_PASSWORD=secret7\n")
        self.git("add", "--all")
        result = self.scan("--staged")
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("secret7", result.stderr)

    def test_staged_secret_cannot_be_hidden_by_clean_working_copy(self):
        self.write("config.py", synthetic_key(("sk", "-")))
        self.git("add", "config.py")
        self.write("config.py", "safe = True\n")
        self.assertEqual(self.scan("--staged").returncode, 1)

    def test_all_checks_unstaged_changes_but_not_private_untracked_data(self):
        self.write("config.py", "safe = True\n")
        self.commit()
        self.write("local.csv", "private untracked data\n")
        self.assertEqual(self.scan("--all").returncode, 0)
        self.write("config.py", synthetic_key(("nv", "api", "-")))
        self.assertEqual(self.scan("--staged").returncode, 0)
        self.assertEqual(self.scan("--all").returncode, 1)

    def test_history_detects_data_and_credentials_deleted_before_push(self):
        self.write("README.md", "safe\n")
        base = self.commit()
        self.write("customers.csv", "id\n1\n")
        self.write("key.txt", synthetic_key(("gh", "p", "_")))
        self.commit()
        self.git("rm", "customers.csv", "key.txt")
        head = self.commit()
        self.assertEqual(self.scan("--all").returncode, 0)
        self.assertEqual(self.scan("--history").returncode, 1)
        self.assertEqual(self.scan("--history", f"{base}..{head}").returncode, 1)
        for previous in (ZERO, base):
            result = self.scan("--pre-push", input=f"refs/heads/main {head} refs/heads/main {previous}\n")
            self.assertEqual(result.returncode, 1)
            self.assertIn("customers.csv", result.stderr)

    def test_history_preserves_forbidden_old_name_after_blob_rename(self):
        self.write("original.csv", "same blob content\n")
        self.commit()
        self.git("mv", "original.csv", "public.txt")
        self.commit()
        self.assertEqual(self.scan("--all").returncode, 0)
        self.assertIn("original.csv", self.scan("--history").stderr)

    def test_pre_push_also_checks_working_tree_and_unknown_remote_history(self):
        self.write("config.py", "safe = True\n")
        head = self.commit()
        self.write("config.py", synthetic_key(("gh", "o", "_")))
        result = self.scan("--pre-push", input=f"refs/heads/main {head} refs/heads/main {'1' * 40}\n")
        self.assertEqual(result.returncode, 1)

    def test_clean_push_and_branch_deletion_are_allowed(self):
        self.write("README.md", "safe\n")
        head = self.commit()
        self.assertEqual(self.scan("--pre-push", input=f"refs/heads/main {head} refs/heads/main {ZERO}\n").returncode, 0)
        self.assertEqual(self.scan("--pre-push", input=f"(delete) {ZERO} refs/heads/old {head}\n").returncode, 0)

    def test_invalid_push_metadata_is_fail_closed(self):
        self.assertEqual(self.scan("--pre-push", input="not Git revision metadata\n").returncode, 2)

    def test_symlinks_are_rejected_without_reading_the_target(self):
        (self.root / "link.txt").symlink_to("../outside.txt")
        self.git("add", "link.txt")
        result = self.scan("--all")
        self.assertEqual(result.returncode, 1)
        self.assertIn("symlink", result.stderr)

    def test_hooks_install_and_pre_commit_blocks_bad_index(self):
        for relative in ("scripts/check_repository.py", "scripts/install_hooks.py",
                         ".githooks/pre-commit", ".githooks/pre-push"):
            target = self.root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / relative, target)
        self.commit()
        installed = subprocess.run([sys.executable, str(INSTALLER)], cwd=self.root,
                                   env=self.environment, capture_output=True, text=True)
        self.assertEqual(installed.returncode, 0)
        self.assertEqual(self.git("config", "--local", "core.hooksPath").stdout.strip(), ".githooks")
        self.write("forbidden.csv", "id\n1\n")
        self.git("add", "forbidden.csv")
        result = self.git("commit", "-qm", "should fail", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("forbidden.csv", result.stderr)

    def test_guard_source_does_not_trigger_its_own_patterns(self):
        spec = importlib.util.spec_from_file_location("repository_guard", GUARD)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        for path in (GUARD, Path(__file__), ROOT / "docs" / "secrets.md"):
            self.assertEqual(module.content_problems(path.read_bytes(), str(path)), set(), path.name)


if __name__ == "__main__":
    unittest.main()
