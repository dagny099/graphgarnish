# Podcast configs for `tools/qa_graph.py`

One JSON file per podcast. It says where the episodes and transcripts come
from, and lists the recurring questions to look for. Each question becomes
its own graph in `qa/<podcast-id>-<question-id>/`.

To try a new podcast, copy `catalog-and-cocktails.json`, change the feed and
hosts, write the questions, then check the config on a handful of episodes
spread across the catalogue before running it on all of them:

```bash
python3 tools/qa_graph.py podcasts/my-show.json --sample 8 --report
```

Or skip the config and ask one question of any feed:

```bash
python3 tools/qa_graph.py --feed https://example.com/feed.xml \
    --ask "What are you reading?" --cue "what are you reading" --report
```

## `podcast`

| key | meaning |
| --- | --- |
| `id`, `name` | `id` names the output folders and the transcript cache |
| `feed` | the RSS feed; every page of a paginated feed is followed |
| `hosts` | full names. Hosts are never counted as guests or as people named in an answer, and a long host turn ends a guest's answer |
| `episodes.skip_title_regex` | titles to skip, such as companion clips |
| `episodes.skip_types` | `itunes:episodeType` values to skip (default `trailer`, `bonus`) |
| `episodes.since`, `episodes.limit` | only episodes on or after a date; only the N most recent |
| `transcripts.sources` | tried in order until one works: `feed`, `dir`, `url_template`, `whisper` |
| `transcripts.dir` | a folder of transcripts named by episode GUID or by the slugged title (`.vtt`, `.srt`, `.json`, `.html`, `.txt`) |
| `transcripts.url_template` | a page per episode holding its transcript, e.g. `https://show.com/episodes/{title_slug}/`. Also `{guid}`, `{link}`, `{link_slug}`. `html_start` / `html_end` are regexes that cut the transcript out of the page |
| `transcripts.whisper` | local speech-to-text with faster-whisper (`pip install faster-whisper`). `from_end_minutes` transcribes only the end of each episode, which is where most shows ask their closing questions, and is about 3x cheaper than the whole episode |
| `guests.from` | where to find each episode's guest, in order: `speakers` (transcript labels), `graph` (a GraphGarnish network's `GUEST_ON` links), `title`, `description` |
| `guests.graph` | path to that network, e.g. `../sample_network.json` |
| `llm.mode` | `auto` runs a Claude pass when `ANTHROPIC_API_KEY` is set; `off` never does |
| `llm.model`, `llm.effort` | default `claude-opus-5`, effort `low` |

Transcripts from the feed (`<podcast:transcript>`, Podcasting 2.0) are used
whenever a show publishes them. They are the best source because they usually
carry speaker labels, which the answer cutter relies on.

## `questions[]`

| key | meaning |
| --- | --- |
| `id`, `question` | the question as you would write it |
| `cues` | phrases the host says when asking. Matched after lowercasing and dropping punctuation. A reworded question sharing enough of its words is also found (`fuzzy_threshold`, default 0.75), but only on episodes with a known guest: on the full Catalog & Cocktails run, matches below 0.75 or on guestless episodes were mostly the hosts talking among themselves |
| `answer_cues` | phrases the guest says when starting *this* answer, counted only in a sentence's first six words ("As far as people, ..."). Use them when the host asks several questions in one breath and the guest answers in order. When set and none is heard, the episode is reported unanswered rather than given the neighbouring answer |
| `stop_cues` | phrases that end the answer: the next answer's opening, the next question, the sign-off |
| `answer_kind` | `people` when the answer names people ("who should we invite next?"). Names become person nodes with `RECOMMENDS` links from the guest; a named person who was also a guest is one node with both roles |
| `occurrence` | `last` (default) or `first`, when the cue appears more than once |
| `max_answer_words`, `host_break_words` | limits on the cut answer |
| `graph` | `concepts_per_answer`, `concept_min_answers`, `concept_max_share`, `similar_top_k`, `similar_threshold` |

## Tuning a config

`answers.json` has one row per episode with a `reason`, found or not:

| reason | what to change |
| --- | --- |
| `question not found` | read the row's `hints`: the three transcript sentences closest to the question, with times. Add the host's actual wording to `cues`. Speech-to-text mishears: Whisper wrote "Who's doing right next?" for "Who should we invite next?", so list the neighbouring questions too |
| `answer cue not found after the question` | add the guest's wording to `answer_cues` |
| `stop cue` / `host took over` / `word limit` | the answer was cut here; check that it ended in the right place |
| `no transcript` | no source produced one |

Every answer keeps its start time and audio URL, so any row can be checked by
listening to it.
