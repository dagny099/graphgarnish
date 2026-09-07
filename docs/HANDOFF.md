# GraphGarnish — session handoff

Written 2026-09-06, updated 2026-09-07. `main` at `6b5b52f`. **Delete this file before making the repo public.**

Read this, then read `tools/build_network.py`'s module docstring and `data/verified.json`'s
`_README`. Between the three you have the whole design. Everything below is what those
files *don't* say.

---

## 1. Where things stand

PR #4 and #5 merged. Counts after the 2026-09-07 dedupe fix (§3.1); the previous
numbers are in brackets where they changed, and every drop is a duplicate collapsing:

```
episode          373   curated=202  feed=171     [388, feed=186]
organization     103   curated=60   inferred=42  verified=1
person           184   curated=141  inferred=40  verified=3
topic             22   curated=22
AFFILIATED_WITH  108   curated=61   inferred=46  verified=1
COVERS           879   inferred=879              [885]
GUEST_ON         248   curated=146  inferred=99  verified=3   [261, inferred=112]
TAKEAWAY_OF       62   inferred=62
58 episodes need a guest checked                 [unchanged]
1 episode has no URL                             [16]
```

The GUEST_ON drop is entirely `inferred` (112 -> 99): a duplicate pair had the guest
curated on the seed copy and re-guessed on the feed copy, so merging them retires the
guess and keeps the curated edge. No curated or verified edge moved.

Site is live at https://graph-gist.com/explore.html (GitHub Pages off `main`).

**What changed this session.** The builder used to read `catalog_cocktails.json` as its
curated seed *and* write it back as an output. Guest extraction only runs when an episode
has no guest yet, so a name a regex guessed on Monday was read back as fact on Tuesday and
could never be re-derived. 24 junk nodes were frozen that way. Now inputs are read-only and
outputs are fully derived — delete both output files, rebuild, and they come back byte for
byte.

---

## 2. Load-bearing decisions (do not undo these by accident)

**Inputs are read-only; outputs are disposable.** `data/seed_curated.json`,
`data/verified.json`, `data/topics.json`, and the RSS feed go in. `catalog_cocktails.json`
and `sample_network.json` come out and are never read back. This is the whole point of the
refactor. Any change that makes the builder read its own output reintroduces the original
bug.

Safe because **the feed carries the whole back catalogue** (373 items covering 2020–2026).
Nothing needs the old accumulator.

**The drop guard compares against the last published graph, not the seed.**
`previous_episode_count(OUTPUTS[0])`. The seed is 203 episodes and the graph is 388; checking
against the seed would let a truncated feed silently drop 185 episodes and still pass.

**On a link, the provenance key is `provenance`, not `source`.** A link's `source` is its
origin node id, and `explore.html` reads it that way in ~19 places. Naming the provenance
field `source` on links would silently break the graph. There is a test asserting every
link's `source` resolves to a real node id — keep it.

**A node inherits the strongest provenance of its edges** (`verified` > `curated`/`feed` >
`inferred`, see `SOURCE_RANK`). A guest curated on one episode and guessed on another is a
curated person with one guessed edge. Flattening to the weakest would understate what's known.

**`hosts_only` only applies where extraction found nobody**, so a pattern can never displace
a real guest. Verified against "Juan & Tim rant w/ Jesus Barrasa about Ontologies…", which
matches a host-only pattern and still keeps Jesus Barrasa.

---

## 3. Open threads

### 3.1 DONE (2026-09-07) — duplicate episodes in the live graph

Fixed: `MIN_EXACT_DATE_PREFIX = 12` applies when the publication dates are *exactly*
equal and one normalized title is a complete prefix of the other. The +/- 1 day window
still needs the full `MIN_PREFIX`. Tests cover all ten pairs, plus three negative
controls (neighbouring day, diverging prefix, companion clip).

**The list of 10 below was incomplete — there were 15.** Four more sit at 17-19
characters (`Data Models are Divas`, `Long live the monolith?`, `The Data Mesh Debate`,
`The Era of Data Usage`), and one runs the other way, where the *feed* title is the
shorter one (`The Future of BI is AI` vs the seed's `... – hosts Tim Gasper & Juan
Sequeda`). All fifteen verified by hand against the feed: same date, exact prefix,
guest suffix. Every one kept its curated guest and gained a URL.

So the predicted 388 -> 378 and 16 -> 6 were both wrong. Actual: **388 -> 373
episodes, 16 -> 1 URL-less.** Every seed episode now matches a distinct feed item
(202 curated + 171 feed-only = 373 = the feed's own item count).

The drop of 15 exceeds the drop guard's tolerance of 7, correctly — the guard cannot
tell a dedupe fix from a truncated feed. Landed with the new
`--accept-episode-drop 373`, which names the expected count rather than blanket-forcing:
any other count still fails. Once this is committed the baseline is 373 and the
scheduled workflow passes normally again. **If the outputs are not committed, the next
scheduled run will refuse to commit** — it would see 388 in the published graph.

The one remaining URL-less episode is the hand-decision below, and the feed settles it:

> **`Data Storytelling with Kat Greenbrook (Episode 2)`** — a genuine two-parter. The
> feed carries *two separate items* under the identical title on 2023-11-02. Because
> `norm_key` collides them, the second feed item merges into the first record and
> Episode 2 keeps no URL. Pre-existing, not caused by the prefix fix. Fixing it means
> falling back to the guid when two feed items normalize the same — left undone.

<details>
<summary>Original diagnosis, kept for the record</summary>

#### 10 duplicate episodes in the live graph

The same episode appears twice: once from the spreadsheet (short title, no URL, no summary),
once from the feed (full title with guest).

**Cause, confirmed:** `MIN_PREFIX = 25` in `tools/build_network.py`. Both `prefix_match` and
`same_date_match` require 25 characters of shared normalized prefix. These titles are 21–24
characters — three of them miss by a single character.

```
len=24  prefix_ok=True  'How to think about data value'
len=21  prefix_ok=True  'Fashion Week...but for data'
len=22  prefix_ok=True  'Modern Data Work at Drizly'
len=23  prefix_ok=True  'Your privacy is my currency'
```

**Proposed fix:** when two episodes share an *exact* publication date and one normalized
title is a full prefix of the other, that is conclusive regardless of length. Add a lower
threshold (~12) for the exact-date case in `same_date_match`; leave `prefix_match` at 25,
since without a date match a short prefix really can collide.

**Test set — all 10 pairs, dates identical in every case:**

| date | curated title | feed title |
|---|---|---|
| 2021-09-16 | How to think about data value | … w/ Lars Albertsson |
| 2021-09-23 | Fashion Week...but for data | … w/ Jans Aasman |
| 2021-11-11 | Is self‑service BI the answer? | Is self-service BI the answer? w/ Cindi Howson |
| 2022-01-13 | Modern Data Work at Drizly | … w/ Emily Hawkins |
| 2022-01-20 | What good is a Metrics Layer? | … w/ Benn Stancil from Mode |
| 2022-01-27 | Can the Data Mesh be Governed? | … w/ Dora Boussias |
| 2022-03-10 | Your privacy is my currency | … with Patricia Thaine from Private AI |
| 2022-03-17 | Agile like a fox, but for data | … W/ Shane Gibson from AgileData.io |
| 2022-06-09 | Getting all meta about data | … w/ Sanjeev Mohan |
| 2022-08-25 | Build bridges. Don't Burn them. | … W/ Vip Parmar, WPP |

Note the 2021-11-11 pair differs only by a non-breaking hyphen (U+2011) in the curated title.
`norm_key` already strips it, so it isn't the cause — length is.

After fixing: episode count should drop 388 → 378, and the 16 episodes with no URL should
drop to 6. Verify both.

**Not in that list, decide by hand:** `Data Storytelling with Kat Greenbrook` vs
`… (Episode 2)`. Both curated. Genuine two-parter or a spreadsheet duplicate — only Barbara
knows.

</details>

### 3.2 DONE (2026-09-07) — no CI on pull requests

`.github/workflows/tests.yml` runs on `pull_request` and on pushes to `main`: the
invariant suite, plus a determinism check that builds twice from the checked-in RSS
fixture and diffs the bytes. No network, and it writes to a scratch path so it never
touches the published outputs.

<details>
<summary>Original diagnosis</summary>

#### no CI on pull requests

`.github/workflows/refresh-network.yml` triggers on `schedule` and `workflow_dispatch` only.
Nothing runs `tools/test_build_network.py` on a PR. That is exactly how the suite sat broken
across two runs. Add a small workflow with a `pull_request` trigger running the test suite.
Highest credibility per line of effort on the whole list.

</details>

### 3.3 Data quality, non-blocking

- **6 review entries have a recoverable guest.** `Takeways with Roel and Valentijn` (typo,
  two first names), `Your thoughts become action; going from Thought Leadership to Practice`,
  `Knowledge is Power: Knowledge Management meets Data Management`,
  `Catalog & Cocktails: Throwback Elixir`, `Data Operations vs. Data Analytics`,
  `Does our understanding of data bias our analytics outcomes?`. Fix via `person_org` in
  `data/verified.json`.
- **~8 review entries are host-only but the patterns miss them:** `Season Two Finale!!!` and
  `SEASON ONE FINALE: Episode 50` (spelled out, `\bSeason \d+` doesn't match),
  `Takeaways from Gartner D&A with Juan and Tim`, `Bonus Episode: Juan's Trip Takeaways from
  MIT CDOIQ`, `Honest No-BS Data Podcast 7th Year and Season 12 Kick Off`,
  `BONUS EPISODE: Live from Big Data London`. Widen `hosts_only_patterns`.
- **The remaining ~44 are live/panel/conference episodes** with many participants or none.
  Recommendation: leave them. A panel doesn't fit one-guest-per-episode, and forcing it in
  distorts the graph more than omitting it.
- **3 `_unresolved` items in `data/verified.json`** need Barbara's judgement: Valentijn
  (Vopak) — dropped because a single first name can't be told from a job title; BAML /
  Elemental / Private AI — kept as orgs, unconfirmed; the Wharton School — kept, long name.

### 3.4 Repo hygiene, non-blocking

- **`catalog_cocktails.json` and `sample_network.json` are byte-identical**, 500KB each, and
  only `sample_network.json` is fetched by `explore.html`. Delete one or document why both
  exist. A reviewer will ask.
- **`MAX_FEED_PAGES = 40` fails silently.** `follow_pages` stops at the cap with no warning.
  Print one. Currently ~373 items so not hit, but it's a silent failure mode.
- **`meta.episodes_needing_review` conflates two states** — "we don't know" and "confirmed
  nobody". Splitting them would make the queue actionable instead of a graveyard.
- **Delete this file** before making the repo public.

### 3.5 Feature, unbuilt

**Topic-over-time.** Nothing in `explore.html` uses the date field. You have 388 episodes
across 2020–2026, 22 topics, dates on everything. A topic-over-time view would show the arc
from data catalogs → data mesh → context graphs and agents. Strongest visual for the blog
post, and the dataset already supports it.

---

## 4. Environment gotchas (these cost real time to rediscover)

- **The sandbox cannot reach the RSS feed or any CDN.** `graph-gist.com`, Azure blob storage
  (Actions artifacts), and `d3js.org` all return 403 through the egress proxy. Do not
  conclude the site or feed is broken.
- **To test the builder with real data:** download the `feed-raw` artifact from a successful
  workflow run (Actions → run → Artifacts, 14-day retention), then
  `python3 tools/build_network.py --feed-file feed-raw.xml --report`. Barbara has to do the
  download; the sandbox can't.
- **To test `explore.html` in a browser:** Chromium is at `/opt/pw-browsers/chromium`.
  `explore.html` loads d3 from `d3js.org`, which is blocked, so vendor it:
  `npm install d3@7` in the scratchpad, copy `dist/d3.min.js` into the repo as a temp file,
  `sed` the script src, serve with `python3 -m http.server`, drive with Playwright.
  **Delete the temp files afterwards.** Two real bugs were only caught this way.
- **Do not reconstruct a feed from the published graph to test with.** The output caps
  `summary` at 400 characters, so a synthetic feed truncates exactly where guests are often
  named. It systematically understates: COVERS came out 564 synthetic vs 885 real, and one
  real guest (Will Briggs) vanished. It's fine for checking *shapes*, never for counts.
- **Asking Barbara for the `--report` output is cheaper than any of this** and usually
  enough — it carries all the counts and provenance.

---

## 5. Things I got wrong this session

Recorded so they aren't repeated.

- **First recommendation was to point `DEFAULT_SEED` at `data/seed_curated.json` and drop
  `catalog_cocktails.json` from outputs — framed as fixing a mistake.** The accumulation was
  deliberate (docstring said "offline, in place"; the drop guard existed to protect it).
  Barbara pushed back and was right. The correct argument wasn't "this is a mistake," it was
  "the feed makes the accumulator unnecessary, and it costs correctness."
- **My own `split_people` fix silently dropped three real guests.** Rejecting
  `Aakriti Agrawal from American Express` as too long meant `strip_affiliation` never ran.
  Caught only by analysing the review queue afterwards, not by reading the diff.
- **The first version of that fix broke two tests** because `strip_affiliation` returns its
  input unchanged when there's no employer suffix, so testing its result alone let every
  topic phrase through. The guard has to check `AFFIL_SUFFIX.match()` explicitly.
- **The provenance filter marked zero edges** because the filter pipeline rebuilt each link
  as `{source, target, type}`, dropping the field. Invisible in the diff.
- **Both new sidebar blocks used white text** on a white control panel. Invisible until
  screenshotted.

Pattern: every one was caught by looking at output, not by reading code.

---

## 6. Blog post direction (Barbara is drafting separately)

**The podcast graph is not the story. The pipeline eating its own output is.**

A regex's guess on Monday became ground truth on Tuesday, and after one cycle nothing could
tell them apart. Same shape as auto-labeling loops, RAG systems indexing their own generated
summaries, any pipeline where output feeds the next input — "model collapse" at a scale you
can actually read. 900 lines of Python, fully inspectable, with a before and after.

Keep:
- The mechanism shown, not asserted — the four-line `if not rec["guests"]` block is the
  entire bug.
- The fix as a general pattern: inputs read-only, outputs disposable, a separate human
  decision layer, provenance on every fact.
- The honesty stance: 43% of guest edges are guesses and the UI dashes them. Most data
  projects present everything with equal confidence.
- One or two concrete examples. `CDO at McKinsey` extracted as a person is funny and
  instantly legible.
- Optionally: the assistant found the structural bug *and* broke something on the way, and
  what caught it was checking output rather than trusting the diff.

Cut: the extraction-regex minutiae. Nobody needs the affiliation-suffix splitter.

---

## 7. Suggested order for the next session

1. ~~Fix the duplicates (§3.1)~~ — done, 15 of them, 388 -> 373.
2. ~~Add PR CI (§3.2)~~ — done, `.github/workflows/tests.yml`.
3. ~~Confirm the counts~~ — done: 373 episodes, 1 URL-less. Predictions were off; see §3.1.
4. `verified.json` pass: 6 recoverable guests, ~8 host-only patterns, 3 `_unresolved` (§3.3).
5. Repo hygiene (§3.4), including deleting this file.
6. Topic-over-time (§3.5) if there's appetite — it's the blog post's best visual.

Steps 1–3 are what make the repo shareable. 4–6 are polish.
