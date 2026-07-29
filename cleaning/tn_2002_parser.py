"""
Parser for the 2002 Tennessee elections, producing OpenElections CSVs
(county + precinct) for the Aug 1 primary and the Nov 5 general, sourced
from the TN SoS results page (https://sos.tn.gov/elections/results#2002).
Same "Acrobat PDFWriter"/"Microsoft Access" era PDFs as 2000, parsed with
``pdftotext -layout`` -- this parser REUSES the low-level helpers (candidate
parsing, column-header / vote mapping, county-row parsing, date-line skip,
etc.) from ``tn_2000_parser`` and ``norm_county``/``fetch_text`` from
``tn_2006_parser``.

Elections converted here:

  20020801__tn__primary  -- Aug 1 primary: Democratic + Republican primaries
        for Governor, U.S. Senate, U.S. House, State Senate, State House
        (merged; party from the section header). The SoS publishes Dem and
        Rep as SEPARATE per-office PDFs (``*-dp8-02`` / ``*-rp8-02``); both
        are parsed and merged with a ``party`` column.
  20021105__tn__general  -- Nov 5 general: Governor, U.S. Senate, U.S. House,
        State Senate, State House, and Constitutional Amendments 1 & 2 (the
        2002 lottery-amendment election), all offices combined into one file
        per granularity, matching the 2000/2006/2008 general convention.

Scope EXCLUSIONS (follow-up): the Aug 1 ballot also had State Executive
Committee races (``excom-*-8-02.pdf``, county-only -- no precinct PDF) and
judicial "State General" races (``general.pdf`` / ``general-precinct.pdf``);
both are OMITTED here -- excom has no precinct source (would break county =
sum-of-precincts parity), and the judicial office names ("Circuit Court,
Part I, Judicial District N", etc.) are a separate naming exercise. The
standard statewide/legislative offices + amendments are the high-value core
and are converted.

Format notes (vs 2000):
  - Party suffix is ``- (X)`` WITH parens (2000 used ``- X`` without); the
    shared ``SUFFIX_RE`` already accepts an optional ``(...)`` group.
  - District is printed INLINE on the office line in the Nov general
    ("Tennessee House of Representatives District 01") but on a SEPARATE
    line in the Aug primary ("Tennessee House of Representatives District"
    / "01"); the parser tries inline first, then a following bare-number
    line.
  - Precinct names are already MIXED-CASE in 2002 (the 2000 source was
    all-caps), so they are kept AS PRINTED (whitespace collapsed); only
    the all-uppercase voting-method keywords ("ABSENTEE", "z-EARLY", ...)
    are title-cased. The ``z-`` sort prefix (used by some counties to push
    absentee/early to the end of the list) is stripped, so ``z-ABSENTEE`` ->
    ``Absentee``, ``z-EARLY`` -> ``Early``.
  - The amendments are a single 4-column table per row
    (Amd1-Yes, Amd1-No, Amd2-Yes, Amd2-No); the parser splits it into two
    ``Constitutional Amendment N`` offices x ``Yes``/``No`` candidates
    (party ``NA``, district ``NA``), matching the 2006/2014 amendment
    convention.

Conventions (standard OpenElections; matching the 2000/2005/2006/2008
files): office President/U.S. Senate/U.S. House/Governor/State Senate/
State House/Presidential Preference; district ``NA`` for statewide, a bare
number (leading zeros stripped) for districted offices; full party names
(Democratic/Republican/Independent -- the ``(Green)``/``(Lib)``/``(Ref)``
sublabels on independent candidates are not present in 2002; all ``- (I)``
-> ``Independent``); title-case counties via ``norm_county``; integer
votes (thousands commas stripped); ``Write-Ins, .`` -> candidate
``Write-In`` (party = section party for primaries, empty for general);
zero-vote rows ARE included; county = sum of precincts (verified).

Requires `requests` (in the Pipfile) and `pdftotext` (poppler) on the PATH.
"""

import csv
import os
import re
import string

import tn_2006_parser as P
import tn_2000_parser as T0

BASE = "https://tnelections.tnsosfiles.com/sharetngov/archived/election/results"

# --- Aug 1 primary (Democratic + Republican, separate per-office PDFs) ---
AUG = f"{BASE}/2002-8"
# OE office -> URL stem used in the 2002-8 filenames.
AUG_OFFICE_URL = {
    "Governor": "gov",
    "U.S. Senate": "us-senate",
    "U.S. House": "us-house",
    "State Senate": "senate",
    "State House": "house",
}
AUG_DEM_COUNTY = {o: f"{AUG}/{stem}-dp8-02.pdf" for o, stem in AUG_OFFICE_URL.items()}
AUG_DEM_PRECINCT = {o: f"{AUG}/{stem}-dp8-02-p.pdf" for o, stem in AUG_OFFICE_URL.items()}
AUG_REP_COUNTY = {o: f"{AUG}/{stem}-rp8-02.pdf" for o, stem in AUG_OFFICE_URL.items()}
AUG_REP_PRECINCT = {o: f"{AUG}/{stem}-rp8-02-p.pdf" for o, stem in AUG_OFFICE_URL.items()}

# --- Nov 5 general (all offices + amendments) ---
NOV = f"{BASE}/2002-11"
NOV_COUNTY = {
    "Governor": f"{NOV}/governor.pdf",
    "U.S. Senate": f"{NOV}/us-senate.pdf",
    "U.S. House": f"{NOV}/us-house.pdf",
    "State Senate": f"{NOV}/tn-senate.pdf",
    "State House": f"{NOV}/tn-house.pdf",
}
NOV_PRECINCT = {
    "Governor": f"{NOV}/governor-p.pdf",
    "U.S. Senate": f"{NOV}/us-senate-p.pdf",
    "U.S. House": f"{NOV}/us-house-p.pdf",
    "State Senate": f"{NOV}/tn-senate-p.pdf",
    "State House": f"{NOV}/tn-house-p.pdf",
}
NOV_AMD_COUNTY = f"{NOV}/amendments.pdf"
NOV_AMD_PRECINCT = f"{NOV}/amendments-p.pdf"

OUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "2002")
COUNTY_HEADER = ["county", "office", "district", "party", "candidate", "votes"]
PRECINCT_HEADER = ["county", "precinct", "office", "district", "party",
                   "candidate", "votes"]

# Office substring -> (OE office name, is_districted). Order matters: the
# longer/more-specific phrases are checked first. Adds "Governor" vs the
# 2000 map (2002 is a midterm with a Governor race and no President).
OFFICE_MAP = [
    ("United States Senate", ("U.S. Senate", False)),
    ("U.S. House of Representatives", ("U.S. House", True)),
    ("Tennessee House of Representatives", ("State House", True)),
    ("Tennessee Senate", ("State Senate", True)),
    ("Governor", ("Governor", False)),
]

# A trailing "District N" (inline district, Nov general).
INLINE_DIST_RE = re.compile(r"\bDistrict\s+0*(\d+)\s*$")

# A "Page N of M" footer (with leading whitespace); the 2002 precinct PDFs
# print these mid-block, and a 1-candidate (Write-Ins-only) race would
# otherwise parse "Page 12 of 69" as a precinct row "Page 12 of" / vote 69.
PAGE_RE = re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.I)
INT_TOKEN_RE = re.compile(r"^\d+$")


def is_column_header(s):
    """A column-header row is 1+ space-separated bare integers (the candidate
    numbers as printed). The 2000-shared ``T0.is_column_header`` requires 2+
    tokens, but the 2002 PDFs print a single "1" header for 1-candidate
    (Write-Ins-only) races; without recognizing it, ``_collect_candidates``
    walks past the data rows and drops the whole race. A bare integer in the
    candidate phase can only be the column header (district numbers are
    consumed earlier by ``extract_district``; candidate lines are "N . Name")."""
    toks = s.split()
    return len(toks) >= 1 and all(INT_TOKEN_RE.match(t) for t in toks)

# 2002 general PDFs print the party as " - (X)" with the letter INSIDE the
# parens (e.g. "Bredesen, Phil - (D)"), unlike the 2000-era bare-letter suffix
# ("Gore, Al - D") that the shared ``T0.SUFFIX_RE`` matches: ``T0.SUFFIX_RE``
# requires a bare ``[A-Z]`` before any parens, so " - (D)" does NOT match and
# the suffix would be left on the candidate name with an empty party. Strip the
# parens suffix here so the party lands in the party column and the candidate
# name is clean, matching the 2000/2006/2008 repo convention. (2002 has no
# (Green)/(Lib)/(Ref) sublabels -- all independents are " - (I)" -- but the
# extra letters are mapped to Independent for safety, per convention.)
PARENS_SUFFIX_RE = re.compile(r"^(.*?)\s+-\s+\(([A-Z])\)\s*$")
_PARTY_LETTER = {"D": "Democratic", "R": "Republican", "I": "Independent",
                 "G": "Independent", "L": "Independent", "F": "Independent",
                 "C": "Independent"}


def parse_candidate(name_part, section_party):
    """Return (name, party) from a candidate-name cell. Handles the 2002
    " - (X)" parens suffix first (general); else delegates to the shared
    ``T0`` logic (a residual bare " - X" letter, or the section party for
    primaries). Write-Ins normalize to "Write-In" (party = section party for
    primaries, "" for general)."""
    name = P.clean(name_part)
    name = T0.PLACEHOLDER_RE.sub("", name).strip()
    m = PARENS_SUFFIX_RE.match(name)
    if m:
        name, party = m.group(1).strip(), _PARTY_LETTER.get(m.group(2), "")
    else:
        sm = T0.SUFFIX_RE.match(name)
        if sm:
            name = sm.group(1).strip()
            party = T0.PARTY_LETTER.get(sm.group(2), "")
        else:
            party = section_party or ""
    if T0.WRITEIN_RE.match(name):
        name = "Write-In"
    return name, party


AMD_OFFICES = ["Constitutional Amendment 1", "Constitutional Amendment 2"]
AMD_CANDS = ["Yes", "No"]
AMD_DISTRICT = "NA"
AMD_PARTY = "NA"
# Tokens that make up a reprinted amendments column header
# ("1- Yes 1- No 2- Yes 2- No"); used to skip those lines precisely without
# dropping precincts whose names contain "No" (e.g. "Northwoods No").
_AMD_HEADER_TOKS = {"1-", "2-", "3-", "4-", "Yes", "No"}


def section_of(s):
    """Return the section party ('' for general, 'Democratic'/'Republican'
    for primary) if s contains a section phrase, else None. Substring match
    (not exact) because the Aug primary by-county PDF puts the section on
    the same line as 'Official Results' ('Official Results  Democratic
    Primary'), while the by-precinct PDF gives it its own line."""
    for key, val in T0.SECTION_PARTY.items():
        if key in s:
            return val
    return None


def office_of(s):
    for phrase, val in OFFICE_MAP:
        if phrase in s:
            return val
    return None


def is_totals(s):
    return bool(T0.TOTALS_RE.match(s))


def extract_district(office_line, lines, i):
    """Return (district, next_i). Tries an inline 'District N' on the office
    line first (Nov general); else a following bare-number line (Aug
    primary / judicial); else 'NA' (statewide)."""
    m = INLINE_DIST_RE.search(office_line)
    if m:
        return m.group(1), i
    j = T0._next_nonblank(lines, i)
    if j < len(lines) and T0.DISTRICT_LINE_RE.match(lines[j]):
        return str(int(lines[j].strip())), j + 1
    return "NA", i


def parse_precinct_row(s, ncols):
    """A 2002 precinct row: <name> <v1>..<vN> (or a voting-method keyword row
    such as 'ABSENTEE' / 'z-EARLY'). Vote tokens are parsed from the right;
    the leading tokens are the precinct name. 2002 precinct names are already
    mixed-case, so they are kept AS PRINTED (whitespace collapsed); only
    all-uppercase keyword rows are title-cased. The 'z-' sort prefix used by
    some counties is stripped. Returns (precinct, votes) or None."""
    toks = s.split()
    if len(toks) < ncols + 1:
        return None
    nums = toks[-ncols:]
    for t in nums:
        if not T0.NUMERIC_TOKEN_RE.match(t):
            return None
    lead = toks[:-ncols]
    if not lead:
        return None
    name = " ".join(lead)
    if name[:2].lower() == "z-":
        name = name[2:]
    name = re.sub(r"\s+", " ", name).strip()
    letters = re.sub(r"[^A-Za-z]", "", name)
    if letters and letters.isupper():
        precinct = string.capwords(name)
    else:
        precinct = name
    votes = [int(t.replace(",", "")) for t in nums]
    return precinct, votes


def _collect_candidates(lines, i, n, section_party):
    """From the office line, collect candidates up to the column header.
    Returns (parsed[(num,name,party)], col_nums, next_i). Mirrors the 2000
    parser's candidate phase but is office/district-agnostic."""
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
        if T0.DATE_RE.match(s2):
            i += 1
            continue
        if PAGE_RE.match(s2):
            i += 1
            continue
        if is_column_header(s2):
            col_nums = T0.column_numbers(s2)
            break
        if s2.startswith("County:"):
            break
        chunks = T0.find_candidates(lines[i])
        if chunks:
            cands.extend(chunks)
        i += 1
    if col_nums is not None:
        i += 1
    cands.sort(key=lambda c: c[0])
    parsed = [(num, *parse_candidate(rest, section_party)) for (num, rest) in cands]
    if col_nums is None:
        col_nums = list(range(1, len(parsed) + 1))
    return parsed, col_nums, i


def parse_county_file(url):
    """Parse a by-county PDF; return (rows, registry).

    rows: list of (county, office, district, party, candidate, votes).
    registry: {(office, district, section_party): {num: (name, party)}}
    -- the FULL ordered candidate list per race, MERGING multiple printed
    blocks that share the same (office, district, party). The 2002 county
    PDFs split a >10-candidate race into two blocks both labeled with the
    same district (e.g. Sullivan State House d=2 R: cands 1-10 then 11-13);
    merging them yields the complete 1-13 candidate list, which the precinct
    parser uses to un-conflate the 13-candidate precinct rows and to restore
    truncated precinct candidate names."""
    text = P.fetch_text(url)
    lines = text.split("\n")
    rows = []
    registry = {}
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
            district, i = extract_district(s, lines, i + 1)
            parsed, col_nums, i = _collect_candidates(lines, i, n, section_party)
            rkey = (office, district, section_party)
            reg = registry.setdefault(rkey, {})
            for (num, name, party) in parsed:
                reg[num] = (name, party)
            while i < n:
                s2 = lines[i].strip()
                if not s2:
                    i += 1
                    continue
                if T0.DATE_RE.match(s2):
                    i += 1
                    continue
                if PAGE_RE.match(s2):
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
                cr = T0.parse_county_row(s2, len(col_nums))
                if cr:
                    county, votes = cr
                    county = P.norm_county(county)
                    for (num, name, party) in parsed:
                        rows.append((county, office, district, party, name,
                                     T0.vote_for(num, col_nums, votes)))
                i += 1
            continue
        i += 1
    return rows, registry


def _resolve_maps(parsed, registry, office, district, section_party):
    """Build (name_map, party_map, total_cands) for a race. Prefer the county
    registry's FULL candidate names (fixes precinct-PDF truncation, e.g.
    "Martin," -> "Martin, Shawn E." and "Miller, R" -> "Miller, Robert L.
    "Bob""), merging multi-block races; fall back to the precinct's own
    (possibly truncated) parsed list if no registry entry."""
    reg = registry.get((office, district, section_party), {})
    name_map, party_map = {}, {}
    for (num, name, party) in parsed:
        name_map[num] = name
        party_map[num] = party
    for num, (name, party) in reg.items():
        name_map[num] = name
        party_map[num] = party
    return name_map, party_map, len(name_map)


def parse_precinct_file(url, registry=None):
    """Parse a by-precinct PDF; return a list of (county, precinct, office,
    district, party, candidate, votes) rows.

    ``registry`` (from ``parse_county_file``) supplies the full per-race
    candidate list (merging multi-block races), used to restore candidate
    names that the precinct PDF column-truncates (e.g. "Miller, R" ->
    "Miller, Robert L. \"Bob\"\"") and to handle >10-candidate "overflow"
    races. The 2002 precinct PDFs use TWO different overflow layouts:

      * Interleaved (e.g. Sullivan State House d=2 R, 13 cands in a 10-col
        layout): each precinct has TWO rows in one block -- row A = cands
        1-10, row B = cands 11-13 in cols 1-3 (cols 4-10 zero). Detected by
        a (county, precinct) pair repeating within the block; the two rows
        are told apart by the trailing columns (row B's are zero).

      * Separate blocks (e.g. Governor, 16 cands): block 1 column-header
        "1..10" (cands 1-10), block 2 column-header "11..16" (cands 11-16).
        Each precinct appears ONCE per block, and col_nums maps directly to
        candidate numbers -- no special handling beyond using the registry
        names.

    The ``col_nums`` column-header (e.g. "1 2 ... 10" or "11 12 ... 16") is
    the source of truth for the column->candidate mapping in both layouts;
    the registry supplies the (untruncated) candidate name for each number.
    """
    registry = registry or {}
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
            district, i = extract_district(s, lines, i + 1)
            parsed, col_nums, i = _collect_candidates(lines, i, n, section_party)
            name_map, party_map, total_cands = _resolve_maps(
                parsed, registry, office, district, section_party)
            ncols = len(col_nums)
            block_rows = []  # (county, precinct, votes)
            while i < n:
                s2 = lines[i].strip()
                if not s2:
                    i += 1
                    continue
                if T0.DATE_RE.match(s2):
                    i += 1
                    continue
                if PAGE_RE.match(s2):
                    i += 1
                    continue
                if office_of(s2) is not None or section_of(s2) is not None:
                    break
                if is_totals(s2):
                    i += 1
                    break
                if s2.startswith("County:"):
                    current_county = P.norm_county(s2.split(":", 1)[1].strip())
                    i += 1
                    continue
                if s2.upper().startswith("COUNTY TOTAL") or "%" in s2:
                    i += 1
                    continue
                if current_county:
                    pr = parse_precinct_row(s2, ncols)
                    if pr:
                        block_rows.append((current_county, pr[0], pr[1]))
                i += 1
            _emit_block(rows, block_rows, office, district, col_nums,
                        name_map, party_map, section_party)
            continue
        i += 1
    return rows


def _emit_block(rows, block_rows, office, district, col_nums, name_map,
                party_map, section_party):
    """Emit the precinct rows of one office/district block. If a
    (county, precinct) pair repeats, the block is the INTERLEAVED overflow
    layout (row A = cands col_nums, row B = the overflow cands beyond
    max(col_nums), printed in cols 1..extra of a second row); otherwise each
    precinct has one row and col_nums maps directly to candidate numbers."""
    if not block_rows or not col_nums:
        return
    ncols = len(col_nums)
    max_col = max(col_nums)
    extra = sum(1 for num in name_map if num > max_col)
    from collections import Counter
    counts = Counter((c, p) for (c, p, v) in block_rows)
    interleaved = extra > 0 and any(v > 1 for v in counts.values())
    if not interleaved:
        for (county, precinct, votes) in block_rows:
            for idx, num in enumerate(col_nums):
                rows.append((county, precinct, office, district,
                             party_map.get(num, section_party),
                             name_map.get(num, ""), votes[idx]))
        return
    _emit_interleaved(rows, block_rows, office, district, col_nums, name_map,
                     party_map, section_party, ncols, max_col, extra)


def _emit_interleaved(rows, block_rows, office, district, col_nums, name_map,
                     party_map, section_party, ncols, max_col, extra):
    """Pair the interleaved two-rows-per-precinct rows of an overflow block.
    Grouped by consecutive (county, precinct); a group of two is the (A, B)
    pair (told apart by trailing cols -- row B's cols extra..ncols are zero);
    a group of one is a row whose pair lives in an adjacent page-reprint
    block (e.g. ABSENTEE), classified by its trailing cols."""
    groups = []
    for (county, prec, votes) in block_rows:
        key = (county, prec)
        if groups and groups[-1][0] == key:
            groups[-1][1].append(votes)
        else:
            groups.append((key, [votes]))

    def is_row_a(v):
        # row A has a non-zero vote in the trailing columns; row B (overflow
        # cands in cols 1..extra) always has zeros in cols extra..ncols.
        return any(v[k] for k in range(extra, ncols))

    def emit(key, votes, kind):
        county, prec = key
        if kind == "A":
            for idx, num in enumerate(col_nums):
                rows.append((county, prec, office, district,
                             party_map.get(num, section_party),
                             name_map.get(num, ""), votes[idx]))
        else:  # overflow cands max_col+1..max_col+extra in cols 1..extra
            for j in range(extra):
                num = max_col + j + 1
                rows.append((county, prec, office, district,
                             party_map.get(num, section_party),
                             name_map.get(num, ""), votes[j]))

    for (key, vlist) in groups:
        if len(vlist) == 2:
            v1, v2 = vlist
            a1, a2 = is_row_a(v1), is_row_a(v2)
            if a1 and not a2:
                a, b = v1, v2
            elif a2 and not a1:
                a, b = v2, v1
            else:  # both zero-tail (or both non-zero, which shouldn't happen)
                a, b = v1, v2
            emit(key, a, "A")
            emit(key, b, "B")
        else:
            v = vlist[0]
            emit(key, v, "A" if is_row_a(v) else "B")


def _amd_county_tuples(county, nums):
    """6-field county rows (no precinct): (county, office, district, party,
    candidate, votes) for Yes/No x the two amendments."""
    a1y, a1n, a2y, a2n = nums
    out = []
    for off, (yes, no) in ((AMD_OFFICES[0], (a1y, a1n)),
                           (AMD_OFFICES[1], (a2y, a2n))):
        out.append((county, off, AMD_DISTRICT, AMD_PARTY, AMD_CANDS[0], yes))
        out.append((county, off, AMD_DISTRICT, AMD_PARTY, AMD_CANDS[1], no))
    return out


def _amd_precinct_tuples(county, precinct, nums):
    """7-field precinct rows: (county, precinct, office, district, party,
    candidate, votes) for Yes/No x the two amendments."""
    a1y, a1n, a2y, a2n = nums
    out = []
    for off, (yes, no) in ((AMD_OFFICES[0], (a1y, a1n)),
                           (AMD_OFFICES[1], (a2y, a2n))):
        out.append((county, precinct, off, AMD_DISTRICT, AMD_PARTY,
                    AMD_CANDS[0], yes))
        out.append((county, precinct, off, AMD_DISTRICT, AMD_PARTY,
                    AMD_CANDS[1], no))
    return out


def _amd_nums(toks):
    """Last 4 tokens as ints if all numeric, else None."""
    nums = toks[-4:]
    if len(toks) < 5 or not all(T0.NUMERIC_TOKEN_RE.match(t) for t in nums):
        return None
    return [int(t.replace(",", "")) for t in nums]


def parse_amendments_county(url):
    text = P.fetch_text(url)
    rows = []
    for line in text.split("\n"):
        s = line.strip()
        if not s:
            continue
        if "TOTAL" in s.upper():
            continue
        toks = s.split()
        nums = _amd_nums(toks)
        if not nums:
            continue
        county_toks = toks[:-4]
        if county_toks and all(T0.COUNTY_TOK_RE.match(t) for t in county_toks):
            county = P.norm_county(" ".join(county_toks))
            rows.extend(_amd_county_tuples(county, nums))
    return rows


def parse_amendments_precinct(url):
    text = P.fetch_text(url)
    lines = text.split("\n")
    rows = []
    current_county = None
    for line in lines:
        s = line.strip()
        if not s:
            continue
        if T0.DATE_RE.match(s):
            continue
        if s.startswith("County:"):
            current_county = P.norm_county(s.split(":", 1)[1].strip())
            continue
        if s.upper().startswith("COUNTY TOTAL") or "TOTAL" in s.upper():
            continue
        toks = s.split()
        nums = _amd_nums(toks)
        if not nums:
            continue
        lead = toks[:-4]
        if not lead or not current_county:
            continue
        # A reprinted column header ("1- Yes 1- No 2- Yes 2- No") -- already
        # skipped by _amd_nums (its last-4 tokens aren't numeric), but guard
        # anyway. Use a PRECISE check: skip only if EVERY lead token is a
        # header token, so precinct names containing "No" (e.g. Hamilton
        # "037 Northwoods No", "083 Soddy Daisy No") are NOT dropped.
        if all(t in _AMD_HEADER_TOKS for t in lead):
            continue
        name = " ".join(lead)
        if name[:2].lower() == "z-":
            name = name[2:]
        name = re.sub(r"\s+", " ", name).strip()
        letters = re.sub(r"[^A-Za-z]", "", name)
        precinct = string.capwords(name) if (letters and letters.isupper()) else name
        rows.extend(_amd_precinct_tuples(current_county, precinct, nums))
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


def build_primary():
    print("--- 20020801 primary ---")
    county_rows, precinct_rows = [], []
    for office in AUG_OFFICE_URL:
        crows, reg = parse_county_file(AUG_DEM_COUNTY[office])
        county_rows.extend(crows)
        precinct_rows.extend(parse_precinct_file(AUG_DEM_PRECINCT[office], reg))
        crows, reg = parse_county_file(AUG_REP_COUNTY[office])
        county_rows.extend(crows)
        precinct_rows.extend(parse_precinct_file(AUG_REP_PRECINCT[office], reg))
    county_rows.sort(key=sort_key_county)
    precinct_rows.sort(key=sort_key_precinct)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, "20020801__tn__primary__county.csv"),
              COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, "20020801__tn__primary__precinct.csv"),
              PRECINCT_HEADER, precinct_rows)


def build_general():
    print("--- 20021105 general ---")
    county_rows, precinct_rows = [], []
    for office in NOV_COUNTY:
        crows, reg = parse_county_file(NOV_COUNTY[office])
        county_rows.extend(crows)
        precinct_rows.extend(parse_precinct_file(NOV_PRECINCT[office], reg))
    county_rows.extend(parse_amendments_county(NOV_AMD_COUNTY))
    precinct_rows.extend(parse_amendments_precinct(NOV_AMD_PRECINCT))
    county_rows.sort(key=sort_key_county)
    precinct_rows.sort(key=sort_key_precinct)
    out = os.path.abspath(OUT_DIR)
    os.makedirs(out, exist_ok=True)
    write_csv(os.path.join(out, "20021105__tn__general__county.csv"),
              COUNTY_HEADER, county_rows)
    write_csv(os.path.join(out, "20021105__tn__general__precinct.csv"),
              PRECINCT_HEADER, precinct_rows)


def main():
    build_primary()
    build_general()


if __name__ == "__main__":
    main()