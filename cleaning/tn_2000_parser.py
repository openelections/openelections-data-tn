"""
Parser for the 2000 Tennessee elections, producing OpenElections CSVs
(county + precinct) for the 2000 general and March presidential-preference
primary, sourced from the TN SoS results page (https://sos.tn.gov/elections/
results#2000). The August 2000 primary (side-by-side Dem/Rep layout, excom
multi-race, judicial, "NO RACE" precinct-only federal files) uses a different
PDF layout and is NOT handled here -- it is a follow-up.

Elections converted here:

  20000314__tn__primary__president  -- Mar 14 Presidential Preference Primary
        (Democratic + Republican, merged; party from the section header).
  20001107__tn__general              -- Nov 7 general: President, U.S. Senate,
        U.S. House, State Senate, State House (all offices combined into one
        file per granularity, matching the 2016/2008/2010 general convention).

Source: the SoS "Acrobat PDFWriter"/"Microsoft Access" era PDFs on
https://tnelections.tnsosfiles.com/sharetngov/archived/election/results/
(``2000-3`` for March, ``2000-11`` for November). Each office has a by-county
PDF (``us-president.pdf`` etc.) and a by-precinct PDF (``-p`` suffix), parsed
with ``pdftotext -layout``.

County layout ("standard", single section per file):
      <date>
      Official Results
      General Election | Democratic Primary | Republican Primary
      <office>                                   <- United States President /
                                                   United States Senate /
                                                   U.S. House of Representatives District /
                                                   Tennessee Senate District /
                                                   Tennessee House of Representatives District /
                                                   Presidential Preference Primary
      <district number>                           <- only for districted offices; on its
                                                   own line (leading zero, e.g. "02")
      1 . Name - X    5 . Name - X   ...          <- candidates, multi-column, with a
                                                   leading "N . " and a trailing
                                                   " - X" party letter (general) or no
                                                   suffix (primary; party = section)
      1 2 3 ... N                                <- column-header row (the candidate
                                                   numbers 1..N)
      COUNTY  v1 v2 ... vN                        <- one row per county (votes may have
                                                   thousands commas; a "%" row follows
                                                   each county row in Nov, skip it)
      District Totals v1..vN  / Statewide Totals <- footer (stop block); then the next
                                                   district repeats from the office line

Precinct layout: same office/district/candidate/column-header preamble, then
per county:
      County:    NAME
      NN[code] PRECINCTNAME  v1 v2 ... vN         <- one row per precinct (code prefix,
                                                   name uppercased + column-truncated;
                                                   Shelby precincts are code-only)
      County Totals v1..vN                       <- per-county subtotal (skip)
then "District Totals" (districted) / "Statewide Totals" (statewide) at the end.

Conventions (standard OpenElections; matching the 2005/2007/2008 files):
  - office: President, U.S. Senate, U.S. House, State Senate, State House,
    Presidential Preference.
  - district: "NA" for statewide (President, U.S. Senate, Presidential
    Preference); a bare number (leading zeros stripped) for districted offices.
  - party: full names Democratic/Republican/Independent. General candidates
    carry a " - X" suffix (D/R/I; the "(Green)"/"(Lib)"/"(Ref)" sublabels on
    independent presidential candidates are DROPPED -> "Independent", matching
    the repo convention). Primary candidates have no suffix -> party from the
    section header. "Write-Ins, _" / "Write-In, ." (aggregate, no name) ->
    candidate "Write-In" (party = section party for primaries, "" for general).
  - county: title-cased (reusing tn_2006_parser.norm_county, with the Mc/DeKalb
    fixes). Multi-word "VAN BUREN" handled by parsing vote tokens from the right.
  - precinct: the precinct name, title-cased, as printed (the source column-
    truncates names, e.g. "ANDERSONVILL" -> "Andersonvill"; Shelby precincts are
    numbered and have no name, so the raw code, e.g. "001"/"013-1", is used).
  - votes: integers (thousands commas stripped).
  - county = sum of precincts (verified by the data_tests vote_breakdown_totals).
  - Zero-vote rows ARE included.

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.
Reuses clean/norm_county/to_int/fetch_text from tn_2006_parser.
"""

import csv
import os
import re
import string

import tn_2006_parser as P

BASE = "https://tnelections.tnsosfiles.com/sharetngov/archived/election/results"

# --- March 14 Presidential Preference Primary (county + precinct) ---
MAR_DEM_COUNTY = f"{BASE}/2000-3/dem-ppp.pdf"
MAR_DEM_PRECINCT = f"{BASE}/2000-3/dem-ppp-p.pdf"
MAR_REP_COUNTY = f"{BASE}/2000-3/rep-ppp.pdf"
MAR_REP_PRECINCT = f"{BASE}/2000-3/rep-ppp-p.pdf"

# --- November 7 General (county + precinct) ---
NOV = f"{BASE}/2000-11"
NOV_COUNTY = {
    "President": f"{NOV}/us-president.pdf",
    "U.S. Senate": f"{NOV}/us-senate.pdf",
    "U.S. House": f"{NOV}/us-house.pdf",
    "State Senate": f"{NOV}/senate1-33.pdf",
    "State House": f"{NOV}/house1-99.pdf",
}
NOV_PRECINCT = {
    "President": f"{NOV}/us-president-p.pdf",
    "U.S. Senate": f"{NOV}/us-senate-p.pdf",
    "U.S. House": f"{NOV}/us-house-p.pdf",
    "State Senate": f"{NOV}/senate1-33-p.pdf",
    "State House": f"{NOV}/house1-99-p.pdf",
}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2000")
COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

PARTY_LETTER = {"D": "Democratic", "R": "Republican", "I": "Independent"}

# Office substring -> (OE office name, is_districted). Order matters: the
# longer/more-specific phrases are checked first so "Tennessee House of
# Representatives" wins over "Tennessee House".
OFFICE_MAP = [
    ("United States President", ("President", False)),
    ("United States Senate", ("U.S. Senate", False)),
    ("U.S. House of Representatives", ("U.S. House", True)),
    ("Presidential Preference Primary", ("Presidential Preference", False)),
    ("Tennessee House of Representatives", ("State House", True)),
    ("Tennessee House", ("State House", True)),
    ("Tennessee Senate", ("State Senate", True)),
]

SECTION_PARTY = {
    "General Election": "",
    "Democratic Primary": "Democratic",
    "Republican Primary": "Republican",
}

# A candidate chunk: "<num> . <rest>", separated from the next by 2+ spaces.
CAND_CHUNK_RE = re.compile(r"\d+\s*\.\s+\S.*?(?=\s{2,}\d+\s*\.\s|\s*$)")
CAND_NUM_RE = re.compile(r"(\d+)\s*\.\s+(.*)")
# Trailing party suffix: " - X" optionally followed by " (sublabel)".
SUFFIX_RE = re.compile(r"^(.*?)\s+-\s+([A-Z])\s*(\([^)]*\))?\s*$")
# Trailing ballot placeholder ", ." / ", _".
PLACEHOLDER_RE = re.compile(r",\s*[._]+\s*$")
DISTRICT_LINE_RE = re.compile(r"^\s*0*(\d+)\s*$")
NUMERIC_TOKEN_RE = re.compile(r"^[\d,]+$")
INT_TOKEN_RE = re.compile(r"^\d+$")
COUNTY_TOK_RE = re.compile(r"^[A-Z][A-Z'.\-]*$")
# A voting-method aggregate keyword token (ABSENTEE / AIS / Early Voting /
# Paper Votes / ...): letters in any case, with optional ., ', -. The precinct
# source names are uppercased; the voting-method keywords are mixed-case
# ("Early Voting", "Paper Votes"), so this is case-insensitive (COUNTY_TOK_RE
# above is uppercase-only and would reject them).
KW_TOK_RE = re.compile(r"^[A-Za-z][A-Za-z'.\-]*$")
TOTALS_RE = re.compile(r"^(District|Statewide)\s+Totals\b", re.I)
WRITEIN_RE = re.compile(r"^write-?ins?\b", re.I)
# A page-break reprint's date line ("March 14, 2000" / "November 7, 2000") --
# the only reprint-header line NOT caught by office_of/section_of/is_totals
# (it precedes them on each new page). With a 2-column office it would
# otherwise parse as a phantom "November"/"March" precinct row with votes
# [7, 2000] (or [14, 2000]), inflating column 1 by the day and the write-in
# column by 2000 on every page break. Skip it (the section/office line that
# follows breaks the row loop and re-enters the office block as usual).
DATE_RE = re.compile(
    r"^(January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2},\s*\d{4}$", re.I)


def section_of(s):
    """Return the section party ('' for general, 'Democratic'/'Republican' for
    primary) if s is a section header line, else None."""
    return SECTION_PARTY.get(s)


def office_of(s):
    """Return (oe_office, is_districted) if s contains a known office phrase,
    else None."""
    for phrase, val in OFFICE_MAP:
        if phrase in s:
            return val
    return None


def is_column_header(s):
    """A column-header row is 2+ space-separated bare integers (the candidate
    numbers as printed, e.g. "1 2 3" -- or "1 3" when a ballot number is
    skipped, e.g. State House district 83 in 2000). This is only ever called
    while collecting candidates (before any county/precinct data row), so a
    county row (alpha county name) or a Shelby-style code-only precinct row
    (which lives in the row loop, never here) cannot be confused with it."""
    toks = s.split()
    if len(toks) < 2:
        return False
    return all(INT_TOKEN_RE.match(t) for t in toks)


def column_numbers(s):
    return [int(t) for t in s.split()]


def vote_for(num, col_nums, votes):
    """Map a candidate's ballot number to its vote column. col_nums are the
    numbers printed on the column-header row (usually 1..N, but a ballot
    number can be skipped, e.g. "1 3" -> candidate 3 is the 2nd column)."""
    idx = col_nums.index(num) if num in col_nums else num - 1
    return votes[idx] if 0 <= idx < len(votes) else 0


def is_totals(s):
    return bool(TOTALS_RE.match(s))


def find_candidates(line):
    """Return a list of (num, name_part) chunks found on a candidate line."""
    out = []
    for m in CAND_CHUNK_RE.finditer(line):
        chunk = m.group(0).strip()
        cm = CAND_NUM_RE.match(chunk)
        if cm:
            out.append((int(cm.group(1)), cm.group(2)))
    return out


def parse_candidate(name_part, section_party):
    """Return (name, party) from a candidate-name cell + the section party."""
    name = P.clean(name_part)
    name = PLACEHOLDER_RE.sub("", name).strip()
    sm = SUFFIX_RE.match(name)
    if sm:
        name = sm.group(1).strip()
        party = PARTY_LETTER.get(sm.group(2), "")
    else:
        party = section_party or ""
    if WRITEIN_RE.match(name):
        name = "Write-In"
    return name, party


def parse_county_row(s, ncols):
    """A county row: <COUNTY> <v1>..<vN>. Vote tokens are parsed from the right
    (handles the multi-word county 'VAN BUREN'). Returns (county, votes) or
    None."""
    toks = s.split()
    if len(toks) < ncols + 1:
        return None
    nums = toks[-ncols:]
    for t in nums:
        if not NUMERIC_TOKEN_RE.match(t):
            return None
    county_toks = toks[:-ncols]
    for t in county_toks:
        if not COUNTY_TOK_RE.match(t):
            return None
    county = " ".join(county_toks)
    votes = [int(t.replace(",", "")) for t in nums]
    return county, votes


def parse_precinct_row(s, ncols):
    """A precinct row: <code> [NAME] <v1>..<vN>, OR a voting-method aggregate
    row <KEYWORD> <v1>..<vN> (ABSENTEE / AIS / Early Voting / Paper Votes /
    ... -- included as precinct rows, matching the 2008+ convention). Vote
    tokens are parsed from the right; the leading code is the first token;
    anything between is the (column-truncated, uppercased) precinct name.

    The precinct identifier keeps the leading code, e.g. "01-1 Joelton
    Baptist" -- matching the 2008/2016 convention -- because the source
    column-truncates names, so distinct precincts can collapse to the same
    printed name (e.g. Davidson "09-2 Madison Fire" and "09-4 Madison Fire"):
    the code is what disambiguates them. Shelby-style code-only precincts
    (the 2000 source prints no name for Shelby) keep the raw code, e.g.
    "001" / "013-1". Names are title-cased from the all-caps source with
    ``string.capwords`` (so "KING'S LANE" -> "King's Lane", not ``.title()``'s
    "King'S Lane"); acronym casing (YMCA/UMC/MS) is unrecoverable from an
    all-caps source and is left as capwords output (Ymca/Umc/Ms). Returns
    (precinct, votes) or None."""
    toks = s.split()
    if len(toks) < ncols + 1:
        return None
    nums = toks[-ncols:]
    for t in nums:
        if not NUMERIC_TOKEN_RE.match(t):
            return None
    lead = toks[:-ncols]
    if not lead:
        return None
    if lead[0][:1].isdigit():
        code = lead[0]
        name_toks = lead[1:]
        if name_toks:
            precinct = f"{code} {string.capwords(' '.join(name_toks))}"
        else:
            precinct = code
    elif all(KW_TOK_RE.match(t) for t in lead):
        # voting-method aggregate keyword (ABSENTEE, AIS, Early Voting, ...)
        precinct = string.capwords(' '.join(lead))
    else:
        return None
    votes = [int(t.replace(",", "")) for t in nums]
    return precinct, votes


def _next_nonblank(lines, i):
    while i < len(lines) and not lines[i].strip():
        i += 1
    return i


def parse_county_file(url):
    """Parse a by-county PDF; return a list of (county, office, district,
    party, candidate, votes) rows."""
    text = P.fetch_text(url)
    lines = text.split("\n")
    rows = []
    section_party = ""
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        sec = section_of(s)
        if sec is not None:
            section_party = sec
            i += 1
            continue
        off = office_of(s)
        if off is not None:
            office, districted = off
            i += 1
            if districted:
                i = _next_nonblank(lines, i)
                dm = DISTRICT_LINE_RE.match(lines[i]) if i < n else None
                district = str(int(dm.group(1))) if dm else ""
                if dm:
                    i += 1
            else:
                district = "NA"
            # collect candidates up to the column header
            cands = []
            col_nums = None
            while i < n:
                s2 = lines[i].strip()
                if not s2:
                    i += 1
                    continue
                if section_of(s2) is not None or office_of(s2) is not None:
                    break
                if is_totals(s2):
                    break
                if is_column_header(s2):
                    col_nums = column_numbers(s2)
                    break
                chunks = find_candidates(lines[i])
                if chunks:
                    cands.extend(chunks)
                i += 1
            if col_nums is not None:
                i += 1  # skip the column-header line
            cands.sort(key=lambda c: c[0])
            parsed = [(num, *parse_candidate(rest, section_party))
                      for (num, rest) in cands]
            if col_nums is None:
                col_nums = list(range(1, len(parsed) + 1))
            # county rows
            while i < n:
                s2 = lines[i].strip()
                if not s2:
                    i += 1
                    continue
                if DATE_RE.match(s2):
                    i += 1
                    continue
                if office_of(s2) is not None or section_of(s2) is not None:
                    break
                if is_totals(s2):
                    i += 1
                    break
                if "%" in s2:
                    i += 1
                    continue
                cr = parse_county_row(s2, len(col_nums))
                if cr:
                    county, votes = cr
                    county = P.norm_county(county)
                    for (num, name, party) in parsed:
                        v = vote_for(num, col_nums, votes)
                        rows.append((county, office, district, party, name, v))
                i += 1
            continue
        i += 1
    return rows


def parse_precinct_file(url):
    """Parse a by-precinct PDF; return a list of (county, precinct, office,
    district, party, candidate, votes) rows."""
    text = P.fetch_text(url)
    lines = text.split("\n")
    rows = []
    section_party = ""
    current_county = None
    i, n = 0, len(lines)
    while i < n:
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        sec = section_of(s)
        if sec is not None:
            section_party = sec
            i += 1
            continue
        off = office_of(s)
        if off is not None:
            office, districted = off
            i += 1
            if districted:
                i = _next_nonblank(lines, i)
                dm = DISTRICT_LINE_RE.match(lines[i]) if i < n else None
                district = str(int(dm.group(1))) if dm else ""
                if dm:
                    i += 1
            else:
                district = "NA"
            cands = []
            col_nums = None
            while i < n:
                s2 = lines[i].strip()
                if not s2:
                    i += 1
                    continue
                if section_of(s2) is not None or office_of(s2) is not None:
                    break
                if is_totals(s2):
                    break
                if is_column_header(s2):
                    col_nums = column_numbers(s2)
                    break
                if s2.startswith("County:"):
                    break
                chunks = find_candidates(lines[i])
                if chunks:
                    cands.extend(chunks)
                i += 1
            if col_nums is not None:
                i += 1  # skip the column-header line
            cands.sort(key=lambda c: c[0])
            parsed = [(num, *parse_candidate(rest, section_party))
                      for (num, rest) in cands]
            if col_nums is None:
                col_nums = list(range(1, len(parsed) + 1))
            while i < n:
                s2 = lines[i].strip()
                if not s2:
                    i += 1
                    continue
                if DATE_RE.match(s2):
                    i += 1
                    continue
                if office_of(s2) is not None or section_of(s2) is not None:
                    break
                if is_totals(s2):
                    i += 1
                    break
                if s2.startswith("County:"):
                    current_county = P.norm_county(
                        s2.split(":", 1)[1].strip())
                    i += 1
                    continue
                if s2.upper().startswith("COUNTY TOTAL") or "%" in s2:
                    i += 1
                    continue
                if current_county:
                    pr = parse_precinct_row(s2, len(col_nums))
                    if pr:
                        precinct, votes = pr
                        for (num, name, party) in parsed:
                            v = vote_for(num, col_nums, votes)
                            rows.append((current_county, precinct, office,
                                         district, party, name, v))
                i += 1
            continue
        i += 1
    return rows


def sort_key_county(r):
    return (r[0], r[1], r[2], r[3], r[4])


def sort_key_precinct(r):
    return (r[0], r[1], r[2], r[3], r[4], r[5])


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {path} ({len(rows)} rows)")


def build_mar_ppp():
    print("--- 20000314 primary president ---")
    county_rows = []
    for url in (MAR_DEM_COUNTY, MAR_REP_COUNTY):
        county_rows.extend(parse_county_file(url))
    precinct_rows = []
    for url in (MAR_DEM_PRECINCT, MAR_REP_PRECINCT):
        precinct_rows.extend(parse_precinct_file(url))
    county_rows.sort(key=sort_key_county)
    precinct_rows.sort(key=sort_key_precinct)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, "20000314__tn__primary__president__county.csv"),
              COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, "20000314__tn__primary__president__precinct.csv"),
              PRECINCT_HEADER, precinct_rows)


def build_nov_general():
    print("--- 20001107 general ---")
    county_rows = []
    precinct_rows = []
    for office, url in NOV_COUNTY.items():
        county_rows.extend(parse_county_file(url))
    for office, url in NOV_PRECINCT.items():
        precinct_rows.extend(parse_precinct_file(url))
    county_rows.sort(key=sort_key_county)
    precinct_rows.sort(key=sort_key_precinct)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, "20001107__tn__general__county.csv"),
              COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, "20001107__tn__general__precinct.csv"),
              PRECINCT_HEADER, precinct_rows)


def main():
    build_mar_ppp()
    build_nov_general()


if __name__ == "__main__":
    main()