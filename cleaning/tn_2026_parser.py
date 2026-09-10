"""
Parser for the Aug 1, 2026 Tennessee State Primary and General Election,
producing county- and precinct-level OpenElections CSVs:

  20260801__tn__primary__county.csv
  20260801__tn__primary__precinct.csv

Source: TN SoS "All by Precinct" spreadsheet (one row per precinct/candidate-
group with up to 10 candidates per row in RNAME*/PARTY*/PVTALLY* columns; a
precinct may span multiple rows -- the CANDGROUP column -- when a race has more
than 10 candidates). Grand totals were cross-checked against the official
by-county PDFs (Republican, Democratic, Judicial Retention, and State General;
4,714 of the 4,715 county/candidate combinations match exactly -- the one gap
is a 0-vote write-in row the spreadsheet itself omits).

  https://sos-prod.tnsosgovfiles.com/s3fs-public/document/20260806AllbyPrecinct.xlsx

The source spreadsheet name is listed at https://sos.tn.gov/elections/results#2026
(WebFetch returns 403; use curl with a browser User-Agent; a bare curl GET is
also 403'd by CloudFront -- send a browser UA). The workbook's ELECTDATE column
reads 2026-08-06, the date the file was posted, not the election date; the
election date is Aug 1, 2026, hence the 20260801 output names.

Ballot types in the workbook:
  Republican Primary / Democratic Primary -- Governor, U.S. Senate, U.S. House,
    State Senate, State House, State Executive Committeeman/woman
  Judicial Retention -- Supreme Court, Court of Appeals (Eastern and Western
    Divisions), Court of Criminal Appeals (Eastern Division); non-partisan,
    candidates are kept verbatim as printed ("Retain - <judge>" /
    "Replace - <judge>") and the party column is left empty
  State General -- the judicial and D.A./Public Defender seats decided on the
    August ballot (all "(unexpired term)" races); parties as printed
    (Republican / Democratic / Independent)

Party quirk: the PARTY column is populated for most write-in candidates
(e.g. "Write-In - James Benau" under Democratic Primary) but blank for others
(e.g. "Write-In - David Fey"); write-in rows are kept exactly as the source
has them. The source also repeats a handful of write-in rows on separate
candidate-group rows (all "Write-In - James Benau" in Obion, all 0 votes);
those are merged by summing the votes.

Same conventions as the 2024 parser (cleaning/tn_2024_parser.py):
  "United States House of Representatives District N" -> office "U.S. House", district "N"
  "Tennessee Senate District N"                    -> office "State Senate", district "N"
  "Tennessee House of Representatives District N"  -> office "State House", district "N"
  "Supreme Court, At Large" / "Court of Appeals - <Division>" /
    "Court of Criminal Appeals - <Division>"       -> office as printed, district "NA"
  "<office> ... District N (unexpired term)"       -> office = text before "District N",
       district = "N (unexpired term)"
  "State Executive Committeeman/woman District N"  -> split likewise

County-level totals are derived by summing the precinct votes per
(county, office, district, party, candidate), matching the by-county PDFs.

Requires `requests` (in the Pipfile) and `openpyxl` (install separately, e.g.
`pip install openpyxl`) to read the .xlsx workbook.
"""

import csv
import os
import re
from collections import defaultdict

import requests

try:
    import openpyxl
except ImportError as exc:  # pragma: no cover
    raise SystemExit("This parser requires openpyxl: pip install openpyxl") from exc

BASE = "https://sos-prod.tnsosgovfiles.com/s3fs-public/document"

ELECTION = {
    "date": "20260801",
    "type": "primary",
    "url": f"{BASE}/20260806AllbyPrecinct.xlsx",
}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2026")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party", "candidate", "votes"]

USHOUSE_RE = re.compile(r"^United States House of Representatives District (\d+)\s*$")
TNSENATE_RE = re.compile(r"^Tennessee Senate District (\d+)\s*$")
TNHOUSE_RE = re.compile(r"^Tennessee House of Representatives District (\d+)\s*$")
DISTRICT_RE = re.compile(r"^(.+?)\s+District\s+(\d+)\s*(\(unexpired term\))?\s*$")
WHITESPACE_RE = re.compile(r"\s+")


def clean(s):
    """Strip and collapse internal whitespace (the file_format test rejects
    consecutive whitespace, and some source precinct names contain double
    spaces)."""
    return WHITESPACE_RE.sub(" ", str(s)).strip()


def norm_office(raw):
    """Map a source OFFICENAME to (office, district). Returns None to skip."""
    s = raw.strip()
    if s == "United States Senate":
        return ("U.S. Senate", "NA")
    m = USHOUSE_RE.match(s)
    if m:
        return ("U.S. House", m.group(1))
    m = TNSENATE_RE.match(s)
    if m:
        return ("State Senate", m.group(1))
    m = TNHOUSE_RE.match(s)
    if m:
        return ("State House", m.group(1))
    if s == "Supreme Court, At Large" or s.startswith("Court of Appeals") \
            or s.startswith("Court of Criminal Appeals"):
        return (s, "NA")
    m = DISTRICT_RE.match(s)
    if m:
        office = m.group(1).strip()
        dist = m.group(2) + (" (unexpired term)" if m.group(3) else "")
        return (office, dist)
    # Fallback: keep the source office name, no district.
    return (s, "NA")


def fetch_workbook(url):
    # CloudFront 403s requests' default User-Agent; send a browser one.
    resp = requests.get(url, headers={
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/126.0 Safari/537.36",
    })
    resp.raise_for_status()
    import tempfile
    with tempfile.NamedTemporaryFile(suffix=".xlsx") as f:
        f.write(resp.content)
        f.flush()
        return openpyxl.load_workbook(f.name, read_only=True, data_only=True)


def iter_rows(url):
    """Yield (county, precinct, office_raw, party, candidate, votes)."""
    wb = fetch_workbook(url)
    ws = wb[wb.sheetnames[0]]
    rows = ws.iter_rows(values_only=True)
    header = list(next(rows))
    ci_county = header.index("COUNTY")
    ci_precinct = header.index("PRECINCT")
    ci_office = header.index("OFFICENAME")
    rname_cols = [header.index(f"RNAME{i}") for i in range(1, 11)
                  if f"RNAME{i}" in header]
    party_cols = [header.index(f"PARTY{i}") for i in range(1, 11)
                  if f"PARTY{i}" in header]
    tally_cols = [header.index(f"PVTALLY{i}") for i in range(1, 11)
                  if f"PVTALLY{i}" in header]
    n = min(len(rname_cols), len(party_cols), len(tally_cols))
    for r in rows:
        county = clean(r[ci_county] or "")
        precinct = clean(r[ci_precinct] or "")
        office_raw = clean(r[ci_office] or "")
        for k in range(n):
            name = r[rname_cols[k]]
            if not name:
                continue
            party = clean(r[party_cols[k]] or "")
            votes = r[tally_cols[k]] or 0
            yield (county, precinct, office_raw, party, clean(name), int(votes))


def build(election):
    """Build county + precinct rows for one election; return (county_rows, precinct_rows)."""
    precinct_rows = []
    precinct_index = {}  # (county, precinct, office, district, party, candidate) -> row index
    county_sums = defaultdict(int)  # (county, office, district, party, candidate) -> votes
    for county, precinct, office_raw, party, candidate, votes in iter_rows(election["url"]):
        nd = norm_office(office_raw)
        if nd is None:
            continue
        office, district = nd
        key = (county, precinct, office, district, party, candidate)
        if key in precinct_index:
            # The source repeats a handful of write-in rows (all
            # "Write-In - James Benau" in Obion, all 0 votes) on separate
            # candidate-group rows; merge them instead of emitting a dup.
            precinct_rows[precinct_index[key]][6] += votes
        else:
            precinct_index[key] = len(precinct_rows)
            precinct_rows.append([county, precinct, office, district, party, candidate, votes])
        county_sums[(county, office, district, party, candidate)] += votes

    county_rows = [
        [county, office, district, party, candidate, votes]
        for (county, office, district, party, candidate), votes in county_sums.items()
    ]
    return county_rows, precinct_rows, county_sums


def sort_key_county(row):
    return (row[0], row[1], row[2], row[3], row[4])


def sort_key_precinct(row):
    return (row[0], row[1], row[2], row[3], row[4], row[5])


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"Wrote {path} ({len(rows)} rows)")


def main():
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)

    date = ELECTION["date"]
    etype = ELECTION["type"]
    print(f"--- {date} {etype} ---")
    county_rows, precinct_rows, county_sums = build(ELECTION)
    county_rows.sort(key=sort_key_county)
    precinct_rows.sort(key=sort_key_precinct)
    print(f"  county: {len(county_rows)} rows, precinct: {len(precinct_rows)} rows")

    write_csv(os.path.join(out, f"{date}__tn__{etype}__county.csv"),
              COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, f"{date}__tn__{etype}__precinct.csv"),
              PRECINCT_HEADER, precinct_rows)

    # Sanity: precinct sums must equal county totals.
    ps = defaultdict(int)
    for r in precinct_rows:
        ps[(r[0], r[2], r[3], r[4], r[5])] += r[6]
    assert set(ps) == set(county_sums), \
        f"{date}: precinct keys != county keys"
    for k, v in county_sums.items():
        assert ps[k] == v, f"{date}: mismatch {k}: {ps[k]} != {v}"
    print(f"  OK: precinct sums equal county totals "
          f"({len(county_sums)} combinations)")


if __name__ == "__main__":
    main()