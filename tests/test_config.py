import os
import shutil

from jobapplier.config import load_config, load_env
from tests.conftest import ROOT


def test_user_config_overrides_defaults(tmp_path):
    shutil.copy(ROOT / "config.default.yaml", tmp_path)
    defaults = load_config(tmp_path)
    (tmp_path / "config.yaml").write_text("rules: {cities: [[vancouver]]}\nlookback_days: 1\n")
    cfg = load_config(tmp_path)
    assert cfg["rules"]["cities"] == [["vancouver"]] and cfg["lookback_days"] == 1
    assert cfg["rules"]["include"] == defaults["rules"]["include"] and cfg["sources"] == defaults["sources"]


def test_null_override_keeps_default(tmp_path):
    shutil.copy(ROOT / "config.default.yaml", tmp_path)
    defaults = load_config(tmp_path)
    (tmp_path / "config.yaml").write_text("rules:\nlookback_days:\n")
    assert load_config(tmp_path) == defaults


def test_load_env_parses_lines_and_keeps_existing_variables(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("# comment\n\nA=1\nB=\"two\"\n C = 'three' \nD=\"x'\nE=a=b\n")
    for k in "ABCDE":
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("A", "keep")
    load_env(env)
    assert (os.environ["A"], os.environ["B"], os.environ["C"], os.environ["D"], os.environ["E"]) == ("keep", "two", "three", "\"x'", "a=b")


def test_load_env_missing_file_is_a_no_op(tmp_path):
    load_env(tmp_path / ".env")
