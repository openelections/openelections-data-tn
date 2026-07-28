"""
Parser for the 2006 Tennessee elections, producing OpenElections CSVs (county
+ precinct) for all three 2006 elections, sourced from the TN SoS results page
(https://sos.tn.gov/elections/results#2006):

  20060112__tn__special__general  -- Jan 12 HD22 special general (Sally Love vs
        Eric Watson; Bradley/Meigs/Polk). COUNTY ONLY -- the SoS publishes only
        a by-county PDF for this special (no precinct breakdown), so only the
        county CSV is produced. The PDF is dated "January 12, 2006" (the old
        repo file was misnamed 20060111). The old repo file used the non-standard
        "...__special__general__house__22" name; the "__house__22" office subtype
        is DROPPED to match the repo's special-election convention
        (2013/2015/2023 specials use bare "__special__general").
  20060803__tn__primary  -- Aug 3 primary: Democratic + Republican primaries for
        Governor / U.S. Senate / U.S. House / State Senate / State House / State
        Executive Committeeman / State Executive Committeewoman. (The Aug ballot
        also had judicial contests + retention, but those are published ONLY as
        by-county PDFs -- no by-precinct source -- so they are EXCLUDED, matching
        the 2008 Aug rationale and keeping county = sum of precincts.)
  20061107__tn__general  -- Nov 7 general: Governor, U.S. Senate, U.S. House,
        State Senate, State House, and Constitutional Amendments 1 & 2 (marriage
        and property-tax-relief amendments).

The repo already had a few 2006 files in a non-standard convention: a
"__house__22" special, UPPERCASE/verbose office names, float votes, and
single-letter parties. This parser REGENERATES them with standard OpenElections
conventions and ADDS the missing county files (and the missing offices).

Source: the SoS by-precinct PDFs on
https://tnelections.tnsosfiles.com/sharetngov/archived/election/results/
(``2006-11`` for Nov, ``2006-08`` for Aug, ``SpecialElections`` for the HD22
special). 2006 is PDF-only (no "All by Precinct" workbook, unlike 2008+). All
parsing uses ``pdftotext -layout``.

By-precinct layout (Nov general offices + Aug partisan primaries -- "Layout A"):
      <date>
      General Election | Democratic Primary | Republican Primary | Special General
      <office>                                   <- Governor / United States Senate /
                                                   U.S. House of Representatives District N /
                                                   Tennessee Senate District N /
                                                   Tennessee House of Representatives District N /
                                                   Tennessee House District N (HD22) /
                                                   State Executive Committeeman District N /
                                                   State Executive Committeewoman District N
      N . <name> - <X>      (Nov general: dot + no-parens party suffix D/R/I)
      N   <name>            (Aug primary: no dot, no suffix; party from the header)
      N . <name> - (<X>)    (HD22 special: dot + PARENS party suffix)
      [two-column when many candidates: "1 . A - D    6 . B - I"]
      1 2 ... N                                 <- column header (bare numbers)
      COUNTY: <UPPER>
         <precinct>   <v1> ... <vN>             <- indented precinct rows
      <UPPER>   <v1> ... <vN>                   <- flush-left county-total row (SKIP)
      Absentee / Early Voting rows are precincts (INCLUDED).
      DISTRICT TOTALS  <v1> ... <vN>            <- (SKIP)
      [page break reprints date/section/office/candidates/column header]
      [multi-district PDFs repeat the office block per district]

Constitutional-amendment by-precinct layout (special 4-column side-by-side):
      <date>
      General Election
      Constitutional Amendment Questions
      Constitutional Amendment # 1 / # 2        <- prose preamble (SKIP)
      <long amendment text lines>               <- (SKIP)
      1 Constitutional Amendment #1             <- candidate-list lines
      2 Constitutional Amendment #2
      1 - Yes   1 - No   2 - Yes   2 - No       <- 4 column header
      <UPPER>                                   <- bare county header (no "COUNTY:")
         <precinct>  <y1> <n1> <y2> <n2>        <- 4 vote columns: am1-Yes, am1-No,
                                                   am2-Yes, am2-No
      <UPPER>  <y1> <n1> <y2> <n2>              <- county-total row (SKIP)
   Each amendment -> office "Constitutional Amendment N", district "NA",
   party "NA", candidate "Yes"/"No" (matching the repo's 2014 convention).

County totals are derived by summing the precinct votes per
(county, office, district, party, candidate). The by-county PDFs are parsed
independently and compared cell-by-cell for verification.

Conventions (standard OpenElections office names; title-case counties with
Mc/DeKalb fixes; integer votes; full party names; bare legislative district
number; precinct names verbatim with internal whitespace collapsed):
  office  "<base> District N" -> OFFICE_MAP[base], district N
          "Governor" -> Governor/NA; "United States Senate" -> U.S. Senate/NA
  party   Nov general + HD22 -> from each candidate's " - X" (no-parens) or
          " - (X)" (parens) suffix; Aug primaries -> from the section header
          (Democratic/Republican); amendments -> "NA".
  write-ins  "Write-in - <name> [- X]" -> normalized to "Write-In - <name>",
          party from the suffix (general/special) or the section (primary).

Zero-vote rows ARE included (the source lists every candidate per precinct,
including 0-vote independents and write-ins), matching the dominant regenerated
convention (2010/2014/2016/2018/2024 all include 0-vote rows).

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.
"""

import csv
import os
import re
import subprocess
import tempfile
from collections import defaultdict

import requests

BASE = "https://tnelections.tnsosfiles.com/sharetngov/archived/election/results"

# HD22 special (by-county only). Note: specials live under
# .../archived/election/SpecialElections/ (no "results/" segment, unlike the
# Nov/Aug PDFs).
HD22_URL = "https://tnelections.tnsosfiles.com/sharetngov/archived/election/SpecialElections/200601TH22.pdf"

# Nov 7 general by-precinct PDFs.
NOV_PCT = [
    f"{BASE}/2006-11/GovPct.pdf",          # Governor
    f"{BASE}/2006-11/USSPct.pdf",          # U.S. Senate
    f"{BASE}/2006-11/USHPct.pdf",          # U.S. House (multi-district)
    f"{BASE}/2006-11/TSPct.pdf",           # State Senate (multi-district)
    f"{BASE}/2006-11/TH1-33Pct.pdf",       # State House 1-33
    f"{BASE}/2006-11/TH34-66Pct.pdf",      # State House 34-66
    f"{BASE}/2006-11/TH67-99Pct.pdf",      # State House 67-99
]
NOV_AMEND_PCT = f"{BASE}/2006-11/RptPctCon1andCon2Prec.pdf"

# Aug 3 primary by-precinct PDFs (each PDF is one party's races; the section
# header supplies the party).
AUG_PCT = [
    f"{BASE}/2006-08/DemGovPct.pdf", f"{BASE}/2006-08/RepGovPct.pdf",
    f"{BASE}/2006-08/DemUSPct.pdf", f"{BASE}/2006-08/RepUSPct.pdf",
    f"{BASE}/2006-08/DemUHPct.pdf", f"{BASE}/2006-08/RepUHPct.pdf",
    f"{BASE}/2006-08/DemTSPct.pdf", f"{BASE}/2006-08/RepTSPct.pdf",
    f"{BASE}/2006-08/DemPctTH133.pdf", f"{BASE}/2006-08/RepPctTH133.pdf",
    f"{BASE}/2006-08/DemPctTH3466.pdf", f"{BASE}/2006-08/RepPctTH3466.pdf",
    f"{BASE}/2006-08/DemPctTH6799.pdf", f"{BASE}/2006-08/RepPctTH6799.pdf",
    f"{BASE}/2006-08/DemXMPct.pdf", f"{BASE}/2006-08/RepXMPct.pdf",
    f"{BASE}/2006-08/DemXWPct.pdf", f"{BASE}/2006-08/RepXWPct.pdf",
]

# By-county PDFs (used for cell-by-cell verification).
NOV_CTY = [
    ("Governor", f"{BASE}/2006-11/RptNovGov.pdf"),
    ("U.S. Senate", f"{BASE}/2006-11/en4uss.pdf"),
    ("U.S. House", f"{BASE}/2006-11/en5ush.pdf"),
    ("State Senate", f"{BASE}/2006-11/en6ts.pdf"),
    ("State House 1-33", f"{BASE}/2006-11/en7th133.pdf"),
    ("State House 34-66", f"{BASE}/2006-11/en7th34-66.pdf"),
    ("State House 67-99", f"{BASE}/2006-11/en7th67-99.pdf"),
]
NOV_AMEND_CTY = f"{BASE}/2006-11/RptCtyCon1andCon2.pdf"
AUG_CTY = [
    ("Dem Governor", f"{BASE}/2006-08/governordem.pdf"),
    ("Rep Governor", f"{BASE}/2006-08/GovernorRep.pdf"),
    ("Dem U.S. Senate", f"{BASE}/2006-08/DemUss.pdf"),
    ("Rep U.S. Senate", f"{BASE}/2006-08/repuss.pdf"),
    ("Dem U.S. House", f"{BASE}/2006-08/demush.pdf"),
    ("Rep U.S. House", f"{BASE}/2006-08/repush.pdf"),
    ("Dem State Senate", f"{BASE}/2006-08/demts.pdf"),
    ("Rep State Senate", f"{BASE}/2006-08/repts.pdf"),
    ("Dem State House 1-33", f"{BASE}/2006-08/demth133.pdf"),
    ("Dem State House 34-66", f"{BASE}/2006-08/demth3466.pdf"),
    ("Dem State House 67-99", f"{BASE}/2006-08/demth6799.pdf"),
    ("Rep State House 1-33", f"{BASE}/2006-08/repth133.pdf"),
    ("Rep State House 34-66", f"{BASE}/2006-08/repth3466.pdf"),
    ("Rep State House 67-99", f"{BASE}/2006-08/repth6799.pdf"),
    ("Dem Committeeman", f"{BASE}/2006-08/DemXM.pdf"),
    ("Rep Committeeman", f"{BASE}/2006-08/RepXM.pdf"),
    ("Dem Committeewoman", f"{BASE}/2006-08/demxw.pdf"),
    ("Rep Committeewoman", f"{BASE}/2006-08/RepXW.pdf"),
]

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2006")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

WHITESPACE_RE = re.compile(r"\s+")
# Party suffixes: parens "Name - (D)" (HD22) and no-parens "Name - D" (Nov gen).
PARENS_SUFFIX_RE = re.compile(r"^(.+?)\s+-\s+\(([A-Z])\)\s*$")
NO_PARENS_SUFFIX_RE = re.compile(r"^(.+?)\s+-\s+([A-Z])\s*$")
PARTY_LETTER = {"R": "Republican", "D": "Democratic", "C": "Constitution",
                "G": "Green", "I": "Independent", "L": "Libertarian"}

OFFICE_MAP = {
    "U.S. House of Representatives": "U.S. House",
    "Tennessee Senate": "State Senate",
    "Tennessee House of Representatives": "State House",
    "Tennessee House": "State House",          # HD22 special short form
    "State Executive Committeeman": "State Executive Committeeman",
    "State Executive Committeewoman": "State Executive Committeewoman",
}
SINGLE_OFFICE = {"Governor": ("Governor", "NA"),
                 "United States Senate": ("U.S. Senate", "NA")}
OFFICE_DIST_RE = re.compile(r"^(.+?)\s+District\s+(\d+)\s*$")

SECTION_RE = re.compile(
    r"^(General Election|Democratic Primary|Republican Primary|"
    r"Special General Election)\s*$")
# Candidate marker: "N . Name" (Nov/HD22, dot) or "N   Name" (Aug, no dot).
# The lookahead requires the name to start with a letter (so a bare column
# header "1 2 3" and numeric precinct prefixes don't match). Applied only in
# the candidate-list phase, and via finditer to split two-column lines.
CAND_MARK_RE = re.compile(r"(\d+)\s*\.?\s+(?=[A-Za-z\"'])")
ALLNUM_RE = re.compile(r"^\d+(\s+\d+)*\s*$")
DATE_RE = re.compile(r"^(January|February|March|April|May|June|July|August|"
                     r"September|October|November|December)\s+\d{1,2},\s+\d{4}\s*$")
FOOTER_RE = re.compile(r"\d{1,2}-\w+-\d{2}|Page \d+ of \d+")
BARE_COUNTY_RE = re.compile(r"^[A-Z][A-Z ]+$")   # "ANDERSON", "VAN BUREN"

# Amendments.
AMEND_LIST_RE = re.compile(r"^(\d+)\s+Constitutional Amendment\s+#\s*(\d+)\s*$")
AMEND_COL_RE = re.compile(r"(\d+)\s*-\s*(Yes|No)", re.IGNORECASE)

# By-county county-row detection (upper county name + numeric vote cols).
COUNTY_ROW_RE = re.compile(r"^([A-Z][A-Z .]+)\s+(.+)$")
NUMERIC_RE = re.compile(r"^[\d,]+$")


def clean(s):
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def norm_county(c):
    s = clean(c)
    if not s:
        return s
    # One source typo: a "COUNTY: GREEN" header in the Aug Dem TH 1-33 PDF
    # (Greene County is spelled correctly elsewhere in the same PDF).
    if s.lower() == "green":
        return "Greene"
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


def to_int(tok):
    return int(tok.replace(",", ""))


def is_int(tok):
    return bool(NUMERIC_RE.match(tok))


def pdftotext_layout(path):
    out = subprocess.run(["pdftotext", "-layout", path, "-"],
                         capture_output=True, text=True, check=True)
    return out.stdout


def fetch_text(url):
    if url.startswith("http://") or url.startswith("https://"):
        resp = requests.get(url, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
            f.write(resp.content)
            f.flush()
            return pdftotext_layout(f.name)
    return pdftotext_layout(url)      # local path (used in tests)


def parse_candidate(text, section_party):
    """Return (name, party) from a candidate-name cell + the section party.

    Handles a trailing " - (X)" (parens) or " - X" (no-parens) party suffix;
    write-ins are normalized to "Write-In - <name>". Bare names take the
    section party (primary) or "" (general)."""
    name = clean(text)
    if not name:
        return "", ""
    if name.lower().startswith("write-in"):
        rest = name[len("write-in"):].lstrip()
        if rest[:1] == "-":
            rest = rest[1:].lstrip()
        # The by-precinct PDFs label write-in candidates "Write-In: Name"
        # (colon) as well as "Write-in - Name" (dash); strip a leading colon
        # so the name normalizes to "Write-In - Name" (matching the 2014+
        # convention "Write-In - Paula Sedgwick").
        if rest[:1] == ":":
            rest = rest[1:].lstrip()
        party = section_party or ""
        m = PARENS_SUFFIX_RE.match(rest)
        if m and m.group(2) in PARTY_LETTER:
            rest, party = clean(m.group(1)), PARTY_LETTER[m.group(2)]
        else:
            m = NO_PARENS_SUFFIX_RE.match(rest)
            if m and m.group(2) in PARTY_LETTER:
                rest, party = clean(m.group(1)), PARTY_LETTER[m.group(2)]
        return f"Write-In - {rest}".strip(), party
    m = PARENS_SUFFIX_RE.match(name)
    if m and m.group(2) in PARTY_LETTER:
        return clean(m.group(1)), PARTY_LETTER[m.group(2)]
    m = NO_PARENS_SUFFIX_RE.match(name)
    if m and m.group(2) in PARTY_LETTER:
        return clean(m.group(1)), PARTY_LETTER[m.group(2)]
    return name, section_party or ""


def norm_office(s):
    """Return (office, district) for an office line, else None."""
    raw = clean(s)
    m = OFFICE_DIST_RE.match(raw)
    if m and m.group(1) in OFFICE_MAP:
        return OFFICE_MAP[m.group(1)], m.group(2)
    if raw in SINGLE_OFFICE:
        return SINGLE_OFFICE[raw]
    return None


def _parse_candidate_line(line, section_party):
    """Split a (possibly two-column) candidate line into (num, name, party)."""
    out = []
    marks = list(CAND_MARK_RE.finditer(line))
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(line)
        text = clean(line[start:end])
        if not text:
            continue
        name, party = parse_candidate(text, section_party)
        if name:
            out.append((int(m.group(1)), name, party))
    return out


def parse_precinct_pdf(path_or_url):
    """Parse a Layout-A by-precinct PDF. Returns (precinct_rows, expected).

    precinct_rows: [county, precinct, office, district, party, candidate, votes].
    expected: {(county, office, district, party, candidate): votes} captured
              from the PDF's own per-county total rows -- the by-precinct PDFs
              are final results, so these are an authoritative completeness
              check (some Aug by-county PDFs are unofficial snapshots)."""
    text = fetch_text(path_or_url)
    rows = []
    expected = {}
    county = office = district = section_party = None
    candidates = []      # list of (num, name, party)
    phase = None         # None -> "cand" -> "rows"

    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if DATE_RE.match(s) or FOOTER_RE.search(s):
            continue
        m = SECTION_RE.match(s)
        if m:
            kind = m.group(1)
            if "Republican" in kind:
                section_party = "Republican"
            elif "Democratic" in kind:
                section_party = "Democratic"
            else:
                section_party = None      # general / special general
            office = district = None
            candidates = []
            phase = None
            continue
        if s.startswith("COUNTY:"):
            county = norm_county(s[len("COUNTY:"):])
            continue
        if "TOTALS" in s.upper():
            continue
        od = norm_office(s)
        if od is not None and not CAND_MARK_RE.search(s):
            office, district = od
            candidates = []
            phase = "cand"
            continue
        if phase == "cand" and CAND_MARK_RE.search(s):
            candidates.extend(_parse_candidate_line(s, section_party))
            continue
        # Column header (bare "1 2 ... N"): only in the candidate-list phase.
        # In rows phase an all-numeric line is a numeric precinct name + votes
        # (e.g. Crockett Co. precincts "01".."12"); the row logic's ">= n+1"
        # tokens check excludes the n-token column-header reprints.
        if ALLNUM_RE.match(s) and phase == "cand":
            candidates.sort(key=lambda c: c[0])
            phase = "rows"
            continue
        if phase == "rows" and candidates and county is not None:
            n = len(candidates)
            toks = s.split()
            if len(toks) >= n + 1 and all(is_int(t) for t in toks[-n:]):
                name = clean(" ".join(toks[:-n]))
                votes = [to_int(t) for t in toks[-n:]]
                # The flush-left/upper county-total row (name is the
                # all-uppercase county name) is captured for the completeness
                # check and NOT emitted as a precinct; precinct names are
                # mixed-case.
                if name == name.upper() and any(c.isalpha() for c in name) \
                        and norm_county(name).upper() == county.upper():
                    for (_num, cname, cparty), v in zip(candidates, votes):
                        expected[(county, office, district, cparty, cname)] = v
                    continue
                # A "Write-in" row is not a real precinct. Two cases:
                #  (a) write-in-only races (every candidate is a write-in):
                #      the by-precinct PDF reports only a per-county write-in
                #      total -- keep those votes under a "Write-in" pseudo-
                #      precinct so county == sum(precinct) still holds.
                #  (b) regular races with a trailing anonymous "Write-in 0 0"
                #      row: the columns are the named candidates, not write-ins,
                #      so skip them (no anonymous write-in name, 0 votes).
                is_writein_row = name.lower() == "write-in"
                for (_num, cname, cparty), v in zip(candidates, votes):
                    if is_writein_row and not cname.lower().startswith("write-in"):
                        continue
                    rows.append([county, name, office, district,
                                 cparty, cname, v])
                continue
    return rows, expected


def parse_amendments_pdf(path_or_url, by_county=False):
    """Parse the constitutional-amendment PDF.

    by_county=False -> (precinct_rows, expected) where precinct_rows are
                      [county, precinct, office, district, party, candidate,
                      votes] (7 fields) and expected is the PDF's own per-county
                      total rows for the completeness check.
    by_county=True  -> (county_rows, {}) where county_rows are [county, office,
                      district, party, candidate, votes] (6 fields)."""
    text = fetch_text(path_or_url)
    rows = []
    expected = {}
    county = None
    cols = None         # ordered list of (amendnum, "Yes"/"No") per vote column
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if DATE_RE.match(s) or FOOTER_RE.search(s):
            continue
        if s in ("General Election", "Constitutional Amendment Questions"):
            continue
        if AMEND_LIST_RE.match(s):
            continue                      # candidate-list line
        cms = AMEND_COL_RE.findall(s)
        if len(cms) >= 2:
            cols = [(int(num), yn.capitalize()) for num, yn in cms]
            continue                      # (reprinted) column header
        if s.startswith("STATEWIDE TOTALS") or s.startswith("DISTRICT TOTALS"):
            continue
        if not by_county and BARE_COUNTY_RE.match(s) and cols:
            county = norm_county(s)
            continue                      # bare county header ("VAN BUREN")
        if not cols:
            continue                      # still in the prose preamble
        ncol = len(cols)
        toks = s.split()
        if len(toks) < ncol + 1 or not all(is_int(t) for t in toks[-ncol:]):
            continue
        name = clean(" ".join(toks[:-ncol]))
        vals = [to_int(t) for t in toks[-ncol:]]
        if by_county:
            cname = norm_county(name)
            for (amendnum, yn), v in zip(cols, vals):
                rows.append([cname, f"Constitutional Amendment {amendnum}",
                             "NA", "NA", yn, v])
        else:
            # Capture (and skip) the county-total row; emit precinct rows.
            if name == name.upper() and any(c.isalpha() for c in name) \
                    and norm_county(name).upper() == county.upper():
                for (amendnum, yn), v in zip(cols, vals):
                    expected[(county, f"Constitutional Amendment {amendnum}",
                              "NA", "NA", yn)] = v
                continue
            for (amendnum, yn), v in zip(cols, vals):
                rows.append([county, name,
                             f"Constitutional Amendment {amendnum}",
                             "NA", "NA", yn, v])
    return rows, expected


def is_county_row(s):
    m = COUNTY_ROW_RE.match(s)
    if not m:
        return False
    toks = m.group(2).split()
    return bool(toks) and all(NUMERIC_RE.match(t) for t in toks)


def parse_county_pdf(path_or_url):
    """Parse a by-county PDF. Returns (county_totals, race_totals) dicts keyed
    by (county, office, district, party, candidate) and
    (office, district, party, candidate). Used for the HD22 data and for
    verification of the precinct-derived county CSVs."""
    text = fetch_text(path_or_url)
    county = {}
    race = {}
    section_party = None
    office = district = None
    candidates = []
    for raw in text.splitlines():
        s = raw.strip()
        if not s:
            continue
        if DATE_RE.match(s) or FOOTER_RE.search(s):
            continue
        m = SECTION_RE.match(s)
        if m:
            kind = m.group(1)
            section_party = ("Republican" if "Republican" in kind
                             else "Democratic" if "Democratic" in kind
                             else None)
            office = district = None
            candidates = []
            continue
        if s.startswith("STATEWIDE TOTALS") or s.startswith("DISTRICT TOTALS"):
            nums = re.sub(r"^(STATEWIDE TOTALS|DISTRICT TOTALS)\s*", "", s).split()
            cands = sorted(candidates, key=lambda c: c[0])
            if len(nums) == len(cands) and all(NUMERIC_RE.match(t) for t in nums):
                for (_num, name, party), v in zip(cands, nums):
                    race[(office, district, party, name)] = to_int(v)
            office = district = None
            candidates = []
            continue
        # Office line.
        if office is None and not CAND_MARK_RE.search(s) and not is_county_row(s):
            od = norm_office(s)
            if od is not None:
                office, district = od
                candidates = []
                continue
        # Candidate line.
        if office is not None and CAND_MARK_RE.search(s):
            candidates.extend(_parse_candidate_line(s, section_party))
            continue
        # Column header.
        if office is not None and ALLNUM_RE.match(s):
            candidates.sort(key=lambda c: c[0])
            continue
        # County row.
        if office is not None and candidates and is_county_row(s):
            m = COUNTY_ROW_RE.match(s)
            nums = m.group(2).split()
            cands = sorted(candidates, key=lambda c: c[0])
            if len(nums) == len(cands) and all(NUMERIC_RE.match(t) for t in nums):
                cname = norm_county(m.group(1))
                for (_num, name, party), v in zip(cands, nums):
                    county[(cname, office, district, party, name)] = to_int(v)
            continue
    return county, race


def dedup_precinct_rows(precinct_rows):
    """Aggregate precinct rows that share the full key
    (county, precinct, office, district, party, candidate) by summing votes.

    A few source by-precinct PDFs list a precinct twice on consecutive lines with
    split votes (e.g. Davidson "18-1 St Bernard Acad" in the Aug Dem Gov PDF).
    The PDF's own county-total row already reflects the summed total, so deduping
    here preserves the county totals and satisfies OpenElections' one-row-per-key
    convention. Input order is otherwise preserved (the rows are sorted later)."""
    agg = defaultdict(int)
    order = []
    for r in precinct_rows:
        key = (r[0], r[1], r[2], r[3], r[4], r[5])
        if key not in agg:
            order.append(key)
        agg[key] += r[6]
    return [[k[0], k[1], k[2], k[3], k[4], k[5], agg[k]] for k in order]


def sum_county(precinct_rows):
    """Derive county rows from precinct rows."""
    agg = defaultdict(int)
    for r in precinct_rows:
        agg[(r[0], r[2], r[3], r[4], r[5])] += r[6]
    return [[c, o, d, p, n, v] for (c, o, d, p, n), v in agg.items()]


def build_full_name_map(cty_urls, amend_cty_url=None):
    """Parse the by-county PDFs and return {(office, district, party): set of
    full candidate names}. The by-precinct PDFs truncate long candidate names at
    a fixed column width; the by-county PDFs carry the full names, so this map
    is used to un-truncate the precinct rows."""
    names = defaultdict(set)
    for _label, url in cty_urls:
        cnty, _race = parse_county_pdf(url)
        for (c, o, d, p, n) in cnty:
            names[(o, d, p)].add(n)
    if amend_cty_url:
        arows, _ = parse_amendments_pdf(amend_cty_url, by_county=True)
        for (c, o, d, p, n, v) in arows:
            names[(o, d, p)].add(n)
    return names


def _full_name(name, fset):
    """If `name` is a strict prefix of exactly one longer name in `fset` (and
    is not itself an exact name in `fset`), return that full name; else return
    `name` unchanged. Used to un-truncate by-precinct candidate names."""
    if not fset or name in fset:
        return name
    matches = [fn for fn in fset if fn.startswith(name) and len(fn) > len(name)]
    if len(matches) == 1:
        return matches[0]
    return name


def apply_full_names(precinct_rows, expected, names_by_key):
    """Replace truncated candidate names in precinct_rows AND the expected
    county-total dict (so the completeness check compares like-for-like).
    Returns the number of precinct rows whose name changed."""
    replaced = 0
    for r in precinct_rows:
        new = _full_name(r[5], names_by_key.get((r[2], r[3], r[4])))
        if new != r[5]:
            r[5] = new
            replaced += 1
    for k in list(expected):
        c, o, d, p, n = k
        new = _full_name(n, names_by_key.get((o, d, p)))
        if new != n:
            expected[(c, o, d, p, new)] = expected.pop(k)
    return replaced


def sort_key_county(row):
    return (row[0], row[1], row[2], row[3], row[4])


def sort_key_precinct(row):
    return (row[0], row[1], row[2], row[3], row[4], row[5])


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  Wrote {path} ({len(rows)} rows)")


def build_precinct_election(name, pct_urls, amend_url=None, cty_urls=None,
                            amend_cty_url=None):
    """Build county + precinct CSVs for a precinct-sourced election.

    Asserts the precinct rows sum to each by-precinct PDF's own per-county total
    rows (the by-precinct PDFs are final results; some Aug by-county PDFs are
    unofficial snapshots, so this internal check is the authoritative one).

    cty_urls / amend_cty_url (the by-county PDFs) supply full candidate names so
    that by-precinct names truncated at the PDF's column width can be restored
    (e.g. 'Ray "Chip" T. Throckmo' -> 'Ray "Chip" T. Throckmorton, III')."""
    print(f"--- {name} ---")
    precinct_rows = []
    expected = {}
    for url in pct_urls:
        prows, pexp = parse_precinct_pdf(url)
        precinct_rows.extend(prows)
        expected.update(pexp)
    if amend_url:
        arows, aexp = parse_amendments_pdf(amend_url, by_county=False)
        precinct_rows.extend(arows)
        expected.update(aexp)
    if cty_urls:
        names_by_key = build_full_name_map(cty_urls, amend_cty_url)
        n = apply_full_names(precinct_rows, expected, names_by_key)
        print(f"  restored {n} truncated candidate name(s) from the by-county PDFs")
    precinct_rows = dedup_precinct_rows(precinct_rows)
    county_rows = sum_county(precinct_rows)

    # Completeness check: derived county totals vs the precinct PDFs' own totals.
    county_sums = {(r[0], r[1], r[2], r[3], r[4]): r[5] for r in county_rows}
    mismatches = [(k, expected[k], county_sums.get(k))
                  for k in expected if county_sums.get(k) != expected[k]]
    missing = [k for k in county_sums if k not in expected]
    if mismatches:
        for k, exp, got in mismatches[:20]:
            print(f"  MISMATCH {k}: pdf_total={exp} derived={got}")
        raise AssertionError(f"{name}: {len(mismatches)} county-total mismatches "
                             f"vs the precinct PDFs' own totals")
    print(f"  completeness: {len(expected)} county/candidate totals all match "
          f"({len(missing)} derived combos with no PDF total line)")

    precinct_rows.sort(key=sort_key_precinct)
    county_rows.sort(key=sort_key_county)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    print(f"  county: {len(county_rows)} rows, precinct: {len(precinct_rows)} rows")
    write_csv(os.path.join(out, f"{name}__county.csv"), COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, f"{name}__precinct.csv"), PRECINCT_HEADER,
              precinct_rows)
    return county_rows, precinct_rows


def build_hd22():
    """Build the county-only HD22 special general CSV."""
    print("--- 20060112__tn__special__general (HD22, county only) ---")
    county_totals, _race = parse_county_pdf(HD22_URL)
    rows = [[c, o, d, p, n, v]
            for (c, o, d, p, n), v in county_totals.items()]
    rows.sort(key=sort_key_county)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    print(f"  county: {len(rows)} rows")
    write_csv(os.path.join(out, "20060112__tn__special__general__county.csv"),
              COUNTY_HEADER, rows)
    return rows


def main():
    build_hd22()
    build_precinct_election("20060803__tn__primary", AUG_PCT, cty_urls=AUG_CTY)
    build_precinct_election("20061107__tn__general", NOV_PCT,
                            amend_url=NOV_AMEND_PCT, cty_urls=NOV_CTY,
                            amend_cty_url=NOV_AMEND_CTY)


if __name__ == "__main__":
    main()