from jobapplier.sources.base import Source
from jobapplier.sources.freehire import FreeHire
from jobapplier.sources.jobspy_sources import Indeed, LinkedIn

SOURCES = {"freehire": FreeHire, "indeed": Indeed, "linkedin": LinkedIn}


def build_sources(cfg: dict) -> list[Source]:
    """In cfg["sources"] order, which is the fetch order."""
    unknown = [n for n in cfg["sources"] if n not in SOURCES]
    if unknown:
        raise ValueError(f"unknown source: {unknown[0]}")
    return [SOURCES[n](c, cfg["lookback_days"]) for n, c in cfg["sources"].items()]
