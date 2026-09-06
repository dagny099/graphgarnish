#!/usr/bin/env python3
"""Build the GraphGarnish network JSON for Catalog & Cocktails.

Inputs are read-only, outputs are fully derived. Nothing this script
writes is ever read back on a later run, so a name it guessed today
cannot be mistaken for a curated fact tomorrow.

  data/seed_curated.json   episodes and guests from the original spreadsheet
  data/verified.json       human corrections, applied last and always
  data/topics.json         the topic taxonomy
  the RSS feed             the full back catalogue, fetched fresh

  catalog_cocktails.json   output
  sample_network.json      output (what the site fetches)

Delete both outputs, rerun, and they come back identical. To correct
something the extractor got wrong, edit data/verified.json -- never the
output files, which the next run overwrites.

Usage:
    python3 tools/build_network.py --feed                # fetch and rebuild
    python3 tools/build_network.py --feed-file feed.xml  # rebuild from a saved feed
    python3 tools/build_network.py --report              # print a summary
"""

from __future__ import annotations

import argparse
import datetime
import json
import re
import sys
import unicodedata
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# The show's own feed, taken from the <atom:link rel="self"> and
# <itunes:new-feed-url> inside the feed itself, so it is the publisher's
# canonical URL rather than one inferred from a directory listing.
OMNY_FEED = ("https://www.omnycontent.com/d/playlist/"
             "922e0f19-2d84-4485-835c-a83f00036b00/"
             "ec23d51c-1891-4428-81ee-b387002de817/"
             "46398967-49a1-45c7-9af5-b387002de875/podcast.rss")
OMNY_SHOW = "https://omny.fm/shows/catalog-and-cocktails-the-honest-no-bs-data-podcast"
DEFAULT_FEED_CANDIDATES = [OMNY_FEED, OMNY_SHOW]
DEFAULT_FEED = DEFAULT_FEED_CANDIDATES[0]

# The feed is paginated: page 1 carries only the most recent episodes and
# links the rest through <atom:link rel="next">. Fetching one page silently
# drops most of the back catalogue, so every page gets followed.
MAX_FEED_PAGES = 40
# Inputs are read-only. The builder never writes to any of them, so a value it
# guessed on one run can never be read back as fact on the next. Everything in
# OUTPUTS is fully derived: delete both files, rebuild, and you get them back
# byte for byte.
DEFAULT_SEED = ROOT / "data" / "seed_curated.json"
DEFAULT_TOPICS = ROOT / "data" / "topics.json"
DEFAULT_VERIFIED = ROOT / "data" / "verified.json"
OUTPUTS = [ROOT / "catalog_cocktails.json", ROOT / "sample_network.json"]

# ---------------------------------------------------------------- utilities

def slug(text: str, limit: int = 40) -> str:
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^a-zA-Z0-9]+", "_", text).strip("_").lower()
    return text[:limit] or "unknown"


def norm_key(title: str, companion: bool = False) -> str:
    """Normalized title with the companion-clip prefix and all punctuation
    removed. Not truncated: length differences are what let prefix_match
    line a 60-char-truncated seed title up with the full title from the feed."""
    t = strip_takeaway_prefix(title, companion)
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


# "TAKEAWAYS - Foo" and "TAKEAWAYS – Foo" both appear, and newer episodes
# drop the separator entirely ("TAKEAWAY Foo").
TAKEAWAY_RE = re.compile(r"^\s*TAKEAWAYS?\s*[-–—:]\s*", re.I)
TAKEAWAY_BARE_RE = re.compile(r"^\s*TAKEAWAYS?\s+(?=[A-Z])")


def strip_takeaway_prefix(title: str, companion: bool = False) -> str:
    """Remove the companion-clip prefix from a title.

    The bare form is only stripped for episodes already known to be
    companions: a real episode is titled "Takeaways from Gartner Data &
    Analytics Rants ...", and stripping its first word would mangle it."""
    stripped = TAKEAWAY_RE.sub("", title).strip()
    if stripped != title.strip():
        return stripped
    if companion:
        return TAKEAWAY_BARE_RE.sub("", title).strip()
    return title.strip()


def is_takeaway(title: str, episode_type: str = "") -> bool:
    """Is this the short companion clip rather than the episode itself?

    Two independent signals, because neither is reliable alone: older
    companions carry <itunes:episodeType>full</itunes:episodeType> and are
    only identifiable by their title prefix, while the newest ones drop the
    prefix separator and are only identifiable by episodeType."""
    if TAKEAWAY_RE.match(title):
        return True
    if episode_type.strip().lower() == "trailer":
        return True
    return False


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

# Feed titles mix the plain hyphen with the non-breaking hyphen and the dashes.
# slug() strips anything non-ASCII rather than folding it, so "Baeza-Yates" and
# "Baeza\u2011Yates" would otherwise become two different people.
DASHES = {ord(c): "-" for c in "\u2010\u2011\u2012\u2013\u2014"}


def normalize_name(name: str) -> str:
    """One spelling per human. Folds dash variants and collapses whitespace."""
    return re.sub(r"\s+", " ", name.translate(DASHES)).strip()


# "Andy Palmer from Tamr" is one person and one company, not a person with a
# company welded into their name. GUEST_SPLIT does not break on these words
# because the tail is an employer rather than another guest.
AFFIL_SUFFIX = re.compile(r"^(.+?)\s+(?:from|of|at)\s+([A-Z].*)$")


def strip_affiliation(name: str) -> tuple[str, str]:
    """Split a trailing employer off a guest name.

    Returns (person, org). The person is empty when what precedes the
    employer is not itself a usable name -- "CDO at McKinsey" and
    "VP of Product" are job descriptions that the title parser mistook for
    guests, and neither names a human the graph can hold."""
    m = AFFIL_SUFFIX.match(name)
    if not m:
        return name, ""
    head, tail = m.group(1).strip(), m.group(2).strip()
    if not looks_like_person(head):
        return "", ""
    return head, clean_org(tail)


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
        # "Aakriti Agrawal from American Express" is too long to pass the name
        # test, but it is a name with an employer attached rather than a topic.
        # Keep it whole: clean_guest_list splits it and files the employer.
        if looks_like_person(p) or (AFFIL_SUFFIX.match(p) and strip_affiliation(p)[0]):
            out.append(p)
    # A single name with a trailing honorific still counts. This fallback is
    # only safe when the split found nothing to split on: "Juan and Tim" splits
    # into two one-word fragments that both fail the name test, and retrying the
    # whole string would then invent a person called "Juan and Tim".
    if not out and len(parts) == 1:
        p = HONORIFIC_TAIL.sub("", raw).strip()
        if looks_like_person(p):
            out.append(p)
    return out


def guests_from_title(title: str, companion: bool = False) -> list[str]:
    t = strip_takeaway_prefix(title, companion)
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

ORG_CUTS = re.compile(
    r"\s+(?:and|who|where|which|he|she|they|author|inventor|former|host"
    r"|this|these|that|join|see|listen|watch|subscribe)\b", re.I)


def clean_org(raw: str) -> str:
    """The seed's org names were parsed out of a free-text 'Role & Company'
    column and often ran past the company name. Trim back to the company."""
    if not raw:
        return ""
    org = raw.strip()
    org = re.split(r"[;,]", org, maxsplit=1)[0]
    # Stop at a sentence boundary, so a description that runs on past the
    # company ("... at Ternary Data. Join Tim and Juan ...") yields the company
    # rather than the rest of the paragraph. A period with no space after it is
    # left alone: it is part of the name in data.world and AgileData.io.
    org = re.split(r"\.\s+", org, maxsplit=1)[0]
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

FETCH_HEADERS = {
    "User-Agent": "GraphGarnish/1.0 (+https://github.com/dagny099/graphgarnish)",
    # Some hosts content-negotiate and hand a browser-shaped client the HTML
    # landing page instead of the feed.
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.5",
}


class Payload:
    """A fetched response plus the context needed to explain a parse failure."""

    def __init__(self, text: str, url: str, content_type: str):
        self.text = text
        self.url = url
        self.content_type = content_type

    @property
    def looks_like_html(self) -> bool:
        head = self.text[:2000].lower()
        return ("html" in self.content_type.lower()
                or "<!doctype html" in head
                or "<html" in head)

    def describe(self) -> str:
        """A compact diagnosis that survives being read out of a CI log."""
        preview = re.sub(r"\s+", " ", self.text[:400]).strip()
        n_items = len(re.findall(r"<item[\s>]", self.text, re.I))
        n_entries = len(re.findall(r"<entry[\s>]", self.text, re.I))
        shape = "HTML page" if self.looks_like_html else "XML/unknown"
        return (
            f"  final URL   : {self.url}\n"
            f"  content-type: {self.content_type or '(none)'}\n"
            f"  bytes       : {len(self.text)}\n"
            f"  looks like  : {shape}\n"
            f"  <item> count: {n_items}\n"
            f"  <entry> cnt : {n_entries}\n"
            f"  first 400ch : {preview}"
        )


def fetch_feed(url: str, timeout: int = 60) -> Payload:
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", "replace")
        return Payload(raw, resp.geturl(), resp.headers.get("Content-Type", ""))


# <link rel="alternate" type="application/rss+xml" href="..."> is the standard
# way a page points at its own feed. If the URL we were given turns out to be a
# landing page, follow that pointer rather than failing.
LINK_TAG_RE = re.compile(r"<link\b[^>]*>", re.I)
HREF_RE = re.compile(r"""href\s*=\s*("[^"]*"|'[^']*')""", re.I)
FEED_TYPE_RE = re.compile(r"""type\s*=\s*["']application/(?:rss|atom)\+xml["']""", re.I)


def discover_feed_url(html: str, base_url: str) -> str | None:
    """Read a page's advertised feed URL.

    The page is not under our control, so the URL it names is untrusted input.
    Restricting the follow to https keeps a hijacked or expired domain from
    redirecting the build at an arbitrary scheme."""
    for tag in LINK_TAG_RE.findall(html):
        if not FEED_TYPE_RE.search(tag):
            continue
        href = HREF_RE.search(tag)
        if not href:
            continue
        target = urllib.parse.urljoin(base_url, href.group(1).strip("\"'"))
        scheme = urllib.parse.urlparse(target).scheme
        if scheme not in ("http", "https"):
            print(f"ignoring advertised feed {target}: unsupported scheme",
                  file=sys.stderr)
            continue
        if scheme == "http" and urllib.parse.urlparse(base_url).scheme == "https":
            print(f"ignoring advertised feed {target}: refuses to downgrade from https",
                  file=sys.stderr)
            continue
        return target
    return None


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


START_TAG_RE = re.compile(
    r"""<([A-Za-z_][\w:.\-]*)((?:\s+[A-Za-z_][\w:.\-]*\s*=\s*(?:"[^"]*"|'[^']*'))+)(\s*/?)>""")
ATTR_RE = re.compile(r"""([A-Za-z_][\w:.\-]*)\s*=\s*("[^"]*"|'[^']*')""")


def dedupe_attributes(xml_text: str) -> str:
    """Drop repeated attributes from start tags, keeping the first of each.

    Real podcast feeds declare the same namespace twice often enough to matter,
    and Python's XML parser treats that as fatal ("duplicate attribute") even
    though every consumer in the wild accepts it."""

    def fix(match):
        tag, attrs, close = match.groups()
        seen, kept = set(), []
        for name, value in ATTR_RE.findall(attrs):
            if name in seen:
                continue
            seen.add(name)
            kept.append(f'{name}={value}')
        return f"<{tag}{' ' + ' '.join(kept) if kept else ''}{close}>"

    return START_TAG_RE.sub(fix, xml_text)


ITEM_RE = re.compile(r"<item[\s>].*?</item\s*>", re.S | re.I)


def _tag_text(block: str, name: str) -> str:
    m = re.search(rf"<{name}\b[^>]*>(.*?)</{name}\s*>", block, re.S | re.I)
    if not m:
        return ""
    inner = m.group(1)
    cdata = re.search(r"<!\[CDATA\[(.*?)\]\]>", inner, re.S)
    return cdata.group(1) if cdata else inner


def parse_feed_loosely(xml_text: str) -> list[dict]:
    """Pull items out with regexes when the document will not parse at all."""
    items = []
    for block in ITEM_RE.findall(xml_text):
        title = strip_html(_tag_text(block, "title"))
        if not title:
            continue
        items.append({
            "title": title,
            "date": parse_pubdate(_tag_text(block, "pubDate")),
            "description": strip_html(_tag_text(block, "description"))
                           or strip_html(_tag_text(block, "content:encoded")),
            "url": strip_html(_tag_text(block, "link")),
            "guid": strip_html(_tag_text(block, "guid")),
            "episode_type": strip_html(_tag_text(block, "itunes:episodeType")),
            "season": strip_html(_tag_text(block, "itunes:season")),
            "number": strip_html(_tag_text(block, "itunes:episode")),
        })
    return items


ATOM_NS = "{http://www.w3.org/2005/Atom}"
ITUNES_NS = "{http://www.itunes.com/dtds/podcast-1.0.dtd}"
CONTENT_NS = "{http://purl.org/rss/1.0/modules/content/}"


def parse_atom(root) -> list[dict]:
    """Atom feeds use <feed>/<entry> where RSS uses <channel>/<item>."""
    items = []
    for entry in root.findall(f"{ATOM_NS}entry") or root.findall("entry"):
        def txt(tag):
            el = entry.find(f"{ATOM_NS}{tag}")
            if el is None:
                el = entry.find(tag)
            return (el.text or "") if el is not None else ""
        title = strip_html(txt("title"))
        if not title:
            continue
        link = ""
        for el in list(entry.findall(f"{ATOM_NS}link")) + list(entry.findall("link")):
            if el.get("rel", "alternate") == "alternate":
                link = el.get("href", "")
                break
        items.append({
            "title": title,
            "date": parse_pubdate(txt("published") or txt("updated")),
            "description": strip_html(txt("summary") or txt("content")),
            "url": link,
            "guid": txt("id"),
            "episode_type": "",
            "season": "",
            "number": "",
        })
    return items


def parse_feed(xml_text: str) -> list[dict]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(dedupe_attributes(xml_text))
            print("feed had duplicate attributes; parsed after cleaning them",
                  file=sys.stderr)
        except ET.ParseError as exc:
            items = parse_feed_loosely(xml_text)
            if not items:
                raise
            print(f"feed is not well-formed XML ({exc}); "
                  f"recovered {len(items)} items with the fallback parser",
                  file=sys.stderr)
            return items

    if root.tag.rsplit("}", 1)[-1].lower() == "feed":
        return parse_atom(root)

    channel = root.find("channel")
    if channel is None:
        channel = root
    items = []
    for it in channel.findall("item"):
        def txt(tag):
            el = it.find(tag)
            return (el.text or "") if el is not None else ""
        title = strip_html(txt("title"))
        if not title:
            continue
        desc = strip_html(txt("description")) or strip_html(
            txt(f"{CONTENT_NS}encoded"))
        items.append({
            "title": title,
            "date": parse_pubdate(txt("pubDate")),
            "description": desc,
            "url": txt("link").strip(),
            "guid": txt("guid").strip(),
            "episode_type": txt(f"{ITUNES_NS}episodeType").strip(),
            "season": txt(f"{ITUNES_NS}season").strip(),
            "number": txt(f"{ITUNES_NS}episode").strip(),
        })
    return items


def next_page_url(xml_text: str) -> str | None:
    """The URL of the next page of the feed, if it advertises one."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        try:
            root = ET.fromstring(dedupe_attributes(xml_text))
        except ET.ParseError:
            return None
    channel = root.find("channel")
    if channel is None:
        channel = root
    for link in channel.findall(f"{ATOM_NS}link") + channel.findall("link"):
        if link.get("rel") == "next" and link.get("href"):
            return link.get("href")
    return None


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
        companion = is_takeaway(n["name"])
        episodes[(norm_key(n["name"], companion), companion)] = {
            "name": n["name"],
            "date": n.get("date", ""),
            # Carried so that an episode already holding a description is not
            # stripped of it on the next pass. Without this, the org and topic
            # extractors -- which read the description -- see an empty string
            # and silently find less than they did the run before.
            "description": n.get("summary", "") or n.get("description", ""),
            "guests": guests.get(n["id"], []),
        }
    # A person with two recorded employers must resolve to the same one every
    # time. Taking whichever link happened to come first made the output depend
    # on the order of the input file.
    person_org = {p: clean_org(sorted(v)[0]) for p, v in orgs.items() if v}
    return {"episodes": episodes, "person_org": person_org}


VERIFIED_KEYS = ("drop_person", "drop_organization", "rename_person",
                 "rename_organization", "person_org", "hosts_only",
                 "hosts_only_patterns")


def empty_verified() -> dict:
    """A decision file with no decisions in it."""
    return {k: ([] if k.startswith(("drop", "hosts")) else {}) for k in VERIFIED_KEYS}


def load_verified(path: Path) -> dict:
    """Read the human decision file, or return an empty one if absent.

    This is the only place a person's judgement enters the pipeline. It is
    never written by the builder, so a correction made once is applied on
    every future run and cannot be overwritten by a later guess."""
    if not path.exists():
        return empty_verified()
    raw = json.loads(path.read_text(encoding="utf-8"))
    out = {}
    for key in VERIFIED_KEYS:
        empty = [] if key.startswith(("drop", "hosts")) else {}
        out[key] = raw.get(key, empty)
    return out


def build(seed: dict, feed_items: list[dict], taxonomy: list[dict],
          verified: dict | None = None) -> dict:
    idx = index_seed(seed)
    seed_eps = idx["episodes"]
    person_org = dict(idx["person_org"])

    verified = verified or empty_verified()
    renames = {normalize_name(k): normalize_name(v)
               for k, v in verified["rename_person"].items()}
    dropped_people = {normalize_name(n).lower() for n in verified["drop_person"]}
    dropped_orgs = {n.strip().lower() for n in verified["drop_organization"]}
    verified_org = {normalize_name(k): v for k, v in verified["person_org"].items()}
    org_renames = {k.strip().lower(): v for k, v in verified["rename_organization"].items()}
    hosts_only_keys = {norm_key(t) for t in verified["hosts_only"]}
    # The show runs recurring host-only formats ("It's Friday, Juan and Tim
    # rant about ..."). A pattern catches next month's instalment too, so the
    # same judgement does not have to be re-entered every week.
    hosts_only_res = [re.compile(x, re.I) for x in verified["hosts_only_patterns"]]
    person_org.update(verified_org)

    def clean_guest_list(names: list[str], rec: dict) -> list[str]:
        """Turn raw extracted names into people the graph can hold.

        Folds spelling variants, splits a trailing employer off into the org
        slot rather than leaving it welded to the name, drops the hosts, and
        applies the human decisions from verified.json."""
        out: list[str] = []
        for raw in names:
            name = renames.get(normalize_name(raw), normalize_name(raw))
            person, org = strip_affiliation(name)
            if not person:
                continue
            person = renames.get(person, person)
            if person.lower() in HOSTS or person.lower() in dropped_people:
                continue
            if org and org.lower() not in dropped_orgs:
                rec.setdefault("guest_orgs", {}).setdefault(person, org)
            if person not in out:
                out.append(person)
        return out

    # Start from every seed episode, then let feed items overwrite/extend.
    records: dict[tuple[str, bool], dict] = {}
    for key, ep in seed_eps.items():
        records[key] = {
            "title": ep["name"], "date": ep["date"],
            "description": ep.get("description", ""),
            "url": "", "guests": list(ep["guests"]), "source": "seed",
            "season": "", "number": "",
        }

    seen_guids: set[str] = set()
    for item in feed_items:
        guid = item.get("guid") or ""
        if guid:
            if guid in seen_guids:
                continue
            seen_guids.add(guid)
        companion = is_takeaway(item["title"], item.get("episode_type", ""))
        same_kind = [k for (k, c) in records if c == companion]
        ikey = norm_key(item["title"], companion)
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
            rec["season"] = item.get("season", "")
            rec["number"] = item.get("number", "")
            rec["source"] = "seed+feed"
        else:
            records[key] = {
                "title": item["title"], "date": item["date"],
                "description": item["description"], "url": item["url"],
                "guests": [], "source": "feed",
                "season": item.get("season", ""), "number": item.get("number", ""),
            }

    # Expand any seed guest entry that packed several humans into one name,
    # and fill in guests for feed-only episodes.
    review = []
    for (key, companion), rec in records.items():
        expanded = []
        for name in rec["guests"]:
            people = split_people(name)
            expanded.extend(people if people else [name])
        rec["guests"] = clean_guest_list(expanded, rec)
        if not rec["guests"] and not companion:
            found = (guests_from_title(rec["title"], companion)
                     or guests_from_description(rec["description"]))
            found = clean_guest_list(found, rec)
            if found:
                rec["guests"] = found
                rec["inferred"] = True
            elif rec["source"] != "seed":
                rec["hosts_only"] = (norm_key(rec["title"]) in hosts_only_keys
                                     or any(r.search(rec["title"]) for r in hosts_only_res))
                if not rec["hosts_only"]:
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
            strip_takeaway_prefix(rec["title"], companion), 24) + ("" if full else "_ta")
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
        # A real episode that Juan and Tim recorded alone. Flagged rather than
        # given an invented guest, and kept out of the review queue.
        if rec.get("hosts_only"):
            node["hosts_only"] = True
        if rec["url"]:
            node["url"] = rec["url"]
        if rec["description"]:
            node["summary"] = rec["description"][:400]
        if rec.get("season"):
            node["season"] = rec["season"]
        if rec.get("number"):
            node["episode"] = rec["number"]
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
                # Order of preference: person_org (a human decision from
                # verified.json, else the curated spreadsheet), then the
                # employer split off the guest's own name, then a guess read
                # out of the episode description.
                org = (person_org.get(person)
                       or rec.get("guest_orgs", {}).get(person)
                       or org_from_description(rec["description"], person))
                org = clean_org(org)
                org = org_renames.get(org.lower(), org)
                if org and org.lower() not in dropped_orgs:
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

    # Emission order follows whatever order the seed happened to be in, which
    # changes every time the seed is regenerated. Sort so that identical input
    # always produces an identical file and the weekly commit shows only real
    # changes.
    type_rank = {"episode": 0, "person": 1, "organization": 2, "topic": 3}
    nodes.sort(key=lambda n: (type_rank.get(n["type"], 9),
                              n.get("date", ""), n.get("name", ""), n["id"]))
    links.sort(key=lambda l: (l["type"], l["source"], l["target"]))

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


def previous_episode_count(path: Path) -> int | None:
    """How many episodes the last published graph held, or None on a first run
    or an unreadable file. Used as the floor a rebuild may not fall through."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return sum(1 for n in data.get("nodes", []) if n.get("type") == "episode")


def episode_drop_tolerance(seed_episodes: int) -> int:
    """How many episodes a rebuild may legitimately lose.

    Genuine duplicates in the seed collapse into one episode, so a small drop
    is expected. A large one means the feed was truncated, pointed somewhere
    else, or stopped being this show."""
    return max(5, seed_episodes // 50)


def episode_drop_is_safe(seed_episodes: int, built_episodes: int) -> bool:
    return built_episodes >= seed_episodes - episode_drop_tolerance(seed_episodes)


def try_payload(payload: Payload, label: str) -> list[dict]:
    """Parse one fetched response, reporting rather than raising on failure."""
    try:
        items = parse_feed(payload.text)
    except Exception as exc:  # noqa: BLE001 - diagnose instead of crashing
        print(f"could not parse {label} ({exc})", file=sys.stderr)
        return []
    if not items:
        print(f"parsed {label} but it contained no episodes", file=sys.stderr)
    return items


def follow_pages(first: Payload, save_to: Path | None) -> list[dict]:
    """Walk <atom:link rel="next"> to the end of the feed.

    Page 1 of a paginated podcast feed carries only the most recent episodes.
    Stopping there would quietly drop most of the back catalogue and look
    like a successful run."""
    extra: list[dict] = []
    seen_urls = {first.url}
    text, base = first.text, first.url
    for _ in range(MAX_FEED_PAGES):
        nxt = next_page_url(text)
        if not nxt:
            break
        nxt = urllib.parse.urljoin(base, nxt)
        if nxt in seen_urls:
            break
        seen_urls.add(nxt)
        try:
            page = fetch_feed(nxt)
        except Exception as exc:  # noqa: BLE001
            print(f"FEED ERROR: stopped paging at {nxt} ({exc}); "
                  f"the graph would be missing episodes", file=sys.stderr)
            break
        try:
            page_items = parse_feed(page.text)
        except Exception as exc:  # noqa: BLE001
            print(f"FEED ERROR: could not parse {nxt} ({exc})", file=sys.stderr)
            break
        if not page_items:
            break
        extra.extend(page_items)
        if save_to:
            with save_to.open("a", encoding="utf-8") as fh:
                fh.write("\n<!-- page: " + nxt + " -->\n")
                fh.write(page.text)
        text, base = page.text, page.url
    if extra:
        print(f"followed {len(seen_urls) - 1} more feed page(s) "
              f"for {len(extra)} additional items", file=sys.stderr)
    return extra


def load_feed_candidates(urls: list[str], save_to: Path | None) -> list[dict]:
    """Try each candidate URL until one yields episodes."""
    for url in urls:
        print(f"trying feed URL: {url}", file=sys.stderr)
        items = load_feed(url, save_to)
        if items:
            print(f"USING FEED: {url} ({len(items)} items) — "
                  f"pin this as DEFAULT_FEED_CANDIDATES[0]", file=sys.stderr)
            return items
    return []


def load_feed(url: str, save_to: Path | None) -> list[dict]:
    """Fetch and parse the feed, following a landing page to the real feed if
    that is what the URL turns out to point at. Any failure is reported with
    enough context to diagnose it from a CI log, and returns no items so the
    caller falls back to an offline rebuild."""
    try:
        payload = fetch_feed(url)
    except Exception as exc:  # noqa: BLE001 - report and fall back
        print(f"could not fetch {url} ({exc})", file=sys.stderr)
        return []

    if save_to:
        save_to.write_text(payload.text, encoding="utf-8")
        print(f"saved raw response to {save_to}", file=sys.stderr)

    items = try_payload(payload, "the response")
    if items:
        return items + follow_pages(payload, save_to)

    # The URL may point at a page that links to the feed rather than the feed.
    discovered = discover_feed_url(payload.text, payload.url)
    if discovered and discovered != payload.url:
        print(f"response was not a usable feed; it advertises one at {discovered}",
              file=sys.stderr)
        try:
            followed = fetch_feed(discovered)
        except Exception as exc:  # noqa: BLE001
            print(f"could not fetch the advertised feed {discovered} ({exc})",
                  file=sys.stderr)
            return []
        if save_to:
            save_to.write_text(followed.text, encoding="utf-8")
        items = try_payload(followed, "the advertised feed")
        if items:
            items += follow_pages(followed, save_to)
            print(f"recovered {len(items)} episodes from {discovered}", file=sys.stderr)
            return items
        payload = followed

    print(f"{url} did not yield a usable feed:\n" + payload.describe(), file=sys.stderr)
    return []


# --------------------------------------------------------------------- main

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--feed", nargs="?", const=True, default=None,
                    help="fetch the RSS feed; with no value, tries "
                         f"{len(DEFAULT_FEED_CANDIDATES)} known candidate URLs")
    ap.add_argument("--feed-file", help="parse a local RSS file instead of fetching")
    ap.add_argument("--save-feed", type=Path,
                    help="write the fetched RSS XML here before parsing it, so a "
                         "parsing problem can be reproduced without the network")
    ap.add_argument("--seed", type=Path, default=DEFAULT_SEED)
    ap.add_argument("--topics", type=Path, default=DEFAULT_TOPICS)
    ap.add_argument("--verified", type=Path, default=DEFAULT_VERIFIED,
                    help="human corrections applied after extraction")
    ap.add_argument("--out", type=Path, action="append",
                    help="output path (repeatable; defaults to both site JSON files)")
    ap.add_argument("--report", action="store_true", help="print a summary to stderr")
    args = ap.parse_args()

    seed = json.loads(args.seed.read_text(encoding="utf-8"))
    taxonomy = load_topics(args.topics)
    verified = load_verified(args.verified)

    items = []
    if args.feed_file:
        items = parse_feed(Path(args.feed_file).read_text(encoding="utf-8"))
    elif args.feed:
        candidates = [args.feed] if args.feed is not True else DEFAULT_FEED_CANDIDATES
        items = load_feed_candidates(candidates, args.save_feed)
        if not items:
            print("FEED ERROR: no candidate URL yielded a usable feed; "
                  "continuing offline", file=sys.stderr)

    graph = build(seed, items, taxonomy, verified)

    outputs = args.out or OUTPUTS

    # The rebuild must not lose episodes. The number to compare against is the
    # one in the last published graph, not the one in the seed: the seed is a
    # small curated file that the feed has long since grown past, so checking
    # against it would let a truncated feed silently drop 185 episodes and
    # still look fine.
    baseline = previous_episode_count(outputs[0])
    if baseline is None:
        baseline = sum(1 for n in seed["nodes"] if n["type"] == "episode")
    built_episodes = graph["meta"]["counts"].get("episode", 0)
    if not episode_drop_is_safe(baseline, built_episodes):
        print(f"FEED ERROR: rebuild produced {built_episodes} episodes but the "
              f"existing data has {baseline} "
              f"(tolerance {episode_drop_tolerance(baseline)}). "
              f"Refusing to overwrite published data. Inspect the feed first.",
              file=sys.stderr)
        return 1

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
