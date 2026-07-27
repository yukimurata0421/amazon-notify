from __future__ import annotations

import re
import tomllib
from pathlib import Path
from urllib.parse import unquote

import pytest

from amazon_notify import config
from amazon_notify.runtime import validate_config

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MARKDOWN_LINK = re.compile(r"\[[^\]]+\]\(([^)]+)\)")


@pytest.mark.parametrize(
    "relative_path",
    [
        "config/config.example.json",
        "config/config.full.example.json",
    ],
)
def test_distributed_example_configs_are_valid(relative_path: str) -> None:
    config_path = PROJECT_ROOT / relative_path
    payload = config.load_config(config_path)
    paths = config.get_runtime_paths(config_path)

    assert validate_config(payload, paths=paths) == []


@pytest.mark.parametrize("readme_name", ["README.md", "README.ja.md"])
def test_readme_local_links_exist(readme_name: str) -> None:
    readme_path = PROJECT_ROOT / readme_name
    missing: list[str] = []

    for target in MARKDOWN_LINK.findall(readme_path.read_text(encoding="utf-8")):
        path_text = target.split("#", 1)[0]
        if not path_text or "://" in path_text or path_text.startswith("mailto:"):
            continue
        resolved = readme_path.parent / unquote(path_text)
        if not resolved.exists():
            missing.append(target)

    assert missing == []


def test_python_package_uses_src_layout() -> None:
    pyproject = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    package_find = pyproject["tool"]["setuptools"]["packages"]["find"]

    assert package_find["where"] == ["src"]
    assert (PROJECT_ROOT / "src" / "amazon_notify" / "__init__.py").is_file()
    assert not (PROJECT_ROOT / "amazon_notify").exists()
