"""
Parser for the 2011 Tennessee special elections, producing OpenElections CSVs
(county + precinct) for the three 2011 specials, sourced from the TN SoS results
page (https://sos.tn.gov/elections/results#2011):

  20110120__tn__special__primary  -- Jan 20 special primary:
        State Senate District 18 (Robertson + Sumner): Republican primary (6
        candidates) and Democratic primary (Ken Wilber); and State House
        District 98 (Shelby): Republican primary ("No Candidate Filed", zero
        votes) and Democratic primary (4 candidates).
  20110308__tn__special__general  -- Mar 8 special general:
        State Senate District 18 (Robertson + Sumner): Kerry Roberts (R) vs Ken
        Wilber (D); and State House District 98 (Shelby): Antonio '2 Shay'
        Parkinson (D) vs Write-In - Artie Smith.
  20111108__tn__special__general  -- Nov 8 special general:
        State Senate District 6 (Knox only): Becky Duncan Massey (R) vs
        Gloria S. Johnson (D).

The repo already had CSVs for each, in a non-standard convention: UPPERCASE
counties, single-letter party (R/D), mis-named county files (a "primary" file
with a blank precinct column instead of a proper __county file), a BROKEN Mar 8
county file (duplicate rows), and a Nov 8 file carrying a `__state_senate__6`
subtype. They are REGENERATED with standard OpenElections conventions; the Nov 8
`__state_senate__6` subtype is removed (legislative specials use no office
subtype, matching the 2013/2015/2017/2019/2021/2023 convention).

Source PDFs (the SoS "by Precinct" / "by County" PDFs, layout-preserving):
  Jan 20 (tnelections.tnsosfiles.com/sharetngov/archived/election/results/2011/):
    20110120SpecialSenate18Precinct.pdf / 20110120SpecialSenate18County.pdf
    20110120SpecialHouse98Precinct.pdf  / 20110120SpecialHouse98County.pdf
  Mar 8 (sos-tn-gov-files.s3.amazonaws.com):
    20110308StateCertPrecinctTotals.pdf / 20110308StateCertCountyTotals.pdf
  Nov 8 (sos-tn-gov-file.s3.amazonaws.com):
    TNSenatePrecinctTotals.pdf / TNSenateCountyTotals.pdf

PDF layout (the "by Precinct" PDFs):
  banner    "State of Tennessee -"          (multi-county; county set by the
                                             "<COUNTY> County" lines below)
            "State of Tennessee - KNOX County"  (single-county Nov 8; county
                                                 comes from the banner -- there
                                                 is no "<COUNTY> County" line)
  section   "Republican Primary" / "Democratic Primary" (party from the header)
            "Special General Election" / "General Election" (party from each
                                                             candidate suffix)
  office    "Tennessee Senate District 18"
            "Tennessee House of Representatives District 98"
  candidates "N . Name"            (primary: no suffix; party = section party)
            "N . Name - R"/"N . Name - D"  (general: single letter, NO parens)
            "N . Write-in - <name>"        (general write-in: empty party)
            listed one-per-line, OR in TWO columns when there are many
            candidates (e.g. "1 . Bryan Bondurant   6 . Jeff Stromatt").
  column hdr "1 2 3 ... N"        (all-numeric; skipped)
  county     "ROBERTSON County"   (multi-county only; absent for single-county)
  marker     " Precincts:"
  precincts  "<precinct name>  <vote cols>"   (vote cols = exactly N trailing
                                               numeric tokens; "Absentee" rows
                                               are included, matching the repo's
                                               2016/2020 convention)
  totals     " Totals:" / "DISTRICT TOTALS"

Conventions (standard OpenElections office names; title-case counties; integer
votes; full party names; bare legislative district number; precinct names
verbatim with internal whitespace collapsed):
  office  "State Senate" / "State House", district = the bare number (18/98/6).
  party   primary -> from the section header (Republican / Democratic);
          general -> from the candidate's " - R"/" - D" suffix (stripped from the
          name); write-ins ("Write-in - <name>") get an empty party and are kept
          verbatim with the prefix normalized to "Write-In".
  "No Candidate Filed" is kept verbatim (party from the section header),
          matching the 2013 special-primary convention.
  Zero-vote rows ARE included (the PDF lists every candidate per precinct,
  including "No Candidate Filed" and write-ins at 0), matching the 2013/2015
  legislative-special convention. County totals are the sum of the precinct
  rows per (county, office, district, party, candidate).

Requires `requests` (in the Pipfile) and the `pdftotext` CLI (poppler) for
layout extraction, which preserves the column spacing used to split precinct
names from their vote columns.
"""

import csv
import os
import re
import subprocess
import tempfile
from collections import defaultdict

import requests

BASE_TNE = ("https://tnelections.tnsosfiles.com/sharetngov/archived/"
            "election/results/2011")
BASE_S3 = "https://sos-tn-gov-files.s3.amazonaws.com"

# Jan 20 special primary (SD18 + HD98).
JAN_SD18_PRECINCT = f"{BASE_TNE}/20110120SpecialSenate18Precinct.pdf"
JAN_SD18_COUNTY = f"{BASE_TNE}/20110120SpecialSenate18County.pdf"
JAN_HD98_PRECINCT = f"{BASE_TNE}/20110120SpecialHouse98Precinct.pdf"
JAN_HD98_COUNTY = f"{BASE_TNE}/20110120SpecialHouse98County.pdf"
# Mar 8 special general (SD18 + HD98).
MAR_PRECINCT = f"{BASE_S3}/20110308StateCertPrecinctTotals.pdf"
MAR_COUNTY = f"{BASE_S3}/20110308StateCertCountyTotals.pdf"
# Nov 8 special general (SD6, Knox only).
NOV_PRECINCT = f"{BASE_S3}/TNSenatePrecinctTotals.pdf"
NOV_COUNTY = f"{BASE_S3}/TNSenateCountyTotals.pdf"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2011")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

WHITESPACE_RE = re.compile(r"\s+")
SECTION_RE = re.compile(
    r"^(Republican Primary|Democratic Primary|"
    r"Special General Election|General Election)\s*$")
OFFICE_RE = re.compile(
    r"^Tennessee (Senate|House of Representatives) District (\d+)\s*$")
COUNTY_RE = re.compile(r"^([A-Z][A-Z .]+) County\s*$")
CAND_MARK_RE = re.compile(r"(\d+)\s*\.\s+")          # "N . " candidate marker
PARTY_SUFFIX_RE = re.compile(r"^(.+?)\s+-\s+([A-Z])\s*$")   # "Name - R" (no parens)
NUMERIC_RE = re.compile(r"^[\d,]+$")

PARTY_LETTER = {"R": "Republican", "D": "Democratic", "C": "Constitution",
                "G": "Green", "I": "Independent", "L": "Libertarian"}


def clean(s):
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def norm_county(c):
    s = clean(c)
    if not s:
        return s
    parts = s.title().split()
    fixed = []
    for p in parts:
        low = p.lower()
        if low == "dekalb":
            p = "DeKalb"
        elif low.startswith("mc") and len(p) > 2:
            p = "Mc" + p[2].upper() + p[3:]
        fixed.append(p)
    return " ".join(fixed)


def parse_candidate_text(text, section_party):
    """Return (name, party) for one candidate's raw text (between "N . " and
    the next marker / end of line).

    General: "Name - R"/"Name - D" -> (Name, full party); "Write-in - X" ->
    ("Write-In - X", ""). Primary: bare name -> (name, section_party)."""
    name = clean(text)
    if name.lower().startswith("write-in"):
        # Write-in: normalize the prefix to "Write-In", keep the rest verbatim.
        rest = name[len("write-in"):].lstrip()
        rest = rest[1:].lstrip() if rest[:1] == "-" else rest
        return f"Write-In - {rest}".strip(), ""
    m = PARTY_SUFFIX_RE.match(name)
    if m:
        party = PARTY_LETTER.get(m.group(2), m.group(2))
        return clean(m.group(1)), party
    # Primary (no suffix) -> party from the section header.
    return name, section_party or ""


def parse_candidate_line(line, section_party):
    """Return [(num, name, party)] for every 'N . ...' on the line (handles the
    two-column candidate lists, e.g. '1 . Bryan Bondurant   6 . Jeff Stromatt')."""
    out = []
    matches = list(CAND_MARK_RE.finditer(line))
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(line)
        text = line[start:end]
        name, party = parse_candidate_text(text, section_party)
        if name:
            out.append((int(m.group(1)), name, party))
    return out


def pdftotext_layout(path):
    out = subprocess.run(["pdftotext", "-layout", path, "-"],
                         check=True, capture_output=True, text=True)
    return out.stdout


def fetch_pdf(url):
    resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as f:
        f.write(resp.content)
        return f.name


def parse_precinct_pdf(path):
    """Parse a "by Precinct" PDF into precinct rows
    [county, precinct, office, district, party, candidate, votes]."""
    text = pdftotext_layout(path)
    rows = []
    current_county = None
    section_party = None        # "Republican"/"Democratic" (primary) or "" (general)
    office = None
    district = None
    candidates = []             # list of (num, name, party)
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        # --- banner (top of each page) ---
        if s.startswith("State of Tennessee"):
            m = re.match(r"State of Tennessee\s*-\s*([A-Z]+)\s+County", s)
            if m:
                current_county = norm_county(m.group(1))
            # keep current_county (a mid-county page break must preserve it);
            # reset the race state so the repeated section/office/candidates are
            # re-collected fresh on the new page.
            office = None
            district = None
            candidates = []
            continue
        # --- section header ---
        m = SECTION_RE.match(s)
        if m:
            sec = m.group(1)
            section_party = ("Republican" if "Republican" in sec
                             else "Democratic" if "Democratic" in sec
                             else "")
            office = None
            district = None
            candidates = []
            continue
        # --- office line (only right after a section/banner reset) ---
        if office is None:
            m = OFFICE_RE.match(s)
            if m:
                office = "State Senate" if m.group(1) == "Senate" else "State House"
                district = m.group(2)
                candidates = []
                continue
        # --- county line (multi-county) ---
        m = COUNTY_RE.match(s)
        if m:
            current_county = norm_county(m.group(1))
            continue
        # --- " Precincts:" marker ---
        if s == "Precincts:" or s.startswith("Precincts:"):
            continue
        # --- candidate line (office known, not yet into precincts) ---
        if office is not None and CAND_MARK_RE.search(s):
            candidates.extend(parse_candidate_line(s, section_party))
            continue
        # --- end-of-race / totals rows ---
        if s.startswith("DISTRICT TOTALS"):
            office = None
            district = None
            candidates = []
            continue
        if s.startswith("Totals") or s.startswith("County Totals"):
            continue
        # --- precinct row ---
        if office is not None and candidates and current_county:
            n = len(candidates)
            tokens = s.split()
            if len(tokens) > n and all(NUMERIC_RE.match(t) for t in tokens[-n:]):
                pname = clean(" ".join(tokens[:-n]))
                if not pname or pname.startswith("Totals"):
                    continue
                cands = sorted(candidates, key=lambda c: c[0])
                votes = [int(t.replace(",", "")) for t in tokens[-n:]]
                if len(votes) != len(cands):
                    continue
                for (num, name, party), v in zip(cands, votes):
                    rows.append([current_county, pname, office, district,
                                 party if party else section_party or "",
                                 name, v])
    return rows


def sum_county(precinct_rows):
    """Sum precinct rows into county rows per (county, office, district, party,
    candidate)."""
    totals = defaultdict(int)
    for r in precinct_rows:
        county, precinct, office, district, party, name, votes = r
        totals[(county, office, district, party, name)] += votes
    return [[k[0], k[1], k[2], k[3], k[4], v] for k, v in totals.items()]


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


def build(name, precinct_urls):
    """Download + parse the given precinct PDFs, write <name>__county.csv and
    <name>__precinct.csv."""
    print(f"--- {name} ---")
    precinct_rows = []
    for url in precinct_urls:
        path = fetch_pdf(url)
        try:
            precinct_rows.extend(parse_precinct_pdf(path))
        finally:
            os.unlink(path)
    precinct_rows.sort(key=sort_key_precinct)
    county_rows = sum_county(precinct_rows)
    county_rows.sort(key=sort_key_county)
    print(f"  county: {len(county_rows)} rows, precinct: {len(precinct_rows)} rows")
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, f"{name}__county.csv"), COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, f"{name}__precinct.csv"), PRECINCT_HEADER,
              precinct_rows)


def main():
    build("20110120__tn__special__primary",
          [JAN_SD18_PRECINCT, JAN_HD98_PRECINCT])
    build("20110308__tn__special__general", [MAR_PRECINCT])
    build("20111108__tn__special__general", [NOV_PRECINCT])


if __name__ == "__main__":
    main()