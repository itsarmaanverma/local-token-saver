"""Tests for the preview-only proxy wiring flow."""
from __future__ import annotations

import json
import subprocess

import pytest

from token_saver.cli import build_parser, main as cli_main
from token_saver.install import (
    PROXY_BASE_URL,
    PROXY_HOOK,
    PROXY_HOOK_COMMAND,
    PXPIPE_HOOK_COMMAND,
    preview_proxy_wiring,
)


def test_preview_missing_files_has_no_side_effects(tmp_path):
    settings = tmp_path / ".claude" / "settings.json"
    hook = tmp_path / ".claude" / "hooks" / "token-saver-proxy-check.sh"
    output = preview_proxy_wiring(settings, hook)
    assert "preview only; no files changed" in output
    assert f"+ {PROXY_BASE_URL}" in output
    assert f"+ {PROXY_HOOK_COMMAND}" in output
    assert not settings.exists()
    assert not hook.exists()


def test_preview_is_targeted_redacted_and_preserves_bytes(tmp_path):
    settings = tmp_path / "settings.json"
    hook = tmp_path / "hook.sh"
    secret = "SECRET_SENTINEL_MUST_NOT_PRINT"
    original = {
        "env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:47821", "API_KEY": secret},
        "hooks": {
            "SessionStart": [{"hooks": [
                {"type": "command", "command": PXPIPE_HOOK_COMMAND},
                {"type": "command", "command": "bash ~/.claude/hooks/unrelated.sh"},
            ]}],
        },
    }
    settings.write_text(json.dumps(original, indent=2), encoding="utf-8")
    hook.write_text("existing hook with " + secret, encoding="utf-8")
    settings_before = settings.read_bytes()
    hook_before = hook.read_bytes()

    output = preview_proxy_wiring(settings, hook)
    assert f"- {PXPIPE_HOOK_COMMAND}" in output
    assert f"+ {PROXY_HOOK_COMMAND}" in output
    assert "unrelated.sh" not in output
    assert secret not in output
    assert settings.read_bytes() == settings_before
    assert hook.read_bytes() == hook_before


def test_preview_handles_invalid_settings_without_writing(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text("{broken", encoding="utf-8")
    before = settings.read_bytes()
    output = preview_proxy_wiring(settings, tmp_path / "hook.sh")
    assert "could not be merged safely" in output
    assert settings.read_bytes() == before
    assert not (tmp_path / "hook.sh").exists()


def test_preview_is_idempotent_when_chain_is_already_configured(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": PROXY_BASE_URL},
        "hooks": {"SessionStart": [{"hooks": [
            {"type": "command", "command": PROXY_HOOK_COMMAND},
        ]}]},
    }), encoding="utf-8")
    output = preview_proxy_wiring(settings, tmp_path / "hook.sh")
    assert "already configured" in output


def test_preview_redacts_credentials_embedded_in_current_base_url(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "https://user:TOP_SECRET@example.test/v1?key=NOPE"},
    }), encoding="utf-8")
    output = preview_proxy_wiring(settings, tmp_path / "hook.sh")
    assert "TOP_SECRET" not in output
    assert "NOPE" not in output
    assert "https://example.test" in output
    assert "/v1" not in output


def test_preview_redacts_non_url_current_base_value(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({
        "env": {"ANTHROPIC_BASE_URL": "TOP_SECRET_NOT_A_URL"},
    }), encoding="utf-8")
    output = preview_proxy_wiring(settings, tmp_path / "hook.sh")
    assert "TOP_SECRET_NOT_A_URL" not in output
    assert "(set; redacted)" in output


def test_generated_hook_is_valid_and_starts_pxpipe_first():
    subprocess.run(["bash", "-n"], input=PROXY_HOOK.encode("utf-8"), check=True)
    assert PROXY_HOOK.index("if ! up \"$PXPIPE_URL\"") < PROXY_HOOK.index(
        "if ! up \"$TOKEN_SAVER_URL\""
    )
    assert PROXY_HOOK.count("< /dev/null") >= 4
    assert "/health" in PROXY_HOOK
    assert 'command -v pxpipe' in PROXY_HOOK
    assert 'env PORT="$PXPIPE_PORT" pxpipe' in PROXY_HOOK
    assert "PXPIPE_CONSOLE_LOG" in PROXY_HOOK
    assert 'PXPIPE_LOG=' not in PROXY_HOOK


def test_cli_proxy_preview_does_not_initialize_workspace(monkeypatch, tmp_path, capsys):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("HOME", str(home))
    assert cli_main(["mcp", "install", str(workspace), "--with-proxy"]) == 0
    output = capsys.readouterr().out
    assert "Proxy chain preview only" in output
    assert not (workspace / ".tokensaver").exists()
    assert not (home / ".claude").exists()


def test_apply_flag_is_intentionally_absent():
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mcp", "install", ".", "--with-proxy", "--apply"])
