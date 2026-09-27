#!/usr/bin/env python3
"""Checks for tools/qa_graph.py.

Run: python3 tools/test_qa_graph.py
No network: a small feed and one transcript per supported format live in
tools/testdata/qa. Each transcript ends with the same two questions, asked
the way real hosts ask them (stacked in one turn, reworded, interrupted).
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import qa_graph as qa  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "tools" / "testdata" / "qa"

failures = []


def check(label, condition, detail=""):
    if condition:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label} {detail}")
        failures.append(label)


def run_fixture():
    tmp = Path(tempfile.mkdtemp())
    qa.main([str(DATA / "config.json"), "--feed-file", str(DATA / "feed.xml"),
             "--cache-dir", str(tmp / "cache"), "--out-dir", str(tmp / "out")])
    out = {}
    for q in ("invite-next", "advice"):
        d = tmp / "out" / f"test-show-{q}"
        out[q] = (json.loads((d / "graph.json").read_text()),
                  {r["guests"][0]: r for r in json.loads((d / "answers.json").read_text())})
    return tmp, out


print("transcript parsing")
vtt = qa.parse_transcript((DATA / "transcripts" / "g-ada.vtt").read_text(), "text/vtt")
check("WebVTT voice tags become speakers", vtt[0].speaker == "Juan Sequeda" and vtt[1].speaker == "Ada Park")
check("WebVTT cue times become seconds", vtt[0].start == 2462.0)
check("consecutive cues from one speaker merge into one turn",
      "Designing Data-Intensive Applications" in vtt[1].text)
srt = qa.parse_transcript((DATA / "transcripts" / "g-ben.srt").read_text(), "")
check("SubRip is detected without a mime type, 'Name:' prefixes are speakers",
      srt[1].speaker == "Ben Ortiz" and srt[1].start == 2286.0)
js = qa.parse_transcript((DATA / "transcripts" / "g-cleo.json").read_text(), "application/json")
check("Podcasting 2.0 JSON keeps speakers and startTime", js[1].speaker == "Cleo Diaz" and js[0].start == 2400.5)
txt = qa.parse_transcript((DATA / "transcripts" / "g-dev.txt").read_text(), "text/plain")
check("'Name (hh:mm:ss):' on its own line labels the text below it",
      txt[1].speaker == "Dev Rao" and txt[1].start == 2654.0 and "Dev Rao" not in txt[1].text)
html_segs = qa.parse_transcript(
    "<html><body><nav>menu</nav><div id='t'><p><cite>Ann Lee:</cite> Hello there.</p>"
    "<p><cite>Bo Kim:</cite> Hi.</p></div><footer>x</footer></body></html>", "text/html",
    html_start=r"<div id='t'>")
check("HTML transcripts: <cite> speakers, page chrome dropped",
      [s.speaker for s in html_segs] == ["Ann Lee", "Bo Kim"] and "menu" not in html_segs[0].text)
rolling = qa.parse_transcript(
    "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nwho should we\n\n"
    "00:00:02.000 --> 00:00:03.000\nwho should we invite next\n", "text/vtt")
check("rolling auto-captions do not repeat words", qa.norm(" ".join(s.text for s in rolling))
      == "who should we invite next")
check("a URL is not mistaken for a speaker label",
      qa.parse_plain("https://example.com: see notes")[0].speaker == "")

print("locating the question")
sents = qa.sentences([{"text": "Who should we invite next? Tell us.", "speaker": "Guest X"},
                      {"text": "What's your advice? And who should we invite next?", "speaker": "Tim Gasper"},
                      {"text": "Invite Ada Park.", "speaker": "Guest X"}])
q = qa.deep_merge(qa.DEFAULT_QUESTION, {"question": "Who should we invite next?",
                                         "cues": ["who should we invite next"]})
loc = qa.locate(sents, q, {"tim gasper"})
check("the host's asking beats a guest repeating the question",
      loc and sents[loc.sentence].speaker == "Tim Gasper")
fuzzy = qa.locate(qa.sentences([{"text": "So, who do you think we should invite on next?", "speaker": ""}]),
                  qa.deep_merge(q, {"cues": ["this cue never matches"]}), set())
check("a reworded question is found by word overlap", fuzzy and fuzzy.method == "fuzzy")
none = qa.locate(qa.sentences([{"text": "We should invite more listeners to rate us.", "speaker": ""}]),
                 qa.deep_merge(q, {"cues": ["zzz"]}), set())
check("a statement sharing the words is not a question", none is None)

print("stacked questions, answered in order (speech-to-text, no speakers)")
# Real Whisper output from a Catalog & Cocktails episode: three questions in
# one breath, "who should we invite next" misheard, the guest answering each
# in turn and repeating the third question.
stacked = qa.sentences([{"text": t, "speaker": "", "start": float(i)} for i, t in enumerate([
    "David, to wrap up three questions.",
    "What's your advice about data, AI, life, whatever you want?",
    "Who's doing right next?",
    "And what resources do you follow?",
    "OK, well, so advice, I would say, is get close to your business sponsors, right?",
    "So understand what's moving the needle.",
    "As far as people, who haven't you guys had on this podcast yet?",
    "I'm really passionate about this output trap.",
    "And what was the third question?",
    "What resources do you follow?",
    "I seek out folks like you guys, Substack, LinkedIn.",
    "Thank you so much.",
])])
cc = json.loads((ROOT / "podcasts" / "catalog-and-cocktails.json").read_text())["questions"]
cc = {q["id"]: qa.deep_merge(qa.DEFAULT_QUESTION, q) for q in cc}
for qid, want, not_want in (("advice", "get close to your business sponsors", "output trap"),
                            ("invite-next", "output trap", "business sponsors")):
    loc = qa.locate(stacked, cc[qid], {"juan sequeda", "tim gasper"})
    text = qa.answer_span(stacked, loc, cc[qid], {"juan sequeda", "tim gasper"})[0] if loc else ""
    check(f"stacked: the {qid} answer is its own part of the reply",
          want in text and not_want not in text and "Substack" not in text, repr(text))
bare = qa.deep_merge(cc["invite-next"], {"answer_cues": ["zzz never said"]})
loc = qa.locate(stacked, bare, set())
check("with answer cues set and none said, the episode is unanswered, not given its neighbour's answer",
      qa.answer_span(stacked, loc, bare, set())[0] == "")

print("the full pipeline on the fixture feed")
tmp, out = run_fixture()
invite, invite_rows = out["invite-next"]
advice, advice_rows = out["advice"]
check("companion clips and trailers are skipped", invite["meta"]["episodes"] == 6)
check("an episode with no transcript is kept and says why",
      invite_rows["Eve Moss"]["reason"] == "no transcript" and not invite_rows["Eve Moss"]["found"])
check("every episode with a transcript has an answer", invite["meta"]["answered"] == 5
      and advice["meta"]["answered"] == 5)
ada = invite_rows["Ada Park"]
check("a short host interjection does not end the answer", "Hana Sato" in ada["answer"])
check("the answer stops at the next recurring question", "resources" not in ada["answer"])
check("stacked questions: the advice answer stops before 'who should we invite'",
      "Ben Ortiz" not in advice_rows["Ada Park"]["answer"]
      and "Designing Data-Intensive" in advice_rows["Ada Park"]["answer"])
check("answers carry the time the guest starts answering", ada["start"] == 2485.0)
check("unlabelled (speech-to-text) transcripts still yield the answer",
      invite_rows["Fay Lin"]["answer"] == "Invite Ada Park. And Hana Sato.")
check("people named in an answer are people", set(ada["people"]) == {"Ben Ortiz", "Grace Hopper Lee", "Hana Sato"})
check("'from Acme Graph' is an organisation, not a person", ada["entities"] == ["Acme Graph"])
check("a leading verb is not part of a name", invite_rows["Fay Lin"]["people"] == ["Ada Park", "Hana Sato"])
check("a book title in an open answer is an entity, not a person",
      advice_rows["Ada Park"]["people"] == []
      and advice_rows["Ada Park"]["entities"] == ["Designing Data-Intensive Applications"])
check("speaker labels identify the guest", ada["guest_source"] == "speakers")
check("the title identifies the guest when the transcript cannot", invite_rows["Fay Lin"]["guest_source"] == "title")

ids = {n["id"]: n for n in invite["nodes"]}
by_name = {n["name"]: n for n in invite["nodes"]}
check("every link points at a node", all(l["source"] in ids and l["target"] in ids for l in invite["links"]))
check("a person both named and a guest is one node with both roles",
      set(by_name["Ben Ortiz"]["roles"]) == {"guest", "mentioned"})
recs = {(ids[l["source"]]["name"], ids[l["target"]]["name"]) for l in invite["links"] if l["type"] == "RECOMMENDS"}
check("a people question links guest to recommended person", ("Ada Park", "Ben Ortiz") in recs
      and ("Dev Rao", "Ben Ortiz") in recs)
check("the report names people recommended who were also guests",
      invite["meta"]["people_named_and_later_guests"] == ["Ada Park", "Ben Ortiz", "Cleo Diaz"])
check("an open question makes no RECOMMENDS links",
      not any(l["type"] == "RECOMMENDS" for l in advice["links"]))
concepts = {n["name"] for n in advice["nodes"] if n["type"] == "concept"}
check("shared ideas become concept nodes", {"knowledge graphs", "governance"} <= concepts, str(concepts))
check("every concept connects at least two answers", all(
    sum(1 for l in advice["links"] if l["target"] == n["id"]) >= 2
    for n in advice["nodes"] if n["type"] == "concept"))
check("similar answers are linked", any(l["type"] == "SIMILAR_TO" for l in advice["links"]))

print("LLM pass (mocked transport; skipped without the anthropic SDK)")
try:
    import anthropic
    import httpx2 as httpx  # anthropic 1.x speaks httpx2, the maintained httpx fork
except ImportError:
    anthropic = None
    print("  skip  anthropic SDK not installed")
if anthropic:
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        body = {"answer_summary": "Invite Ben Ortiz.", "found": True, "quote": "You should invite Ben Ortiz.",
                "people": ["Ben Ortiz", "Juan Sequeda"], "entities": [{"name": "Acme Graph", "kind": "organization"}],
                "themes": ["Semantics"]}
        return httpx.Response(200, json={
            "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5",
            "content": [{"type": "text", "text": json.dumps(body)}], "stop_reason": "end_turn",
            "stop_sequence": None, "usage": {"input_tokens": 10, "output_tokens": 10}})

    llm = qa.LLM.__new__(qa.LLM)
    llm.anthropic, llm.model, llm.effort, llm.use_fallbacks = anthropic, "claude-opus-5", "low", True
    llm.client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    got = llm.extract(q, qa.Episode(key="k", title="T", date="2026-01-01"), ["Ada Park"], "excerpt", ["semantics"])
    check("the LLM reply is parsed", got and got["people"][0] == "Ben Ortiz")
    req = sent[0] if sent else {}
    check("the request asks for schema-constrained JSON", req.get("output_config", {}).get("format", {}).get("type") == "json_schema")
    check("the request opts into default refusal fallbacks", req.get("fallbacks") == "default")
    check("themes already used are offered back to the model", "semantics" in req["messages"][0]["content"])

print("cache")
cached = list((tmp / "cache" / "test-show" / "transcripts").glob("*.json"))
check("local transcript files are read fresh, not cached", cached == [])
store = qa.TranscriptStore({"id": "t", "transcripts": qa.DEFAULT_PODCAST["transcripts"]}, tmp / "c2")
ep = qa.Episode(key="k", title="T", date="2026-01-01")
(store.dir / "k.json").write_text(json.dumps({"source": "feed", "url": "u", "mime": "text/plain",
                                              "raw": "Ann Lee (00:00:01): Hello."}))
check("a cached fetch is re-parsed on read, so parser fixes apply to it",
      store.get(ep)["segments"][0]["speaker"] == "Ann Lee")

print("config")
podcast, questions = qa.load_config(DATA / "config.json", qa.argparse.Namespace(
    feed=None, ask=None, question="advice", cue=None, stop_cue=None, answer_kind=None))
check("--question picks one question", [q["id"] for q in questions] == ["advice"])
check("relative paths resolve against the config file", podcast["transcripts"]["dir"] == str(DATA / "transcripts"))
podcast, questions = qa.load_config(None, qa.argparse.Namespace(
    feed="https://x/feed", ask="What are you reading?", question=None, cue=None, stop_cue=None, answer_kind=None))
check("--ask works with no config at all", questions[0]["cues"] == ["What are you reading?"]
      and podcast["feed"] == "https://x/feed")
check("every committed podcast config loads", all(
    qa.load_config(p, qa.argparse.Namespace(feed=None, ask=None, question=None, cue=None,
                                             stop_cue=None, answer_kind=None))[1]
    for p in (ROOT / "podcasts").glob("*.json")))

print()
if failures:
    print(f"{len(failures)} check(s) failed")
    raise SystemExit(1)
print("all checks passed")
