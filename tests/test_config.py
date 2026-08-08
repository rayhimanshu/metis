from pathlib import Path

import pytest
import yaml

from metis.config import ConfigError, find_config, load, sample


def write(tmp_path: Path, data: dict) -> Path:
    path = tmp_path / "metis.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def test_defaults_apply_when_no_file(tmp_path):
    cfg = load(tmp_path / "missing.yaml")
    assert cfg.environment == "dev"
    assert cfg.max_iterations == 4
    assert set(cfg.agents) == {"swe", "devops", "tester"}
    assert cfg.agents["swe"].mode == "attached"
    assert cfg.agents["devops"].mode == "spawned"


def test_sample_config_is_valid(tmp_path):
    path = tmp_path / "metis.yaml"
    path.write_text(sample(), encoding="utf-8")
    cfg = load(path)
    assert cfg.agents["tester"].wake_on == ["deployed"]


def test_partial_override_keeps_other_defaults(tmp_path):
    path = write(tmp_path, {"run": {"environment": "staging"}})
    cfg = load(path)
    assert cfg.environment == "staging"
    assert cfg.max_iterations == 4  # untouched
    assert "swe" in cfg.agents  # not clobbered by the partial override


def test_empty_wake_on_is_rejected(tmp_path):
    """An agent with no triggers can never be woken.

    Every process looks healthy and nothing ever happens, so this has to fail
    at load rather than be discovered during a run.
    """
    path = write(tmp_path, {"agents": {"swe": {"mode": "attached", "wake_on": []}}})
    with pytest.raises(ConfigError, match="never be woken"):
        load(path)


def test_unknown_mode_is_rejected(tmp_path):
    path = write(tmp_path, {"agents": {"swe": {"mode": "turbo", "wake_on": ["requirement"]}}})
    with pytest.raises(ConfigError, match="mode must be one of"):
        load(path)


def test_workspace_resolves_relative_to_config(tmp_path):
    (tmp_path / "src").mkdir()
    path = write(tmp_path, {"run": {"workspace": "src"}})
    assert load(path).workspace == (tmp_path / "src").resolve()


def test_find_config_walks_upward(tmp_path):
    write(tmp_path, {"run": {"environment": "dev"}})
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert find_config(nested) == tmp_path / "metis.yaml"


def test_sample_contains_no_credentials():
    """The starter config is meant to be committed."""
    text = sample().lower()
    for word in ("token", "password", "secret", "api_key"):
        for line in text.splitlines():
            stripped = line.strip()
            if word in stripped and not stripped.startswith("#"):
                pytest.fail(f"sample() has an uncommented line mentioning {word!r}: {line}")
