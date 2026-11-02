from jobapplier.sources.base import Source
from jobapplier.sources.freehire import FreeHire
from jobapplier.sources.jobspy import Indeed, LinkedIn

SOURCES = {"freehire": FreeHire, "indeed": Indeed, "linkedin": LinkedIn}


def build_sources(cfg: dict) -> list[Source]:
    """In cfg["sources"] order, which is the fetch order."""
    return [SOURCES[n](c, cfg["lookback_days"]) for n, c in cfg["sources"].items()]
