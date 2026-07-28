"""
Parser for the 2019 Tennessee special elections, producing OpenElections CSVs
(county + precinct) for all six 2019 specials linked from
https://sos.tn.gov/elections/results#2019:

  20190124__tn__special__primary  -- Jan 24 Senate District 32 special primary
                                      (Republican + Democratic primaries merged)
  20190307__tn__special__primary  -- Mar 7  Senate District 22 special primary
                                      (Republican + Democratic primaries merged)
  20190312__tn__special__general  -- Mar 12 Senate District 32 special general
  20190423__tn__special__general  -- Apr 23 Senate District 22 special general
  20191105__tn__special__primary  -- Nov 5  House District 77 special primary
                                      (Republican + Democratic primaries merged)
  20191219__tn__special__general  -- Dec 19 House District 77 special general

Sources (WebFetch 403s; use curl) are by-precinct PDFs on the pre-2025 bucket
https://sos-tn-gov-files.tnsosfiles.com/  (the SoS results page links carry
?<token> query strings but the plain URLs without tokens work). Each primary's
Republican and Democratic results are published as SEPARATE PDFs and merged
here with a `party` column; each general is a single PDF.

Layout (the same "Layout B" multi-county format used by the 2023 Jun+ specials):
  Banner:     State of Tennessee
  Date:       <Month Day, Year>
  Section:    Republican Primary | Democratic Primary | State General
              (2021 used "Special State General Election"; accepted too)
  Office:     Tennessee Senate District N (unexpired term)
              Tennessee House of Representatives District N (unexpired term)
  Candidates: N. Name                  (primaries; party from the section header)
              N. Name - Party          (generals; party from the suffix)
  Column hdr: 1  2  3  ... N          (present only when >1 candidate)
  <County> County
   Precincts:
    <precinct name>  v1 v2 ... vN      (votes may contain commas, e.g. 1,512)
   County Totals:   v1 v2 ... vN
  ...
   DISTRICT TOTALS  v1 v2 ... vN

Conventions (standard OpenElections office names; matches the 2013/2015/2021/
2023 legislative-special convention of a BARE district number -- the
"(unexpired term)" suffix is for judicial/executive races, NOT legislative
specials, so it is dropped here):
  Tennessee Senate District N            -> office "State Senate",  district "N"
  Tennessee House of Representatives N    -> office "State House",   district "N"
Party: full names from the primary section header or each general candidate's
` - Party` suffix (so generals carry `Independent`). Precinct names kept verbatim
as printed (e.g. `Memphis 88-2`, `1-1 NE Covington`, `A1- Bonicord`) with
internal whitespace collapsed so file_format passes. Votes are integers.

County totals are derived by summing precinct votes per (county, candidate) and
asserted against each precinct PDF's own `County Totals:` and `DISTRICT TOTALS`
lines, then spot-checked against the separate by-county PDFs.

Requires `requests` (in the Pipfile) and `pdftotext` (poppler).
"""

import csv
import os
import re
import subprocess
import tempfile
from collections import defaultdict

import requests

BASE = "https://sos-tn-gov-files.tnsosfiles.com"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2019")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

SECTION_RE = re.compile(
    r"^(Republican Primary|Democratic Primary|State General|"
    r"Special State General Election)\s*$")
OFFICE_SENATE_RE = re.compile(
    r"^Tennessee Senate District (\d+)(?:\s*\(unexpired term\))?\s*$")
OFFICE_HOUSE_RE = re.compile(
    r"^Tennessee House of Representatives District (\d+)"
    r"(?:\s*\(unexpired term\))?\s*$")
CAND_RE = re.compile(r"^\s*(\d+)\.\s+(.+?)\s*$")
COUNTY_RE = re.compile(r"^([A-Z][A-Za-z .]+) County\s*$")
PARTY_SUFFIX_RE = re.compile(
    r"^(.+?)\s+-\s+(Republican|Democratic|Independent|Libertarian|Green|"
    r"Constitution)\s*$")
VOTE_RE = re.compile(r"^\d[\d,]*$")
WHITESPACE_RE = re.compile(r"\s+")


def clean(s):
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def to_int(tok):
    return int(tok.replace(",", ""))


def split_candidate(raw, section):
    """Return (name, party). Primaries take party from the section header;
    generals take it from the candidate's ` - Party` suffix."""
    if section == "Republican Primary":
        return (raw.strip(), "Republican")
    if section == "Democratic Primary":
        return (raw.strip(), "Democratic")
    m = PARTY_SUFFIX_RE.match(raw.strip())
    if m:
        return (m.group(1).strip(), m.group(2))
    return (raw.strip(), "")


def pdftotext_layout(pdf_bytes):
    """Run `pdftotext -layout` on the given PDF bytes; return the text."""
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(pdf_bytes)
        path = f.name
    try:
        out = subprocess.run(
            ["pdftotext", "-layout", path, "-"],
            check=True, capture_output=True, text=True)
        return out.stdout
    finally:
        os.unlink(path)


def parse_pdf(text):
    """Parse one by-precinct PDF. Returns (office, district, candidates,
    precinct_rows, county_totals) where candidates is [(name, party), ...],
    precinct_rows is [[county, precinct, office, district, party, candidate,
    votes], ...] and county_totals is {(county, name, party): votes} from the
    PDF's own `County Totals:` lines (for cross-checking)."""
    lines = text.splitlines()
    section = None
    office = None
    district = None
    cands_raw = []
    # Header block: consume up to the first "<County> County" line.
    start = 0
    for idx, line in enumerate(lines):
        s = line.strip()
        if SECTION_RE.match(s):
            section = s
            continue
        m = OFFICE_SENATE_RE.match(s)
        if m:
            office = "State Senate"
            district = m.group(1)
            continue
        m = OFFICE_HOUSE_RE.match(s)
        if m:
            office = "State House"
            district = m.group(1)
            continue
        if office and CAND_RE.match(line):
            cands_raw.append(CAND_RE.match(line).group(2).strip())
            continue
        if COUNTY_RE.match(line):
            start = idx
            break
    if office is None or section is None or not cands_raw:
        raise ValueError(
            f"header parse failed: office={office!r} section={section!r} "
            f"cands={cands_raw!r}")
    candidates = [split_candidate(c, section) for c in cands_raw]
    n = len(candidates)

    precinct_rows = []
    county_totals = {}      # (county, name, party) -> votes from "County Totals:"
    district_totals = {}    # (name, party) -> votes from "DISTRICT TOTALS"
    current_county = None
    in_precincts = False
    for line in lines[start:]:
        if not line.strip():
            continue
        m = COUNTY_RE.match(line)
        if m:
            current_county = m.group(1).strip()
            in_precincts = False
            continue
        s = line.strip()
        if s == "Precincts:":
            in_precincts = True
            continue
        if s.startswith("County Totals:"):
            nums = s[len("County Totals:"):].split()
            if len(nums) != n:
                raise ValueError(
                    f"County Totals has {len(nums)} cols, expected {n}: {s!r}")
            for k, (name, party) in enumerate(candidates):
                county_totals[(current_county, name, party)] = to_int(nums[k])
            in_precincts = False
            continue
        if s.startswith("DISTRICT TOTALS"):
            nums = s[len("DISTRICT TOTALS"):].split()
            if len(nums) == n:
                for k, (name, party) in enumerate(candidates):
                    district_totals[(name, party)] = to_int(nums[k])
            in_precincts = False
            continue
        if in_precincts and current_county:
            tokens = line.split()
            if len(tokens) < n + 1:
                continue
            vote_tokens = tokens[-n:]
            if not all(VOTE_RE.match(t) for t in vote_tokens):
                continue
            precinct = clean(" ".join(tokens[:-n]))
            votes = [to_int(t) for t in vote_tokens]
            for k, (name, party) in enumerate(candidates):
                precinct_rows.append([current_county, precinct, office,
                                      district, party, name, votes[k]])
    return office, district, candidates, precinct_rows, county_totals


def build(election):
    """Build county + precinct rows for one election (which may combine
    several PDFs, e.g. a primary's Rep+Dem PDFs). Returns (precinct_rows,
    expected_county). `expected_county` maps (county, office, district, party,
    candidate) -> votes from the PDFs' own County Totals lines."""
    precinct_rows = []
    expected = {}
    for pdf_url in election["precincts"]:
        resp = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = pdftotext_layout(resp.content)
        office, district, candidates, prows, ctotals = parse_pdf(text)
        precinct_rows.extend(prows)
        for (county, name, party), v in ctotals.items():
            expected[(county, office, district, party, name)] = v
    # Derive county totals by summing precincts.
    derived = defaultdict(int)
    for r in precinct_rows:
        county, precinct, office, district, party, name, votes = r
        derived[(county, office, district, party, name)] += votes
    # Cross-check derived vs the PDFs' own County Totals lines.
    mismatches = []
    for k, v in derived.items():
        if k in expected and expected[k] != v:
            mismatches.append((k, v, expected[k]))
    for k, v in expected.items():
        if k not in derived:
            mismatches.append((k, "MISSING", v))
    if mismatches:
        detail = "\n  ".join(
            f"{k}: derived={dv} expected={ev}" for k, dv, ev in mismatches[:20])
        raise AssertionError(f"county-total mismatch in {election['name']}:\n  "
                            f"{detail}")
    county_rows = [[k[0], k[1], k[2], k[3], k[4], v]
                   for k, v in derived.items()]
    return county_rows, precinct_rows, derived


def sort_key_county(row):
    return (row[0], row[2], row[3], row[1], row[4])


def sort_key_precinct(row):
    return (row[0], row[1], row[2], row[3], row[4], row[5])


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  Wrote {path} ({len(rows)} rows)")


ELECTIONS = [
    {
        "name": "20190124__tn__special__primary",
        "type": "primary",
        "precincts": [
            f"{BASE}/D32%20Republican%20Primary%20Precinct%20Totals.pdf",
            f"{BASE}/D32%20Democratic%20Primary%20Precinct%20Totals.pdf",
        ],
    },
    {
        "name": "20190307__tn__special__primary",
        "type": "primary",
        "precincts": [
            f"{BASE}/D22%20Republican%20Primary%20Precinct%20Totals.pdf",
            f"{BASE}/D22%20Democratic%20Primary%20Precinct%20Totals.pdf",
        ],
    },
    {
        "name": "20190312__tn__special__general",
        "type": "general",
        "precincts": [
            f"{BASE}/D32%20General%20Precinct%20Totals.pdf",
        ],
    },
    {
        "name": "20190423__tn__special__general",
        "type": "general",
        "precincts": [
            f"{BASE}/D22%20General%20Precinct%20Totals.pdf",
        ],
    },
    {
        "name": "20191105__tn__special__primary",
        "type": "primary",
        "precincts": [
            f"{BASE}/2019%20D77%20Republican%20Primary%20Precinct%20Totals.pdf",
            f"{BASE}/2019%20D77%20Democratic%20Primary%20Precinct%20Totals.pdf",
        ],
    },
    {
        "name": "20191219__tn__special__general",
        "type": "general",
        "precincts": [
            f"{BASE}/2019%20D77%20General%20Precinct%20Totals.pdf",
        ],
    },
]


def main():
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    for election in ELECTIONS:
        print(f"--- {election['name']} ({election['type']}) ---")
        county_rows, precinct_rows, derived = build(election)
        county_rows.sort(key=sort_key_county)
        precinct_rows.sort(key=sort_key_precinct)
        print(f"  county: {len(county_rows)} rows, precinct: "
              f"{len(precinct_rows)} rows")
        suffix = election["type"]  # "primary" or "general"
        write_csv(os.path.join(out, f"{election['name']}__county.csv"),
                  COUNTY_HEADER, county_rows)
        write_csv(os.path.join(out, f"{election['name']}__precinct.csv"),
                  PRECINCT_HEADER, precinct_rows)


if __name__ == "__main__":
    main()