import secrets
"""Configuration precedence, private initialization, and server entrypoint."""

import os
import runpy
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from beesmart.config import Settings
from scripts.setup_env import create_env


def test_private_environment_is_created_once_without_replacing_values(tmp_path):
    assert create_env(tmp_path)
    target = tmp_path / ".env"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    before = target.read_bytes()
    assert not create_env(tmp_path)
    assert target.read_bytes() == before


def test_existing_environment_symlink_is_preserved(tmp_path):
    actual = tmp_path / "private-config"
    actual.write_text("existing private configuration")
    (tmp_path / ".env").symlink_to(actual)
    assert not create_env(tmp_path)
    assert actual.read_text() == "existing private configuration"


def test_process_variables_override_dotenv_and_token_is_not_in_repr(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("BEESMART_"):
            monkeypatch.delenv(key)
    assert create_env(tmp_path)
    monkeypatch.setenv("BEESMART_PORT", "8123")
    # dotenv's mutation is isolated from the rest of the test process.
    with patch.dict(os.environ, os.environ.copy(), clear=True):
        settings = Settings.from_env(tmp_path)
        assert settings.port == 8123
        assert len(settings.api_token) >= 32
        assert settings.api_token not in repr(settings)
        assert settings.storage_path == tmp_path.resolve() / "work"
        assert settings.data_path == tmp_path.resolve()


def test_relative_storage_and_data_paths_are_rooted_at_project(tmp_path):
    settings = Settings(root=tmp_path, storage_dir=Path("state"), data_dir=Path("inputs"))
    assert settings.storage_path == tmp_path.resolve() / "state"
    assert settings.data_path == tmp_path.resolve() / "inputs"


@pytest.mark.parametrize("invalid", [
    {"api_token": ""},
    {"api_token": "a" * 31},
    {"api_token": "a" * 32 + "\0"},
    {"allowed_hosts": ("*",)},
    {"allowed_hosts": ()},
    {"allowed_origins": ("http://app.example.com",)},
    {"allowed_origins": ("https://*.example.com",)},
    {"allowed_origins": ("https://app.example.com/path",)},
    {"forwarded_allow_ips": "*"},
    {"forwarded_allow_ips": "172.31.250.0/28"},
])
def test_production_invalid_configuration_fails_without_leaking_token(tmp_path, invalid):
    token = secrets.token_urlsafe(48)
    kwargs = {"root": tmp_path, "environment": "production", "api_token": token,
              "allowed_hosts": ("api.example.com",)}
    kwargs.update(invalid)
    with pytest.raises(ValueError) as error:
        Settings(**kwargs)
    assert token not in str(error.value)


def test_production_from_env_does_not_invent_allowed_hosts(tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("BEESMART_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("BEESMART_ENVIRONMENT", "production")
    monkeypatch.setenv("BEESMART_API_TOKEN", "unit-test-token-for-settings-check-only")
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        Settings.from_env(tmp_path)


def test_entrypoint_uses_one_worker_and_only_configured_proxy_ip(tmp_path):
    settings = Settings(root=tmp_path, host="0.0.0.0", port=8123,
                        forwarded_allow_ips="172.31.250.2")
    with patch("beesmart.config.Settings.from_env", return_value=settings), patch("uvicorn.run") as serve:
        runpy.run_module("beesmart", run_name="__main__")
    assert serve.call_args.kwargs["host"] == "0.0.0.0"
    assert serve.call_args.kwargs["port"] == 8123
    assert serve.call_args.kwargs["workers"] == 1
    assert serve.call_args.kwargs["proxy_headers"] is True
    assert serve.call_args.kwargs["forwarded_allow_ips"] == "172.31.250.2"
