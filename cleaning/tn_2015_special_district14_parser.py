"""
Parser for the August 12, 2015 Tennessee House District 14 special primary
(Knox County).

Source PDFs (TN SoS S3 bucket):
  county totals:   http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14PrimaryCountyTotals.pdf
  precinct totals: http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14PrimaryPrecinctTotals.pdf

Outputs (in 2015/):
  20150812__tn__special__primary__county.csv
  20150812__tn__special__primary__precinct.csv

Both primaries are included (per repo convention of one `primary` file):
  Republican - Karen Carson (1,742) and Jason Zachary (2,397)
  Democratic - "No Candidate Qualified" (0 votes)

Conventions (matching the repo's canonical 2014/2016 Knox files):
  county:   "Knox"
  office:   "State House"
  district: "14"
  party:    full names ("Democratic", "Republican")
  precinct: exactly as printed in the source PDF (e.g. "66N Farragut I",
            "69N A L Lotts"). These 2015 precincts were reorganized by 2016, so
            the 2015 spellings are preserved for fidelity to the source.

Requires `requests` (in Pipfile) and the `pdftotext` CLI (poppler) for layout
extraction, which preserves the column spacing used to parse the precinct table.
"""

import csv
import os
import re
import subprocess
import tempfile

import requests

TOTALS_URL = "http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14PrimaryCountyTotals.pdf"
PRECINCT_URL = "http://sos-tn-gov-files.s3.amazonaws.com/KnoxDist14PrimaryPrecinctTotals.pdf"

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "2015")

DISTRICT_RE = re.compile(r"Tennessee House of Representatives District (\d+)")
# Section header, e.g. "Special Republican Primary" / "Special Democratic Primary".
PARTY_SECTION_RE = re.compile(r"Special (Democratic|Republican) Primary")
# County-totals candidate line, e.g. "1 Karen Carson   1,742" or
# "1 No Candidate Qualified   0". Number prefix may or may not have a trailing
# period; the name is non-greedy up to the first run of 2+ spaces before votes.
CAND_LINE_RE = re.compile(r"^\s*\d+\.?\s+(.+?)\s{2,}([\d,]+)\s*$")
# Precinct row, e.g. "65 Concord                 343     564". Starts with a digit
# (the precinct code), which excludes the "Precincts:" and "Totals:" lines (they
# start with letters). Leading indentation is optional. The votes group must
# contain a digit, so candidate header lines ("1. Karen Carson", which end in
# letters) are excluded.
PRECINCT_RE = re.compile(r"^\s*(\d.*?)\s{2,}(\d[\d,\s]*)\s*$")


def pdftotext_layout(pdf_bytes):
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        return subprocess.check_output(
            ["pdftotext", "-layout", f.name, "-"]
        ).decode("utf-8")


def parse_totals(text):
    """Parse the county-totals PDF into an ordered candidate list per party.

    Returns a dict: party -> list of (candidate, votes).
    """
    parties = {}
    current_party = None
    for line in text.splitlines():
        m = PARTY_SECTION_RE.search(line)
        if m:
            current_party = m.group(1)
            parties.setdefault(current_party, [])
            continue
        cm = CAND_LINE_RE.match(line)
        if cm and current_party:
            parties[current_party].append(
                (cm.group(1).strip(), int(cm.group(2).replace(",", "")))
            )
    return parties


def parse_precincts(text, party_candidates):
    """Parse the precinct PDF into precinct-level rows.

    `party_candidates` maps party -> ordered candidate names (from the totals
    PDF), used to map the numbered columns of the precinct table to candidates.

    Returns a list of [county, precinct, office, district, party, candidate, votes].
    """
    rows = []
    current_party = None
    for line in text.splitlines():
        m = PARTY_SECTION_RE.search(line)
        if m:
            current_party = m.group(1)
            continue
        pm = PRECINCT_RE.match(line)
        if not pm or current_party is None:
            continue
        precinct = pm.group(1).strip()
        numbers = [n for n in pm.group(2).split() if n.replace(",", "").isdigit()]
        names = party_candidates.get(current_party, [])
        if len(numbers) != len(names):
            continue
        for name, votes in zip(names, numbers):
            rows.append(["Knox", precinct, "State House", "14",
                         current_party, name, int(votes)])
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

    party_candidates = parse_totals(pdftotext_layout(requests.get(TOTALS_URL).content))

    # County-level CSV (ordered Democratic, then Republican for stable output).
    county_rows = []
    for party in ("Democratic", "Republican"):
        for candidate, votes in party_candidates.get(party, []):
            county_rows.append(["Knox", "State House", "14", party, candidate, votes])

    # Precinct-level CSV.
    section_candidates = {
        party: [c for c, _ in party_candidates.get(party, [])]
        for party in party_candidates
    }
    precinct_rows = parse_precincts(
        pdftotext_layout(requests.get(PRECINCT_URL).content), section_candidates
    )

    write_csv(
        os.path.join(county_dir, "20150812__tn__special__primary__county.csv"),
        ["county", "office", "district", "party", "candidate", "votes"],
        county_rows,
    )
    write_csv(
        os.path.join(county_dir, "20150812__tn__special__primary__precinct.csv"),
        ["county", "precinct", "office", "district", "party", "candidate", "votes"],
        precinct_rows,
    )


if __name__ == "__main__":
    main()