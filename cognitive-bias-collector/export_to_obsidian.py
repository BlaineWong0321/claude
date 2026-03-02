"""
Export articles_merged.csv to Obsidian Markdown notes.

Usage:
    python export_to_obsidian.py [csv_path] [vault_path]

Example:
    python export_to_obsidian.py results/articles_merged.csv ~/Documents/MyVault

Graph View year colors are written to <vault>/.obsidian/graph.json automatically.
Darker blue = newer paper.
"""

import csv
import json
import os
import re
import sys


# Year range in dataset
YEAR_MIN = 2018
YEAR_MAX = 2026

# Blue gradient: lightest (old) → darkest (new)
# HSL(210, saturation, lightness): lightness goes from 80% down to 20%
YEAR_COLORS = {
    2018: 0xADD8E6,  # light blue
    2019: 0x87CEEB,
    2020: 0x6CB4E4,
    2021: 0x4169E1,
    2022: 0x2E75D4,
    2023: 0x1A5DAD,
    2024: 0x0D4785,
    2025: 0x073763,
    2026: 0x02264A,  # very dark navy
}


def safe_filename(name: str, max_len: int = 80) -> str:
    name = re.sub(r'[<>:"/\\|?*\n\r]', '', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name[:max_len]


def parse_keyword_sets(raw: str) -> list[str]:
    return [k.strip() for k in raw.split(';') if k.strip()]


def make_paper_note(paper: dict) -> str:
    title    = paper['title'].replace('"', "'")
    authors  = paper['authors']
    year     = paper['year']
    journal  = paper['journal']
    doi      = paper['doi']
    abstract = paper['abstract']
    summary  = paper.get('summary_zh', '').strip()
    tier     = paper['tier']
    is_oa    = paper['is_oa']
    kw_sets  = parse_keyword_sets(paper['keyword_set'])

    tags = [f"year-{year}", f"tier-{tier}"]
    if is_oa == '1':
        tags.append("open-access")
    for kw in kw_sets:
        tags.append(f"topic-{kw.replace(' ', '_')}")

    tags_yaml = '\n'.join(f'  - {t}' for t in tags)

    journal_link = f"[[Journals/{safe_filename(journal)}|{journal}]]"
    topic_links  = '  '.join(
        f"[[Topics/{safe_filename(kw)}|{kw}]]" for kw in kw_sets
    )

    doi_line = f"[{doi}](https://doi.org/{doi})" if doi else 'N/A'
    oa_badge = "✓ Open Access" if is_oa == '1' else "✗ Closed"

    lines = [
        f"---",
        f'title: "{title}"',
        f'authors: "{authors}"',
        f"year: {year}",
        f'journal: "{journal}"',
        f'doi: "{doi}"',
        f"tier: {tier}",
        f"is_oa: {is_oa}",
        f"tags:",
        tags_yaml,
        f"---",
        f"",
        f"# {paper['title']}",
        f"",
        f"| Field   | Value |",
        f"|---------|-------|",
        f"| Authors | {authors} |",
        f"| Year    | {year} |",
        f"| Journal | {journal_link} |",
        f"| Tier    | {tier} |",
        f"| DOI     | {doi_line} |",
        f"| Access  | {oa_badge} |",
        f"| Topics  | {topic_links} |",
        f"",
    ]

    if summary:
        lines += [
            "## 摘要（中文）",
            "",
            summary,
            "",
            "## Abstract",
            "",
            abstract,
        ]
    else:
        lines += [
            "## Abstract",
            "",
            abstract,
        ]

    return '\n'.join(lines)


def make_journal_note(journal: str) -> str:
    return '\n'.join([
        f"# {journal}",
        "",
        "## Papers",
        "",
        "```dataview",
        "TABLE year, authors, tier",
        f'FROM [[{safe_filename(journal)}]]',
        "SORT year DESC",
        "```",
    ])


def make_topic_note(topic: str) -> str:
    return '\n'.join([
        f"# {topic}",
        "",
        "## Papers",
        "",
        "```dataview",
        "TABLE year, journal, authors",
        f'FROM [[{safe_filename(topic)}]]',
        "SORT year DESC",
        "```",
    ])


def make_graph_config(years: list[int]) -> dict:
    """
    Build Obsidian graph.json colorGroups so each year gets a distinct shade.
    Darker blue = newer year.
    """
    color_groups = []
    for year in sorted(years):
        rgb = YEAR_COLORS.get(year, 0x4169E1)
        color_groups.append({
            "query": f"tag:#year-{year}",
            "color": {"a": 1, "rgb": rgb},
        })
    return {
        "colorGroups": color_groups,
        "showTags": True,
        "showAttachments": False,
        "hideUnresolved": False,
        "showOrphans": True,
    }


def export(csv_path: str, vault_path: str):
    papers_dir   = os.path.join(vault_path, 'Papers')
    journals_dir = os.path.join(vault_path, 'Journals')
    topics_dir   = os.path.join(vault_path, 'Topics')
    obsidian_dir = os.path.join(vault_path, '.obsidian')

    for d in [papers_dir, journals_dir, topics_dir, obsidian_dir]:
        os.makedirs(d, exist_ok=True)

    journals_seen: set[str] = set()
    topics_seen:   set[str] = set()
    years_seen:    set[int] = set()

    with open(csv_path, encoding='utf-8-sig') as f:
        papers = list(csv.DictReader(f))

    for paper in papers:
        # Paper note
        note     = make_paper_note(paper)
        filename = safe_filename(paper['title']) + '.md'
        with open(os.path.join(papers_dir, filename), 'w', encoding='utf-8') as f:
            f.write(note)

        journals_seen.add(paper['journal'])
        years_seen.add(int(paper['year']))
        for kw in parse_keyword_sets(paper['keyword_set']):
            topics_seen.add(kw)

    # Journal hub notes
    for journal in journals_seen:
        path = os.path.join(journals_dir, safe_filename(journal) + '.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(make_journal_note(journal))

    # Topic hub notes
    for topic in topics_seen:
        path = os.path.join(topics_dir, safe_filename(topic) + '.md')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(make_topic_note(topic))

    # Graph color config
    graph_cfg = make_graph_config(list(years_seen))
    with open(os.path.join(obsidian_dir, 'graph.json'), 'w', encoding='utf-8') as f:
        json.dump(graph_cfg, f, indent=2)

    print(f"Done.")
    print(f"  {len(papers)} paper notes  → {papers_dir}")
    print(f"  {len(journals_seen)} journal notes → {journals_dir}")
    print(f"  {len(topics_seen)} topic notes   → {topics_dir}")
    print(f"  Graph color config  → {obsidian_dir}/graph.json")
    print()
    print("Next steps:")
    print("  1. Open Obsidian → Open folder as vault → select the vault path you specified")
    print("  2. Install plugins: Dataview (for table queries)")
    print("  3. Open Graph View (Ctrl+G) to see the paper network")


if __name__ == '__main__':
    _csv   = sys.argv[1] if len(sys.argv) > 1 else 'results/articles_merged.csv'
    _vault = sys.argv[2] if len(sys.argv) > 2 else './obsidian_vault'
    export(_csv, _vault)
