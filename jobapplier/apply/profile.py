"""The owner's application answers, from the git-ignored profile.yaml."""

from pathlib import Path

import yaml

__all__ = ["ProfileError", "load_profile", "profile_values"]


class ProfileError(Exception):
    pass


def load_profile(path: Path) -> dict:
    if not path.exists():
        raise ProfileError(f"no {path.name}: copy profile.example.yaml to {path.name} and edit it")
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ProfileError(f"{path.name} must be a mapping of keys, like profile.example.yaml")
    return data


def _text(value) -> str:
    if isinstance(value, bool):  # forms ask yes/no questions
        return "Yes" if value else "No"
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return "" if value is None else str(value)


def profile_values(profile: dict, prefix: str = "") -> dict[str, str]:
    """Dotted keys to form text: {"name": {"first": "A"}} -> {"name.first": "A"}, plus name.full and
    location.full."""
    out = {}
    for key, value in profile.items():
        if isinstance(value, dict):
            out |= profile_values(value, f"{prefix}{key}.")
        else:
            out[f"{prefix}{key}"] = _text(value)
    if not prefix:
        name = (out.get("name.first", ""), out.get("name.last", ""))
        out["name.full"] = " ".join(n for n in name if n)
        out["location.full"] = ", ".join(
            p for p in (out.get("location.city", ""), out.get("location.province", "")) if p
        )
    return out
