"""Fetch DNR weekly fishing reports and split them into port chunks.

Usage: python fetch_reports.py urls.txt

Deterministic, no LLM. HTML is cached in raw/ so re-runs don't hit the network.
Output: chunks.jsonl, one line per (report, region, port) chunk.
"""
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

RAW_DIR = Path("raw")
OUT = Path("chunks.jsonl")
REGIONS = {"SELP", "SWLP", "NELP", "NWLP", "UP", "TIP"}
SKIP_TEXT = {"back to top"}
PORT_ALIASES = {"St Joseph": "St. Joseph", "St Joseph River": "St. Joseph River", "St. Joe": "St. Joseph", "St Joe": "St. Joseph",
                "Berrien Springs": "St. Joseph River"}
# Section headings (h2); they win over the anchors, which are sometimes misplaced (e.g. 2024-04-24)
REGION_HEADINGS = {"Southeast Lower Peninsula": "SELP", "Southwest Lower Peninsula": "SWLP",
                   "Northeast Lower Peninsula": "NELP", "Northwest Lower Peninsula": "NWLP", "Upper Peninsula": "UP"}
# Lake Michigan ports and connected waters in SWLP; inland counties etc. are excluded
LAKE_PORTS = {
    "St. Joseph", "St. Joseph River", "South Haven", "Black River", "Saugatuck", "Holland",
    "Grand Haven", "Grand River", "Muskegon", "Muskegon Lake", "Muskegon River", "Whitehall",
    "White Lake", "Pentwater", "Ludington", "New Buffalo",
}


def bulletin_id(url: str) -> str:
    return url.rstrip("/").split("/")[-1].split("?")[0].replace("MIDNR-", "")


def fetch(url: str) -> str:
    path = RAW_DIR / f"{bulletin_id(url)}.html"
    if path.exists():
        return path.read_text()
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    resp.raise_for_status()
    path.write_text(resp.text)
    time.sleep(1)  # be polite
    return resp.text


def clean(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def split_port(p: Tag) -> tuple[str | None, str]:
    """Return (port, text) if the paragraph starts with a bolded port name, else (None, text)."""
    # bold may be nested in <font>/<span> (Word-pasted HTML), so take the first bold
    # descendant and require that the paragraph text starts with it
    bold = p.find(["strong", "b"])
    full = clean(p.get_text(" "))
    name = clean(bold.get_text(" ")) if bold else ""
    if name and full.startswith(name):
        rest = full[len(name):]
        # colon may be inside the bold ("Holland:") or just after it ("Muskegon" + ":")
        if name.endswith(":") or rest.lstrip().startswith(":"):
            port = name.rstrip(":").strip()
            text = rest.lstrip().lstrip(":").strip()
            if port and len(port) < 60:
                return port, text
    # fallback: un-bolded "Known Port: ..." paragraph
    for known in sorted(LAKE_PORTS | set(PORT_ALIASES), key=len, reverse=True):
        if re.match(re.escape(known) + r"\s*:", full):
            return known, full.split(":", 1)[1].strip()
    return None, full


def parse(html: str, url: str) -> tuple[dict, list[dict]]:
    soup = BeautifulSoup(html, "html.parser")
    rid = bulletin_id(url)
    m = re.search(r"sent this bulletin at (\d{2}/\d{2}/\d{4})", soup.get_text(" "))
    date = datetime.strptime(m.group(1), "%m/%d/%Y").date().isoformat() if m else None
    title = clean(soup.title.get_text()) if soup.title else ""
    report = {"report_id": rid, "url": url, "date": date, "title": title}

    body = soup.find(id="bulletin_body") or soup.body
    chunks: list[dict] = []
    region = None
    heading_port = None  # some reports put the port in its own <h3> with the text in the next <p>
    for el in body.descendants:
        if not isinstance(el, Tag):
            continue
        if el.name == "a" and el.get("name") in REGIONS:
            region = el["name"]
            heading_port = None
            continue
        if region is not None and el.name == "h2" and clean(el.get_text(" ")) in REGION_HEADINGS:
            region = REGION_HEADINGS[clean(el.get_text(" "))]
            heading_port = None
            continue
        if region is not None and el.name in ("h3", "h4"):
            name = clean(el.get_text(" ")).rstrip(":").strip()
            heading_port = name if 0 < len(name) < 60 else None
            continue
        if region is None or el.name != "p":
            continue
        port, text = split_port(el)
        if not text or text.lower() in SKIP_TEXT:
            continue
        if not port and heading_port:
            port, heading_port = heading_port, None
        if port:
            port = PORT_ALIASES.get(port, port)
            chunks.append({"region": region, "port": port, "raw_text": text,
                           "lake_port": region == "SWLP" and port in LAKE_PORTS})
        elif chunks and chunks[-1]["region"] == region:
            # continuation paragraph belongs to the previous port
            chunks[-1]["raw_text"] += "\n" + text
        else:
            # region-level text before any port (e.g. the fishing tip)
            chunks.append({"region": region, "port": None, "raw_text": text, "lake_port": False})

    for i, c in enumerate(chunks):
        c["chunk_id"] = f"{rid}-{i:03d}"
        c["report_id"] = rid
        c["date"] = date
    return report, chunks


def main(urls_file: str) -> None:
    RAW_DIR.mkdir(exist_ok=True)
    urls = [u.strip() for u in Path(urls_file).read_text().splitlines() if u.strip() and not u.startswith("#")]
    reports, n_chunks = [], 0
    with OUT.open("w") as out:
        for url in urls:
            try:
                report, chunks = parse(fetch(url), url)
            except Exception as e:
                print(f"FAIL {url}: {e}", file=sys.stderr)
                continue
            reports.append(report)
            for c in chunks:
                out.write(json.dumps(c) + "\n")
            n_chunks += len(chunks)
            swlp = [c["port"] for c in chunks if c["region"] == "SWLP"]
            print(f"{report['date']} {report['report_id']}: {len(chunks)} chunks, SWLP ports: {swlp}")
    Path("reports.json").write_text(json.dumps(reports, indent=1))
    print(f"\n{len(reports)} reports, {n_chunks} chunks -> {OUT}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "urls.txt")
