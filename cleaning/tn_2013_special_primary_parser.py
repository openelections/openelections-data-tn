"""
Parser for the October 8, 2013 Tennessee House District 91 Special Primary.

Source PDFs (Shelby County):
  county-level totals:   http://sos-tn-gov-files.s3.amazonaws.com/TNH91PrimaryTotals.pdf
  precinct-level totals: http://sos-tn-gov-files.s3.amazonaws.com/TNH91PrimaryPrecinct.pdf

Outputs:
  2013/20131008__tn__special__primary__county.csv
  2013/20131008__tn__special__primary__precinct.csv

The PDFs contain both the Democratic and Republican primaries. The Democratic
primary had seven candidates; the Republican primary had "No Candidate Filed"
(zero votes). Both are included so the output matches the repo's convention of
listing every party in a single `primary` file.

Requires `requests` (in Pipfile) and the `pdftotext` CLI (poppler) for layout
extraction, which preserves the column spacing used to parse the precinct table.
"""

import csv
import os
import re
import subprocess
import tempfile

import requests

TOTALS_URL = "http://sos-tn-gov-files.s3.amazonaws.com/TNH91PrimaryTotals.pdf"
PRECINCT_URL = "http://sos-tn-gov-files.s3.amazonaws.com/TNH91PrimaryPrecinct.pdf"

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "2013")

DISTRICT_RE = re.compile(r"Tennessee House of Representatives District (\d+)")
PARTY_RE = re.compile(r"(Democratic|Republican) Primary")
# County totals candidate line, e.g. "1. Raumesh A. Akbari -   503".
# The name is non-greedy up to the last " - " separator so hyphenated names
# such as "Doris Deberry-Bradsh" are preserved intact.
CAND_LINE_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s+-\s+([\d,]+)\s*$")
# Precinct row, e.g. "Memphis 26                       2   2   0   2   3   0   0".
PRECINCT_RE = re.compile(r"^(Memphis\s+\d[\d-]*)\s+(.+?)\s*$")
# Numbered candidate header in the precinct PDF, e.g. "1. Raumesh A. Akbari".
HEADER_CAND_RE = re.compile(r"(\d+)\.\s+(.+?)\s{2,}|(\d+)\.\s+(.+?)\s*$")


def pdftotext_layout(pdf_bytes):
    """Return `pdftotext -layout` output for the given PDF bytes."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        return subprocess.check_output(
            ["pdftotext", "-layout", f.name, "-"]
        ).decode("utf-8")


def parse_totals(text):
    """Parse the county-totals PDF into an ordered list of candidates per party.

    Returns a dict: party -> list of (candidate, votes).
    """
    parties = {}
    current_party = None
    for line in text.splitlines():
        m = PARTY_RE.search(line)
        if m:
            current_party = m.group(1)
            parties.setdefault(current_party, [])
            continue
        cm = CAND_LINE_RE.match(line)
        if cm and current_party:
            name = cm.group(1).strip()
            votes = int(cm.group(2).replace(",", ""))
            parties[current_party].append((name, votes))
    return parties


def parse_precincts(text, party_candidates):
    """Parse the precinct PDF into precinct-level rows.

    `party_candidates` maps party -> ordered candidate list (from the totals PDF),
    used to map the numbered columns of the precinct table to candidate names.

    Returns a list of [county, precinct, office, district, party, candidate, votes].
    """
    rows = []
    current_party = None
    for line in text.splitlines():
        m = PARTY_RE.search(line)
        if m:
            current_party = m.group(1)
            continue
        pm = PRECINCT_RE.match(line)
        if not pm or current_party is None:
            continue
        precinct = pm.group(1)
        numbers = [n for n in pm.group(2).split() if n.isdigit()]
        # Candidate names only (totals are county-level, not precinct-level).
        candidate_names = [c for c, _ in party_candidates.get(current_party, [])]
        if len(numbers) != len(candidate_names):
            # Skip the column-header row ("1 2 3 4 5 6 7") and the "Totals:" row.
            continue
        for name, votes in zip(candidate_names, numbers):
            rows.append(["Shelby", precinct, "State House", "91",
                         current_party, name, int(votes)])
    return rows


def main():
    county_dir = os.path.abspath(OUT_DIR)
    os.makedirs(county_dir, exist_ok=True)

    totals_text = pdftotext_layout(requests.get(TOTALS_URL).content)
    precinct_text = pdftotext_layout(requests.get(PRECINCT_URL).content)

    party_candidates = parse_totals(totals_text)

    # County-level CSV.
    county_path = os.path.join(county_dir, "20131008__tn__special__primary__county.csv")
    with open(county_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["county", "office", "district", "party", "candidate", "votes"])
        for party in ("Democratic", "Republican"):
            for candidate, votes in party_candidates.get(party, []):
                w.writerow(["Shelby", "State House", "91", party, candidate, votes])

    # Precinct-level CSV.
    precinct_rows = parse_precincts(precinct_text, party_candidates)
    precinct_path = os.path.join(county_dir, "20131008__tn__special__primary__precinct.csv")
    with open(precinct_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["county", "precinct", "office", "district",
                    "party", "candidate", "votes"])
        w.writerows(precinct_rows)

    print(f"Wrote {county_path} ({sum(len(v) for v in party_candidates.values())} rows)")
    print(f"Wrote {precinct_path} ({len(precinct_rows)} rows)")


if __name__ == "__main__":
    main()