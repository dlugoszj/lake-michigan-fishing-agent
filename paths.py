"""Every file location the project reads or writes, in one place."""
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

DB = DATA / "fishing.db"            # reports, chunks, extracted observations, buoy history, lake-model profiles
URLS = DATA / "urls.txt"            # DNR bulletin links, collected by hand / web search
REPORTS = DATA / "reports.json"     # one row per bulletin (id, url, date, title)
CHUNKS = DATA / "chunks.jsonl"      # one row per (report, region, port) paragraph
RAW = DATA / "raw"                  # downloaded bulletin HTML
CACHE = DATA / "cache"              # NDBC buoy files (not committed)

LABELS = ROOT / "evals" / "extraction" / "labels"  # hand labels for the extraction eval
