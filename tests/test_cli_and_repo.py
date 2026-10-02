from __future__ import annotations

import json
import re

import pytest

from factory.cli import main
from tests.conftest import REPO_ROOT


def test_cli_validates_configs_and_schema():
    assert main(["validate-config"]) == 0
    assert main(["schema"]) == 0


def test_cli_rejects_unknown_channel():
    assert main(["validate-config", "--channel", "nope"]) == 1


@pytest.mark.parametrize("mode, expected", [("TEST", "COMPLETED"), ("SHUTDOWN", "BLOCKED"), ("", "BLOCKED")])
def test_cli_run_respects_mode(tmp_path, monkeypatch, capsys, mode, expected):
    monkeypatch.setenv("FACTORY_MODE", mode)
    monkeypatch.delenv("GITHUB_RUN_ID", raising=False)
    rc = main(["run", "--channel", "drama", "--run-id", "123",
               "--state-dir", str(tmp_path / "s"), "--workspace-dir", str(tmp_path / "w")])
    out = capsys.readouterr().out
    assert rc == 0 and f"outcome={expected}" in out
    if expected == "COMPLETED":
        assert "JOB-" in out and "-drama-123" in out


def test_cli_refuses_non_fake_pipeline_until_real_stages_exist(tmp_path):
    assert main(["run", "--channel", "drama", "--pipeline", "real"]) == 2


def test_cli_rejects_path_like_channel_ids(tmp_path):
    assert main(["run", "--channel", "../etc", "--state-dir", str(tmp_path)]) == 1


# ---- repository hygiene guards ------------------------------------------------------------
_MEDIA = {".mp4", ".mov", ".wav", ".mp3", ".png", ".jpg", ".jpeg", ".webp"}
_SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", "workspace", ".factory-state"}


def _repo_files():
    for path in REPO_ROOT.rglob("*"):
        if path.is_file() and not (set(path.relative_to(REPO_ROOT).parts) & _SKIP_DIRS) \
                and not any(p.endswith(".egg-info") for p in path.parts):
            yield path


def test_no_generated_media_in_repo():
    assert [p for p in _repo_files() if p.suffix.lower() in _MEDIA] == []


def test_gitignore_blocks_media_and_secrets():
    text = (REPO_ROOT / ".gitignore").read_text()
    for needle in ("workspace/", "*.mp4", "*.wav", "*.png", ".env", ".factory-state/"):
        assert needle in text


_SECRET_SHAPES = [
    re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),        # Google API key
    re.compile(r"1//[0-9A-Za-z_\-]{30,}"),          # Google OAuth refresh token
    re.compile(r"https://discord(app)?\.com/api/webhooks/\d+/[\w\-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def test_no_secret_shaped_strings_committed():
    hits = []
    for path in _repo_files():
        if path.suffix in {".py", ".yaml", ".yml", ".json", ".md", ".toml", ".txt"} and path.name != "test_cli_and_repo.py":
            text = path.read_text(errors="ignore")
            hits += [f"{path.relative_to(REPO_ROOT)}: {rx.pattern}" for rx in _SECRET_SHAPES if rx.search(text)]
    assert hits == []


def test_manual_workflow_defaults_to_test_mode_and_never_to_auto():
    text = (REPO_ROOT / ".github" / "workflows" / "factory.yml").read_text()
    match = re.search(r"mode:\s*\n(?:.*\n)*?\s+default:\s*(\w+)", text)
    assert match and match.group(1) == "TEST"
    assert "schedule:" not in re.sub(r"(?m)^\s*#.*$", "", text)  # no active cron until real stages exist


def test_workflow_passes_inputs_via_env_not_shell_interpolation():
    text = (REPO_ROOT / ".github" / "workflows" / "factory.yml").read_text()
    run_lines = [l for l in text.splitlines() if l.strip().startswith("python -m factory run")]
    assert run_lines and all("${{" not in l for l in run_lines)


def test_published_schema_is_valid_json_with_version_const():
    schema = json.loads((REPO_ROOT / "schemas" / "director_plan.schema.json").read_text())
    assert schema["properties"]["schema_version"]["const"] == "1.0"
