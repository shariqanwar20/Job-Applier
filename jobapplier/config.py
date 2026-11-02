import os
from pathlib import Path

import yaml

__all__ = ["load_config", "load_env"]


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        if v is None:  # a bare `rules:` in the override keeps the default
            continue
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def load_config(root: Path) -> dict:
    """Shipped defaults with the user's optional config.yaml merged over them.

    Dicts merge key by key; lists and scalars replace.
    """
    cfg = yaml.safe_load((root / "config.default.yaml").read_text())
    user = root / "config.yaml"
    return _merge(cfg, yaml.safe_load(user.read_text()) or {}) if user.exists() else cfg


def load_env(path: Path) -> None:
    """Reads KEY=VALUE lines into os.environ; variables already set win. A missing file is fine."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = (part.strip() for part in line.split("=", 1))
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)
