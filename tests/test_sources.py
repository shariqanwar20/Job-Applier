import pytest

from jobapplier.sources import FreeHire, Indeed, LinkedIn, build_sources


def test_build_sources_follows_config_order(config):
    assert [type(s) for s in build_sources(config)] == [FreeHire, Indeed, LinkedIn]


def test_build_sources_rejects_unknown_source(config):
    with pytest.raises(ValueError, match="nope"):
        build_sources({**config, "sources": {"nope": {}}})
