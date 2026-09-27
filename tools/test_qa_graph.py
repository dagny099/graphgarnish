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
    sib = [c for other in cc.values() if other is not cc[qid] for c in other["cues"]]
    loc = qa.locate(stacked, cc[qid], set()) or qa.locate_block(stacked, cc[qid])
    text = qa.answer_span(stacked, loc, cc[qid], set(), sib)[0] if loc else ""
    check(f"stacked: the {qid} answer is its own part of the reply",
          want in text and not_want not in text and "Substack" not in text, repr(text))
bare = qa.deep_merge(cc["invite-next"], {"answer_cues": ["zzz never said"]})
loc = qa.locate(stacked, bare, set()) or qa.locate_block(stacked, bare)
check("a question speech-to-text mangled ('Who's doing right next?') is still located",
      loc and stacked[loc.sentence].text == "Who's doing right next?")
others = cc["advice"]["cues"] + cc["resources"]["cues"]
check("stacked, with answer cues set and none said: unanswered, not given its neighbour's answer",
      qa.answer_span(stacked, loc, bare, set(), others)[0] == "")
block_only = qa.sentences([{"text": t, "speaker": ""} for t in [
    "Three final questions.", "What's your advice?", "Who's on mumble mumble?", "What resources do you follow?",
    "My advice is ship small.", "And I would invite Ada Park.", "Resources, I read Locally Optimistic."]])
loc = qa.locate(block_only, cc["invite-next"], set()) or qa.locate_block(block_only, cc["invite-next"])
check("with the question unheard, its sibling questions mark the block", loc and loc.method == "block")
check("...and the answer cue finds this answer inside it",
      qa.answer_span(block_only, loc, cc["invite-next"], set(), others)[0] == "And I would invite Ada Park.")

print("real speech-to-text excerpts from the sample run")
REAL = {
    # Stacked, the guest answers only the first, the host re-asks alone.
    "perry": ["So back to you.", "Two questions.", "What's your advice?", "We went through a lot.",
              "But what's your final advice about data, about life, whatever, and second?",
              "Who should invite next?", "Yeah, I guess this is relevant to data and life.",
              "But change always takes much longer than you think.", "Love that.", "Another great quote.",
              "So, who should we invite next?", "So, I don't have anyone specific, but more practitioners.",
              "I'm Juan at data.world, very simple."],
    # Asked alone, answered without any answer cue.
    "bailis": ["Peter thank you so much but one last question to you who should we invite next to be part of cataloging cocktails.",
               "So, I'll be honest you have a pretty amazing lineup of people.",
               "One person I don't think is in the lineup yet is a fellow academic."],
    # Stacked; the guest repeats the invite question, mangled.
    "tabb": ["Quickly.", "What's your advice about data, about life?", "Second.", "Who should invite next?", "Third.",
             "What are the resources you buy?", "So, my advice.", "Don't do a job you don't like.",
             "Who's on right next?", "I'd have to have my data value Wingman.", "Matt Hounsley.",
             "And finally, what resources do you follow?", "People."],
    # Stacked in one sentence; the guest repeats the question and answers.
    "erik": ["What's your advice and who should we invite next?", "Wow.",
             "What's my advice is keep a curious mind.", "That's great advice.",
             "Who do you invite next?", "So I was going to say Sarah Catanzaro.", "Thank you so much."],
}
want = {("perry", "advice"): "change always takes", ("perry", "invite-next"): "more practitioners",
        ("bailis", "invite-next"): "One person", ("tabb", "advice"): "Don't do a job",
        ("tabb", "invite-next"): "Matt Hounsley", ("tabb", "resources"): "People.",
        ("erik", "advice"): "curious mind", ("erik", "invite-next"): "Sarah Catanzaro"}
for (ep, qid), expect in want.items():
    sents = qa.sentences([{"text": t, "speaker": ""} for t in REAL[ep]])
    sib = [c for other in cc.values() if other is not cc[qid] for c in other["cues"]]
    loc = qa.locate(sents, cc[qid], set()) or qa.locate_block(sents, cc[qid])
    got = qa.answer_span(sents, loc, cc[qid], set(), sib)[0] if loc else ""
    others = [v for (e, q2), v in want.items() if e == ep and q2 != qid]
    check(f"{ep}: the {qid} answer", expect in got and not any(o in got for o in others), repr(got))

hints = qa.near_misses(qa.sentences([{"text": t, "speaker": ""} for t in [
    "Great chat.", "So who do you reckon we get on the show next?", "Thanks everyone."]]), cc["invite-next"])
check("an unanswered episode records the sentences closest to the question",
      hints and hints[0]["text"].startswith("So who do you reckon"))
check("answer cues count only near the start of a sentence",
      qa.opens_with("As far as people, invite Ada.", ["as far as people"])
      and not qa.opens_with("I think it matters a great deal, you should have a plan.", ["you should have"]))

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

print("companions and names")
rows = [
    {"title": "Takeaways with Steve Perry", "date": "2022-04-21", "guests": ["Steve Perry"], "found": True, "reason": ""},
    {"title": "Is anyone a data expert? w/ Steve Perry", "date": "2022-04-20", "guests": ["Steve Perry"], "found": False, "reason": "question not found"},
    {"title": "Steve Perry returns", "date": "2024-01-10", "guests": ["Steve Perry"], "found": True, "reason": ""},
]
qa.merge_companions(rows)
check("an episode and its companion clip are one appearance; the clip's answer is kept",
      rows[0]["found"] and rows[1]["merged_into"] == rows[0]["title"])
check("the same guest two years later is a separate appearance", rows[2]["found"])
both = [{"title": "TAKEAWAYS - X with Ann Lee", "date": "2025-01-02", "guests": ["Ann Lee"], "found": True, "reason": ""},
        {"title": "X with Ann Lee", "date": "2025-01-01", "guests": ["Ann Lee"], "found": True, "reason": ""}]
qa.merge_companions(both)
check("when both answer, the full episode wins", both[1]["found"] and not both[0]["found"])
known = {qa.norm("Sarah Catanzaro"): "Sarah Catanzaro"}
check("a near-miss transcription snaps to a known guest", qa.snap("Sarah Catanzero", known) == "Sarah Catanzaro")
check("an unrelated name does not snap", qa.snap("Sarah Connor", known) == "")
people, things = qa.mentions("I'd have to say Sarah Catanzero's work. Take a look at dbt and Snowflake, the Irish team at LinkedIn.",
                             set(), known, "people")
check("possessives are stripped and names snapped", people == ["Sarah Catanzaro"], str(people))
check("lone capitalised words are dropped unless distinctive", "LinkedIn" in things and "Irish" not in things
      and "Take" not in things and "Snowflake" not in things, str(things))

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
