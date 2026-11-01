import pathlib

import pytest
import yaml

from jobapplier.filters import Rules

ROOT = pathlib.Path(__file__).parent.parent


@pytest.fixture
def config():
    return yaml.safe_load((ROOT / "config.default.yaml").read_text())


@pytest.fixture
def rules(config):
    return Rules.from_config(config)
