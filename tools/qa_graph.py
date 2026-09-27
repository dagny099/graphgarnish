#!/usr/bin/env python3
"""Find every episode's answer to one recurring question, and graph them.

Many shows end each episode the same way: "Who should we invite next?",
"What's your advice?", "What book would you recommend?". Read across a whole
catalogue, those answers form a network: guests recommending other guests,
the same advice given years apart, the same book named by five people.

A podcast and its questions are described in one config file
(podcasts/*.json). Pick a different podcast by writing another config; ask a
different question by adding an entry to its "questions" list, or pass one on
the command line with --ask.

    python3 tools/qa_graph.py podcasts/catalog-and-cocktails.json --report
    python3 tools/qa_graph.py podcasts/catalog-and-cocktails.json --question advice
    python3 tools/qa_graph.py --feed URL --ask "What are you reading?" \\
        --cue "what are you reading" --out-dir qa/my-show

The pipeline runs in five stages, each of which records what it did so a
wrong answer can be traced back to its source:

    episodes     the RSS feed, every page of it
    transcripts  <podcast:transcript> in the feed, a local folder, a URL
                 template, or local speech-to-text on the audio; cached
    locate       find where the host asks the question (cue phrases first,
                 fuzzy word overlap second)
    extract      cut the guest's answer out of the following turns, then pull
                 names and recurring concepts from it; optionally refine with
                 Claude when ANTHROPIC_API_KEY is set
    graph        episodes, guests, answers, the people and things the answers
                 name, the concepts they share, and answer-to-answer similarity

Outputs go to qa/<podcast-id>-<question-id>/: graph.json (the network, in the
same nodes/links shape as sample_network.json), answers.json (one row per
episode, found or not, with the reason) and answers.csv. qa/index.json lists
every graph so qa.html can offer them.

Standard library only, except for the optional stages: faster-whisper for
speech-to-text and the anthropic SDK for the LLM pass. Neither is imported
unless the config turns it on.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import math
import os
import re
import shutil
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build_network as bn  # noqa: E402  - reuse the feed fetcher and name rules

ROOT = Path(__file__).resolve().parent.parent
QA_DIR = ROOT / "qa"
CACHE_DIR = ROOT / ".cache" / "qa"

# ------------------------------------------------------------------ config

DEFAULT_PODCAST = {
    "id": "",
    "name": "",
    "feed": "",
    "hosts": [],
    "episodes": {
        # Titles matching this are skipped (trailers, companion clips).
        "skip_title_regex": "",
        # itunes:episodeType values to skip.
        "skip_types": ["trailer", "bonus"],
        "since": "",          # YYYY-MM-DD; skip older episodes
        "limit": 0,           # 0 = every episode
    },
    "transcripts": {
        # Tried in order until one yields text. See fetch_transcript().
        "sources": ["feed", "dir"],
        "dir": "",
        "url_template": "",
        "html_start": "",
        "html_end": "",
        "whisper": {
            "model": "base.en",
            "compute_type": "int8",
            # Transcribe only the last N minutes (0 = the whole episode).
            # A question asked at the end of every episode needs only the end.
            "from_end_minutes": 0,
        },
    },
    "guests": {
        # Where to look for each episode's guest(s), in order.
        "from": ["speakers", "graph", "title", "description"],
        # A GraphGarnish network whose GUEST_ON links are trusted more than
        # anything parsed from a title (e.g. sample_network.json).
        "graph": "",
    },
    "llm": {
        # "auto" uses Claude when ANTHROPIC_API_KEY is set; "off" never does.
        "mode": "auto",
        "model": "claude-opus-5",
        "effort": "low",
    },
}

DEFAULT_QUESTION = {
    "id": "",
    "question": "",
    # Phrases the host actually says. Matched case- and punctuation-
    # insensitively against each sentence of the transcript.
    "cues": [],
    # Phrases that mean the answer is over (usually the next recurring
    # question, or the sign-off).
    "stop_cues": [],
    # "people" when the answer names people (who should we invite next?);
    # names found in it become person nodes and RECOMMENDS links.
    "answer_kind": "open",
    # When the cue appears more than once, which occurrence to use.
    "occurrence": "last",
    "max_answer_words": 220,
    # A host turn at least this long ends the guest's answer.
    "host_break_words": 30,
    # Word-overlap threshold for the fuzzy fallback when no cue matches.
    "fuzzy_threshold": 0.6,
    "graph": {
        "concepts_per_answer": 5,
        "concept_min_answers": 2,
        "concept_max_share": 0.3,
        "similar_top_k": 3,
        "similar_threshold": 0.15,
    },
}


def deep_merge(base: dict, over: dict) -> dict:
    out = json.loads(json.dumps(base))
    for key, value in (over or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit].strip("-") or "x"


def load_config(path: Path | None, args) -> tuple[dict, list[dict]]:
    raw: dict = {}
    if path:
        raw = json.loads(path.read_text(encoding="utf-8"))
    podcast = deep_merge(DEFAULT_PODCAST, raw.get("podcast", {}))
    if args.feed:
        podcast["feed"] = args.feed
    if not podcast["id"]:
        podcast["id"] = slug(podcast["name"] or (path.stem if path else "podcast"))
    if path:
        # Relative paths in a config are relative to the config file.
        base = path.resolve().parent
        for section, key in (("transcripts", "dir"), ("guests", "graph")):
            val = podcast[section].get(key)
            if val and not Path(val).is_absolute():
                podcast[section][key] = str((base / val).resolve())

    questions = [deep_merge(DEFAULT_QUESTION, q) for q in raw.get("questions", [])]
    if args.ask:
        questions.append(deep_merge(DEFAULT_QUESTION, {
            "id": args.question or slug(args.ask, 30),
            "question": args.ask,
            "cues": args.cue or [args.ask],
            "stop_cues": args.stop_cue or [],
            "answer_kind": args.answer_kind or "open",
        }))
    elif args.question:
        questions = [q for q in questions if q["id"] == args.question]
        if not questions:
            sys.exit(f"no question with id {args.question!r} in {path}")
    for q in questions:
        if not q["id"]:
            q["id"] = slug(q["question"], 30)
        if not q["cues"]:
            q["cues"] = [q["question"]]
    if not questions:
        sys.exit("no question to ask: add one to the config or pass --ask")
    return podcast, questions


# ---------------------------------------------------------------- episodes

PODCAST_NS = "{https://podcastindex.org/namespace/1.0}"
MEDIA_NS = "{http://search.yahoo.com/mrss/}"


@dataclass
class Episode:
    key: str
    title: str
    date: str
    description: str = ""
    link: str = ""
    guid: str = ""
    episode_type: str = ""
    audio_url: str = ""
    transcripts: list = field(default_factory=list)   # [(url, mime type)]


def episode_key(guid: str, title: str, date: str) -> str:
    basis = guid or f"{date}|{title}"
    return f"{date or 'nodate'}_{slug(title, 40)}_{hashlib.sha1(basis.encode()).hexdigest()[:6]}"


def parse_items(xml_text: str) -> list[Episode]:
    """RSS items, keeping what build_network's parser drops: the audio
    enclosure and any <podcast:transcript> links."""
    out: list[Episode] = []
    for page in bn.split_saved_pages(xml_text):
        try:
            root = ET.fromstring(page)
        except ET.ParseError:
            root = ET.fromstring(bn.dedupe_attributes(page))
        channel = root.find("channel")
        if channel is None:
            channel = root
        for it in channel.findall("item"):
            def txt(tag):
                el = it.find(tag)
                return (el.text or "").strip() if el is not None else ""
            title = bn.strip_html(txt("title"))
            if not title:
                continue
            audio = ""
            enc = it.find("enclosure")
            if enc is not None and "audio" in (enc.get("type") or "audio"):
                audio = enc.get("url", "")
            if not audio:
                for mc in it.findall(f"{MEDIA_NS}content"):
                    if (mc.get("type") or "").startswith("audio"):
                        audio = mc.get("url", "")
                        break
            transcripts = [(t.get("url", ""), (t.get("type") or "").lower())
                           for t in it.findall(f"{PODCAST_NS}transcript") if t.get("url")]
            date = bn.parse_pubdate(txt("pubDate"))
            guid = txt("guid")
            out.append(Episode(
                key=episode_key(guid, title, date),
                title=title,
                date=date,
                description=bn.strip_html(txt("description"))
                or bn.strip_html(txt(bn.CONTENT_NS + "encoded")),
                link=txt("link"),
                guid=guid,
                episode_type=txt(bn.ITUNES_NS + "episodeType").lower(),
                audio_url=audio,
                transcripts=transcripts,
            ))
    return out


def load_episodes(podcast: dict, feed_file: Path | None, save_feed: Path | None) -> list[Episode]:
    if feed_file:
        text = feed_file.read_text(encoding="utf-8")
    else:
        if not podcast["feed"]:
            sys.exit("no feed: set podcast.feed in the config or pass --feed / --feed-file")
        pages, url, seen = [], podcast["feed"], set()
        for _ in range(bn.MAX_FEED_PAGES):
            if url in seen:
                break
            seen.add(url)
            payload = bn.fetch_feed(url)
            pages.append(f"<!-- page: {payload.url} -->\n{payload.text}")
            nxt = bn.next_page_url(payload.text)
            if not nxt:
                break
            url = urllib.parse.urljoin(payload.url, nxt)
        else:
            print(f"warning: stopped after {bn.MAX_FEED_PAGES} feed pages", file=sys.stderr)
        text = "\n".join(pages)
        print(f"fetched {len(pages)} feed page(s)", file=sys.stderr)
        if save_feed:
            save_feed.write_text(text, encoding="utf-8")

    episodes, seen_keys = [], set()
    for ep in parse_items(text):
        if ep.key in seen_keys:
            continue
        seen_keys.add(ep.key)
        episodes.append(ep)

    rules = podcast["episodes"]
    skip_re = re.compile(rules["skip_title_regex"], re.I) if rules["skip_title_regex"] else None
    skip_types = {t.lower() for t in rules["skip_types"]}
    kept = [ep for ep in episodes
            if not (skip_re and skip_re.search(ep.title))
            and ep.episode_type not in skip_types
            and not (rules["since"] and ep.date and ep.date < rules["since"])]
    kept.sort(key=lambda e: (e.date, e.title), reverse=True)
    if rules["limit"]:
        kept = kept[: rules["limit"]]
    print(f"{len(episodes)} items in feed, {len(kept)} episodes kept", file=sys.stderr)
    return kept


# ------------------------------------------------------------- transcripts

@dataclass
class Segment:
    text: str
    speaker: str = ""
    start: float | None = None


TIME_RE = r"(?:\d{1,2}:)?\d{1,2}:\d{2}(?:[.,]\d{1,3})?"


def to_seconds(stamp: str) -> float | None:
    if not stamp:
        return None
    parts = stamp.replace(",", ".").split(":")
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


VOICE_RE = re.compile(r"<v(?:\.[^ >]*)?\s+([^>]+)>")
TAG_RE = re.compile(r"<[^>]+>")
SPEAKER_PREFIX_RE = re.compile(
    rf"^\s*(?:\[?(?P<t1>{TIME_RE})\]?\s*)?"
    rf"(?P<spk>[A-Z][\w.'\- ]{{0,38}}?)\s*(?:\(?\[?(?P<t2>{TIME_RE})\]?\)?)?\s*:\s+(?P<rest>.*)$")
BARE_TIME_RE = re.compile(rf"^\s*\[?\(?(?P<t>{TIME_RE})\)?\]?\s*(?P<rest>.*)$")
NOT_SPEAKER = re.compile(r"^(?:note|http|https|www|transcript|chapter|see|and|but|so|q|a)$", re.I)


def parse_cue_blocks(text: str) -> list[Segment]:
    """WebVTT and SubRip: blank-line separated cues with a --> timing line."""
    segs = []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [l for l in block.split("\n") if l.strip()]
        timing = next((i for i, l in enumerate(lines) if "-->" in l), None)
        if timing is None:
            continue
        start = to_seconds(lines[timing].split("-->")[0].strip().split(" ")[0])
        body = " ".join(lines[timing + 1:])
        speaker = ""
        vm = VOICE_RE.search(body)
        if vm:
            speaker = vm.group(1).strip()
        body = html.unescape(TAG_RE.sub("", body)).strip()
        if not speaker:
            sm = SPEAKER_PREFIX_RE.match(body)
            if sm and not NOT_SPEAKER.match(sm.group("spk")):
                speaker, body = sm.group("spk").strip(), sm.group("rest")
        if body:
            segs.append(Segment(body, speaker, start))
    return dedupe_rolling(segs)


def dedupe_rolling(segs: list[Segment]) -> list[Segment]:
    """Auto-captions repeat each line as it scrolls ("a b" / "a b c"). Keep
    only the new words of each cue."""
    out: list[Segment] = []
    for s in segs:
        if out:
            prev = out[-1].text
            if s.text == prev:
                continue
            if s.text.startswith(prev) and s.speaker == out[-1].speaker:
                extra = s.text[len(prev):].strip()
                if extra:
                    out.append(Segment(extra, s.speaker, s.start))
                continue
        out.append(s)
    return out


def parse_json_transcript(data) -> list[Segment]:
    """Podcasting 2.0 JSON ({"segments": [...]}) and close relatives."""
    rows = data.get("segments") if isinstance(data, dict) else data
    if not isinstance(rows, list):
        return []
    segs = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        body = r.get("body") or r.get("text") or ""
        start = r.get("startTime", r.get("start"))
        segs.append(Segment(str(body).strip(), str(r.get("speaker") or "").strip(),
                            float(start) if isinstance(start, (int, float)) else to_seconds(str(start or ""))))
    return [s for s in segs if s.text]


def parse_plain(text: str) -> list[Segment]:
    """Plain text or stripped HTML: 'Speaker Name (00:12:03): words' lines,
    '[00:12:03] words' lines, or just paragraphs."""
    segs: list[Segment] = []
    pending_speaker, pending_time = "", None
    for line in text.replace("\r", "").split("\n"):
        line = line.strip()
        if not line:
            continue
        m = SPEAKER_PREFIX_RE.match(line)
        if m and not NOT_SPEAKER.match(m.group("spk")) and len(m.group("spk").split()) <= 4:
            t = to_seconds(m.group("t1") or m.group("t2") or "")
            if m.group("rest").strip():
                segs.append(Segment(m.group("rest").strip(), m.group("spk").strip(), t))
            else:
                pending_speaker, pending_time = m.group("spk").strip(), t
            continue
        # "Speaker Name  00:12:03" alone on a line, text on the next ones.
        hm = re.match(rf"^([A-Z][\w.'\- ]{{1,38}}?)\s+\(?({TIME_RE})\)?$", line)
        if hm and len(hm.group(1).split()) <= 4:
            pending_speaker, pending_time = hm.group(1).strip(), to_seconds(hm.group(2))
            continue
        tm = BARE_TIME_RE.match(line)
        if tm and tm.group("rest"):
            segs.append(Segment(tm.group("rest"), pending_speaker, to_seconds(tm.group("t"))))
            continue
        segs.append(Segment(line, pending_speaker, pending_time))
        pending_time = None
    return segs


def html_to_text(raw: str, start: str = "", end: str = "") -> str:
    if start:
        m = re.search(start, raw, re.I | re.S)
        if m:
            raw = raw[m.end():]
    if end:
        m = re.search(end, raw, re.I | re.S)
        if m:
            raw = raw[: m.start()]
    raw = re.sub(r"<(script|style|nav|header|footer)\b.*?</\1>", " ", raw, flags=re.S | re.I)
    raw = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h\d>|<cite\b[^>]*>", "\n", raw, flags=re.I)
    raw = re.sub(r"</cite>", ": ", raw, flags=re.I)
    text = html.unescape(TAG_RE.sub(" ", raw))
    return "\n".join(re.sub(r"[ \t]+", " ", l).strip() for l in text.split("\n"))


def parse_transcript(raw: str, mime: str = "", html_start: str = "", html_end: str = "") -> list[Segment]:
    mime = (mime or "").lower()
    head = raw.lstrip()[:200]
    if "json" in mime or head.startswith("{") or head.startswith("["):
        try:
            return merge_turns(parse_json_transcript(json.loads(raw)))
        except (ValueError, TypeError):
            pass
    if "vtt" in mime or "subrip" in mime or "srt" in mime or head.startswith("WEBVTT") \
            or re.search(r"\d{2}:\d{2}[.,]\d{3}\s*-->", raw[:2000]):
        return merge_turns(parse_cue_blocks(raw))
    if "html" in mime or re.search(r"<(html|body|p|div)\b", head, re.I):
        raw = html_to_text(raw, html_start, html_end)
    return merge_turns(parse_plain(raw))


def merge_turns(segs: list[Segment]) -> list[Segment]:
    """Consecutive segments from the same labelled speaker become one turn."""
    out: list[Segment] = []
    for s in segs:
        if out and s.speaker and s.speaker == out[-1].speaker:
            out[-1].text = f"{out[-1].text} {s.text}"
        else:
            out.append(Segment(s.text, s.speaker, s.start))
    return out


class TranscriptStore:
    """Fetches, parses and caches transcripts. A cached transcript is reused
    on every later run, so speech-to-text happens once per episode."""

    def __init__(self, podcast: dict, cache_dir: Path, offline: bool = False):
        self.cfg = podcast["transcripts"]
        self.dir = cache_dir / podcast["id"] / "transcripts"
        self.dir.mkdir(parents=True, exist_ok=True)
        self.offline = offline
        self._whisper = None

    def get(self, ep: Episode) -> dict | None:
        path = self.dir / f"{ep.key}.json"
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
        errors = []
        for source in self.cfg["sources"]:
            try:
                got = self.fetch(source, ep)
            except Exception as exc:  # noqa: BLE001 - try the next source
                errors.append(f"{source}: {exc}")
                continue
            if got and got["segments"]:
                path.write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")
                return got
        if errors:
            print(f"  no transcript for {ep.title[:60]!r}: {'; '.join(errors)}", file=sys.stderr)
        return None

    def fetch(self, source: str, ep: Episode) -> dict | None:
        def packed(segs, where, kind, window_start=0.0):
            return {"source": kind, "url": where, "window_start": window_start,
                    "segments": [asdict(s) for s in segs]}

        if source == "feed":
            if not ep.transcripts or self.offline:
                return None
            rank = ["json", "vtt", "srt", "subrip", "html", "text"]
            best = sorted(ep.transcripts, key=lambda t: next(
                (i for i, r in enumerate(rank) if r in t[1]), len(rank)))
            url, mime = best[0]
            raw, ctype = http_get(url)
            return packed(parse_transcript(raw, mime or ctype), url, "feed")

        if source == "dir":
            folder = Path(self.cfg.get("dir") or "")
            if not self.cfg.get("dir") or not folder.is_dir():
                return None
            stems = {ep.key, slug(ep.title), slug(ep.guid) if ep.guid else "", ep.guid}
            for f in sorted(folder.iterdir()):
                if f.stem in stems or slug(f.stem) in stems:
                    raw = f.read_text(encoding="utf-8", errors="replace")
                    mime = {".vtt": "text/vtt", ".srt": "application/x-subrip",
                            ".json": "application/json", ".html": "text/html"}.get(f.suffix.lower(), "")
                    return packed(parse_transcript(raw, mime), str(f), "dir")
            return None

        if source == "url_template":
            tmpl = self.cfg.get("url_template")
            if not tmpl or self.offline:
                return None
            url = tmpl.format(title_slug=slug(ep.title, 200), guid=ep.guid, link=ep.link,
                              link_slug=ep.link.rstrip("/").rsplit("/", 1)[-1])
            raw, ctype = http_get(url)
            return packed(parse_transcript(raw, ctype, self.cfg.get("html_start", ""),
                                           self.cfg.get("html_end", "")), url, "url_template")

        if source == "whisper":
            if not ep.audio_url or self.offline:
                return None
            segs, offset = self.transcribe(ep.audio_url)
            return packed(segs, ep.audio_url, "whisper", offset)

        raise ValueError(f"unknown transcript source {source!r}")

    def transcribe(self, audio_url: str) -> tuple[list[Segment], float]:
        """Local speech-to-text with faster-whisper, which decodes audio
        itself (PyAV), so no ffmpeg is needed. Only the tail of the episode
        when from_end_minutes is set. Timestamps are shifted back to episode
        time so a link can seek straight to the answer."""
        wcfg = self.cfg["whisper"]
        from faster_whisper import WhisperModel, decode_audio  # optional dependency
        if self._whisper is None:
            self._whisper = WhisperModel(wcfg["model"], device="cpu",
                                         compute_type=wcfg["compute_type"])
        rate = 16000
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "audio"
            req = urllib.request.Request(audio_url, headers=bn.FETCH_HEADERS)
            with urllib.request.urlopen(req, timeout=300) as resp, src.open("wb") as fh:
                shutil.copyfileobj(resp, fh)
            audio = decode_audio(str(src), sampling_rate=rate)
        tail = int(float(wcfg.get("from_end_minutes") or 0) * 60 * rate)
        offset = 0.0
        if tail and len(audio) > tail:
            offset = (len(audio) - tail) / rate
            audio = audio[-tail:]
        pieces, _ = self._whisper.transcribe(audio, vad_filter=True)
        segs = [Segment(p.text.strip(), "", round(p.start + offset, 1)) for p in pieces]
        return [s for s in segs if s.text], offset


def http_get(url: str, timeout: int = 60) -> tuple[str, str]:
    req = urllib.request.Request(url, headers={**bn.FETCH_HEADERS, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return (resp.read().decode("utf-8", "replace"),
                resp.headers.get("Content-Type", ""))


# ------------------------------------------------------------------ locate

WORD_RE = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")


def norm(text: str) -> str:
    text = text.lower().replace("’", "'").replace("‘", "'")
    return " ".join(WORD_RE.findall(text))


SENT_SPLIT = re.compile(r"(?<=[.?!])\s+(?=[A-Z0-9\"'“])")


@dataclass
class Sentence:
    text: str
    seg: int
    speaker: str
    start: float | None


def sentences(segs: list[dict]) -> list[Sentence]:
    out = []
    for i, s in enumerate(segs):
        for piece in SENT_SPLIT.split(s["text"]):
            piece = piece.strip()
            if piece:
                out.append(Sentence(piece, i, s.get("speaker") or "", s.get("start")))
    return out


STOPWORDS = set("""
a about above after again against all am an and any are aren't as at be because been before
being below between both but by can can't cannot could couldn't did didn't do does doesn't doing
don't down during each few for from further had hadn't has hasn't have haven't having he he'd
he'll he's her here here's hers herself him himself his how how's i i'd i'll i'm i've if in
into is isn't it it's its itself let's me more most mustn't my myself no nor not of off on once
only or other ought our ours ourselves out over own same shan't she she'd she'll she's should
shouldn't so some such than that that's the their theirs them themselves then there there's these
they they'd they'll they're they've this those through to too under until up very was wasn't we
we'd we'll we're we've were weren't what what's when when's where where's which while who who's
whom why why's with won't would wouldn't you you'd you'll you're you've your yours yourself
yourselves yeah yes like know think really um uh gonna kind sort thing things lot just actually
right okay ok mean stuff going get got say said want also much well even way something one two
go goes lot lots maybe probably definitely absolutely basically literally honestly anyway
oh hey alright sure great good thank thanks everybody everyone somebody someone people guy guys
make makes made take takes see look looking come comes came need needs time times will
us let because 's re ve ll don doesn didn isn aren wasn weren wouldn couldn shouldn gotta wanna
back still around always never every lot today now then first last next new bit little big
""".split())


def content_words(text: str) -> list[str]:
    return [w for w in norm(text).split() if w not in STOPWORDS and len(w) > 2]


@dataclass
class Location:
    sentence: int
    method: str
    score: float


def locate(sents: list[Sentence], q: dict, hosts: set[str]) -> Location | None:
    cues = [norm(c) for c in q["cues"] if norm(c)]
    qwords = set(content_words(q["question"])) | {w for c in q["cues"] for w in content_words(c)}
    hits: list[Location] = []
    for i, s in enumerate(sents):
        n = norm(s.text)
        if any(c in n for c in cues):
            hits.append(Location(i, "cue", 1.0))
            continue
        words = set(content_words(s.text))
        if qwords and words and s.text.rstrip().endswith("?"):
            overlap = len(qwords & words) / len(qwords)
            if overlap >= q["fuzzy_threshold"]:
                hits.append(Location(i, "fuzzy", round(overlap, 2)))
    if not hits:
        return None
    # Prefer exact cues, and a host asking over a guest repeating the question.
    def asked_by_host(loc):
        spk = sents[loc.sentence].speaker.lower()
        return 1 if spk and any(h in spk or spk in h for h in hosts) else 0
    best_method = "cue" if any(h.method == "cue" for h in hits) else "fuzzy"
    pool = [h for h in hits if h.method == best_method]
    if any(asked_by_host(h) for h in pool):
        pool = [h for h in pool if asked_by_host(h)]
    if best_method == "fuzzy":
        top = max(h.score for h in pool)
        pool = [h for h in pool if h.score == top]
    return pool[-1] if q["occurrence"] == "last" else pool[0]


def is_host(speaker: str, hosts: set[str]) -> bool:
    spk = speaker.lower().strip()
    if not spk:
        return False
    return any(h == spk or h.split()[0] == spk or spk in h or h in spk for h in hosts)


def answer_span(sents: list[Sentence], loc: Location, q: dict, hosts: set[str]) -> tuple[str, float | None, str]:
    """The guest's words after the question, until the next recurring
    question, a substantial host turn, or the word limit. Returns (text,
    start seconds, why it stopped)."""
    stops = [norm(c) for c in q["stop_cues"] if norm(c)]
    cues = [norm(c) for c in q["cues"] if norm(c)]
    labelled = any(s.speaker for s in sents)
    # Several questions often arrive in one breath ("What's your advice? Who
    # should we invite next?"). Skip the rest of the asker's turn.
    i = loc.sentence + 1
    asker = sents[loc.sentence].speaker
    if labelled and asker:
        while i < len(sents) and sents[i].speaker == asker:
            i += 1
    words: list[str] = []
    start, reason = None, "end of transcript"
    host_run, host_run_words = [], 0
    while i < len(sents):
        s = sents[i]
        n = norm(s.text)
        if words and any(c in n for c in stops):
            reason = "stop cue"
            break
        if words and any(c in n for c in cues):
            reason = "question asked again"
            break
        if labelled and is_host(s.speaker, hosts):
            host_run.append(s.text)
            host_run_words += len(s.text.split())
            if words and host_run_words >= q["host_break_words"]:
                reason = "host took over"
                break
            i += 1
            continue
        if host_run and words:
            # A short host interjection ("Ha, great pick") stays out of the
            # answer but does not end it.
            host_run, host_run_words = [], 0
        if start is None:
            start = s.start
        words.extend(s.text.split())
        if len(words) >= q["max_answer_words"]:
            reason = "word limit"
            break
        i += 1
    return " ".join(words[: q["max_answer_words"]]), start, reason


# ----------------------------------------------------------------- extract

NAME_TOKEN = r"(?:[A-Z][a-z]+(?:[-'][A-Z]?[a-z]+)?|[A-Z]{2,}|[A-Z]\.)"
NAME_RE = re.compile(rf"\b{NAME_TOKEN}(?:\s+(?:de|van|von|da|del|la|le|di)?\s*{NAME_TOKEN}){{1,2}}\b")
ENTITY_RE = re.compile(r"\b(?:[A-Z][\w&.'-]*[A-Za-z0-9])(?:\s+(?:of|the|and|&|for|de)?\s*[A-Z][\w&.'-]*[A-Za-z0-9]){0,4}")
CAP_STOP = set("""I I'm I've I'd I'll The A An And But So Or If When What Who Why How Where This That
These Those There Then Yeah Yes No Oh Okay Ok Well Like Also Because Just Really Actually My Our
Your We You He She They It It's Its Um Uh Hey Thanks Thank Absolutely Definitely Honestly Great
Good Right Sure Maybe Probably Mr Mrs Ms Dr Monday Tuesday Wednesday Thursday Friday Saturday Sunday
January February March April May June July August September October November December Catalog
Cocktails Honest Podcast Episode Season Juan Tim""".split())


def candidate_names(text: str, exclude: set[str]) -> list[str]:
    """Two- or three-token capitalised runs that look like a person."""
    out = []
    for m in NAME_RE.finditer(text):
        name = m.group(0).strip()
        toks = name.split()
        if toks[0] in CAP_STOP or any(t.upper() == t and len(t) > 1 for t in toks):
            continue
        if not bn.looks_like_person(name):
            continue
        if name.lower() in exclude:
            continue
        out.append(name)
    return list(dict.fromkeys(out))


def candidate_entities(text: str, exclude: set[str]) -> list[str]:
    """Capitalised runs that are not at the start of a sentence: books,
    companies, products, frameworks. Single words qualify when they appear
    mid-sentence, which filters most sentence-initial noise."""
    out = []
    for m in ENTITY_RE.finditer(text):
        ent = m.group(0).strip(" .,'")
        first = ent.split()[0]
        if first in CAP_STOP:
            ent = " ".join(ent.split()[1:])
            if not ent:
                continue
        before = text[: m.start()].rstrip()
        sentence_start = not before or before[-1] in ".?!\""
        if sentence_start and len(ent.split()) == 1:
            continue
        if len(ent) < 3 or ent.lower() in exclude or ent in CAP_STOP:
            continue
        out.append(ent)
    return list(dict.fromkeys(out))


LLM_SCHEMA = {
    "type": "object",
    "properties": {
        "found": {"type": "boolean"},
        "answer_summary": {"type": "string"},
        "quote": {"type": "string"},
        "people": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "kind": {"type": "string", "enum": ["organization", "work", "product", "place", "other"]},
            },
            "required": ["name", "kind"],
            "additionalProperties": False,
        }},
        "themes": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "answer_summary", "quote", "people", "entities", "themes"],
    "additionalProperties": False,
}


class LLM:
    """Optional refinement with Claude. Reads the transcript excerpt around
    the located question (or the tail of the transcript when the locator
    found nothing), and returns the answer, a verbatim quote, the people and
    things named, and short themes. Themes already used are passed back in so
    the same idea gets the same label across episodes."""

    def __init__(self, cfg: dict):
        import anthropic  # optional dependency
        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = cfg["model"]
        self.effort = cfg["effort"]
        self.use_fallbacks = True

    def extract(self, q: dict, ep: Episode, guests: list[str], excerpt: str,
                known_themes: list[str]) -> dict | None:
        prompt = (
            f"Podcast episode: {ep.title} ({ep.date})\n"
            f"Guest(s): {', '.join(guests) or 'unknown'}\n"
            f"Recurring question the hosts ask every guest: \"{q['question']}\"\n\n"
            "Below is a transcript excerpt. Find the guest's answer to that question.\n"
            "- found: false if the question is not asked or not answered in the excerpt.\n"
            "- answer_summary: one or two sentences, in the guest's terms.\n"
            "- quote: the most representative verbatim sentence or two of the answer.\n"
            "- people: full names of people the answer names (not the hosts, not the guest).\n"
            "- entities: organizations, books/papers (work), products, places the answer names.\n"
            "- themes: 1-4 short lowercase labels (1-3 words) for the ideas in the answer. "
            "Reuse a label from this list when it fits: "
            f"{', '.join(known_themes[:150]) or '(none yet)'}\n\n"
            f"<transcript>\n{excerpt}\n</transcript>"
        )
        kwargs = dict(
            model=self.model,
            max_tokens=4000,
            thinking={"type": "adaptive"},
            output_config={"effort": self.effort,
                           "format": {"type": "json_schema", "schema": LLM_SCHEMA}},
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            if self.use_fallbacks:
                # Route a safety-classifier decline to Anthropic's recommended
                # fallback model instead of losing the episode.
                resp = self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
            else:
                resp = self.client.messages.create(**kwargs)
        except self.anthropic.BadRequestError as exc:
            if self.use_fallbacks:
                print(f"  LLM: fallbacks rejected ({exc.message}); retrying without", file=sys.stderr)
                self.use_fallbacks = False
                return self.extract(q, ep, guests, excerpt, known_themes)
            print(f"  LLM: bad request for {ep.title[:50]!r}: {exc.message}", file=sys.stderr)
            return None
        except (self.anthropic.RateLimitError, self.anthropic.APIConnectionError,
                self.anthropic.APIStatusError) as exc:
            print(f"  LLM: {type(exc).__name__} for {ep.title[:50]!r}", file=sys.stderr)
            return None
        if resp.stop_reason == "refusal":
            return None
        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return json.loads(text)
        except ValueError:
            return None


def make_llm(cfg: dict) -> LLM | None:
    mode = cfg["mode"]
    if mode == "off":
        return None
    if mode == "auto" and not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        return LLM(cfg)
    except ImportError:
        print("LLM pass skipped: pip install anthropic", file=sys.stderr)
        return None


# ------------------------------------------------------------------ guests

GENERIC_SPEAKER = re.compile(r"^(speaker|spk|unknown|host|guest|interviewer|narrator)\b", re.I)


def load_guest_graph(path: str) -> dict[str, list[str]]:
    """episode-title key -> guest names, from an existing GraphGarnish graph."""
    if not path or not Path(path).exists():
        return {}
    g = json.loads(Path(path).read_text(encoding="utf-8"))
    names = {n["id"]: n["name"] for n in g["nodes"] if n.get("type") == "person"}
    titles = {n["id"]: n for n in g["nodes"] if n.get("type") == "episode"}
    out: dict[str, list[str]] = defaultdict(list)
    for l in g["links"]:
        if l.get("type") == "GUEST_ON" and l["source"] in names and l["target"] in titles:
            ep = titles[l["target"]]
            out[title_key(ep["name"])].append(names[l["source"]])
            if ep.get("date"):
                out[f"{ep['date']}"].append(names[l["source"]])
    return out


def title_key(title: str) -> str:
    return norm(title)[:60]


def find_guests(ep: Episode, segs: list[dict], podcast: dict, hosts: set[str],
                guest_graph: dict[str, list[str]]) -> tuple[list[str], str]:
    for how in podcast["guests"]["from"]:
        found: list[str] = []
        if how == "speakers":
            counts = Counter(s["speaker"] for s in segs if s.get("speaker"))
            found = [s for s, _ in counts.most_common()
                     if not is_host(s, hosts) and not GENERIC_SPEAKER.match(s)
                     and len(s.split()) >= 2]
        elif how == "graph":
            found = guest_graph.get(title_key(ep.title), [])
        elif how == "title":
            found = bn.guests_from_title(ep.title)
        elif how == "description":
            found = bn.guests_from_description(ep.description)
        found = [bn.normalize_name(g) for g in found if g.lower() not in hosts]
        if found:
            return list(dict.fromkeys(found)), how
    return [], ""


# ------------------------------------------------------------------- graph

def tokens_for_tfidf(text: str) -> list[str]:
    words = content_words(text)
    return words + [f"{a} {b}" for a, b in zip(words, words[1:])]


def tfidf(docs: list[list[str]]) -> tuple[list[dict[str, float]], Counter]:
    df = Counter()
    for d in docs:
        df.update(set(d))
    n = len(docs)
    vecs = []
    for d in docs:
        tf = Counter(d)
        v = {t: (c / len(d)) * math.log((1 + n) / (1 + df[t])) for t, c in tf.items()} if d else {}
        norm_ = math.sqrt(sum(x * x for x in v.values())) or 1.0
        vecs.append({t: x / norm_ for t, x in v.items()})
    return vecs, df


def cosine(a: dict, b: dict) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(t, 0.0) for t, x in a.items())


def node_id(prefix: str, name: str) -> str:
    return f"{prefix}_{slug(name, 50).replace('-', '_')}"


def build_graph(podcast: dict, q: dict, rows: list[dict], hosts: set[str]) -> dict:
    nodes: dict[str, dict] = {}
    links: list[dict] = []

    def add(node):
        nid = node["id"]
        if nid in nodes:
            for k, v in node.items():
                nodes[nid].setdefault(k, v)
        else:
            nodes[nid] = node
        return nid

    def link(s, t, typ, **extra):
        links.append({"source": s, "target": t, "type": typ, **extra})

    qid = add({"id": "question", "type": "question", "name": q["question"]})
    answered = [r for r in rows if r["found"]]

    # Concepts: words and word pairs that recur across answers but are not
    # everywhere. LLM themes, when present, are used as given.
    vecs, df = tfidf([tokens_for_tfidf(r["answer"]) for r in answered])
    n = max(1, len(answered))
    gcfg = q["graph"]
    concept_ok = {t for t, c in df.items()
                  if c >= gcfg["concept_min_answers"] and c / n <= gcfg["concept_max_share"]}

    person_keys: dict[str, str] = {}

    def person(name, role):
        key = norm(name)
        nid = person_keys.get(key) or node_id("person", name)
        person_keys[key] = nid
        add({"id": nid, "type": "person", "name": name, "roles": []})
        if role not in nodes[nid]["roles"]:
            nodes[nid]["roles"].append(role)
        return nid

    for r in rows:
        eid = add({"id": f"ep_{r['key']}", "type": "episode", "name": r["title"],
                   "date": r["date"], "url": r["link"], "is_full": True,
                   "has_transcript": r["has_transcript"], "answered": r["found"]})
        guest_ids = [person(g, "guest") for g in r["guests"]]
        for gid in guest_ids:
            link(gid, eid, "GUEST_ON")
        if not r["found"]:
            continue
        aid = add({
            "id": f"ans_{r['key']}", "type": "answer",
            "name": (r["summary"] or r["answer"])[:90],
            "date": r["date"], "episode": r["title"], "guests": r["guests"],
            "text": r["answer"], "summary": r["summary"], "quote": r["quote"],
            "start": r["start"], "method": r["method"], "confidence": r["confidence"],
            "transcript_source": r["transcript_source"], "audio_url": r["audio_url"],
        })
        link(aid, qid, "ANSWERS")
        link(aid, eid, "FROM_EPISODE")
        for gid in guest_ids:
            link(gid, aid, "GAVE")
        for name in r["people"]:
            pid = person(name, "mentioned")
            if pid in guest_ids:
                continue
            link(aid, pid, "MENTIONS")
            if q["answer_kind"] == "people":
                for gid in guest_ids:
                    link(gid, pid, "RECOMMENDS", date=r["date"])
        for ent in r["entities"]:
            name, kind = (ent["name"], ent.get("kind", "other")) if isinstance(ent, dict) else (ent, "other")
            if norm(name) in person_keys:
                continue
            xid = add({"id": node_id("ent", name), "type": "entity", "name": name, "kind": kind})
            link(aid, xid, "MENTIONS")
        for theme in r["themes"]:
            cid = add({"id": node_id("concept", theme), "type": "concept", "name": theme})
            link(aid, cid, "ABOUT", provenance="llm")

    # TF-IDF concepts for answers the LLM did not theme.
    for r, vec in zip(answered, vecs):
        if r["themes"]:
            continue
        ranked = sorted(((w, t) for t, w in vec.items() if t in concept_ok), reverse=True)
        chosen: list[str] = []
        for _, term in ranked:
            # Keep "data governance" and drop "governance" once it is covered.
            if any(term in c.split() or c in term.split() for c in chosen):
                continue
            chosen.append(term)
            if len(chosen) >= gcfg["concepts_per_answer"]:
                break
        for term in chosen:
            cid = add({"id": node_id("concept", term), "type": "concept", "name": term})
            link(f"ans_{r['key']}", cid, "ABOUT", provenance="tfidf")

    # Answer-to-answer similarity: each answer's nearest neighbours.
    seen_pairs = set()
    for i, (ri, vi) in enumerate(zip(answered, vecs)):
        scored = sorted(((cosine(vi, vj), j) for j, vj in enumerate(vecs) if j != i), reverse=True)
        for score, j in scored[: gcfg["similar_top_k"]]:
            if score < gcfg["similar_threshold"]:
                break
            pair = tuple(sorted((i, j)))
            if pair in seen_pairs:
                continue
            seen_pairs.add(pair)
            link(f"ans_{ri['key']}", f"ans_{answered[j]['key']}", "SIMILAR_TO",
                 weight=round(score, 3))

    # Degree counts help the viewer size nodes without recomputing.
    deg = Counter()
    for l in links:
        deg[l["source"]] += 1
        deg[l["target"]] += 1
    for nid, node in nodes.items():
        node["degree"] = deg[nid]
    # A concept named by one answer connects nothing; drop it.
    keep = {nid for nid, node in nodes.items()
            if node["type"] not in ("concept", "entity") or deg[nid] >= 2
            or node["type"] == "entity"}
    links = [l for l in links if l["source"] in keep and l["target"] in keep]

    recommended_then_guest = sorted(
        node["name"] for node in nodes.values()
        if node["type"] == "person" and {"guest", "mentioned"} <= set(node["roles"]))

    with_transcript = sum(1 for r in rows if r["has_transcript"])
    return {
        "meta": {
            "podcast": podcast["name"] or podcast["id"],
            "podcast_id": podcast["id"],
            "question_id": q["id"],
            "question": q["question"],
            "answer_kind": q["answer_kind"],
            "episodes": len(rows),
            "with_transcript": with_transcript,
            "answered": len(answered),
            "methods": dict(Counter(r["method"] for r in answered)),
            "people_named_and_later_guests": recommended_then_guest,
            "node_types": dict(Counter(nodes[k]["type"] for k in keep)),
            "link_types": dict(Counter(l["type"] for l in links)),
        },
        "nodes": [nodes[k] for k in nodes if k in keep],
        "links": links,
    }


# --------------------------------------------------------------------- run

def excerpt_for_llm(sents: list[Sentence], loc: Location | None, words: int = 1500) -> str:
    if loc:
        lo = max(0, loc.sentence - 8)
        chunk = sents[lo:]
    else:
        chunk = sents
    lines, count = [], 0
    source = chunk if loc else list(reversed(chunk))
    for s in source:
        lines.append(f"{s.speaker + ': ' if s.speaker else ''}{s.text}")
        count += len(s.text.split())
        if count >= words:
            break
    return "\n".join(lines if loc else reversed(lines))


def run_question(podcast: dict, q: dict, episodes: list[Episode], store: TranscriptStore,
                 llm: LLM | None, guest_graph: dict, hosts: set[str]) -> list[dict]:
    rows, themes = [], Counter()
    guest_exclude = hosts | {h.split()[0] for h in hosts}
    for idx, ep in enumerate(episodes, 1):
        tr = store.get(ep)
        segs = tr["segments"] if tr else []
        guests, guest_source = find_guests(ep, segs, podcast, hosts, guest_graph)
        row = {
            "key": ep.key, "title": ep.title, "date": ep.date, "link": ep.link,
            "audio_url": ep.audio_url, "guests": guests, "guest_source": guest_source,
            "has_transcript": bool(segs),
            "transcript_source": (tr or {}).get("source", ""),
            "found": False, "reason": "", "method": "", "confidence": 0.0,
            "answer": "", "summary": "", "quote": "", "start": None,
            "people": [], "entities": [], "themes": [],
        }
        rows.append(row)
        if not segs:
            row["reason"] = "no transcript"
            continue
        sents = sentences(segs)
        loc = locate(sents, q, hosts)
        if loc:
            text, start, why = answer_span(sents, loc, q, hosts)
            row.update(answer=text, start=start, method=loc.method,
                       confidence=loc.score, reason=why, found=bool(text.strip()))
            if not text.strip():
                row["reason"] = "question found, no answer after it"
        else:
            row["reason"] = "question not found"

        exclude = {g.lower() for g in guests} | guest_exclude
        if llm:
            got = llm.extract(q, ep, guests, excerpt_for_llm(sents, loc), sorted(themes, key=lambda t: -themes[t]))
            if got is not None and got.get("found"):
                row.update(found=True, summary=got["answer_summary"].strip(),
                           quote=got["quote"].strip(),
                           people=[p for p in got["people"] if p.lower() not in exclude],
                           entities=got["entities"],
                           themes=[t.lower().strip() for t in got["themes"] if t.strip()])
                if not row["answer"]:
                    row.update(answer=row["quote"], method="llm", reason="located by LLM")
                row["method"] = f"{row['method'] or 'llm'}+llm" if row["method"] != "llm" else "llm"
                themes.update(row["themes"])
            elif got is not None and not got.get("found") and row["found"] and loc and loc.method == "fuzzy":
                # A fuzzy match the model reads as not-an-answer is dropped.
                row.update(found=False, reason="fuzzy match rejected by LLM")
        if row["found"] and not row["people"] and not row["entities"]:
            row["people"] = candidate_names(row["answer"], exclude)
            row["entities"] = [e for e in candidate_entities(row["answer"], exclude)
                               if not any(e in p or p in e for p in row["people"])]
        if row["found"] and not row["quote"]:
            row["quote"] = " ".join(row["answer"].split()[:60])
        if idx % 25 == 0:
            print(f"  {idx}/{len(episodes)} episodes", file=sys.stderr)
    return rows


def write_outputs(out_dir: Path, graph: dict, rows: list[dict]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "graph.json").write_text(json.dumps(graph, indent=1, ensure_ascii=False) + "\n",
                                        encoding="utf-8")
    (out_dir / "answers.json").write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n",
                                          encoding="utf-8")
    cols = ["date", "title", "guests", "found", "reason", "method", "start",
            "summary", "answer", "people", "entities", "themes", "transcript_source"]
    with (out_dir / "answers.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for r in rows:
            w.writerow([
                "; ".join(x["name"] if isinstance(x, dict) else x for x in r[c])
                if isinstance(r[c], list) else r[c] for c in cols])


def write_index(qa_dir: Path) -> None:
    entries = []
    for g in sorted(qa_dir.glob("*/graph.json")):
        meta = json.loads(g.read_text(encoding="utf-8"))["meta"]
        entries.append({"path": f"{g.parent.name}/graph.json", **{k: meta[k] for k in (
            "podcast", "podcast_id", "question_id", "question", "episodes",
            "with_transcript", "answered")}})
    (qa_dir / "index.json").write_text(json.dumps(entries, indent=1, ensure_ascii=False) + "\n",
                                       encoding="utf-8")


def report(graph: dict, rows: list[dict]) -> None:
    m = graph["meta"]
    print(f"\n== {m['podcast']} :: {m['question']}")
    print(f"   episodes {m['episodes']}  with transcript {m['with_transcript']}  "
          f"answered {m['answered']}  methods {m['methods']}")
    print(f"   nodes {m['node_types']}")
    print(f"   links {m['link_types']}")
    reasons = Counter(r["reason"] for r in rows if not r["found"])
    if reasons:
        print(f"   not answered: {dict(reasons)}")
    if m["people_named_and_later_guests"]:
        print(f"   named in an answer and also a guest: {', '.join(m['people_named_and_later_guests'][:15])}")
    for r in [r for r in rows if r["found"]][:5]:
        print(f"   - {r['date']} {', '.join(r['guests']) or '?'}: {r['quote'][:110]}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("config", nargs="?", type=Path, help="podcasts/<name>.json")
    ap.add_argument("--feed", help="RSS feed URL (overrides the config)")
    ap.add_argument("--feed-file", type=Path, help="read a saved feed instead of fetching")
    ap.add_argument("--save-feed", type=Path, help="keep the fetched feed here")
    ap.add_argument("--question", help="run only the question with this id")
    ap.add_argument("--ask", help="ask an ad-hoc question instead of the config's")
    ap.add_argument("--cue", action="append", help="phrase the host uses (repeatable)")
    ap.add_argument("--stop-cue", action="append", help="phrase that ends an answer (repeatable)")
    ap.add_argument("--answer-kind", choices=["open", "people"])
    ap.add_argument("--limit", type=int, help="only the N most recent episodes")
    ap.add_argument("--sources", help="comma-separated transcript sources, overriding the config")
    ap.add_argument("--shard", help="K/N: fetch transcripts for every Nth episode only, then stop")
    ap.add_argument("--offline", action="store_true", help="use cached transcripts only")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    ap.add_argument("--out-dir", type=Path, help="default: qa/<podcast>-<question>")
    ap.add_argument("--report", action="store_true")
    args = ap.parse_args(argv)

    podcast, questions = load_config(args.config, args)
    if args.limit is not None:
        podcast["episodes"]["limit"] = args.limit
    if args.sources:
        podcast["transcripts"]["sources"] = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.no_llm:
        podcast["llm"]["mode"] = "off"
    hosts = {h.lower() for h in podcast["hosts"]}

    episodes = load_episodes(podcast, args.feed_file, args.save_feed)
    store = TranscriptStore(podcast, args.cache_dir, offline=args.offline)

    if args.shard:
        # Transcript prefetch only, for splitting speech-to-text across CI
        # jobs. The graph is built afterwards from the merged cache.
        k, n = (int(x) for x in args.shard.split("/"))
        mine = [ep for i, ep in enumerate(episodes) if i % n == k]
        got = 0
        for i, ep in enumerate(mine, 1):
            got += store.get(ep) is not None
            print(f"  shard {k}/{n}: {i}/{len(mine)} ({got} transcripts)", file=sys.stderr)
        print(f"shard {k}/{n}: {got}/{len(mine)} transcripts cached", file=sys.stderr)
        return 0

    llm = make_llm(podcast["llm"])
    print(f"LLM pass: {'on (' + podcast['llm']['model'] + ')' if llm else 'off'}", file=sys.stderr)
    guest_graph = load_guest_graph(podcast["guests"]["graph"])

    for q in questions:
        rows = run_question(podcast, q, episodes, store, llm, guest_graph, hosts)
        graph = build_graph(podcast, q, rows, hosts)
        out_dir = args.out_dir if (args.out_dir and len(questions) == 1) else \
            (args.out_dir or QA_DIR) / f"{podcast['id']}-{q['id']}"
        write_outputs(out_dir, graph, rows)
        print(f"wrote {out_dir}/graph.json", file=sys.stderr)
        if args.report:
            report(graph, rows)
    if not args.out_dir or args.out_dir.parent == QA_DIR or args.out_dir == QA_DIR:
        write_index(QA_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
