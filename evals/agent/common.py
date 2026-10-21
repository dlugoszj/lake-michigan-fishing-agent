"""Shared rules for the agent backtest: which ports and methods, and what a report says the rating was.

A report's rating for (port, method) is the answer key:
  1. the highest rated salmon observation (chinook, coho, salmon_unspecified, pink) for that port and method;
  2. else a rated fish_unspecified observation for that method ("pier fishing was slow for all species");
  3. else None: the port isn't rated that week and is skipped in scoring.
Reads the extractions the agent uses (config.models.EXTRACTION_MODEL, prompt PROMPT_VERSION, status ok).
"""
import sqlite3
from functools import lru_cache
from pathlib import Path

from config.models import EXTRACTION_MODEL
from config.paths import DB
from extraction.schema import PROMPT_VERSION

PORTS = ["St. Joseph", "South Haven", "Grand Haven", "Muskegon"]
METHODS = ["pier", "boat"]
SALMON = ("chinook", "coho", "salmon_unspecified", "pink")
# "Worth going" cutoff per method. DNR reports rate pier salmon >= 2 in only ~4% of port-weeks (May-Oct),
# so on a pier "a few were caught" (1) already counts; boat uses average or better (2), true ~56% of the time.
GOOD = {"pier": 1, "boat": 2}
HERE = Path(__file__).parent
SPLITS = ["dev", "test"]


def cases_file(split: str) -> Path:
    return HERE / f"cases_{split}.jsonl"


def results_file(split: str, model: str) -> Path:
    """One file per split and agent model, so runs on different models are never mixed or skipped as done."""
    return HERE / f"results_{split}_{model}.jsonl"


def conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    return c


@lru_cache(maxsize=None)
def reports() -> list[tuple[str, str]]:
    """(date, report_id) of every report with extracted SW Michigan lake-port paragraphs, oldest first."""
    rows = conn().execute("""
        SELECT DISTINCT p.date, p.report_id FROM port_chunks p
          JOIN extractions e ON e.chunk_id = p.chunk_id AND e.model = ? AND e.prompt_version = ? AND e.status = 'ok'
         WHERE p.lake_port = 1 ORDER BY p.date""", (EXTRACTION_MODEL, PROMPT_VERSION)).fetchall()
    return [(r["date"], r["report_id"]) for r in rows]


@lru_cache(maxsize=None)
def rating(report_id: str, port: str, method: str) -> int | None:
    """The report's 0-4 rating for salmon at this port and method (rules in the module docstring)."""
    rows = conn().execute("""
        SELECT o.species, o.catch_quality FROM observations o
          JOIN port_chunks p ON p.chunk_id = o.chunk_id
          JOIN extractions e ON e.chunk_id = o.chunk_id AND e.model = o.model AND e.prompt_version = o.prompt_version
         WHERE p.report_id = ? AND p.port = ? AND p.lake_port = 1 AND o.method = ?
           AND o.model = ? AND o.prompt_version = ? AND e.status = 'ok' AND o.catch_quality IS NOT NULL""",
                          (report_id, port, method, EXTRACTION_MODEL, PROMPT_VERSION)).fetchall()
    salmon = [r["catch_quality"] for r in rows if r["species"] in SALMON]
    if salmon:
        return max(salmon)
    general = [r["catch_quality"] for r in rows if r["species"] == "fish_unspecified"]
    return max(general) if general else None


def ratings(report_id: str, method: str) -> dict[str, int | None]:
    return {p: rating(report_id, p, method) for p in PORTS}
