from __future__ import annotations

import copy
from pathlib import Path

import pytest
import yaml

from factory.contracts.channel_config import ChannelConfig, list_channels, load_channel, load_channel_config
from factory.contracts.enums import ChannelState
from factory.contracts.errors import ConfigError
from tests.conftest import REPO_ROOT


def _raw() -> dict:
    return yaml.safe_load((REPO_ROOT / "channels" / "drama" / "config.yaml").read_text())


def _write(tmp_path: Path, raw, name: str = "config.yaml") -> Path:
    p = tmp_path / name
    p.write_text(yaml.safe_dump(raw))
    return p


def test_drama_config_loads(channel: ChannelConfig):
    assert channel.channel == "drama"
    assert channel.lifecycle_state is ChannelState.TEST
    assert "narrator" in channel.voices
    assert channel.default_format in channel.allowed_formats


def test_list_channels_finds_drama():
    assert "drama" in list_channels(REPO_ROOT / "channels")


@pytest.mark.parametrize(
    "mutate, fragment",
    [
        (lambda r: r.pop("niche"), "niche"),
        (lambda r: r.update(config_version="9.9"), "unsupported config_version"),
        (lambda r: r["voices"].pop("narrator"), "narrator"),
        (lambda r: r.update(default_format="long"), "default_format"),
        (lambda r: r.update(lifecycle_state="ZOMBIE"), "lifecycle_state"),
        (lambda r: r.update(surprise="field"), "surprise"),
        (lambda r: r["target_duration_s"].update(min_s=90, max_s=30), "min_s"),
        (lambda r: r.update(channel="Drama-1"), "channel"),
        (lambda r: r["secret_env"].update(youtube_refresh_token="1//0gAbCdEfGhIjKlMnOp-realtoken"), "secret_env"),
    ],
)
def test_invalid_config_rejected(tmp_path: Path, mutate, fragment):
    raw = copy.deepcopy(_raw())
    mutate(raw)
    with pytest.raises(ConfigError) as err:
        load_channel_config(_write(tmp_path, raw))
    assert fragment in str(err.value)


def test_non_mapping_and_bad_yaml_rejected(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("- just\n- a list\n")
    with pytest.raises(ConfigError):
        load_channel_config(p)
    p.write_text("key: [unclosed")
    with pytest.raises(ConfigError):
        load_channel_config(p)
    with pytest.raises(ConfigError):
        load_channel_config(tmp_path / "missing.yaml")


def test_directory_name_must_match_declared_channel(tmp_path: Path):
    raw = _raw()
    (tmp_path / "other").mkdir()
    _write(tmp_path / "other", raw)
    with pytest.raises(ConfigError, match="must match directory"):
        load_channel(tmp_path, "other")


def test_yaml_cannot_execute_code(tmp_path: Path):
    p = tmp_path / "config.yaml"
    p.write_text("!!python/object/apply:os.system ['echo pwned']\n")
    with pytest.raises(ConfigError):
        load_channel_config(p)


def test_second_channel_needs_only_a_config_file(tmp_path: Path):
    """Adding a channel = adding data. No Factory code involved."""
    raw = _raw()
    raw.update(channel="stoic", display_name="Stoic", experiment_prefix="ST", lifecycle_state="IDEA")
    (tmp_path / "stoic").mkdir()
    _write(tmp_path / "stoic", raw)
    cfg = load_channel(tmp_path, "stoic")
    assert cfg.lifecycle_state is ChannelState.IDEA
