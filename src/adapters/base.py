"""BaseAdapter: the shape every data source follows.

Each adapter (schedule, news, results, ...) subclasses this and implements the
three steps of the pipeline:

    fetch()      -> grab raw data from the source (HTTP, RSS, PDF, ...)
    normalize()  -> turn raw data into a list of dict rows matching our schema
    upsert()     -> write those rows to Postgres (idempotently)

run() ties them together so a pipeline runner just calls adapter.run().
"""

from abc import ABC, abstractmethod
from typing import Any, Sequence

from ..db import get_connection


class BaseAdapter(ABC):
    # Subclasses set these so logging/reporting is consistent.
    name: str = "base"

    @abstractmethod
    def fetch(self) -> Any:
        """Retrieve raw data from the source and return it untouched."""
        raise NotImplementedError

    @abstractmethod
    def normalize(self, raw: Any) -> Sequence[dict]:
        """Transform raw data into a list of dict rows ready for the database."""
        raise NotImplementedError

    @abstractmethod
    def upsert(self, conn, rows: Sequence[dict]) -> int:
        """Write normalized rows to the database. Return the number affected."""
        raise NotImplementedError

    def run(self) -> int:
        """Execute fetch -> normalize -> upsert in one transaction.

        Returns the number of rows written.
        """
        raw = self.fetch()
        rows = self.normalize(raw)
        with get_connection() as conn:
            count = self.upsert(conn, rows)
        print(f"[{self.name}] upserted {count} row(s)")
        return count
