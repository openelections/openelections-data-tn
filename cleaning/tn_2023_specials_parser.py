"""
Parser for the 2023 Tennessee special elections, producing county- and
precinct-level OpenElections CSVs for the seven TN House special elections
held in 2023:

  20230124__tn__special__primary  -- Jan 24, HD86 primary (Shelby)
  20230314__tn__special__general  -- Mar 14, HD86 general (Shelby)
  20230615__tn__special__primary  -- Jun 15, HD52 + HD86 primaries
  20230622__tn__special__primary  -- Jun 22, HD3 primary
  20230803__tn__special__primary  -- Aug 3, HD51 primary
  20230803__tn__special__general  -- Aug 3, HD3 + HD52 + HD86 general
  20230914__tn__special__general  -- Sep 14, HD51 general
                                       (source file is dated 20230913; the
                                       election was held Sep 14, 2023)

All seven are TN House district specials, so office is always "State House"
and district is the bare district number (matching the repo's 2013/2015
special-election convention, e.g. 2013/20131008__tn__special__primary).

Source: TN SoS by-precinct PDFs (one per election). Two PDF layouts appear:

  Layout A (Jan 24 primary, Mar 14 general -- single-county Shelby):
      State of Tennessee - Shelby County      <- banner, sets county
      <date>
      Republican Primary | Democratic Primary | State General
      Tennessee House of Representatives District 86
      1. <name>            (primaries: party from the header above)
      1. <name> - <Party>  (general: party from each candidate's suffix)
      Precincts: 1 2 ... N                   <- column numbers on this line
      <precinct>   <v1> ... <vN>
      Totals:      <v1> ... <vN>

  Layout B (Jun 15 onward -- multi-county):
      State of Tennessee                     <- banner (no county)
      <date>
      Republican Primary | Democratic Primary | State General
      Tennessee House of Representatives District N
      1. <name> / 1. <name> - <Party>
      1 2 ... N                              <- column header (separate line)
      <County> County                        <- county header (repeats)
       Precincts:                            <- bare marker
        <precinct>   <v1> ... <vN>
       County Totals: <v1> ... <vN>
      DISTRICT TOTALS  <v1> ... <vN>

The parser is data-driven: it reads office/district, party, and candidate
names straight from the PDF headers, so which districts/counties appear in
each file need not be hardcoded. County-level totals are derived by summing
the precinct votes per (county, office, district, party, candidate). The
"Totals:" / "County Totals:" lines printed in each precinct PDF are captured
and asserted to equal the derived county totals, so completeness is checked
against the source itself.

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.

Conventions: office "State House", district the bare number; party full names
(Democratic, Republican, Independent); "No Candidate Qualified" kept verbatim
(as the source prints it); Title-case counties as printed; precinct names as
printed with internal whitespace collapsed to single spaces (the file_format
test rejects consecutive whitespace); candidate names kept as printed, with
CSV quote-doubling for embedded quotes (e.g. Andrew "Rome" Withers).
"""

import csv
import os
import re
import subprocess
import tempfile
from collections import defaultdict

import requests

BASE = "https://sos-prod.tnsosgovfiles.com/s3fs-public/document"

# Jan 24 / Mar 14 use old-style names on the new bucket; Jun onward use dated names.
ELECTIONS = [
    {"date": "20230124", "type": "primary",
     "precinct": f"{BASE}/Primary%20TN%20House%2086%20Precinct%20Totals.pdf"},
    {"date": "20230314", "type": "general",
     "precinct": f"{BASE}/General%20TN%20House%2086%20Precinct%20Totals.pdf"},
    {"date": "20230615", "type": "primary",
     "precinct": f"{BASE}/20230615_PrimaryPrecinct.pdf"},
    {"date": "20230622", "type": "primary",
     "precinct": f"{BASE}/20230622_PrimaryPrecinct.pdf"},
    {"date": "20230803", "type": "primary",
     "precinct": f"{BASE}/20230803_PrimaryPrecinct.pdf"},
    {"date": "20230803", "type": "general",
     "precinct": f"{BASE}/20230803_GeneralPrecinct.pdf"},
    {"date": "20230914", "type": "general",
     "precinct": f"{BASE}/20230913_GeneralPrecinct.pdf"},
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2023")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

OFFICE_RE = re.compile(r"^Tennessee House of Representatives District (\d+)\s*$")
SECTION_RE = re.compile(r"^(Republican Primary|Democratic Primary|State General)\s*$")
CAND_RE = re.compile(r"^(\d+)\.\s+(.+?)\s*$")
BANNER_COUNTY_RE = re.compile(r"^State of Tennessee - (.+?) County\s*$")
COUNTY_RE = re.compile(r"^(.+?)\s+County\s*$")
ALLNUM_RE = re.compile(r"^\d+(\s+\d+)*\s*$")
PAGE_RE = re.compile(r"Page \d+ of \d+")
DATE_RE = re.compile(r"^(January|February|March|April|May|June|July|August|"
                     r"September|October|November|December)\s+\d{1,2},\s+\d{4}\s*$")
WHITESPACE_RE = re.compile(r"\s+")


def clean(s):
    """Strip and collapse internal whitespace (file_format rejects consecutive
    whitespace; some precinct names contain double spaces)."""
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def to_int(tok):
    return int(tok.replace(",", ""))


def is_int(tok):
    return re.match(r"^\d{1,3}(,\d{3})*$", tok) is not None or tok.isdigit()


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
    """Return (precinct_rows, expected_county_totals).

    precinct_rows: list of [county, precinct, office, district, party,
                           candidate, votes]
    expected_county_totals: {(county, office, district, party, candidate): votes}
                             captured from the PDF's "Totals:"/"County Totals:" lines.
    """
    rows = []
    expected = {}

    county = None          # current county (from banner or "X County" header)
    office = None
    district = None
    section_party = None   # set by primary header; None for general
    general = False        # True after a "State General" header
    candidates = []        # list of (name, party) for the current office
    phase = None           # None -> "cand" (after office header) -> "rows" (after Precincts:)

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

        # Banner with embedded county (Layout A): sets county.
        m = BANNER_COUNTY_RE.match(s)
        if m:
            county = clean(m.group(1))
            continue
        if s == "State of Tennessee":       # Layout B banner (no county)
            continue
        if PAGE_RE.search(s):              # page footer, e.g. "July 13, 2023  Page 1 of 1"
            continue
        if DATE_RE.match(s):                # election date or watermark dates
            continue

        # Section header: sets party (primary) or marks general.
        m = SECTION_RE.match(s)
        if m:
            reset_section()
            kind = m.group(1)
            if kind == "State General":
                general = True
                section_party = None
            else:
                general = False
                section_party = "Republican" if kind == "Republican Primary" else "Democratic"
            continue

        # Office header: reset candidates for this race, set office/district.
        m = OFFICE_RE.match(s)
        if m:
            office = "State House"
            district = m.group(1)
            candidates = []
            phase = "cand"
            continue

        # Candidate line: "N. Name" (primary) or "N. Name - Party" (general).
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

        # Column header (Layout B): a bare line of column numbers.
        if phase == "cand" and ALLNUM_RE.match(s):
            continue

        # "Precincts:" marker (Layout A: "Precincts: 1 2 ..."; Layout B: "Precincts:").
        if s.startswith("Precincts:") or s == "Precincts:":
            phase = "rows"
            continue

        # Totals lines.
        if "TOTALS" in s.upper():
            if s.startswith("Totals:") or s.startswith("County Totals:"):
                label = "County Totals:" if s.startswith("County Totals:") else "Totals:"
                toks = s[len(label):].split()
                if county is not None and len(candidates) == len(toks) \
                        and all(is_int(t) for t in toks):
                    votes = [to_int(t) for t in toks]
                    for (cname, cparty), v in zip(candidates, votes):
                        expected[(county, office, district, cparty, cname)] = v
            continue

        # County header (Layout B): "X County". Appears both before the first
        # "Precincts:" (while phase is "cand") and between county blocks, so do
        # not gate on phase. (Banner and "County Totals:" are handled above.)
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

        # Unrecognized non-empty line in "rows" phase: warn (helps catch a misparse).
        if phase == "rows" and candidates:
            print(f"  WARN: unhandled line: {s!r}")

    return rows, expected


def build(election):
    text = fetch_text(election["precinct"])
    rows, expected = parse_precinct(text)

    county_sums = defaultdict(int)  # (county, office, district, party, candidate) -> votes
    for r in rows:
        county_sums[tuple(r[i] for i in (0, 2, 3, 4, 5))] += r[6]

    county_rows = [
        [county, office, district, party, candidate, votes]
        for (county, office, district, party, candidate), votes in county_sums.items()
    ]

    # Completeness check: derived county totals must equal the PDF's own totals.
    mismatches = []
    for k, v in expected.items():
        if county_sums.get(k) != v:
            mismatches.append((k, v, county_sums.get(k)))
    if mismatches:
        for k, exp, got in mismatches[:20]:
            print(f"  MISMATCH {k}: pdf={exp} derived={got}")
        raise AssertionError(f"{election['date']}: {len(mismatches)} county-total mismatches "
                             f"vs the precinct PDF's own totals")
    # Every derived combination should have a matching expected total.
    missing = [k for k in county_sums if k not in expected]
    if missing:
        print(f"  NOTE: {len(missing)} derived combos with no PDF total line "
              f"(ok for races whose totals line was skipped): {missing[:5]}")

    return county_rows, rows, county_sums


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