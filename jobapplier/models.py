from dataclasses import dataclass


@dataclass
class Posting:
    source: str  # "freehire" | "linkedin" | "indeed"
    source_id: str
    title: str
    company: str
    location: str
    url: str
    posted: str = ""  # YYYY-MM-DD
    description: str = ""
    seniority: str = ""
    no_location: bool = False
    identity: str | None = None
