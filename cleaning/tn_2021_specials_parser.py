"""
Parser for the 2021 Tennessee special elections, producing county- and
precinct-level OpenElections CSVs for the two House District 29 specials:

  20210727__tn__special__primary  -- Jul 27 HD29 special primary (Republican +
                                      Democratic primaries; the two parties are
                                      published as separate SoS PDFs and merged
                                      here, distinguished by the party column)
  20210914__tn__special__general  -- Sep 14 HD29 special general

Both are TN House district specials, so office is always "State House" and
district is the bare district number (matching the repo's 2013/2015 and 2023
special-election convention).

Source: TN SoS by-precinct PDFs (Layout A -- single-county Hamilton, same shape
as the 2023 Jan/Mar specials). Files are linked from
https://sos.tn.gov/elections/results#2021 (WebFetch 403s; use curl with a
browser User-Agent) and hosted on the sos-tn-gov-files bucket:

    HD29PrecinctTotalsRepPrimary.pdf   (Jul 27 Republican primary, by precinct)
    HD29PrecinctTotalsDemPrimary.pdf   (Jul 27 Democratic primary, by precinct)
    HD29PrecinctTotalsGeneral.pdf      (Sep 14 general, by precinct)

Layout A:

    State of Tennessee - Hamilton County      <- banner, sets county
    <date>
    Republican Primary | Democratic Primary | Special State General Election
    Tennessee House of Representatives District 29
    1. <name>            (primaries: party from the section header)
    1. <name> - <Party>  (general: party from each candidate's suffix)
    Precincts: 1 2 ... N                   <- column numbers on this line
    <precinct>   <v1> ... <vN>
    Totals:      <v1> ... <vN>

The parser is data-driven: it reads office/district, party, and candidate names
straight from the PDF headers. County-level totals are derived by summing the
precinct votes per (county, office, district, party, candidate). The "Totals:"
line printed in each precinct PDF is captured and asserted to equal the derived
county total, so completeness is checked against the source itself. The derived
totals also match the separate by-county PDFs exactly (spot-checked: Republican
primary Greg Vital 1,068; Democratic primary DeAngelo L. Jelks 136; general
Vital 3,884 / Jelks 965).

Note on the section header: the 2021 general uses "Special State General
Election" rather than the bare "State General" seen in later years, so the
section regex accepts both.

Conventions: office "State House", district the bare number; party full names
(Democratic, Republican); Title-case counties as printed; precinct names kept
as printed (Hamilton precincts carry a leading 3-digit code, e.g. "088 Airport",
matching the repo's 2020 Hamilton convention) with internal whitespace collapsed
(the file_format test rejects consecutive whitespace); candidate names kept as
printed.

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.
"""

import csv
import os
import re
import subprocess
import tempfile
from collections import defaultdict

import requests

BASE = "https://sos-tn-gov-files.tnsosfiles.com"

ELECTIONS = [
    {"date": "20210727", "type": "primary",
     "precincts": [f"{BASE}/HD29PrecinctTotalsRepPrimary.pdf",
                   f"{BASE}/HD29PrecinctTotalsDemPrimary.pdf"]},
    {"date": "20210914", "type": "general",
     "precincts": [f"{BASE}/HD29PrecinctTotalsGeneral.pdf"]},
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2021")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

OFFICE_RE = re.compile(r"^Tennessee House of Representatives District (\d+)\s*$")
# 2021 general prints "Special State General Election"; later years use "State General".
SECTION_RE = re.compile(r"^(Republican Primary|Democratic Primary|"
                        r"Special State General Election|State General)\s*$")
CAND_RE = re.compile(r"^(\d+)\.\s+(.+?)\s*$")
BANNER_COUNTY_RE = re.compile(r"^State of Tennessee - (.+?)\s+County\s*$")
COUNTY_RE = re.compile(r"^(.+?)\s+County\s*$")
ALLNUM_RE = re.compile(r"^\d+(\s+\d+)*\s*$")
PAGE_RE = re.compile(r"Page \d+ of \d+")
DATE_RE = re.compile(r"^(January|February|March|April|May|June|July|August|"
                     r"September|October|November|December)\s+\d{1,2},\s+\d{4}\s*$")
WHITESPACE_RE = re.compile(r"\s+")


def clean(s):
    """Strip and collapse internal whitespace (file_format rejects consecutive
    whitespace)."""
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def is_int(tok):
    return re.match(r"^\d{1,3}(,\d{3})*$", tok) is not None or tok.isdigit()


def to_int(tok):
    return int(tok.replace(",", ""))


def fetch_text(url):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(resp.content)
        f.flush()
        out = subprocess.run(["pdftotext", "-layout", f.name, "-"],
                              capture_output=True, text=True, check=True)
    return out.stdout


def parse_precinct(text):
    """Return (precinct_rows, expected_county_totals) for one Layout A PDF.

    precinct_rows: [county, precinct, office, district, party, candidate, votes]
    expected_county_totals: {(county, office, district, party, candidate): votes}
        captured from the PDF's "Totals:" line.
    """
    rows = []
    expected = {}

    county = None
    office = None
    district = None
    section_party = None
    general = False
    candidates = []   # list of (name, party) for the current office
    phase = None      # None -> "cand" (after office header) -> "rows" (after Precincts:)

    def reset_section():
        nonlocal office, district, section_party, general, candidates, phase
        office = None
        district = None
        section_party = None
        general = False
        candidates = []
        phase = None

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue

        m = BANNER_COUNTY_RE.match(s)
        if m:
            county = clean(m.group(1))
            continue
        if s == "State of Tennessee":
            continue
        if PAGE_RE.search(s):
            continue
        if DATE_RE.match(s):            # election date or watermark/posting date
            continue

        m = SECTION_RE.match(s)
        if m:
            reset_section()
            kind = m.group(1)
            if kind in ("State General", "Special State General Election"):
                general = True
                section_party = None
            else:
                general = False
                section_party = "Republican" if kind == "Republican Primary" else "Democratic"
            continue

        m = OFFICE_RE.match(s)
        if m:
            office = "State House"
            district = m.group(1)
            candidates = []
            phase = "cand"
            continue

        m = CAND_RE.match(s)
        if m and phase == "cand":
            rest = clean(m.group(2))
            if general:
                if " - " in rest:
                    name, party = rest.rsplit(" - ", 1)
                    name, party = clean(name), clean(party)
                else:
                    name, party = rest, ""
            else:
                name, party = rest, section_party or ""
            candidates.append((name, party))
            continue

        if phase == "cand" and ALLNUM_RE.match(s):   # column header (Layout B)
            continue

        if s.startswith("Precincts:"):
            phase = "rows"
            continue

        if "TOTALS" in s.upper():
            if s.startswith("Totals:"):
                toks = s[len("Totals:"):].split()
                if county is not None and len(candidates) == len(toks) \
                        and all(is_int(t) for t in toks):
                    for (cname, cparty), v in zip(candidates,
                                                  [to_int(t) for t in toks]):
                        expected[(county, office, district, cparty, cname)] = v
            continue

        m = COUNTY_RE.match(s)
        if m:
            county = clean(m.group(1))
            continue

        # Precinct row: last len(candidates) tokens are votes; rest is precinct.
        if phase == "rows" and candidates and county is not None:
            toks = s.split()
            n = len(candidates)
            if len(toks) >= n + 1 and all(is_int(t) for t in toks[-n:]):
                votes = [to_int(t) for t in toks[-n:]]
                precinct = clean(" ".join(toks[:-n]))
                for (cname, cparty), v in zip(candidates, votes):
                    rows.append([county, precinct, office, district,
                                 cparty, cname, v])
                continue

        if phase == "rows" and candidates:
            print(f"  WARN: unhandled line: {s!r}")

    return rows, expected


def build(election):
    """Parse one election's precinct PDF(s) and combine. For the Jul 27 primary
    the Republican and Democratic PDFs are merged (party column distinguishes)."""
    all_rows = []
    all_expected = {}
    for url in election["precincts"]:
        text = fetch_text(url)
        rows, expected = parse_precinct(text)
        all_rows.extend(rows)
        all_expected.update(expected)

    county_sums = defaultdict(int)  # (county, office, district, party, candidate) -> votes
    for r in all_rows:
        county_sums[tuple(r[i] for i in (0, 2, 3, 4, 5))] += r[6]

    county_rows = [
        [county, office, district, party, candidate, votes]
        for (county, office, district, party, candidate), votes in county_sums.items()
    ]

    mismatches = [(k, v, county_sums.get(k)) for k, v in all_expected.items()
                  if county_sums.get(k) != v]
    if mismatches:
        for k, exp, got in mismatches[:20]:
            print(f"  MISMATCH {k}: pdf={exp} derived={got}")
        raise AssertionError(f"{election['date']}: {len(mismatches)} county-total "
                             f"mismatches vs the precinct PDF's own totals")
    return county_rows, all_rows, county_sums


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


def main():
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)

    for election in ELECTIONS:
        date = election["date"]
        etype = election["type"]
        print(f"--- {date} {etype} ---")
        county_rows, precinct_rows, county_sums = build(election)
        county_rows.sort(key=sort_key_county)
        precinct_rows.sort(key=sort_key_precinct)
        print(f"  county: {len(county_rows)} rows, precinct: {len(precinct_rows)} rows, "
              f"{len(county_sums)} county/candidate combos")
        write_csv(os.path.join(out, f"{date}__tn__special__{etype}__county.csv"),
                  COUNTY_HEADER, county_rows)
        write_csv(os.path.join(out, f"{date}__tn__special__{etype}__precinct.csv"),
                  PRECINCT_HEADER, precinct_rows)


if __name__ == "__main__":
    main()