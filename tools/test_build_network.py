#!/usr/bin/env python3
"""Invariant checks for tools/build_network.py.

Run: python3 tools/test_build_network.py
These run against the real seed JSON plus a small RSS fixture, so they
exercise both the offline and the feed-merge paths.
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_network import (  # noqa: E402
    build, clean_org, guests_from_title, load_topics, parse_feed,
    parse_pubdate, split_people, strip_takeaway_prefix,
)

ROOT = Path(__file__).resolve().parent.parent
SEED = json.loads((ROOT / "catalog_cocktails.json").read_text(encoding="utf-8"))
TOPICS = load_topics(ROOT / "data" / "topics.json")
FIXTURE = parse_feed((ROOT / "tools" / "testdata" / "sample_feed.xml").read_text(encoding="utf-8"))

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def graph_index(graph):
    nodes = {n["id"]: n for n in graph["nodes"]}
    by_type = Counter(n["type"] for n in graph["nodes"])
    links = Counter(l["type"] for l in graph["links"])
    return nodes, by_type, links


print("unit: parsing helpers")
check("takeaway prefix stripped (hyphen and en dash)",
      strip_takeaway_prefix("TAKEAWAYS - X") == "X"
      and strip_takeaway_prefix("TAKEAWAYS – X") == "X")
check("guest parsed from 'with'", guests_from_title("Digital Twins and AI with Hala Nelson") == ["Hala Nelson"])
check("guest parsed from 'w/'", guests_from_title("AI isn't magic w/ Danielle Crop") == ["Danielle Crop"])
check("multi-guest split on &", split_people("Dean Allemang & Bryon Jacob") == ["Dean Allemang", "Bryon Jacob"])
check("multi-guest split on 'and'", split_people("Gabi Steele and Leah Weiss") == ["Gabi Steele", "Leah Weiss"])
check("topic-shaped tail rejected", guests_from_title("What's the deal with Reverse ETL?") == [])
check("org trailing punctuation trimmed", clean_org("American Express.") == "American Express")
check("org run-on trimmed", clean_org("Profisee and host of CDO Matters Podcast") == "Profisee")
check("data.world truncation repaired", clean_org("data") == "data.world")
check("RFC-822 date parsed", parse_pubdate("Wed, 19 Nov 2025 10:00:00 -0600") == "2025-11-19")

print("\noffline build (seed only)")
offline = build(SEED, [], TOPICS)
nodes, by_type, links = graph_index(offline)
eps = [n for n in offline["nodes"] if n["type"] == "episode"]
companions = [e for e in eps if not e["is_full"]]
parented = {l["source"] for l in offline["links"] if l["type"] == "TAKEAWAY_OF"}
guested = {l["target"] for l in offline["links"] if l["type"] == "GUEST_ON"}

check("no dangling link endpoints",
      all(l["source"] in nodes and l["target"] in nodes for l in offline["links"]))
check("node ids unique", len(nodes) == len(offline["nodes"]))
check("every companion clip has a parent",
      all(e["id"] in parented for e in companions),
      f"{sum(1 for e in companions if e['id'] not in parented)} orphaned")
check("no guest attached to a companion clip",
      not any(e["id"] in guested for e in companions))
check("episode titles no longer collapse two shows into one",
      len(eps) == 202, f"got {len(eps)}")
check("topics present", by_type["topic"] > 10, f"got {by_type['topic']}")
check("hosts are not guest nodes",
      not any(n["name"] in {"Tim Gasper", "Juan Sequeda"}
              for n in offline["nodes"] if n["type"] == "person"))
check("no person node still packs several humans",
      not any(" & " in n["name"] or n["name"].startswith("Various guests")
              for n in offline["nodes"] if n["type"] == "person"))

print("\nfeed merge (seed + fixture)")
online = build(SEED, FIXTURE, TOPICS)
onodes, oby_type, olinks = graph_index(online)
titles = {n["name"] for n in online["nodes"] if n["type"] == "episode"}

check("new feed episodes appended",
      "Decision intelligence and context graphs with Priya Raman" in titles)
check("truncated seed title repaired from feed",
      "Netflix’s Unified Data Architecture: Model Once, Represent Everywhere with Alex Bertails" in titles)
check("title divergence does not duplicate the episode",
      sum(1 for t in titles if "Observability and Why" in t and not t.startswith("TAKEAWAYS")) == 1)
check("episode count is seed plus the genuinely new ones",
      len([n for n in online['nodes'] if n['type'] == 'episode']) == 205,
      f"got {len([n for n in online['nodes'] if n['type'] == 'episode'])}")
check("curated seed guest survives the merge",
      any(onodes[l["source"]]["name"] == "Barr Moses"
          for l in online["links"] if l["type"] == "GUEST_ON"))
check("guest inferred for a feed-only episode",
      any(n["name"] == "Priya Raman" for n in online["nodes"]))
check("multi-guest feed episode split into two people",
      {"Dean Allemang", "Bryon Jacob"} <= {n["name"] for n in online["nodes"] if n["type"] == "person"})
check("host-only episodes land in the review queue",
      any("Season 12 Finale" in r["title"] for r in online["meta"]["episodes_needing_review"]))
check("feed url and summary carried onto episodes",
      any(n.get("url") and n.get("summary") for n in online["nodes"] if n["type"] == "episode"))

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    raise SystemExit(1)
print("all checks passed")
