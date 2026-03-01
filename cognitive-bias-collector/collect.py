"""
collect.py — fetch metadata from OpenAlex by ISSN + broad date window,
filter by keywords (title + abstract), look up authoritative publication
year from Crossref by DOI, look up OA PDFs via Unpaywall, download where
possible.

Design rationale
----------------
OpenAlex as primary source (bulk fetch with abstracts):
  OpenAlex provides abstracts in bulk, enabling keyword matching on both
  title and abstract without extra per-paper API calls. However, OpenAlex
  assigns the earliest-known version date to a paper: for many econ papers,
  this is the SSRN/NBER working-paper year — often 5-10 years before the
  actual journal publication. The fetch window is therefore expanded 8 years
  backwards to avoid silently dropping papers like
  "Correlation Neglect in Belief Formation" (SSRN 2013, RES Jan 2019).

Crossref for authoritative year (per matched paper):
  After keyword matching, each hit is cross-referenced against Crossref to
  get the actual journal-publication year. Priority:
    published-print > published-online > issued
  If Crossref returns a print year < date_from_year, the paper is excluded.
  If Crossref has no print year (common for OUP online-first articles), the
  online/issued year is used and the paper is kept if that year >= (date_from
  year - 2), since online-first articles are typically in a print volume 1-2
  years later.
"""
import argparse, csv, json, os, re, time
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
JOURNALS_CSV     = os.path.join(ROOT, "config", "journals.csv")
KEYWORDS_TXT     = os.path.join(ROOT, "config", "keywords_broad.txt")
WINDOW_JSON      = os.path.join(ROOT, "config", "window.json")
YEAR_OVERRIDES   = os.path.join(ROOT, "config", "year_overrides.json")
RESULTS_CSV      = os.path.join(ROOT, "results", "articles_broad.csv")
PAYWALLED_TXT    = os.path.join(ROOT, "results", "paywalled_broad.txt")
PDF_DIR          = os.path.join(ROOT, "pdfs")

CROSSREF_API  = "https://api.crossref.org/works"
OPENALEX_API  = "https://api.openalex.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"

# 重要：换成你自己的邮箱（用于 Crossref polite pool + Unpaywall API）
MAILTO = "your_email@domain.com"


# --- utils ---

def load_keywords(path=None):
    """Load keywords from a flat or structured (HARD/SOFT/ANCHORS) file.

    Flat format  → returns a list of strings (original behaviour).
    Structured   → returns a dict with keys 'hard', 'soft', 'anchors'.
    """
    if path is None:
        path = KEYWORDS_TXT
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    # Detect structured format by presence of section headers like [HARD]
    section_headers = {l.strip() for l in lines
                       if l.strip().startswith("[") and l.strip().endswith("]")}
    if section_headers:
        result = {"hard": [], "soft": [], "anchors": []}
        mode = None
        for line in lines:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            if s == "[HARD]":
                mode = "hard"
            elif s == "[SOFT]":
                mode = "soft"
            elif s == "[ANCHORS]":
                mode = "anchors"
            elif mode:
                result[mode].append(s.lower())
        return result

    # Flat format (original keywords.txt)
    return [l.strip().lower() for l in lines
            if l.strip() and not l.strip().startswith("#")]


def keyword_hit(text, kws):
    """Return True if text matches the keyword specification.

    kws may be:
      - a list  → flat mode: True if any keyword is a substring of text
      - a dict  → structured mode (HARD/SOFT/ANCHORS):
                  True if any HARD keyword matches, OR
                  any SOFT keyword matches AND any ANCHOR keyword matches
    """
    if not text:
        return False
    t = text.lower()
    if isinstance(kws, dict):
        if any(kw in t for kw in kws.get("hard", [])):
            return True
        if any(kw in t for kw in kws.get("soft", [])):
            return any(anc in t for anc in kws.get("anchors", []))
        return False
    return any(kw in t for kw in kws)

def safe_filename(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\w\-\.\(\) ]+", "_", s)
    return s[:180]


# --- OpenAlex (primary: bulk fetch with abstracts) ---

def openalex_fetch_by_issn(issn, date_from, date_to, per_page=200, max_pages=200):
    items = []
    cursor = "*"
    for _ in range(max_pages):
        params = {
            "filter": (
                f"primary_location.source.issn:{issn},"
                f"from_publication_date:{date_from},"
                f"to_publication_date:{date_to}"
            ),
            "per-page": per_page,
            "cursor": cursor,
            "mailto": MAILTO,
        }
        r = requests.get(OPENALEX_API, params=params, timeout=60)
        r.raise_for_status()
        msg = r.json()
        batch = msg.get("results", [])
        if not batch:
            break
        items.extend(batch)
        cursor = msg.get("meta", {}).get("next_cursor")
        if not cursor:
            break
        time.sleep(0.2)
    return items

def reconstruct_openalex_abstract(inv_idx):
    if not inv_idx:
        return ""
    positions = {}
    for word, pos_list in inv_idx.items():
        for p in pos_list:
            positions[p] = word
    return " ".join(positions[p] for p in sorted(positions))


# --- Crossref (authoritative year, per matched paper) ---

def crossref_fetch_by_doi(doi):
    """Fetch Crossref metadata for a single DOI. Returns message dict or None."""
    try:
        r = requests.get(
            f"{CROSSREF_API}/{doi}",
            params={"mailto": MAILTO},
            timeout=30,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json().get("message")
    except Exception:
        return None

def crossref_year(msg, key):
    """Extract year from a Crossref date field (published-print / published-online / issued)."""
    try:
        parts = msg[key]["date-parts"][0]
        if parts and parts[0]:
            return int(parts[0])
    except Exception:
        pass
    return None

def crossref_authors(msg):
    authors = msg.get("author") or []
    parts = []
    for a in authors[:6]:
        given  = (a.get("given")  or "").strip()
        family = (a.get("family") or "").strip()
        name = f"{given} {family}".strip() if given else family
        if name:
            parts.append(name)
    return "; ".join(parts)


# --- Unpaywall ---

def unpaywall_lookup(doi):
    url = f"{UNPAYWALL_API}/{doi}"
    r = requests.get(url, params={"email": MAILTO}, timeout=60)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()

def download_pdf(url, out_path):
    r = requests.get(url, stream=True, timeout=120)
    r.raise_for_status()
    with open(out_path, "wb") as f:
        for chunk in r.iter_content(chunk_size=1024 * 256):
            if chunk:
                f.write(chunk)


# --- main ---

def main():
    parser = argparse.ArgumentParser(description="Collect cognitive-bias papers from OpenAlex.")
    parser.add_argument("--keywords", default=KEYWORDS_TXT,
                        help="Path to keywords file (flat or HARD/SOFT/ANCHORS format). "
                             f"Default: {KEYWORDS_TXT}")
    parser.add_argument("--output", default=RESULTS_CSV,
                        help=f"Path for output articles CSV. Default: {RESULTS_CSV}")
    args = parser.parse_args()

    results_csv  = args.output
    paywalled_txt = os.path.join(os.path.dirname(results_csv),
                                 "paywalled_" + os.path.basename(results_csv).replace("articles_", "").replace(".csv", "") + ".txt")
    # Keep original path for the default case so existing callers are unaffected
    if args.output == RESULTS_CSV:
        paywalled_txt = PAYWALLED_TXT

    os.makedirs(os.path.dirname(results_csv), exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)

    with open(WINDOW_JSON, "r", encoding="utf-8") as f:
        window = json.load(f)
    date_from, date_to = window["from"], window["to"]
    date_from_year = int(date_from[:4])

    # OpenAlex fetch window: 8 years earlier to capture papers with old
    # working-paper dates (e.g., SSRN 2013, journal pub 2019).
    fetch_from_year = date_from_year - 8
    fetch_from = f"{fetch_from_year}{date_from[4:]}"  # e.g. 2010-01-01

    # Manual year corrections for papers where no free API has the print year.
    # Keys are lowercase DOIs without the https://doi.org/ prefix.
    try:
        with open(YEAR_OVERRIDES, "r", encoding="utf-8") as f:
            raw = json.load(f)
        year_overrides = {k: v for k, v in raw.items() if not k.startswith("_")}
    except FileNotFoundError:
        year_overrides = {}

    kws = load_keywords(args.keywords)

    journals = []
    with open(JOURNALS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            issn = (row.get("issn_online") or "").strip() or (row.get("issn_print") or "").strip()
            if issn:
                journals.append(row)

    out_rows  = []
    paywalled = []

    for j in journals:
        tier  = j.get("tier", "").strip()
        jname = j.get("journal_name", "").strip()
        issn  = (j.get("issn_online") or "").strip() or (j.get("issn_print") or "").strip()
        print(f"[OpenAlex] {jname} ({issn}) ...")

        items = openalex_fetch_by_issn(issn, fetch_from, date_to)

        for w in items:
            doi       = (w.get("doi") or "").replace("https://doi.org/", "").strip()
            title     = (w.get("title") or "").strip()
            abstract  = reconstruct_openalex_abstract(w.get("abstract_inverted_index"))
            container = (w.get("primary_location") or {}).get("source", {}).get("display_name", "") or ""
            oa_year   = w.get("publication_year")  # may be working-paper year

            # Keyword filter on title + abstract
            if not keyword_hit(f"{title}\n{abstract}", kws):
                continue

            # --- Authoritative year from Crossref ---
            year = oa_year  # fallback
            author_str = "; ".join(
                [(a.get("author", {}).get("display_name") or "").strip()
                 for a in (w.get("authorships") or [])][:6]
            )

            # Only call Crossref when OpenAlex year looks suspicious
            # (working-paper date earlier than our window start).
            # This avoids ~280 redundant API calls for normal papers.
            needs_crossref = doi and (oa_year is None or oa_year < date_from_year)
            if needs_crossref:
                cr = crossref_fetch_by_doi(doi)
                time.sleep(0.2)
                if cr:
                    print_yr  = crossref_year(cr, "published-print")
                    online_yr = crossref_year(cr, "published-online")
                    issued_yr = crossref_year(cr, "issued")
                    if print_yr:
                        if print_yr < date_from_year:
                            continue  # genuinely published before our window
                        year = print_yr
                    elif online_yr or issued_yr:
                        best_yr = online_yr or issued_yr
                        # Online-first: allow if online year is within 1 year of window start
                        if best_yr < date_from_year - 1:
                            continue  # too old to plausibly be in our window
                        year = best_yr
                    else:
                        continue  # no date info at all; skip
                    cr_authors = crossref_authors(cr)
                    if cr_authors:
                        author_str = cr_authors
                else:
                    # Crossref has no record → skip (can't verify year)
                    continue
            elif oa_year and oa_year < date_from_year:
                # No DOI and OpenAlex year is before window; skip
                continue

            # Apply manual year override if present
            if doi in year_overrides:
                year = year_overrides[doi]

            # --- Unpaywall: find OA PDF ---
            oa          = unpaywall_lookup(doi) if doi else None
            is_oa       = bool(oa and oa.get("is_oa"))
            best        = (oa or {}).get("best_oa_location") or {}
            pdf_url     = best.get("url_for_pdf")
            landing_url = best.get("url") or (oa or {}).get("doi_url")

            pdf_path = ""
            if pdf_url:
                fn       = safe_filename(f"{year or 'NA'}_{jname}_{title}_{doi.replace('/', '_')}.pdf")
                pdf_path = os.path.join("pdfs", fn)
                abs_path = os.path.join(ROOT, pdf_path)
                if not os.path.exists(abs_path):
                    try:
                        download_pdf(pdf_url, abs_path)
                    except Exception as e:
                        print(f"  [download failed] {doi}: {e}")
                        pdf_path = ""

            if (not pdf_path) and (not pdf_url):
                paywalled.append(doi)

            out_rows.append({
                "tier":            tier,
                "journal":         jname,
                "container_title": container,
                "year":            year or "",
                "title":           title,
                "authors":         author_str,
                "doi":             doi,
                "abstract":        abstract,
                "is_oa":           "1" if is_oa else "0",
                "oa_pdf_url":      pdf_url or "",
                "landing_url":     landing_url or "",
                "pdf_path":        pdf_path,
            })

        time.sleep(0.3)

    # write outputs
    fieldnames = [
        "tier", "journal", "container_title", "year", "title",
        "authors", "doi", "abstract", "is_oa", "oa_pdf_url", "landing_url", "pdf_path"
    ]
    with open(results_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    with open(paywalled_txt, "w", encoding="utf-8") as f:
        for doi in sorted(set([d for d in paywalled if d])):
            f.write(doi + "\n")

    print(f"\nDone.\n- Metadata: {results_csv}\n- Paywalled DOI list: {paywalled_txt}\n- PDFs (OA only): {PDF_DIR}\n")


if __name__ == "__main__":
    main()
