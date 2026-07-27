"""
Parser for the May 5, 2026 Tennessee primary election (Democratic and Republican
primaries), producing county- and precinct-level OpenElections CSVs.

Source (TN SoS "by County" / "by Precinct" PDFs, hosted on the 2025+ bucket):
  https://sos-prod.tnsosgovfiles.com/s3fs-public/document/20260506_DemocraticPrimarybyCounty.pdf
  https://sos-prod.tnsosgovfiles.com/s3fs-public/document/20260506_DemocraticPrimarybyPrecinct.pdf
  https://sos-prod.tnsosgovfiles.com/s3fs-public/document/20260506_RepublicanPrimarybyCounty.pdf
  https://sos-prod.tnsosgovfiles.com/s3fs-public/document/20260506_RepublicanPrimarybyPrecinct.pdf

The source filenames use 20260506 (the posting date); the election date in the
PDFs and in the OpenElections output filenames is May 5, 2026 -> 20260505.

The 2026 primaries are entirely judicial/local offices by judicial district
(Circuit Court Judge, Criminal Court Judge, Chancellor, Circuit and Chancery
Court Judge, District Attorney General, Public Defender), all "(unexpired
term)". There is no Governor / U.S. Senate / U.S. House / State Senate /
State House on this primary ballot.

PDF layout (via `pdftotext -layout`, which preserves columns):
  <office header>            e.g. "Circuit Court Judge Division III District 20 (unexpired term)"
  1. <candidate>             numbered candidate list ("1. No Candidate Qualified" if uncontested)
  2. <candidate>
      1      2      3        column header (one number per candidate) [county PDF only]
  <county>  <v1> <v2> ...    county rows (county PDF) -- votes are comma-formatted
  -or-
  <X> County                 county header (precinct PDF)
   Precincts:
    <precinct> <v1> ...      precinct rows (precinct PDF) -- votes comma-formatted >= 1000
   County Totals: <v1> ...   county subtotal (precinct PDF)
  DISTRICT TOTALS <v1> ...   district total (county PDF)

Office/district split (matching the repo's canonical 2016/2018 primary files):
  the "Division"/"Part"/"Division II" qualifier stays in the OFFICE column, and
  "District N (unexpired term)" becomes the DISTRICT column "N (unexpired term)".
  e.g. "Circuit Court Judge Division III District 20 (unexpired term)"
       -> office  = "Circuit Court Judge Division III"
          district = "20 (unexpired term)"

Vote columns are matched to candidates by taking the LAST N whitespace-delimited
tokens of each row as the vote values (N = number of candidates), and the
remaining leading tokens as the county/precinct name. This is robust to names
that contain digits or commas (e.g. "01-1", "Cocke County High",
"William K. Lane, III").

Conventions (matching the repo's canonical files):
  county:   Title case as printed (e.g. "Davidson", "Van Buren")
  office:   text before "District N" (Division/Part kept)
  district: "N (unexpired term)" (or "N" if a full-term race ever appears)
  party:    "Democratic" / "Republican"
  precinct: as printed, single-spaced (e.g. "1 Dandridge ES", "01-1")

Requires `requests` (in the Pipfile) and the `pdftotext` command (poppler) to be
on PATH.
"""

import csv
import os
import re
import subprocess
import tempfile

import requests

BASE = "https://sos-prod.tnsosgovfiles.com/s3fs-public/document"
SOURCES = {
    "Democratic": {
        "county": f"{BASE}/20260506_DemocraticPrimarybyCounty.pdf",
        "precinct": f"{BASE}/20260506_DemocraticPrimarybyPrecinct.pdf",
    },
    "Republican": {
        "county": f"{BASE}/20260506_RepublicanPrimarybyCounty.pdf",
        "precinct": f"{BASE}/20260506_RepublicanPrimarybyPrecinct.pdf",
    },
}

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2026")
COUNTY_CSV = "20260505__tn__primary__county.csv"
PRECINCT_CSV = "20260505__tn__primary__precinct.csv"

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party", "candidate", "votes"]

# Office header: everything before "District N", then an optional "(unexpired term)".
OFFICE_RE = re.compile(r"^(.+?)\s+District\s+(\d+)\s*(\(unexpired term\))?\s*$")
# Candidate line: "1. Name" (name may contain commas, e.g. "William K. Lane, III").
CAND_RE = re.compile(r"^\s*\d+\.\s+(.+?)\s*$")
# County header in the precinct PDF: "Davidson County" (ends with "County").
COUNTY_HEADER_RE = re.compile(r"^([A-Za-z][A-Za-z.'\- ]+?)\s+County\s*$")
# Column-header line in the county PDF: just candidate-index numbers.
COL_HEADER_RE = re.compile(r"^\s*\d+(\s+\d+)*\s*$")
FOOTER_RE = re.compile(r"Page\s+\d+\s+of\s+\d+")


def split_office(stripped):
    """Return (office, district) from a stripped office header line."""
    m = OFFICE_RE.match(stripped)
    office = m.group(1).strip()
    district = m.group(2)
    if m.group(3):
        district = f"{district} (unexpired term)"
    return office, district


def is_num(tok):
    return tok.replace(",", "").isdigit()


def to_int(tok):
    return int(tok.replace(",", ""))


def pdftotext_layout(url):
    """Download a PDF and return its `pdftotext -layout` text."""
    resp = requests.get(url)
    resp.raise_for_status()
    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(resp.content)
        f.flush()
        out = subprocess.run(
            ["pdftotext", "-layout", f.name, "-"],
            check=True, capture_output=True, text=True,
        )
    return out.stdout


def _skip_line(s):
    if not s:
        return True
    if s == "State of Tennessee":
        return True
    if s.startswith("May ") and "2026" in s:  # election + posting dates
        return True
    if s.endswith("Primary"):  # "Democratic Primary" / "Republican Primary"
        return True
    if FOOTER_RE.search(s):  # "Page 1 of 6"
        return True
    return False


def parse_county(text, party):
    """Parse a by-county PDF text -> list of county rows."""
    rows = []
    office = district = None
    candidates = []
    for raw in text.splitlines():
        s = raw.strip()
        if _skip_line(s):
            continue
        if OFFICE_RE.match(s):
            office, district = split_office(s)
            candidates = []
            continue
        mc = CAND_RE.match(raw)
        if mc:
            candidates.append(mc.group(1).strip())
            continue
        if COL_HEADER_RE.match(raw) and candidates:
            continue  # column header
        if s.startswith("DISTRICT TOTALS") or "County Totals" in s:
            continue
        if not candidates:
            continue
        toks = s.split()
        n = len(candidates)
        if len(toks) < n + 1:
            continue
        vote_toks = toks[-n:]
        if not all(is_num(t) for t in vote_toks):
            continue
        county = " ".join(toks[:-n])
        votes = [to_int(t) for t in vote_toks]
        for cand, v in zip(candidates, votes):
            rows.append([county, office, district, party, cand, v])
    return rows


def parse_precinct(text, party):
    """Parse a by-precinct PDF text -> list of precinct rows."""
    rows = []
    office = district = None
    candidates = []
    county = None
    for raw in text.splitlines():
        s = raw.strip()
        if _skip_line(s):
            continue
        if OFFICE_RE.match(s):
            office, district = split_office(s)
            candidates = []
            county = None
            continue
        mc = CAND_RE.match(raw)
        if mc:
            candidates.append(mc.group(1).strip())
            continue
        if COL_HEADER_RE.match(raw) and candidates:
            continue  # column header
        mcy = COUNTY_HEADER_RE.match(s)
        if mcy and "Totals" not in s:
            county = mcy.group(1).strip()
            continue
        if s == "Precincts:" or "County Totals" in s or s.startswith("DISTRICT TOTALS"):
            continue
        if not candidates or county is None:
            continue
        toks = s.split()
        n = len(candidates)
        if len(toks) < n + 1:
            continue
        vote_toks = toks[-n:]
        if not all(is_num(t) for t in vote_toks):
            continue
        precinct = " ".join(toks[:-n])
        votes = [to_int(t) for t in vote_toks]
        for cand, v in zip(candidates, votes):
            rows.append([county, precinct, office, district, party, cand, v])
    return rows


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"Wrote {path} ({len(rows)} rows)")


def main():
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)

    county_rows = []
    precinct_rows = []
    for party, urls in SOURCES.items():
        print(f"--- {party} ---")
        ctext = pdftotext_layout(urls["county"])
        ptext = pdftotext_layout(urls["precinct"])
        crows = parse_county(ctext, party)
        prows = parse_precinct(ptext, party)
        print(f"  county: {len(crows)} rows, precinct: {len(prows)} rows")
        county_rows.extend(crows)
        precinct_rows.extend(prows)

    # Sort: county by (county, office, district, party, candidate); precinct likewise
    # with precinct after county.
    county_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[4]))
    precinct_rows.sort(key=lambda r: (r[0], r[1], r[2], r[3], r[4], r[5]))

    write_csv(os.path.join(out, COUNTY_CSV), COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, PRECINCT_CSV), PRECINCT_HEADER, precinct_rows)

    # Sanity check: precinct sums must equal county totals.
    from collections import defaultdict
    ps = defaultdict(int)
    for r in precinct_rows:
        ps[(r[0], r[2], r[3], r[4], r[5])] += r[6]  # county, office, district, party, candidate
    cs = defaultdict(int)
    for r in county_rows:
        cs[(r[0], r[1], r[2], r[3], r[4])] += r[5]
    assert set(ps) == set(cs), "precinct keys != county keys"
    for k in cs:
        assert ps[k] == cs[k], f"mismatch {k}: precinct {ps[k]} != county {cs[k]}"
    print("OK: precinct sums equal county totals for all "
          f"{len(cs)} county/candidate combinations.")


if __name__ == "__main__":
    main()