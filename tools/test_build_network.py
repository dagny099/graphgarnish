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
    episode_drop_is_safe, is_takeaway, next_page_url, strip_takeaway_prefix,
    guests_from_title, load_feed, load_topics, load_verified, normalize_name,
    parse_feed, parse_feed_loosely, parse_pubdate, previous_episode_count,
    split_people, strip_affiliation, strip_takeaway_prefix,
)

ROOT = Path(__file__).resolve().parent.parent
# The curated spreadsheet extract, not an output file. Reading an output here
# would reintroduce exactly the loop the split was made to remove.
SEED = json.loads((ROOT / "data" / "seed_curated.json").read_text(encoding="utf-8"))
VERIFIED = load_verified(ROOT / "data" / "verified.json")
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

print("\nthe real feed's shape")
OMNY1 = (TESTDATA / "omny_feed_page1.xml").read_text(encoding="utf-8")
OMNY2 = (TESTDATA / "omny_feed_page2.xml").read_text(encoding="utf-8")
omny_items = parse_feed(OMNY1)

check("itunes:episodeType trailer marks a companion clip",
      is_takeaway("TAKEAWAY Intelligence Without Action", "trailer"))
check("a title starting 'Takeaways from' is a real episode, not a companion",
      not is_takeaway("Takeaways from Gartner Data & Analytics Rants", "full"))
check("the title prefix still wins when episodeType says full",
      is_takeaway("TAKEAWAYS - What is Data + AI Observability", "full"))
check("the bare prefix is only stripped for known companions",
      strip_takeaway_prefix("TAKEAWAY Intelligence", companion=True) == "Intelligence"
      and strip_takeaway_prefix("Takeaways from Gartner") == "Takeaways from Gartner")
check("season and episode numbers are read",
      any(i["season"] == "12" and i["number"] == "3" for i in omny_items))
check("the next page is discovered",
      next_page_url(OMNY1) is not None and next_page_url(OMNY2) is None)

omny_graph = build(SEED, omny_items, TOPICS)
onodes = {n["id"]: n for n in omny_graph["nodes"]}
oparent = {l["source"] for l in omny_graph["links"] if l["type"] == "TAKEAWAY_OF"}
ocompanions = [n for n in omny_graph["nodes"]
               if n["type"] == "episode" and not n["is_full"]
               and n["date"] >= "2026-08-01"]
check("the bare-prefix companion attaches to its parent",
      all(n["id"] in oparent for n in ocompanions),
      f"{[n['name'][:40] for n in ocompanions if n['id'] not in oparent]}")
opeople = {n["name"] for n in omny_graph["nodes"] if n["type"] == "person"}
check("both guests of a two-guest episode become people",
      {"Jenna Jordan", "Amalia Child"} <= opeople)
check("the hosts are still excluded when named in a title",
      not ({"Juan Sequeda", "Tim Gasper"} & opeople))
oaff = {onodes[l["source"]]["name"]: onodes[l["target"]]["name"]
        for l in omny_graph["links"] if l["type"] == "AFFILIATED_WITH"}
check("companies are read out of episode descriptions",
      oaff.get("Patrick McGarry") == "ServiceNow"
      and oaff.get("Bethany Sehon") == "Capital One"
      and oaff.get("Lena Hall") == "Akamai",
      f"got {[oaff.get(x) for x in ('Patrick McGarry', 'Bethany Sehon', 'Lena Hall')]}")

# Pagination end to end: page 1 links page 2, and both must land in the graph.
PAGED_HOST = ""  # filled in once the test server has a port


class PagedHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        page = OMNY2 if "page=2" in self.path else OMNY1
        # The feed advertises absolute next-page URLs, so point them at this
        # server to exercise the same code path the real feed will take.
        body = page.replace("https://www.omnycontent.com", PAGED_HOST).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


with socketserver.TCPServer(("127.0.0.1", 0), PagedHandler) as srv:
    PAGED_HOST = f"http://127.0.0.1:{srv.server_address[1]}"
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    paged = load_feed(f"{PAGED_HOST}/d/playlist/ORG/PROG/PLAYLIST/podcast.rss", None)
    srv.shutdown()

check("every page of a paginated feed is followed",
      len(paged) == len(omny_items) + len(parse_feed(OMNY2)),
      f"got {len(paged)} items, expected {len(omny_items) + len(parse_feed(OMNY2))}")
check("a page-2 episode reaches the graph",
      "Ethan Mollick" in {n["name"] for n in build(SEED, paged, TOPICS)["nodes"]})

print("\nname and affiliation cleanup")
check("employer split off a guest name",
      strip_affiliation("Andy Palmer from Tamr") == ("Andy Palmer", "Tamr"))
check("'of' and 'at' work the same way",
      strip_affiliation("Patrick Bangert of Samsung") == ("Patrick Bangert", "Samsung")
      and strip_affiliation("Bethany Sehon at Capital One") == ("Bethany Sehon", "Capital One"))
check("a job title is not a person",
      strip_affiliation("CDO at McKinsey") == ("", "")
      and strip_affiliation("VP of Product") == ("", ""))
check("a plain name is left alone",
      strip_affiliation("Dean Allemang") == ("Dean Allemang", ""))
check("a name too long for the name test survives if it is name-plus-employer",
      split_people("Aakriti Agrawal from American Express")
      == ["Aakriti Agrawal from American Express"],
      "kept whole here; clean_guest_list splits it and files the employer")
check("a topic-shaped tail is still not a person",
      split_people("Reverse ETL?") == [] and split_people("the future of data") == [])
check("a conjunction of two first names invents nobody",
      split_people("Juan and Tim") == [] and split_people("Roel and Valentijn") == [])
check("a single name with an honorific still counts",
      split_people("Bob Seiner PhD") == ["Bob Seiner"])
check("the non-breaking hyphen folds to a plain one",
      normalize_name("Ricardo Baeza\u2011Yates") == "Ricardo Baeza-Yates")
check("org name stops at a sentence boundary",
      clean_org("Ternary Data. Join Tim and Juan") == "Ternary Data")
check("a dotted company name survives",
      clean_org("AgileData.io") == "AgileData.io" and clean_org("data.world") == "data.world")

print("\nverified.json decisions")
DECIDED = {
    "drop_person": ["Priya Raman"],
    "drop_organization": [],
    "rename_person": {"Dean Allemang": "Dean A. Allemang"},
    "rename_organization": {},
    "person_org": {"Bryon Jacob": "Test Corp"},
    "hosts_only": ["Season 12 Finale"],
    "hosts_only_patterns": [],
}
decided = build(SEED, FIXTURE, TOPICS, DECIDED)
dnames = {n["name"] for n in decided["nodes"]}
check("drop_person removes an extracted person", "Priya Raman" not in dnames)
check("rename_person folds a name", "Dean A. Allemang" in dnames and "Dean Allemang" not in dnames)
dnodes = {n["id"]: n for n in decided["nodes"]}
aff = {dnodes[l["source"]]["name"]: dnodes[l["target"]]["name"]
       for l in decided["links"] if l["type"] == "AFFILIATED_WITH"}
check("person_org beats every guess", aff.get("Bryon Jacob") == "Test Corp")
check("hosts_only flags the episode instead of inventing a guest",
      any(n.get("hosts_only") for n in decided["nodes"] if n["type"] == "episode"))
check("a hosts_only episode leaves the review queue",
      not any("Season 12 Finale" in r["title"]
              for r in decided["meta"]["episodes_needing_review"]))
hosts_ids = {n["id"] for n in decided["nodes"] if n.get("hosts_only")}
check("a hosts_only episode carries no guest link",
      not any(l["target"] in hosts_ids for l in decided["links"] if l["type"] == "GUEST_ON"))

PATTERNED = dict(DECIDED, hosts_only=[], hosts_only_patterns=[r"\bSeason \d+ Finale\b"])
check("a hosts_only pattern matches the same episode as the exact title",
      {n["id"] for n in build(SEED, FIXTURE, TOPICS, PATTERNED)["nodes"] if n.get("hosts_only")}
      == hosts_ids)
check("a pattern never overrides a real guest",
      "Alex Bertails" in {n["name"] for n in build(
          SEED, FIXTURE, TOPICS,
          dict(DECIDED, drop_person=[], hosts_only=[],
               hosts_only_patterns=[r"."]))["nodes"]})
check("an absent verified.json is not an error",
      build(SEED, FIXTURE, TOPICS, load_verified(ROOT / "no-such-file.json"))
      == build(SEED, FIXTURE, TOPICS))
check("the shipped verified.json applies cleanly",
      isinstance(build(SEED, FIXTURE, TOPICS, VERIFIED), dict))

print("\ninputs are never outputs")
from build_network import DEFAULT_SEED, DEFAULT_TOPICS, DEFAULT_VERIFIED, OUTPUTS  # noqa: E402
check("no input path is also an output path",
      not ({DEFAULT_SEED, DEFAULT_TOPICS, DEFAULT_VERIFIED} & set(OUTPUTS)),
      "an output being read back is the loop this split removes")
check("the previous episode count is read from the published graph",
      previous_episode_count(OUTPUTS[0]) is None
      or previous_episode_count(OUTPUTS[0]) > 0)
check("a missing or unreadable previous graph reports None",
      previous_episode_count(ROOT / "no-such-file.json") is None)
check("the guard is measured against the published graph, not the seed",
      not episode_drop_is_safe(388, 203),
      "a feed that dropped back to seed size must not pass")

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    raise SystemExit(1)
print("all checks passed")
