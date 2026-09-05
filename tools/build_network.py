#!/usr/bin/env python3
"""Build the GraphGarnish network JSON for Catalog & Cocktails.

Two modes, same code path:

  offline   Normalize and enrich whatever is already in the seed JSON.
            No network access needed.
  online    Additionally pull the podcast RSS feed, repair truncated
            titles, and append episodes published since the seed was made.

The seed JSON is treated as curated ground truth: guest and company
links that came from the spreadsheet are preserved as-is. Anything the
feed adds beyond the seed is auto-extracted and flagged for review.

Usage:
    python3 tools/build_network.py                      # offline, in place
    python3 tools/build_network.py --feed FEED_URL      # online
    python3 tools/build_network.py --report             # print a summary
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import unicodedata
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FEED = "https://feeds.casted.us/127/Catalog-&-Cocktails-2fcf8728/feed"
DEFAULT_SEED = ROOT / "catalog_cocktails.json"
DEFAULT_TOPICS = ROOT / "data" / "topics.json"
OUTPUTS = [ROOT / "catalog_cocktails.json", ROOT / "sample_network.json"]

# ---------------------------------------------------------------- utilities

def slug(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text[:limit] or "unknown"


def norm_key(title: str) -> str:
    """Normalized title with the companion-clip prefix and all punctuation
    removed. Not truncated: length differences are what let prefix_match
    line a 60-char-truncated seed title up with the full title from the feed."""
    t = strip_takeaway_prefix(title)
    t = t.replace("\u2019", "'").replace("\u2018", "'")
    t = re.sub(r"\bw/\b", "w", t, flags=re.I)
    return re.sub(r"[^a-z0-9]", "", t.lower())


MIN_PREFIX = 25  # shorter than this and unrelated episodes start colliding


def common_prefix_len(a: str, b: str) -> int:
    n = 0
    for x, y in zip(a, b):
        if x != y:
            break
        n += 1
    return n


def near_dates(date: str) -> set[str]:
    """The publication date and its immediate neighbours. Feed timestamps and
    spreadsheet dates routinely differ by a day across time zones."""
    try:
        y, m, d = (int(x) for x in date.split("-"))
        base = datetime.date(y, m, d)
    except (ValueError, AttributeError):
        return {date}
    delta = datetime.timedelta(days=1)
    return {(base + n * delta).isoformat() for n in (-1, 0, 1)}


def same_date_match(key: str, date: str, records: dict, companion: bool) -> str | None:
    """Last-resort match: same publication date (give or take a day), same kind
    of episode, and a long enough shared opening that it cannot plausibly be a
    different show."""
    if not date:
        return None
    window = near_dates(date)
    best, best_len = None, 0
    for (cand, cand_companion), rec in records.items():
        if cand_companion != companion or rec["date"] not in window:
            continue
        n = common_prefix_len(key, cand)
        if n >= MIN_PREFIX and n > best_len:
            best, best_len = cand, n
    return best


def prefix_match(key: str, candidates) -> str | None:
    """Find the candidate that is the same episode as `key`.

    Seed titles were cut at 60 characters and companion clips lose another
    12 to the "TAKEAWAYS - " prefix, so exact equality misses most pairs.
    Whichever string is shorter must be a prefix of the other, and must be
    long enough that the match is not a coincidence."""
    if key in candidates:
        return key
    best = None
    for cand in candidates:
        short, long = (key, cand) if len(key) <= len(cand) else (cand, key)
        if len(short) >= MIN_PREFIX and long.startswith(short):
            # Prefer the longest candidate that still matches.
            if best is None or len(cand) > len(best):
                best = cand
    return best


TAKEAWAY_RE = re.compile(r"^\s*TAKEAWAYS?\s*[-–—:]\s*", re.I)


def strip_takeaway_prefix(title: str) -> str:
    return TAKEAWAY_RE.sub("", title).strip()


def is_takeaway(title: str) -> bool:
    return bool(TAKEAWAY_RE.match(title))


# ------------------------------------------------------------ guest parsing

GUEST_SPLIT = re.compile(
    r"\s+(?:with|w/|w|ft\.?|feat\.?|featuring|hosts?|guest)\s+", re.I)

# Phrases that mean the captured tail is a topic, not a person.
NOT_A_NAME = re.compile(
    r"^(the|a|an|our|your|their|this|that|us|me|you|it|data|ai|no |some)\b", re.I)

PERSON_SPLIT = re.compile(r"\s*(?:,|&|\band\b)\s*", re.I)

# The two hosts appear on every episode. Treating them as guests would give
# them an edge to the whole graph and drown out the actual guest structure.
HOSTS = {"tim gasper", "juan sequeda"}

HONORIFIC_TAIL = re.compile(r",?\s*(Ph\.?D\.?|M\.?D\.?|Jr\.?|Sr\.?|III?|MBA)\.?$", re.I)


def looks_like_person(name: str) -> bool:
    name = name.strip()
    if not (3 <= len(name) <= 45):
        return False
    if NOT_A_NAME.match(name):
        return False
    if name.endswith("?") or name.endswith("!"):
        return False
    words = name.split()
    if not (2 <= len(words) <= 4):
        return False
    # Every word should start uppercase (allow lowercase particles like "van").
    caps = sum(1 for w in words if w[:1].isupper())
    return caps >= 2


def split_people(raw: str) -> list[str]:
    """'Dean Allemang & Bryon Jacob' -> ['Dean Allemang', 'Bryon Jacob']"""
    raw = re.sub(r"^Various guests:\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*[\(\[].*?[\)\]]", "", raw)
    raw = re.sub(r"\s*[–—-]\s*[“\"].*$", "", raw)  # trailing nickname
    parts = [p.strip(" .,-–—") for p in PERSON_SPLIT.split(raw)]
    out = []
    for p in parts:
        p = HONORIFIC_TAIL.sub("", p).strip()
        if looks_like_person(p):
            out.append(p)
    # A single name with a trailing honorific still counts.
    if not out:
        p = HONORIFIC_TAIL.sub("", raw).strip()
        if looks_like_person(p):
            out.append(p)
    return out


def guests_from_title(title: str) -> list[str]:
    t = strip_takeaway_prefix(title)
    parts = GUEST_SPLIT.split(t)
    if len(parts) < 2:
        return []
    return split_people(parts[-1].strip())


DESC_PATTERNS = [
    re.compile(r"(?:our guest(?:s)?(?: is| are|:)?|joined by|welcomes?|we(?:'re| are) joined by|special guest(?:s)?:?)\s+([A-Z][^.;\n]{2,80})"),
    re.compile(r"^\s*Guest(?:s)?:\s*([^\n.;]{2,80})", re.M),
]


def guests_from_description(desc: str) -> list[str]:
    for pat in DESC_PATTERNS:
        m = pat.search(desc)
        if m:
            people = split_people(m.group(1))
            if people:
                return people
    return []


# ------------------------------------------------------------- org cleaning

ORG_CUTS = re.compile(r"\s+(?:and|who|where|which|he|she|they|author|inventor|former|host)\b", re.I)


def clean_org(raw: str) -> str:
    """The seed's org names were parsed out of a free-text 'Role & Company'
    column and often ran past the company name. Trim back to the company."""
    if not raw:
        return ""
    org = raw.strip()
    org = re.split(r"[;,]", org, maxsplit=1)[0]
    org = ORG_CUTS.split(org, maxsplit=1)[0]
    org = org.strip().strip(".,;:").strip()
    org = re.sub(r"^(?:the\s+)?at\s+", "", org, flags=re.I).strip()
    # Repair the one known truncation of data.world.
    if org.lower() in {"data", "data."}:
        org = "data.world"
    return org


ORG_FROM_DESC = re.compile(
    r"\b(?:at|of|with|from)\s+([A-Z][\w&.\-]*(?:\s+[A-Z][\w&.\-]*){0,3})")


def org_from_description(desc: str, person: str) -> str:
    idx = desc.find(person)
    if idx < 0:
        return ""
    window = desc[idx: idx + 220]
    m = ORG_FROM_DESC.search(window)
    return clean_org(m.group(1)) if m else ""


# ------------------------------------------------------------------ topics

def load_topics(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["topics"]


def topics_for(text: str, taxonomy: list[dict]) -> list[str]:
    low = " " + text.lower() + " "
    hits = []
    for topic in taxonomy:
        if any(x in low for x in topic.get("exclude", [])):
            continue
        if any(m in low for m in topic["match"]):
            hits.append(topic["name"])
    return hits


# -------------------------------------------------------------- feed access

def fetch_feed(url: str, timeout: int = 60) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "GraphGarnish/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


MONTHS = {m: i for i, m in enumerate(
    "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split(), 1)}


def parse_pubdate(raw: str) -> str:
    """RFC-822 -> YYYY-MM-DD, without pulling in a date library."""
    if not raw:
        return ""
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\s+(\d{4})", raw)
    if m:
        day, mon, year = m.groups()
        if mon.title() in MONTHS:
            return f"{year}-{MONTHS[mon.title()]:02d}-{int(day):02d}"
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    return m.group(0) if m else ""


def strip_html(raw: str) -> str:
    txt = re.sub(r"<[^>]+>", " ", raw or "")
    txt = (txt.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
              .replace("&quot;", '"').replace("&#39;", "'").replace("&nbsp;", " "))
    return re.sub(r"\s+", " ", txt).strip()


def parse_feed(xml_text: str) -> list[dict]:
    root = ET.fromstring(xml_text)
    channel = root.find("channel") or root
    items = []
    for it in channel.findall("item"):
        def txt(tag):
            el = it.find(tag)
            return (el.text or "") if el is not None else ""
        title = strip_html(txt("title"))
        if not title:
            continue
        desc = strip_html(txt("description")) or strip_html(
            txt("{http://purl.org/rss/1.0/modules/content/}encoded"))
        items.append({
            "title": title,
            "date": parse_pubdate(txt("pubDate")),
            "description": desc,
            "url": txt("link").strip(),
        })
    return items


# ------------------------------------------------------------------- build

def index_seed(seed: dict) -> dict:
    nodes = {n["id"]: n for n in seed["nodes"]}
    guests = defaultdict(list)
    orgs = defaultdict(list)
    for link in seed.get("links", []):
        src, tgt = link["source"], link["target"]
        if link["type"] == "GUEST_ON":
            guests[tgt].append(nodes[src]["name"])
        elif link["type"] == "AFFILIATED_WITH":
            orgs[nodes[src]["name"]].append(nodes[tgt]["name"])
    episodes = {}
    for n in seed["nodes"]:
        if n["type"] != "episode":
            continue
        episodes[(norm_key(n["name"]), is_takeaway(n["name"]))] = {
            "name": n["name"],
            "date": n.get("date", ""),
            "guests": guests.get(n["id"], []),
        }
    person_org = {p: clean_org(v[0]) for p, v in orgs.items() if v}
    return {"episodes": episodes, "person_org": person_org}


def build(seed: dict, feed_items: list[dict], taxonomy: list[dict]) -> dict:
    idx = index_seed(seed)
    seed_eps = idx["episodes"]
    person_org = dict(idx["person_org"])

    # Start from every seed episode, then let feed items overwrite/extend.
    records: dict[tuple[str, bool], dict] = {}
    for key, ep in seed_eps.items():
        records[key] = {
            "title": ep["name"], "date": ep["date"], "description": "",
            "url": "", "guests": list(ep["guests"]), "source": "seed",
        }

    for item in feed_items:
        companion = is_takeaway(item["title"])
        same_kind = [k for (k, c) in records if c == companion]
        ikey = norm_key(item["title"])
        hit = prefix_match(ikey, same_kind)
        if hit is None:
            # The seed titles came from a spreadsheet and sometimes diverge from
            # the feed's wording before the 60-char cut, so a prefix test misses.
            # Same publication date plus a long shared opening is enough.
            hit = same_date_match(ikey, item["date"], records, companion)
        key = (hit, companion) if hit else (ikey, companion)
        rec = records.get(key)
        if rec:
            # Feed wins on title (untruncated) and description; seed wins on guests.
            rec["title"] = item["title"]
            rec["description"] = item["description"]
            rec["url"] = item["url"] or rec["url"]
            rec["date"] = item["date"] or rec["date"]
            rec["source"] = "seed+feed"
        else:
            records[key] = {
                "title": item["title"], "date": item["date"],
                "description": item["description"], "url": item["url"],
                "guests": [], "source": "feed",
            }

    # Expand any seed guest entry that packed several humans into one name,
    # and fill in guests for feed-only episodes.
    review = []
    for key, rec in records.items():
        expanded = []
        for name in rec["guests"]:
            people = split_people(name)
            expanded.extend(people if people else [name])
        rec["guests"] = [p for p in expanded if p.lower() not in HOSTS]
        if not rec["guests"] and not is_takeaway(rec["title"]):
            found = guests_from_title(rec["title"]) or guests_from_description(rec["description"])
            found = [p for p in found if p.lower() not in HOSTS]
            if found:
                rec["guests"] = found
                rec["inferred"] = True
            elif rec["source"] != "seed":
                review.append({"date": rec["date"], "title": rec["title"],
                               "reason": "no guest found in title or description"})

    nodes, links = [], []
    seen_nodes, seen_links = set(), set()

    def add_node(node):
        if node["id"] in seen_nodes:
            return node["id"]
        seen_nodes.add(node["id"])
        nodes.append(node)
        return node["id"]

    def add_link(src, tgt, kind):
        sig = (src, tgt, kind)
        if sig in seen_links:
            return
        seen_links.add(sig)
        links.append({"source": src, "target": tgt, "type": kind})

    ordered = sorted(records.items(), key=lambda kv: (kv[1]["date"], kv[1]["title"]))
    node_id: dict[tuple[str, bool], str] = {}
    used_ids: set[str] = set()
    for (key, companion), rec in ordered:
        full = not companion
        eid = base_eid = "ep_" + (rec["date"] or "0000-00-00").replace("-", "") + "_" + slug(
            strip_takeaway_prefix(rec["title"]), 24) + ("" if full else "_ta")
        # Same date and same first 24 slug characters still means two different
        # episodes (e.g. a two-parter). Keep them apart.
        suffix = 2
        while eid in used_ids:
            eid = f"{base_eid}_{suffix}"
            suffix += 1
        used_ids.add(eid)
        node = {
            "id": eid, "name": rec["title"], "type": "episode",
            "date": rec["date"], "is_full": full,
        }
        if rec["url"]:
            node["url"] = rec["url"]
        if rec["description"]:
            node["summary"] = rec["description"][:400]
        add_node(node)
        node_id[(key, companion)] = eid

    # Attach each companion clip to the full episode it summarizes, so it
    # stops standing in the graph as a second copy of that episode.
    full_keys = [k for (k, c) in records if not c]
    parent_of: dict[tuple[str, bool], str] = {}
    for (key, companion) in records:
        if not companion:
            continue
        hit = prefix_match(key, full_keys)
        if hit:
            parent_of[(key, companion)] = node_id[(hit, False)]
            add_link(node_id[(key, companion)], node_id[(hit, False)], "TAKEAWAY_OF")

    for (key, companion), rec in ordered:
        full = not companion
        eid = node_id[(key, companion)]
        parent = parent_of.get((key, companion))
        # A companion whose parent is missing keeps its own guests so the
        # episode is not lost from the graph entirely.
        target_for_guests = parent or eid
        if full or parent is None:
            for person in rec["guests"]:
                pid = "person_" + slug(person)
                add_node({"id": pid, "name": person, "type": "person"})
                add_link(pid, target_for_guests, "GUEST_ON")
                org = person_org.get(person) or org_from_description(rec["description"], person)
                org = clean_org(org)
                if org:
                    oid = "org_" + slug(org)
                    add_node({"id": oid, "name": org, "type": "organization"})
                    add_link(pid, oid, "AFFILIATED_WITH")
        # Topics come off the full episode only, so companions don't double-count.
        if full:
            text = rec["title"] + " " + rec["description"]
            for topic in topics_for(text, taxonomy):
                tid = "topic_" + slug(topic)
                add_node({"id": tid, "name": topic, "type": "topic"})
                add_link(eid, tid, "COVERS")

    counts = Counter(n["type"] for n in nodes)
    return {
        "meta": {
            "generated_by": "tools/build_network.py",
            "source": "seed+feed" if feed_items else "seed only (offline)",
            "counts": dict(counts),
            "episodes_needing_review": review,
        },
        "nodes": nodes,
        "links": links,
    }


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", nargs="?", const=DEFAULT_FEED, default=None,
                    help=f"fetch the RSS feed (default {DEFAULT_FEED})")
    ap.add_argument("--feed-file", help="parse a local RSS file instead of fetching")
    ap.add_argument("--save-feed", type=Path,
                    help="write the fetched RSS XML here before parsing it, so a "
                         "parsing problem can be reproduced without the network")
    ap.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    ap.add_argument("--topics", type=Path, default=DEFAULT_TOPICS)
    ap.add_argument("--out", type=Path, action="append",
                    help="output path (repeatable; defaults to both site JSON files)")
    ap.add_argument("--report", action="store_true", help="print a summary to stderr")
    args = ap.parse_args()

    seed = json.loads(args.seed.read_text(encoding="utf-8"))
    taxonomy = load_topics(args.topics)

    items = []
    if args.feed_file:
        items = parse_feed(Path(args.feed_file).read_text(encoding="utf-8"))
    elif args.feed:
        try:
            raw = fetch_feed(args.feed)
            if args.save_feed:
                args.save_feed.write_text(raw, encoding="utf-8")
                print(f"saved raw feed to {args.save_feed}", file=sys.stderr)
            items = parse_feed(raw)
        except Exception as exc:  # noqa: BLE001 - report and fall back
            print(f"feed fetch failed ({exc}); continuing offline", file=sys.stderr)

    graph = build(seed, items, taxonomy)

    outputs = args.out or OUTPUTS
    payload = json.dumps(graph, indent=2, ensure_ascii=False) + "\n"
    for path in outputs:
        Path(path).write_text(payload, encoding="utf-8")

    if args.report:
        m = graph["meta"]
        print(f"source: {m['source']}", file=sys.stderr)
        print(f"feed items parsed: {len(items)}", file=sys.stderr)
        for k, v in sorted(m["counts"].items()):
            print(f"  {k:14s} {v}", file=sys.stderr)
        print(f"  links          {len(graph['links'])}", file=sys.stderr)
        if m["episodes_needing_review"]:
            print(f"\n{len(m['episodes_needing_review'])} episode(s) need a guest checked:",
                  file=sys.stderr)
            for r in m["episodes_needing_review"][:20]:
                print(f"  {r['date']}  {r['title'][:70]}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
