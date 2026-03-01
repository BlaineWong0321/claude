"""
compare.py — Compare two keyword strategies on the already-fetched articles.csv.

Usage:
    python compare.py                          # uses defaults below
    python compare.py --base results/articles.csv \
                      --belief results/articles_belief.csv   # after full re-run

Without a second CSV, compare.py applies the belief keyword logic directly to
the existing articles.csv (fast, offline — no API calls).  This shows:
  - Papers KEPT by both strategies     (intersection)
  - Papers DROPPED by belief keywords  (in base, not in belief)
  - Papers ADDED by belief keywords    (in belief, not in base)
                                        → only meaningful after a full re-run

Run `python collect.py --keywords config/keywords_belief.txt \
                       --output results/articles_belief.csv`
first to get a full belief-filtered dataset.
"""
import argparse, csv, os, sys

ROOT = os.path.dirname(os.path.abspath(__file__))
BASE_CSV   = os.path.join(ROOT, "results", "articles.csv")
BELIEF_CSV = os.path.join(ROOT, "results", "articles_belief.csv")
KW_BELIEF  = os.path.join(ROOT, "config", "keywords_belief.txt")


# ---------------------------------------------------------------------------
# keyword helpers (same logic as collect.py)
# ---------------------------------------------------------------------------

def load_keywords(path):
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
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
    return [l.strip().lower() for l in lines
            if l.strip() and not l.strip().startswith("#")]


def keyword_hit(text, kws):
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


def which_keywords_matched(text, kws):
    """Return the list of keyword(s) that caused a match (for diagnostics)."""
    if not text:
        return []
    t = text.lower()
    matched = []
    if isinstance(kws, dict):
        for kw in kws.get("hard", []):
            if kw in t:
                matched.append(f"HARD:{kw}")
        soft_hits = [kw for kw in kws.get("soft", []) if kw in t]
        if soft_hits:
            anchor_hits = [a for a in kws.get("anchors", []) if a in t]
            if anchor_hits:
                for kw in soft_hits:
                    matched.append(f"SOFT:{kw}+ANC:{anchor_hits[0]}")
    else:
        matched = [kw for kw in kws if kw in t]
    return matched


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def read_csv(path):
    with open(path, "r", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def doi_key(row):
    return (row.get("doi") or "").strip().lower()


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base",   default=BASE_CSV,
                        help=f"Original articles CSV (default: {BASE_CSV})")
    parser.add_argument("--belief", default=None,
                        help=f"Belief-filtered articles CSV (optional; "
                             f"default: {BELIEF_CSV} if it exists, "
                             "else applies belief keywords to --base in-memory)")
    parser.add_argument("--kw-belief", default=KW_BELIEF,
                        help=f"Belief keyword file (default: {KW_BELIEF})")
    parser.add_argument("--show-dropped", action="store_true",
                        help="Print titles of papers dropped by belief filter")
    parser.add_argument("--show-added", action="store_true",
                        help="Print titles of papers added by belief filter (requires --belief CSV)")
    args = parser.parse_args()

    if not os.path.exists(args.base):
        sys.exit(f"Base CSV not found: {args.base}")

    base_rows = read_csv(args.base)
    kws_belief = load_keywords(args.kw_belief)

    # ---- Apply belief filter in-memory to the base CSV ----
    kept_in_memory = []
    dropped        = []
    for row in base_rows:
        text = f"{row.get('title', '')}\n{row.get('abstract', '')}"
        if keyword_hit(text, kws_belief):
            kept_in_memory.append(row)
        else:
            dropped.append(row)

    print("=" * 70)
    print("STRATEGY COMPARISON")
    print("=" * 70)
    print(f"\nBase CSV ({args.base}):")
    print(f"  Total papers: {len(base_rows)}")
    print(f"\nBelief keywords applied to base CSV (in-memory, no API calls):")
    print(f"  Kept   (pass belief filter): {len(kept_in_memory)}")
    print(f"  Dropped (fail belief filter): {len(dropped)}")

    # ---- If a belief CSV exists from a full re-run, compare DOIs ----
    belief_path = args.belief or (BELIEF_CSV if os.path.exists(BELIEF_CSV) else None)
    if belief_path and os.path.exists(belief_path):
        belief_rows = read_csv(belief_path)
        base_dois   = {doi_key(r) for r in base_rows}
        belief_dois = {doi_key(r) for r in belief_rows}

        only_in_base   = base_dois - belief_dois
        only_in_belief = belief_dois - base_dois
        in_both        = base_dois & belief_dois

        print(f"\nFull re-run belief CSV ({belief_path}):")
        print(f"  Total papers: {len(belief_rows)}")
        print(f"\nDOI-level diff:")
        print(f"  In both               : {len(in_both)}")
        print(f"  Only in BASE (dropped): {len(only_in_base)}")
        print(f"  Only in BELIEF (added): {len(only_in_belief)}")

        if args.show_added and only_in_belief:
            print("\n--- Papers ADDED by belief keywords (not in base) ---")
            doi_to_row = {doi_key(r): r for r in belief_rows}
            for doi in sorted(only_in_belief):
                r = doi_to_row[doi]
                text = f"{r.get('title','')}\n{r.get('abstract','')}"
                hits = which_keywords_matched(text, kws_belief)
                print(f"  [{r.get('journal','')[:20]}] {r.get('title','')[:60]}")
                print(f"    ↳ matched: {', '.join(hits[:3])}")
    else:
        print(f"\n(No belief CSV found at {BELIEF_CSV}. "
              "Run collect.py with --keywords and --output to generate one.)")

    # ---- Dropped papers details ----
    if args.show_dropped and dropped:
        print(f"\n--- Papers DROPPED by belief filter ({len(dropped)} total) ---")
        for row in sorted(dropped, key=lambda r: r.get("journal", "")):
            print(f"  [{row.get('journal','')[:20]}] {row.get('title','')[:65]}")

    # ---- Breakdown: what matched what ----
    print("\n--- Belief keyword match breakdown (in-memory kept papers) ---")
    hard_count = soft_count = 0
    keyword_freq = {}
    for row in kept_in_memory:
        text = f"{row.get('title','')}\n{row.get('abstract','')}"
        hits = which_keywords_matched(text, kws_belief)
        is_hard = any(h.startswith("HARD:") for h in hits)
        if is_hard:
            hard_count += 1
        else:
            soft_count += 1
        for h in hits:
            kw = h.split(":")[1].split("+")[0]
            keyword_freq[kw] = keyword_freq.get(kw, 0) + 1

    print(f"  Via HARD keywords: {hard_count}")
    print(f"  Via SOFT+ANCHOR  : {soft_count}")
    print("\n  Top matched keywords:")
    for kw, cnt in sorted(keyword_freq.items(), key=lambda x: -x[1])[:20]:
        print(f"    {cnt:4d}  {kw}")


if __name__ == "__main__":
    main()
