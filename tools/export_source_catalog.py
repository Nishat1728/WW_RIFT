"""Export the reviewed Markdown source inventory as a notebook-friendly CSV."""

from __future__ import annotations

import csv
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "docs" / "RIFT_INPUT_SOURCE_CATALOG.md"
OUTPUT = ROOT / "docs" / "rift_input_source_catalog.csv"

heading_pattern = re.compile(r"^## \d+\. (.+)$")
link_pattern = re.compile(r"^\[([^]]+)\]\(([^)]+)\)$")
rows: list[dict[str, str]] = []
category = ""

for raw_line in SOURCE.read_text(encoding="utf-8").splitlines():
    line = raw_line.strip()
    heading = heading_pattern.match(line)
    if heading:
        category = heading.group(1)
        continue
    if not line.startswith("|") or line.startswith("|---") or "Provider key" in line:
        continue
    fields = [field.strip() for field in line.strip("|").split("|")]
    if len(fields) != 5 or not fields[0].startswith("`"):
        continue
    source_match = link_pattern.match(fields[1])
    rows.append({
        "input_category": category,
        "provider_key": fields[0].strip("`"),
        "source_name": source_match.group(1) if source_match else fields[1],
        "official_url": source_match.group(2) if source_match else "",
        "coverage_native_resolution": fields[2],
        "status": fields[3].replace("**", ""),
        "rift_conversion": fields[4],
    })

OUTPUT.parent.mkdir(parents=True, exist_ok=True)
with OUTPUT.open("w", newline="", encoding="utf-8") as stream:
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)

print(f"Wrote {len(rows)} sources to {OUTPUT}")
