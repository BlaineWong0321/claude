"""
collect.py — fetch metadata from Crossref by ISSN + date window,
filter by keywords, look up OA PDFs via Unpaywall, download where possible.
"""
import csv, json, os, re, time
from datetime import datetime
from urllib.parse import urlencode
import requests

ROOT = os.path.dirname(os.path.abspath(__file__))
JOURNALS_CSV  = os.path.join(ROOT, "config", "journals.csv")
KEYWORDS_TXT  = os.path.join(ROOT, "config", "keywords.txt")
WINDOW_JSON   = os.path.join(ROOT, "config", "window.json")
RESULTS_CSV   = os.path.join(ROOT, "results", "articles.csv")
PAYWALLED_TXT = os.path.join(ROOT, "results", "paywalled.txt")
PDF_DIR       = os.path.join(ROOT, "pdfs")

CROSSREF_API  = "https://api.crossref.org/works"
UNPAYWALL_API = "https://api.unpaywall.org/v2"

# 重要：换成你自己的邮箱（用于 Crossref polite pool + Unpaywall API）
MAILTO = "your_email@domain.com"


# --- utils ---

def load_keywords():
    with open(KEYWORDS_TXT, "r", encoding="utf-8") as f:
        kws = [line.strip().lower() for line in f if line.strip() and not line.strip().startswith("#")]
    return kws

def keyword_hit(text, kws):
    if not text:
        return False
    t = text.lower()
    return any(kw in t for kw in kws)

def safe_filename(s):
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"[^\w\-\.\(\) ]+", "_", s)
    return s[:180]

def crossref_fetch_by_issn(issn, date_from, date_to, rows=200, max_items=5000):
    """
    Uses cursor-based pagination.
    """
    items = []
    cursor = "*"
    seen = 0
    while True:
        params = {
            "filter": f"issn:{issn},from-pub-date:{date_from},until-pub-date:{date_to},type:journal-article",
            "rows": rows,
            "cursor": cursor,
            "mailto": MAILTO,
            "select": "DOI,title,author,issued,published-online,published-print,container-title,URL"
        }
        r = requests.get(CROSSREF_API, params=params, timeout=60)
        r.raise_for_status()
        data = r.json()["message"]
        batch = data.get("items", [])
        if not batch:
            break
        items.extend(batch)
        seen += len(batch)
        cursor = data.get("next-cursor")
        if not cursor or seen >= max_items:
            break
        time.sleep(0.2)  # be polite
    return items

def unpaywall_lookup(doi):
    # Unpaywall: /v2/{doi}?email=...
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

def pick_year(item):
    # Prefer issued.year; fallback to published-print/online
    for k in ("issued", "published-print", "published-online"):
        try:
            parts = item[k]["date-parts"][0]
            return int(parts[0])
        except Exception:
            pass
    return None


def main():
    os.makedirs(os.path.dirname(RESULTS_CSV), exist_ok=True)
    os.makedirs(PDF_DIR, exist_ok=True)

    with open(WINDOW_JSON, "r", encoding="utf-8") as f:
        window = json.load(f)
    date_from, date_to = window["from"], window["to"]

    kws = load_keywords()

    # Read journals
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
        print(f"[Crossref] {jname} ({issn}) ...")
        items = crossref_fetch_by_issn(issn, date_from, date_to)

        for it in items:
            doi       = it.get("DOI")
            title     = " ".join(it.get("title") or []).strip()
            container = " ".join(it.get("container-title") or []).strip()
            year      = pick_year(it)

            # keyword filter on title + container (Crossref abstracts often missing)
            text_for_filter = f"{title} {container}"
            if not keyword_hit(text_for_filter, kws):
                continue

            # Unpaywall: find OA PDF
            oa       = unpaywall_lookup(doi) if doi else None
            is_oa    = bool(oa and oa.get("is_oa"))
            best     = (oa or {}).get("best_oa_location") or {}
            pdf_url  = best.get("url_for_pdf")   # preferred direct PDF when present
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

            authors = it.get("author") or []
            author_str = "; ".join(
                [(" ".join([a.get("given","").strip(), a.get("family","").strip()]).strip())
                 for a in authors][:6]
            )

            out_rows.append({
                "tier":           tier,
                "journal":        jname,
                "container_title": container,
                "year":           year or "",
                "title":          title,
                "authors":        author_str,
                "doi":            doi or "",
                "is_oa":          "1" if is_oa else "0",
                "oa_pdf_url":     pdf_url or "",
                "landing_url":    landing_url or "",
                "pdf_path":       pdf_path
            })

        time.sleep(0.3)

    # write outputs
    fieldnames = [
        "tier", "journal", "container_title", "year", "title",
        "authors", "doi", "is_oa", "oa_pdf_url", "landing_url", "pdf_path"
    ]
    with open(RESULTS_CSV, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(out_rows)

    with open(PAYWALLED_TXT, "w", encoding="utf-8") as f:
        for doi in sorted(set([d for d in paywalled if d])):
            f.write(doi + "\n")

    print(f"\nDone.\n- Metadata: {RESULTS_CSV}\n- Paywalled DOI list: {PAYWALLED_TXT}\n- PDFs (OA only): {PDF_DIR}\n")


if __name__ == "__main__":
    main()
