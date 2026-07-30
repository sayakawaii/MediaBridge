from __future__ import annotations

import pytest

from mediabridge.config import expand_env_vars, load_config
from mediabridge.errors import ConfigError


def test_expands_set_variables(monkeypatch):
    monkeypatch.setenv("MB_HOST", "video.example.org")
    assert expand_env_vars("https://${MB_HOST}/api") == "https://video.example.org/api"


def test_leaves_unset_variables_intact(monkeypatch):
    monkeypatch.delenv("MB_ABSENT", raising=False)
    # Collapsing to "" would let a missing secret fail silently much later.
    assert expand_env_vars("${MB_ABSENT}") == "${MB_ABSENT}"


def test_expands_recursively(monkeypatch):
    monkeypatch.setenv("MB_TAG", "搬运")
    result = expand_env_vars({"a": ["${MB_TAG}", 1], "b": {"c": "${MB_TAG}"}})
    assert result == {"a": ["搬运", 1], "b": {"c": "搬运"}}


def _write(tmp_path, text):
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


MINIMAL = """
version: 1
sources:
  - name: demo
    type: peertube
    options: {host: video.blender.org}
    publish: {channel_id: 190}
"""


def test_loads_minimal_config(tmp_path):
    config = load_config(_write(tmp_path, MINIMAL))
    assert len(config.enabled_sources()) == 1
    assert config.sources[0].publish.creation_type == 1
    assert config.limits.max_filesize_mb == 2000


def test_missing_file_is_reported(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.yaml")


def test_unknown_key_is_rejected(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, MINIMAL + "\nunexpected_key: 1\n"))


def test_invalid_creation_type_is_rejected(tmp_path):
    text = MINIMAL.replace("{channel_id: 190}", "{channel_id: 190, creation_type: 9}")
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, text))


def test_source_name_must_be_a_safe_identifier(tmp_path):
    with pytest.raises(ConfigError):
        load_config(_write(tmp_path, MINIMAL.replace("name: demo", "name: 'bad name/../'")))


def test_disabled_sources_are_excluded(tmp_path):
    config = load_config(
        _write(tmp_path, MINIMAL.replace("type: peertube", "type: peertube\n    enabled: false"))
    )
    assert config.sources and config.enabled_sources() == []
