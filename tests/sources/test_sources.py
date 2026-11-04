from jobapplier.sources import build_sources
from jobapplier.sources.freehire import FreeHire
from jobapplier.sources.jobspy import Indeed, LinkedIn


def test_build_sources_follows_config_order(config):
    assert [type(s) for s in build_sources(config)] == [FreeHire, Indeed, LinkedIn]
