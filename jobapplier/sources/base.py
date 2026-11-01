from abc import ABC, abstractmethod

from jobapplier.models import Posting


class Source(ABC):
    name: str

    @abstractmethod
    def fetch(self) -> tuple[list[Posting], str | None]:
        """Never raises: returns the postings gathered so far plus an error string."""
