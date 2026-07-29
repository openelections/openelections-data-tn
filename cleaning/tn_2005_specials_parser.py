"""
Parser for the 2005 Tennessee special elections, producing OpenElections
COUNTY-ONLY CSVs for the three 2005 specials. The SoS publishes only by-county
PDFs for these specials (no precinct breakdown), so -- like the 2006 HD22 and
2007 specials -- only the county CSV is produced for each.

Three specials (sourced from the SoS results page
https://sos.tn.gov/elections/results#2005):

  20050804__tn__special__primary  -- Aug 4 SD29 + HD87 special primaries
                                      (Democratic + Republican, merged; Shelby)
  20050915__tn__special__general  -- Sep 15 HD87 special general (Gary L. Rowe,
                                      unopposed -- no Republican filed; the Rep
                                      primary had only a write-in). Shelby.
  20051129__tn__special__primary  -- Nov 29 HD22 special primary
                                      (Democratic + Republican, merged;
                                      Bradley/Meigs/Polk). Its general was held
                                      Jan 12, 2006 (already in the repo as
                                      20060112__tn__special__general).

Source gap: the SD29 special general (Ophelia Ford vs Terry Roland, a famously
contested race) is NOT published on the SoS results page (the page lists only
the HD87 general for Sep 15, 2005), so only the Aug 4 SD29 primary is converted
here; the SD29 general is omitted (not on the page).

Source: the SoS by-county PDFs on
https://tnelections.tnsosfiles.com/sharetngov/archived/election/SpecialElections/
(the same SpecialElections bucket as the 2006/2007 specials). They use the same
by-county "Layout A" + section headers (``Democratic Primary`` / ``Republican
Primary`` / ``General Election``) and office-line format
``Tennessee Senate/House District N`` as the 2006 by-county PDFs, so this parser
REUSES ``parse_county_pdf`` from ``tn_2006_parser``.

Write-in handling: the 2005 Rep HD87 primary lists its sole candidate as
``1 . Palmer Harris - Write-In - (R)`` -- a write-in whose ballot label puts the
NAME first (``Palmer Harris - Write-In``), the OPPOSITE of the 2006/2007
``Write-in - Name`` order. ``parse_county_pdf`` (which only catches names
STARTING with ``write-in``) leaves it as ``Palmer Harris - Write-In``; this
parser post-processes it to the standard ``Write-In - Palmer Harris`` (capital
I), party from the ``- (R)`` suffix.

Conventions (standard OpenElections; bare ``__special__primary`` /
``__special__general`` filename with no office subtype, office
``State Senate``/``State House``, district a bare number): party full names
(Democratic/Republican) from the primary section header (the primaries also
carry a ``- (X)`` parens suffix that agrees with the header); the HD87 general
party from the candidate's ``- (D)`` suffix. Candidate names verbatim as printed
(e.g. ``John Deberry, Jr.``, ``Ophelia E. Ford``, ``Andrew 'Rome' Withers``,
``Kevin Mclellan``). Votes are integers. Zero-vote rows ARE included (the HD87
Rep primary write-in Palmer Harris, 0; matches the dominant regenerated
convention).

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.
"""

import csv
import os
import re

import tn_2006_parser as P

BASE = "https://tnelections.tnsosfiles.com/sharetngov/archived/election/SpecialElections"

# The five source PDFs.
AUG_DEM = f"{BASE}/200508DemPrTS29TH87.pdf"   # SD29 + HD87 Dem primary
AUG_REP = f"{BASE}/200508RepPrTS29TH87.pdf"   # SD29 + HD87 Rep primary
SEP_GEN = f"{BASE}/200509GenTH87.pdf"          # HD87 general (Sep 15)
NOV_DEM = f"{BASE}/200511DemPrTH22.pdf"       # HD22 Dem primary (Nov 29)
NOV_REP = f"{BASE}/200511RepPrTH22.pdf"       # HD22 Rep primary (Nov 29)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2005")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]

ELECTIONS = [
    {"name": "20050804__tn__special__primary", "pdfs": [AUG_DEM, AUG_REP]},
    {"name": "20050915__tn__special__general", "pdfs": [SEP_GEN]},
    {"name": "20051129__tn__special__primary", "pdfs": [NOV_DEM, NOV_REP]},
]

# "<name> - Write-In" (name-first write-in label, used by the 2005 Rep HD87
# primary) -> "Write-In - <name>". Names already starting with "Write-In -" are
# left alone (parse_county_pdf already normalized those).
NAME_FIRST_WRITEIN_RE = re.compile(r"^(.+?)\s+-\s*[Ww]rite-?[Ii]n\s*$")


def fix_writein(name):
    if name.lower().startswith("write-in"):
        return name
    m = NAME_FIRST_WRITEIN_RE.match(name)
    if m:
        return f"Write-In - {m.group(1).strip()}"
    return name


def sort_key_county(row):
    return (row[0], row[1], row[2], row[3], row[4])


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  Wrote {path} ({len(rows)} rows)")


def build(election):
    print(f"--- {election['name']} ---")
    rows = []
    fixed = 0
    for url in election["pdfs"]:
        cnty, _race = P.parse_county_pdf(url)
        for (county, office, district, party, candidate), votes in cnty.items():
            cand = fix_writein(candidate)
            if cand != candidate:
                fixed += 1
            rows.append([county, office, district, party, cand, votes])
    if fixed:
        print(f"  normalized {fixed} name-first write-in candidate(s)")
    rows.sort(key=sort_key_county)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, f"{election['name']}__county.csv"),
              COUNTY_HEADER, rows)
    return rows


def main():
    for election in ELECTIONS:
        build(election)


if __name__ == "__main__":
    main()