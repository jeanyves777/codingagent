import re
import tomllib
from pathlib import Path

import pytest

from app.config import ConfigError, load_config
from app.settings import load_settings

ROOT = Path(__file__).resolve().parent


def test_load_config_accepts_str_and_path(tmp_path):
    config = tmp_path / "deploy.toml"
    config.write_text('[deploy]\nreplicas = 5\ntags = ["a", "b"]\n')
    assert load_config(str(config)) == load_config(config) == {"deploy": {"replicas": 5, "tags": ["a", "b"]}}
    assert load_settings(config).tags == ["a", "b"]


def test_invalid_toml_raises_config_error_with_file_name(tmp_path):
    broken = tmp_path / "broken.toml"
    broken.write_text("[deploy\nregion = ")
    with pytest.raises(ConfigError, match="broken.toml"):
        load_config(broken)


def test_toml_dependency_removed_everywhere():
    requirements = [re.split(r"[=<>~! \[]", line.strip(), maxsplit=1)[0].lower()
                    for line in (ROOT / "requirements.txt").read_text().splitlines() if line.strip()]
    assert "toml" not in requirements and {"click", "rich"} <= set(requirements)
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    names = [re.split(r"[=<>~! \[]", item, maxsplit=1)[0].lower() for item in project["dependencies"]]
    assert "toml" not in names and {"click", "rich"} <= set(names)
    assert not any(re.search(r"^\s*import toml\b|^\s*from toml\b", path.read_text(), re.M)
                   for path in (ROOT / "app").rglob("*.py"))
