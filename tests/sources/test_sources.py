from jobapplier.sources import FreeHire, Indeed, LinkedIn, build_sources


def test_build_sources_follows_config_order(config):
    assert [type(s) for s in build_sources(config)] == [FreeHire, Indeed, LinkedIn]

