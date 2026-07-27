"""
Parser for the September 29, 2015 Tennessee House District 14 special general
election (Knox County).

Source PDFs (TN SoS S3 bucket):
  county totals:   http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14GeneralCountyTotals.pdf
  precinct totals: http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14GeneralPrecinctTotals.pdf

Outputs (in 2015/):
  20150929__tn__special__general__county.csv
  20150929__tn__special__general__precinct.csv

The special general had a single unopposed candidate, Jason Zachary (Republican),
with 210 total votes across 7 precincts.

Note on the date: the issue body listed filenames prefixed `20150915`, but the
source PDF and the issue title both give the election date as September 29, 2015.
Per the OpenElections filename convention (election date), `20150929` is used.

Conventions (matching the repo's canonical 2014/2016 Knox files):
  county:   "Knox"
  office:   "State House"
  district: "14"
  party:    full names ("Republican")
  precinct: exactly as printed in the source PDF (e.g. "66N Farragut I",
            "69N A L Lotts"), matching the Aug 12, 2015 D14 special primary.

Requires `requests` (in Pipfile) and the `pdftotext` CLI (poppler) for layout
extraction, which preserves the column spacing used to parse the precinct table.
"""

import csv
import os
import re
import subprocess
import tempfile

import requests

TOTALS_URL = "http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14GeneralCountyTotals.pdf"
PRECINCT_URL = "http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14GeneralPrecinctTotals.pdf"

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "2015")

# Single-letter party code -> full name (the source PDF spells out "Republican",
# but handle the single-letter form too for robustness).
PARTY_FROM_CODE = {
    "D": "Democratic",
    "R": "Republican",
    "L": "Libertarian",
    "G": "Green",
    "I": "Independent",
    "C": "Constitution",
}

# County-totals candidate line, e.g. "1 Jason Zachary - Republican   210".
# Party may be a full word ("Republican") or a single letter ("R").
CAND_LINE_RE = re.compile(r"^\s*\d+\.?\s+(.+?)\s+-\s+(\w+)\s+([\d,]+)\s*$")
# Precinct row, e.g. "65 Concord                 48". Starts with a digit (the
# precinct code), which excludes the "Precincts:" and "Totals:" lines (they start
# with letters). The votes group must contain a digit, so the candidate header
# line ("1. Jason Zachary - Republican", ending in letters) is excluded.
PRECINCT_RE = re.compile(r"^\s*(\d.*?)\s{2,}(\d[\d,\s]*)\s*$")


def pdftotext_layout(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        return subprocess.check_output(
            ["pdftotext", "-layout", f.name, "-"]
        ).decode("utf-8")


def normalize_party(token):
    token = token.strip()
    if token in PARTY_FROM_CODE:
        return PARTY_FROM_CODE[token]
    return token


def parse_totals(text):
    """Parse the county-totals PDF into an ordered candidate list.

    Returns a list of (candidate, party, votes).
    """
    out = []
    for line in text.splitlines():
        m = CAND_LINE_RE.match(line)
        if m:
            name = m.group(1).strip()
            party = normalize_party(m.group(2))
            votes = int(m.group(3).replace(",", ""))
            out.append((name, party, votes))
    return out


def parse_precincts(text, candidates):
    """Parse the precinct PDF into precinct-level rows.

    `candidates` is the ordered list of (name, party) from the totals PDF.
    Returns a list of [county, precinct, office, district, party, candidate, votes].
    """
    rows = []
    names = [c[0] for c in candidates]
    parties = [c[1] for c in candidates]
    for line in text.splitlines():
        pm = PRECINCT_RE.match(line)
        if not pm:
            continue
        precinct = pm.group(1).strip()
        numbers = [n for n in pm.group(2).split() if n.replace(",", "").isdigit()]
        if len(numbers) != len(names):
            # Skip the column-header row ("Precincts:   1") and the "Totals:" row.
            continue
        for name, party, votes in zip(names, parties, numbers):
            rows.append(["Knox", precinct, "State House", "14",
                         party, name, int(votes)])
    return rows


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"Wrote {path} ({len(rows)} rows)")


def main():
    county_dir = os.path.abspath(OUT_DIR)
    os.makedirs(county_dir, exist_ok=True)

    candidates = parse_totals(pdftotext_layout(requests.get(TOTALS_URL).content))

    county_rows = [
        ["Knox", "State House", "14", party, candidate, votes]
        for candidate, party, votes in candidates
    ]
    precinct_rows = parse_precincts(
        pdftotext_layout(requests.get(PRECINCT_URL).content), candidates
    )

    write_csv(
        os.path.join(county_dir, "20150929__tn__special__general__county.csv"),
        ["county", "office", "district", "party", "candidate", "votes"],
        county_rows,
    )
    write_csv(
        os.path.join(county_dir, "20150929__tn__special__general__precinct.csv"),
        ["county", "precinct", "office", "district", "party", "candidate", "votes"],
        precinct_rows,
    )


if __name__ == "__main__":
    main()