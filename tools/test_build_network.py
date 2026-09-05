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
    Payload, build, clean_org, dedupe_attributes, discover_feed_url,
    episode_drop_is_safe,
    guests_from_title, load_feed, load_topics, parse_feed, parse_feed_loosely,
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

print("\nfeed robustness")
TESTDATA = ROOT / "tools" / "testdata"
dup_xml = (TESTDATA / "sample_feed_duplicate_attrs.xml").read_text(encoding="utf-8")
bad_xml = (TESTDATA / "sample_feed_malformed.xml").read_text(encoding="utf-8")

check("duplicate attributes are dropped, first one kept",
      dedupe_attributes('<rss a="1" a="2" b="3">') == '<rss a="1" b="3">')
check("self-closing tags survive deduplication",
      dedupe_attributes('<x a="1" a="2"/>') == '<x a="1"/>')
check("a feed with a repeated namespace still parses",
      len(parse_feed(dup_xml)) == len(FIXTURE),
      f"got {len(parse_feed(dup_xml))} vs {len(FIXTURE)}")
check("a feed that is not well-formed falls back to the loose parser",
      len(parse_feed(bad_xml)) == len(FIXTURE),
      f"got {len(parse_feed(bad_xml))}")
check("the loose parser recovers titles and dates",
      any(i["title"] == "Season 12 Finale" and i["date"] == "2026-08-12"
          for i in parse_feed_loosely(bad_xml)))
check("recovery paths produce the same graph as a clean feed",
      len([n for n in build(SEED, parse_feed(dup_xml), TOPICS)["nodes"] if n["type"] == "episode"])
      == len([n for n in build(SEED, FIXTURE, TOPICS)["nodes"] if n["type"] == "episode"]))

print("\nlanding pages and Atom")
atom_xml = (TESTDATA / "sample_feed_atom.xml").read_text(encoding="utf-8")
landing_html = (TESTDATA / "landing_page.html").read_text(encoding="utf-8")

check("Atom <feed>/<entry> parses like RSS",
      [i["title"] for i in parse_feed(atom_xml)][:1] ==
      ["Decision intelligence and context graphs with Priya Raman"])
check("Atom dates and links are read",
      parse_feed(atom_xml)[1]["date"] == "2026-08-12"
      and parse_feed(atom_xml)[1]["url"] == "https://example.com/ep/s12-finale")
check("a feed link is discovered in an HTML page and resolved against the URL",
      discover_feed_url(landing_html, "https://example.com/127/Show/feed")
      == "https://example.com/real/feed.xml")
check("a page with no feed link discovers nothing",
      discover_feed_url("<html><head></head></html>", "https://example.com/") is None)
check("an https page is not downgraded to an http feed",
      discover_feed_url(
          '<link rel="alternate" type="application/rss+xml" href="http://evil/f.xml">',
          "https://example.com/") is None)
check("a non-http scheme is never followed",
      discover_feed_url(
          '<link rel="alternate" type="application/rss+xml" href="file:///etc/passwd">',
          "https://example.com/") is None)
check("an HTML response is identified as HTML",
      Payload(landing_html, "https://example.com/x", "text/html").looks_like_html)
check("the diagnosis names the shape and the counts",
      all(k in Payload(landing_html, "https://example.com/x", "text/html").describe()
          for k in ("final URL", "content-type", "HTML page", "<item> count")))

# End-to-end: a URL that serves a landing page must follow the advertised feed.
import http.server, socketserver, threading  # noqa: E402

feed_body = (TESTDATA / "sample_feed.xml").read_bytes()


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/real/feed.xml":
            body, ctype = feed_body, "application/rss+xml"
        else:
            body, ctype = landing_html.encode(), "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


with socketserver.TCPServer(("127.0.0.1", 0), Handler) as srv:
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_address[1]}/podcast"
    recovered = load_feed(base, None)
    srv.shutdown()

check("a landing-page URL is followed through to the real feed",
      len(recovered) == len(FIXTURE), f"got {len(recovered)} items")
check("load_feed returns no items rather than raising when a host is unreachable",
      load_feed("http://127.0.0.1:9/nothing", None) == [])

print("\nsafety and determinism")
check("a rebuild that loses one duplicate episode is allowed",
      episode_drop_is_safe(203, 202))
check("a rebuild that loses most episodes is refused",
      not episode_drop_is_safe(203, 20))
check("a rebuild that adds episodes is allowed",
      episode_drop_is_safe(203, 240))

# The seed's own node order must not leak into the output, or every weekly
# commit shows a reshuffle instead of the actual change.
shuffled = {"nodes": list(reversed(SEED["nodes"])), "links": list(reversed(SEED["links"]))}
a = build(SEED, [], TOPICS)
b = build(shuffled, [], TOPICS)
check("output does not depend on the order of the seed",
      [n["id"] for n in a["nodes"]] == [n["id"] for n in b["nodes"]]
      and a["links"] == b["links"])
check("building twice gives an identical result",
      build(SEED, FIXTURE, TOPICS) == build(SEED, FIXTURE, TOPICS))

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    raise SystemExit(1)
print("all checks passed")
