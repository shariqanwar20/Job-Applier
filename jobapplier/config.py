from pathlib import Path

import yaml


def _merge(base: dict, over: dict) -> dict:
    return {k: _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v for k, v in {**base, **over}.items()}


def load_config(root: Path) -> dict:
    """Shipped defaults, with the user's optional config.yaml merged over them (dicts merge, lists and scalars replace)."""
    cfg = yaml.safe_load((root / "config.default.yaml").read_text())
    user = root / "config.yaml"
    return _merge(cfg, yaml.safe_load(user.read_text()) or {}) if user.exists() else cfg
