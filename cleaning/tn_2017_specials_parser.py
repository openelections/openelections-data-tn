"""
Parser for the 2017 Tennessee special elections, producing OpenElections CSVs
(county + precinct) for all four 2017 specials linked from
https://sos.tn.gov/elections/results#2017:

  20170427__tn__special__primary  -- Apr 27 House District 95 special primary
                                      (Republican + Democratic primaries; both
                                      are in ONE SoS PDF, merged with a party
                                      column)
  20170615__tn__special__general  -- Jun 15 House District 95 special general
  20171107__tn__special__primary  -- Nov 7  Senate District 17 special primary
                                      (Republican + Democratic primaries; both
                                      are in ONE SoS PDF, merged with a party
                                      column)
  20171219__tn__special__general  -- Dec 19 Senate District 17 special general

Sources are by-precinct PDFs on the OLDER s3 bucket
https://sos-tn-gov-files.s3.amazonaws.com/ . For each special, the primary's
Republican AND Democratic primaries are published together in a single PDF (as
consecutive "Special Republican Primary" / "Special Democratic Primary" sections)
and merged here with a `party` column; each general is a single PDF section.

All four use the multi-county "Layout B" format (same as the 2018/2019/2023
specials):
  Banner:     State of Tennessee
  Date:       <Month Day, Year>
  Section:    Special Republican Primary | Special Democratic Primary |
              Special State General
  Office:     Tennessee Senate District N [(unexpired term)]
              Tennessee House of Representatives District N [(unexpired term)]
  Candidates: N. Name            (primaries; party from the section header)
              N. Name - Party    (generals; party from the suffix)
  Column hdr: 1 2 3 ... N        (present only when >1 candidate)
  <County> County
   Precincts:
    <precinct name>  v1 v2 ... vN      (votes may contain commas, e.g. 1,512)
   County Totals:   v1 v2 ... vN
  ...
   DISTRICT TOTALS  v1 v2 ... vN

TWO wrinkles beyond the bare Layout B parser:
  (1) A candidate list may be laid out in TWO COLUMNS (e.g. the HD95 Republican
      primary lists candidates 1-5 on the left and 6-7 on the right of the same
      lines). The parser finds every "N. Name" on a candidate line (not just the
      first) and sorts the candidates by number so vote columns align.
  (2) A race/section may span multiple pages, and a single county may be SPLIT
      across a page break within a race. The parser is a state machine that
      re-reads (office, candidates) on each repeated header and treats the
      page-break footer ("<date> Page N of M") and next-page banner
      ("State of Tennessee") as sentinels ending the in-progress precinct block,
      so the date line (e.g. "November 7, 2017") is not mis-parsed as a precinct
      row -- its "7," and "2017" tokens both look like votes.

Conventions (standard OpenElections office names; integer votes; full party
names; precinct names verbatim with internal whitespace collapsed):
  "Tennessee Senate District N [(unexpired term)]"
                              -> "State Senate", district "N" (bare)
  "Tennessee House of Representatives District N [(unexpired term)]"
                              -> "State House", district "N" (bare)
Legislative specials use a BARE district number -- the "(unexpired term)"
suffix is for judicial/executive races, not legislative ones, so it is stripped
(matching the 2013/2015/2018/2019/2021/2023 special convention). Party from the
primary section header or each general candidate's ` - Party` suffix (so
generals carry `Independent`). "Absentee"/"Provisional" pseudo-precincts are
kept verbatim.

County totals are derived by summing precinct votes per (county, office,
district, party, candidate) and asserted against each precinct PDF's own
"County Totals:" lines, then verified against the separate by-county PDFs.

Requires `requests` (in the Pipfile) and `pdftotext` (poppler).
"""

import csv
import os
import re
import subprocess
import tempfile
from collections import defaultdict

import requests

BASE = "https://sos-tn-gov-files.s3.amazonaws.com"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2017")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

SECTION_RE = re.compile(
    r"^(Special Republican Primary|Special Democratic Primary|"
    r"Special State General Election|Special State General|"
    r"Republican Primary|Democratic Primary|State General)\s*$")
OFFICE_SENATE_RE = re.compile(
    r"^Tennessee Senate District (\d+)(?:\s*\(unexpired term\))?\s*$")
OFFICE_HOUSE_RE = re.compile(
    r"^Tennessee House of Representatives District (\d+)"
    r"(?:\s*\(unexpired term\))?\s*$")
DISTRICT_RE = re.compile(
    r"^(.+?)\s+District\s+(\d+(?:/\d+)?)\s*(\(unexpired term\))?\s*$")
CAND_START_RE = re.compile(r"(\d+)\.\s+")
COUNTY_RE = re.compile(r"^([A-Z][A-Za-z .]+) County\s*$")
PARTY_SUFFIX_RE = re.compile(
    r"^(.+?)\s+-\s+(Republican|Democratic|Independent|Libertarian|Green|"
    r"Constitution)\s*$")
VOTE_RE = re.compile(r"^\d[\d,]*$")
ALLDIGITS_RE = re.compile(r"^\d+$")
WHITESPACE_RE = re.compile(r"\s+")


def clean(s):
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def to_int(tok):
    return int(tok.replace(",", ""))


def norm_office(raw):
    s = raw.strip()
    m = OFFICE_SENATE_RE.match(s)
    if m:
        return ("State Senate", m.group(1))
    m = OFFICE_HOUSE_RE.match(s)
    if m:
        return ("State House", m.group(1))
    m = DISTRICT_RE.match(s)
    if m:
        office = m.group(1).strip()
        dist = m.group(2) + (" (unexpired term)" if m.group(3) else "")
        return (office, dist)
    return (s, "NA")


def split_candidate(raw, section):
    if section in ("Special Republican Primary", "Republican Primary"):
        return (raw.strip(), "Republican")
    if section in ("Special Democratic Primary", "Democratic Primary"):
        return (raw.strip(), "Democratic")
    m = PARTY_SUFFIX_RE.match(raw.strip())
    if m:
        return (m.group(1).strip(), m.group(2))
    return (raw.strip(), "")


def parse_candidate_entries(line):
    """Return [(num, name), ...] for every 'N. Name' on the line. Handles
    two-column candidate lists where two candidates share a line (e.g. the
    HD95 Republican primary: '1. ...   6. ...'). Names are the text between one
    candidate's 'N. ' and the next candidate's 'M. ' (or end of line)."""
    matches = list(CAND_START_RE.finditer(line))
    entries = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        name = clean(line[start:end])
        if name:
            entries.append((int(m.group(1)), name))
    return entries


def pdftotext_layout(pdf_bytes):
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
    """Parse a Layout B by-precinct PDF (possibly multi-section, multi-race,
    multi-page, with two-column candidate lists). Returns (precinct_rows,
    county_totals) where precinct_rows are [county, precinct, office, district,
    party, candidate, votes] and county_totals maps (county, office, district,
    party, candidate) -> votes from the PDFs' own "County Totals:" lines."""
    precinct_rows = []
    county_totals = {}
    section = None
    office = None
    district = None
    candidates = []          # list of (num, name, party)
    expect_office = False
    collecting_candidates = False
    in_precincts = False
    current_county = None
    for line in text.splitlines():
        s = line.strip()
        # Page-break sentinels: footer + next-page banner end any in-progress
        # precinct block so the date/banner lines aren't mis-parsed as precinct
        # rows (e.g. "November 7, 2017" -> "November" w/ votes [7, 2017]).
        if "Page " in s and " of " in s:
            in_precincts = False
            continue
        if s == "State of Tennessee":
            in_precincts = False
            continue
        m = SECTION_RE.match(s)
        if m:
            section = s
            office = None
            district = None
            candidates = []
            expect_office = True
            collecting_candidates = False
            in_precincts = False
            current_county = None
            continue
        if expect_office:
            if not s:
                continue
            office, district = norm_office(s)
            expect_office = False
            collecting_candidates = True
            continue
        if collecting_candidates:
            if not s:
                continue
            entries = parse_candidate_entries(line)
            if entries:
                for num, name in entries:
                    candidates.append((num,) + split_candidate(name, section))
                continue
            toks = s.split()
            if toks and all(ALLDIGITS_RE.match(t) for t in toks):
                continue  # column-header line
            # First non-candidate, non-column-header line starts the body:
            # finalize the ordered candidate list (sorted by candidate number
            # so vote columns align) and process this line as a body line.
            candidates.sort(key=lambda c: c[0])
            nums = [c[0] for c in candidates]
            if nums != list(range(1, len(candidates) + 1)):
                raise ValueError(
                    f"non-contiguous candidate numbers {nums} for "
                    f"{office!r} {district!r}")
            collecting_candidates = False
            # fall through to body handling for this line
        # body handling
        if not s:
            continue
        cm = COUNTY_RE.match(line)
        if cm:
            current_county = cm.group(1).strip()
            in_precincts = False
            continue
        if s == "Precincts:":
            in_precincts = True
            continue
        if s.startswith("County Totals:"):
            nums_str = s[len("County Totals:"):].split()
            if len(nums_str) == len(candidates):
                for k, (_num, name, party) in enumerate(candidates):
                    county_totals[(current_county, office, district, party,
                                   name)] = to_int(nums_str[k])
            in_precincts = False
            continue
        if s.startswith("DISTRICT TOTALS"):
            in_precincts = False
            continue
        if in_precincts and current_county and candidates:
            n = len(candidates)
            tokens = line.split()
            if len(tokens) < n + 1:
                continue
            vote_tokens = tokens[-n:]
            if not all(VOTE_RE.match(t) for t in vote_tokens):
                continue
            precinct = clean(" ".join(tokens[:-n]))
            for k, (_num, name, party) in enumerate(candidates):
                precinct_rows.append([current_county, precinct, office,
                                      district, party, name,
                                      to_int(vote_tokens[k])])
    return precinct_rows, county_totals


def build(election):
    """Build county + precinct rows for one election (which may combine
    several PDFs). Cross-checks derived county totals against the PDFs' own
    County Totals lines."""
    precinct_rows = []
    expected = {}
    for pdf_url in election["pdfs"]:
        resp = requests.get(pdf_url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        text = pdftotext_layout(resp.content)
        prows, ctotals = parse_pdf(text)
        precinct_rows.extend(prows)
        for k, v in ctotals.items():
            expected[k] = v
    derived = defaultdict(int)
    for r in precinct_rows:
        county, precinct, office, district, party, name, votes = r
        derived[(county, office, district, party, name)] += votes
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
        "name": "20170427__tn__special__primary",
        "pdfs": [f"{BASE}/StateTNHouse95PrimaryPrecincts.pdf"],
    },
    {
        "name": "20170615__tn__special__general",
        "pdfs": [f"{BASE}/TNH95GeneralPrecincts.pdf"],
    },
    {
        "name": "20171107__tn__special__primary",
        "pdfs": [f"{BASE}/TN%20Senate%2017%20Primary%20Precincts.pdf"],
    },
    {
        "name": "20171219__tn__special__general",
        "pdfs": [f"{BASE}/TN%20Senate%2017%20General%20Precinct.pdf"],
    },
]


def main():
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    for election in ELECTIONS:
        print(f"--- {election['name']} ---")
        county_rows, precinct_rows, _ = build(election)
        county_rows.sort(key=sort_key_county)
        precinct_rows.sort(key=sort_key_precinct)
        print(f"  county: {len(county_rows)} rows, precinct: "
              f"{len(precinct_rows)} rows")
        write_csv(os.path.join(out, f"{election['name']}__county.csv"),
                  COUNTY_HEADER, county_rows)
        write_csv(os.path.join(out, f"{election['name']}__precinct.csv"),
                  PRECINCT_HEADER, precinct_rows)


if __name__ == "__main__":
    main()