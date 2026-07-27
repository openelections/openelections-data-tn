"""
Parser for the 2013 Tennessee House District 91 special elections (Shelby County).

Source PDFs (Shelby County, hosted on the TN SoS S3 bucket):
  Special primary, Oct 8, 2013:
    county totals:   http://sos-tn-gov-files.s3.amazonaws.com/TNH91PrimaryTotals.pdf
    precinct totals: http://sos-tn-gov-files.s3.amazonaws.com/TNH91PrimaryPrecinct.pdf
  Special general, Nov 21, 2013:
    county totals:   http://sos-tn-gov-files.s3.amazonaws.com/TNH91GeneralTotals.pdf
    precinct totals: http://sos-tn-gov-files.s3.amazonaws.com/TNH91GeneralPrecinct.pdf

Outputs (in 2013/):
  20131008__tn__special__primary__county.csv
  20131008__tn__special__primary__precinct.csv
  20131121__tn__special__general__county.csv
  20131121__tn__special__general__precinct.csv

Conventions (matching the repo's canonical 2014/2016 files):
  county:   "Shelby"
  office:   "State House"
  district: "91"
  party:    full names ("Democratic", "Republican", "Libertarian", ...)
  precinct: "Memphis 26" (with space, as printed in the source PDFs)

The primary PDFs contain both the Democratic and Republican primaries; the
Democratic primary had seven candidates and the Republican primary had "No
Candidate Filed" (zero votes). Both parties are included in the single
`primary` files, per repo convention.

Requires `requests` (in Pipfile) and the `pdftotext` CLI (poppler) for layout
extraction, which preserves the column spacing used to parse the precinct tables.
"""

import csv
import os
import re
import subprocess
import tempfile

import requests

BASE = "http://sos-tn-gov-files.s3.amazonaws.com"

PRIMARY_TOTALS = f"{BASE}/TNH91PrimaryTotals.pdf"
PRIMARY_PRECINCT = f"{BASE}/TNH91PrimaryPrecinct.pdf"
GENERAL_TOTALS = f"{BASE}/TNH91GeneralTotals.pdf"
GENERAL_PRECINCT = f"{BASE}/TNH91GeneralPrecinct.pdf"

OUT_DIR = os.path.join(os.path.dirname(__file__), "..", "2013")

# Map single-letter party codes from the source PDFs to full party names.
PARTY_FROM_CODE = {
    "D": "Democratic",
    "R": "Republican",
    "L": "Libertarian",
    "G": "Green",
    "I": "Independent",
    "C": "Constitution",
}

DISTRICT_RE = re.compile(r"Tennessee House of Representatives District (\d+)")
PARTY_SECTION_RE = re.compile(r"(Democratic|Republican) Primary")
# Primary county-totals candidate line, e.g. "1. Raumesh A. Akbari -   503".
# The name is non-greedy up to the last " - " separator so hyphenated names
# such as "Doris Deberry-Bradsh" are preserved intact.
PRIMARY_CAND_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s+-\s+([\d,]+)\s*$")
# General county-totals candidate line, e.g. "1. Raumesh A. Akbari - D   3,088".
GENERAL_CAND_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s+-\s+([A-Z])\s+([\d,]+)\s*$")
# General precinct candidate header, e.g. "1. James "Jim" Tomasik - (L)".
GENERAL_HEADER_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s+-\s+\(?([A-Z])\)?\s*$")
# Precinct row, e.g. "Memphis 26                       2   2   0   2   3   0   0".
PRECINCT_RE = re.compile(r"^(Memphis\s+\d[\d-]*)\s+(.+?)\s*$")


def pdftotext_layout(pdf_bytes):
    """Return `pdftotext -layout` output for the given PDF bytes."""
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(pdf_bytes)
        f.flush()
        return subprocess.check_output(
            ["pdftotext", "-layout", f.name, "-"]
        ).decode("utf-8")


def fetch(url):
    return pdftotext_layout(requests.get(url).content)


def parse_primary_totals(text):
    """Parse the primary county-totals PDF into an ordered candidate list per party.

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
        cm = PRIMARY_CAND_RE.match(line)
        if cm and current_party:
            parties[current_party].append(
                (cm.group(1).strip(), int(cm.group(2).replace(",", "")))
            )
    return parties


def parse_general_totals(text):
    """Parse the general county-totals PDF into an ordered candidate list.

    Returns a list of (candidate, party, votes).
    """
    out = []
    for line in text.splitlines():
        m = GENERAL_CAND_RE.match(line)
        if m:
            name = m.group(1).strip()
            party = PARTY_FROM_CODE.get(m.group(2), m.group(2))
            votes = int(m.group(3).replace(",", ""))
            out.append((name, party, votes))
    return out


def parse_precinct_rows(text, section_candidates):
    """Parse a precinct PDF into precinct-level rows.

    `section_candidates` maps a section key (party name for the primary, or the
    single party-agnostic key "general" for the general) to the ordered list of
    candidate names for that section's columns.

    Returns a list of [county, precinct, office, district, party, candidate, votes].
    For the general, each candidate carries its own party; for the primary, the
    party is the section key.
    """
    rows = []
    current_section = None
    is_general = "general" in section_candidates
    for line in text.splitlines():
        if not is_general:
            m = PARTY_SECTION_RE.search(line)
            if m:
                current_section = m.group(1)
                continue
        else:
            current_section = "general"
        pm = PRECINCT_RE.match(line)
        if not pm or current_section is None:
            continue
        precinct = pm.group(1)
        numbers = [n for n in pm.group(2).split() if n.isdigit()]
        entry = section_candidates.get(current_section, [])
        if is_general:
            # entry is a list of (name, party)
            names = [c[0] for c in entry]
            parties = [c[1] for c in entry]
        else:
            names = entry
            parties = [current_section] * len(names)
        if len(numbers) != len(names):
            # Skip the column-header row ("1 2 3 4 5 6 7") and the "Totals:" row.
            continue
        for name, party, votes in zip(names, parties, numbers):
            rows.append(["Shelby", precinct, "State House", "91",
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

    # --- Special primary (Oct 8, 2013) ---
    primary_parties = parse_primary_totals(fetch(PRIMARY_TOTALS))
    primary_county = []
    for party in ("Democratic", "Republican"):
        for candidate, votes in primary_parties.get(party, []):
            primary_county.append(
                ["Shelby", "State House", "91", party, candidate, votes]
            )
    primary_section_candidates = {
        party: [c for c, _ in primary_parties.get(party, [])]
        for party in primary_parties
    }
    primary_precinct = parse_precinct_rows(
        fetch(PRIMARY_PRECINCT), primary_section_candidates
    )

    write_csv(
        os.path.join(county_dir, "20131008__tn__special__primary__county.csv"),
        ["county", "office", "district", "party", "candidate", "votes"],
        primary_county,
    )
    write_csv(
        os.path.join(county_dir, "20131008__tn__special__primary__precinct.csv"),
        ["county", "precinct", "office", "district", "party", "candidate", "votes"],
        primary_precinct,
    )

    # --- Special general (Nov 21, 2013) ---
    general_candidates = parse_general_totals(fetch(GENERAL_TOTALS))
    general_county = [
        ["Shelby", "State House", "91", party, candidate, votes]
        for candidate, party, votes in general_candidates
    ]
    general_section_candidates = {"general": general_candidates}
    general_precinct = parse_precinct_rows(
        fetch(GENERAL_PRECINCT), general_section_candidates
    )

    write_csv(
        os.path.join(county_dir, "20131121__tn__special__general__county.csv"),
        ["county", "office", "district", "party", "candidate", "votes"],
        general_county,
    )
    write_csv(
        os.path.join(county_dir, "20131121__tn__special__general__precinct.csv"),
        ["county", "precinct", "office", "district", "party", "candidate", "votes"],
        general_precinct,
    )


if __name__ == "__main__":
    main()