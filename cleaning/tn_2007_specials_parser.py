"""
Parser for the 2007 Tennessee special elections, producing OpenElections
COUNTY-ONLY CSVs for the six 2007 specials. The SoS publishes only by-county
PDFs for these specials (no precinct breakdown), so -- like the 2006 HD22
special -- only the county CSV is produced for each.

Six specials (seven old repo files -> six, because the SD30 general was
mis-dated `20070331` in the old repo; it was actually held March 13, 2007,
the same day as the HD92 general, so the two are merged into one
`20070313__tn__special__general` file):

  20070125__tn__special__primary  -- Jan 25 SD30 + HD92 special primaries
                                      (Democratic + Republican, merged)
  20070313__tn__special__general  -- Mar 13 SD30 + HD92 special generals
                                      (both held Mar 13, 2007; the old repo
                                      split them across `20070313` (HD92) and
                                      a mis-dated `20070331` (SD30))
  20070531__tn__special__primary  -- May 31 HD89 special primary
                                      (Democratic + Republican, merged)
  20070717__tn__special__general  -- Jul 17 HD89 special general (incl. a
                                      write-in independent, Steve Edmundson)
  20071004__tn__special__primary  -- Oct 4 SD10 special primary
                                      (Democratic + Republican, merged;
                                      Hamilton + Marion counties)
  20071115__tn__special__general  -- Nov 15 SD10 special general
                                      (Hamilton + Marion counties)

Source: the SoS by-county PDFs on
https://tnelections.tnsosfiles.com/sharetngov/archived/election/SpecialElections/
(the same SpecialElections bucket as the 2006 HD22 special). Each PDF is one
section (``Democratic Primary`` / ``Republican Primary`` / ``Special General
Election`` / ``General Election``) of one or two races. They use the same
by-county "Layout A" as the 2006 by-county PDFs, so this parser REUSES
``parse_county_pdf`` from ``tn_2006_parser``.

Party handling:
  * Primaries -- the section header supplies the party (Democratic/Republican).
  * Generals -- the candidate's `` - X`` suffix supplies it (D/R/I), EXCEPT the
    Mar 13 general PDF, which prints candidate names with NO party suffix. For
    those rows the party is INFERRED from the matching primary winner (the
    general candidates are the primary nominees; the Jan 25 primaries name the
    SD30 and HD92 nominees). A ``(office, district, candidate) -> party`` map is
    built from the three primary PDF groups and applied to any general row
    whose party is empty.

Conventions (standard OpenElections; matches the 2013/2015/2019/2023
legislative-special convention of a BARE ``__special__primary`` /
``__special__general`` filename with no office subtype, office
``State Senate``/``State House``, district a bare number):
  Tennessee Senate District N            -> office "State Senate",  district "N"
  Tennessee House of Representatives N    -> office "State House",   district "N"
Party: full names (Democratic/Republican/Independent). Write-ins normalized to
``Write-In - <name>`` (capital I, matching the 2006+ convention). Candidate
names are kept verbatim as printed (e.g. ``G. A. Hardaway, Sr.``,
``Dave Wicker, Jr``, ``Basil Marceaux I``) with internal whitespace collapsed.
Votes are integers. Zero-vote rows ARE included (e.g. Basil Marceaux I in
Marion County, 0 votes), matching the dominant regenerated convention.

The old repo's seven 2007 files used a non-standard convention: a precinct
column that was always empty (which failed the data_tests ``missing_values``
check), single-letter parties (DEM/REP), one row with an empty party (the
write-in), and the mis-dated SD30 general. They are regenerated as proper
county-only ``__county.csv`` files.

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.
"""

import csv
import os

import tn_2006_parser as P

BASE = "https://tnelections.tnsosfiles.com/sharetngov/archived/election/SpecialElections"

# The nine source PDFs.
JAN_DEM = f"{BASE}/200701DemPrTS30TH92.pdf"   # SD30 + HD92 Dem primary
JAN_REP = f"{BASE}/200701RepPrTS30TH92.pdf"   # SD30 + HD92 Rep primary
MAR_GEN = f"{BASE}/200703GenTS30TH92.pdf"      # SD30 + HD92 general (Mar 13)
MAY_DEM = f"{BASE}/200705DemPrTH89.pdf"       # HD89 Dem primary
MAY_REP = f"{BASE}/200705RepPrTH89.pdf"       # HD89 Rep primary
JUL_GEN = f"{BASE}/200707GenTH89.pdf"         # HD89 general (Jul 17)
OCT_DEM = f"{BASE}/200710DemPrTS10.pdf"       # SD10 Dem primary
OCT_REP = f"{BASE}/200710RepPrTS10.pdf"       # SD10 Rep primary
NOV_GEN = f"{BASE}/200711GenTS10.pdf"         # SD10 general (Nov 15)

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2007")

COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]

# Each election: name, type (primary/general), source PDFs.
ELECTIONS = [
    {"name": "20070125__tn__special__primary", "pdfs": [JAN_DEM, JAN_REP]},
    {"name": "20070313__tn__special__general", "pdfs": [MAR_GEN]},
    {"name": "20070531__tn__special__primary", "pdfs": [MAY_DEM, MAY_REP]},
    {"name": "20070717__tn__special__general", "pdfs": [JUL_GEN]},
    {"name": "20071004__tn__special__primary", "pdfs": [OCT_DEM, OCT_REP]},
    {"name": "20071115__tn__special__general", "pdfs": [NOV_GEN]},
]

# All primary PDFs, used to build the (office, district, candidate) -> party
# map for inferring the Mar 13 general's parties.
PRIMARY_PDFS = [JAN_DEM, JAN_REP, MAY_DEM, MAY_REP, OCT_DEM, OCT_REP]


def build_primary_party_map():
    """Return {(office, district, candidate): party} from the primary PDFs.
    Each primary candidate runs in exactly one party's primary, so the key is
    unique. Used to infer party for general rows whose PDF prints no party
    suffix (the Mar 13 general)."""
    pm = {}
    for url in PRIMARY_PDFS:
        cnty, _race = P.parse_county_pdf(url)
        for (county, office, district, party, candidate) in cnty:
            if not party:
                continue
            key = (office, district, candidate)
            if key in pm and pm[key] != party:
                raise AssertionError(
                    f"primary party conflict for {key}: {pm[key]} vs {party}")
            pm[key] = party
    return pm


def sort_key_county(row):
    return (row[0], row[1], row[2], row[3], row[4])


def write_csv(path, header, rows):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  Wrote {path} ({len(rows)} rows)")


def build(election, party_map):
    """Parse an election's PDFs into county rows (6 fields), inferring party
    for general rows whose PDF prints no party suffix."""
    print(f"--- {election['name']} ---")
    rows = []
    inferred = 0
    for url in election["pdfs"]:
        cnty, _race = P.parse_county_pdf(url)
        for (county, office, district, party, candidate), votes in cnty.items():
            p = party
            if not p:
                key = (office, district, candidate)
                if key not in party_map:
                    raise AssertionError(
                        f"{election['name']}: no primary party for general "
                        f"candidate {key} (PDF printed no party suffix)")
                p = party_map[key]
                inferred += 1
            rows.append([county, office, district, p, candidate, votes])
    if inferred:
        print(f"  inferred party for {inferred} general row(s) from primaries")
    rows.sort(key=sort_key_county)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, f"{election['name']}__county.csv"),
              COUNTY_HEADER, rows)
    return rows


def main():
    party_map = build_primary_party_map()
    print(f"primary party map: {len(party_map)} candidates")
    for election in ELECTIONS:
        build(election, party_map)


if __name__ == "__main__":
    main()