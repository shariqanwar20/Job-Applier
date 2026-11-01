import shutil

from jobapplier.config import load_config
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
