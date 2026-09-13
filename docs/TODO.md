# TODO

Open work on the data, the builder, and the explorer. None of it blocks a
rebuild.

## Data quality

The review queue (`meta.episodes_needing_review` in `sample_network.json`) holds
58 full episodes as of the 2026-09-13 build.

- **Six queued episodes may have a recoverable guest.** An earlier pass read
  these as naming a guest the extractor missed; the guests have not been
  re-checked since. Record decisions under `person_org` in `data/verified.json`.
  - `Takeways with Roel and Valentijn` (typo in the title; first names only)
  - `Your thoughts become action; going from Thought Leadership to Practice w/ Roel and Valentijn of Vopak`
  - `Knowledge is Power: Knowledge Management meets Data Management`
  - `Catalog & Cocktails: Throwback Elixir`
  - `Data Operations vs. Data Analytics`
  - `Does our understanding of data bias our analytics outcomes?`
- **Six host-only episodes slip past `hosts_only_patterns`.** For example,
  `\bSeason \d+ (Premiere|Finale)\b` needs a numeral, so a spelled-out season
  falls through. Widen the patterns in `data/verified.json`, or list the titles
  under `hosts_only`.
  - `Season Two Finale!!!`
  - `SEASON ONE FINALE: Episode 50`
  - `Takeaways from Gartner D&A with Juan and Tim`
  - `Bonus Episode: Juan’s Trip Takeaways from MIT CDOIQ`
  - `Honest No-BS Data Podcast 7th Year and Season 12 Kick Off`
  - `BONUS EPISODE: Live from Big Data London`
- **The other 46 are mostly live, panel, and conference episodes** with many
  participants or none. Recommendation: leave them without a guest. A panel does
  not fit one guest per episode, and forcing one in distorts the graph more than
  omitting it.
- **Three `_unresolved` entries in `data/verified.json` need a decision:**
  Valentijn (Vopak), dropped because a lone first name cannot be told from a job
  title; BAML / Elemental / Private AI, kept as organizations but unconfirmed;
  the Wharton School of the University of Pennsylvania, kept under its long name.

## Topic match phrases

Topic edges are case-insensitive substring matches against each full
episode's title and description (`data/topics.json`). On 2026-09-13 every
match of the broadest phrases was read in context against the real feed. That
pass moved the host footer and the show's own name into `strip`, dropped
`risk`, narrowed `career` to `career path` / `careers`, and replaced bare
`analytics` with named kinds of analytics work. Topic edges went from 880 to
551, and each case is pinned by a check in `tools/test_build_network.py`.

Still open:

- **77 of 311 full episodes carry no topic.** Many were attached only through
  false matches before. Nobody has read what they are about: the taxonomy may
  have gaps, or they may be banter and bonus episodes. Worth one read-through
  before building topic over time.
- **`architect` matches guest job titles** ("director of enterprise
  architecture", "Snowflake architect") as well as episodes about
  architecture. It is the only reason for 19 of its 29 matches, and a title in
  a guest's bio is weak evidence of what an episode covers.
- **`bias` catches the verb.** "Cultures that bias for good" is one of its 5
  matches.
- **`catalog` still matches "the catalog framework"** in *Intelligence Without
  Action Is Just Overhead*, a framework's name rather than a data catalog.
- **Narrowing `analytics` cost one genuine match.** *Does our understanding of
  data bias our analytics outcomes?* is no longer under Analytics & BI. Accepted
  rather than adding a phrase for a single title.
- **Descriptions reach the matcher with raw HTML entities** (`&rdquo;`,
  `&mdash;`). No phrase is affected today, but a phrase containing `&` or a
  quote mark could silently fail to match.
- **The saturation guard only catches the extreme.** A topic inflated by a
  generic phrase to 20% passes it: Analytics & BI sat at 66 episodes, about
  half of them false. Only reading matches in context finds that. To make the
  audit repeatable, add a script that prints each phrase's match count, the
  number of episodes where it is the only match, and the surrounding text.

## Repo hygiene

- **`catalog_cocktails.json` and `sample_network.json` are byte-identical**
  (501 KB each). The builder writes both (`OUTPUTS` in `tools/build_network.py`)
  and `explore.html` fetches only `sample_network.json`. Drop one, or document
  why both exist.
- **`MAX_FEED_PAGES` fails silently.** `follow_pages` stops after 40 pages
  without a warning. The feed currently spans 4 pages, so the cap is not hit,
  but a catalogue cut off there would look like a clean run. Print a warning
  when the loop runs out.
- **The review queue cannot record a decision to leave an episode alone.**
  Episodes reviewed and deliberately left without a guest (the panels above)
  sit in `meta.episodes_needing_review` next to ones nobody has looked at, so
  the queue never empties. A separate state for "reviewed, no single guest"
  would make what remains actionable.

## Feature: topic over time

Episode dates drive only the year-range filter and the tooltip in
`explore.html`. No view plots topics against time. All 311 full episodes are
dated (2020-05 to 2026-09) and 243 of them carry at least one topic, so a
topic-over-time view could be built from the graph as it stands, and might show
the field's arc from data catalogs through data mesh to context graphs and
agents.

Topic edges are keyword matches (`provenance: inferred`), so a time view
inherits whatever noise the match phrases in `data/topics.json` carry.
