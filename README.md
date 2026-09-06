# 🍸 GraphGarnish

**Transform podcast metadata into interactive network visualizations**

GraphGarnish is a web-based tool for visualizing podcast episode networks. See which guests return, which organizations dominate the conversation, and how your podcast has evolved over time—all through an interactive, filterable network graph.

🔗 **Live Demo:** [graph-gist.com](https://graph-gist.com) | [graphgist.studio](https://graphgist.studio)

---

## 🎯 What It Does

GraphGarnish takes podcast metadata (episodes, guests, organizations) and creates an **interactive force-directed network visualization** where you can:

- **Filter by year** to see how your network evolved
- **Search** for specific people or organizations
- **Click nodes** to focus on their immediate connections
- **Adjust thresholds** to show only repeat guests or major organizations
- **Export insights** about your podcast's guest network

Perfect for podcast producers, researchers, or anyone who wants to understand the social graph hiding in their episode list.

---

## ✨ Features

### 🎛️ **Smart Filtering**
- Year range slider (e.g., "show only 2024 episodes")
- Minimum connection thresholds (e.g., "guests with 3+ episodes")
- Node type toggles (show/hide episodes, guests, orgs)
- Real-time search across all node names

### 👆 **Interactive Exploration**
- Click any node to highlight its network
- Drag nodes to rearrange the layout
- Zoom and pan to explore dense clusters
- Hover for detailed tooltips

### 📊 **Real-Time Stats**
- Live counts as you filter (X episodes, Y guests, Z orgs)
- Connection metrics in tooltips
- Network density visualization

### 🎨 **Beautiful Defaults**
- D3.js force-directed physics
- Color-coded node types (episodes=red, guests=cyan, orgs=yellow, topics=violet)
- Smooth animations and transitions
- Responsive design (works on mobile)

---

## 🚀 Quick Start

### Option 1: Try the Demo
1. Visit **[graph-gist.com](https://graph-gist.com)**
2. The demo loads with 162 episodes from *Catalog & Cocktails*
3. Use filters and search to explore

### Option 2: Use Your Own Data
1. Download `explore.html` from this repo
2. Prepare your network JSON (see [Data Format](#-data-format) below)
3. Open `explore.html` in a browser
4. Click **"Load Network Data"** and select your JSON file
5. Explore!

### Option 3: Clone & Customize
```bash
git clone https://github.com/dagny099/graphgarnish.git
cd graphgarnish
# Open index.html in your browser
```

---

## 📁 Data Format

GraphGarnish expects a JSON file with two arrays: `nodes` and `links`.

### Example Structure

```json
{
  "nodes": [
    {
      "id": "ep_1",
      "name": "Episode Title Here",
      "type": "episode",
      "date": "2024-01-15",
      "is_full": true
    },
    {
      "id": "person_juan_sequeda",
      "name": "Juan Sequeda",
      "type": "person"
    },
    {
      "id": "org_dataworld",
      "name": "data.world",
      "type": "organization"
    }
  ],
  "links": [
    {
      "source": "person_juan_sequeda",
      "target": "ep_1",
      "type": "GUEST_ON"
    },
    {
      "source": "person_juan_sequeda",
      "target": "org_dataworld",
      "type": "AFFILIATED_WITH"
    }
  ]
}
```

### Node Types

**Episode:**
- `id`: Unique identifier (e.g., `ep_1`)
- `name`: Episode title
- `type`: `"episode"`
- `date`: Publication date (YYYY-MM-DD format)
- `is_full`: Boolean (true for full episodes, false for shorts/takeaways)

**Person (Guest):**
- `id`: Unique identifier (e.g., `person_juan_sequeda`)
- `name`: Guest's full name
- `type`: `"person"`

**Organization:**
- `id`: Unique identifier (e.g., `org_dataworld`)
- `name`: Organization name
- `type`: `"organization"`

**Topic:**
- `id`: Unique identifier (e.g., `topic_data_governance`)
- `name`: Topic name
- `type`: `"topic"`

Topics are what make the graph a network rather than a list. A podcast that
books one guest per episode produces almost no guest-to-guest structure — in
the *Catalog & Cocktails* data only four guests have ever appeared twice.
Topics recur constantly, so they are what actually connects episodes across
years and guests.

Topics are assigned by matching phrases from `data/topics.json` against each
episode's title and description. That file is meant to be edited: add a topic,
add or remove match phrases, delete one you do not care about, then re-run the
builder.

### Link Types

- `GUEST_ON`: Connects a person to an episode
- `AFFILIATED_WITH`: Connects a person to an organization
- `COVERS`: Connects an episode to a topic
- `TAKEAWAY_OF`: Connects a short companion clip to the full episode it summarizes

### Full episodes vs. companion clips

The show publishes a short `TAKEAWAYS - ...` clip alongside most episodes.
These carry `is_full: false` and a `TAKEAWAY_OF` link to their parent. Guests
attach only to the parent, so a guest's episode count is a real count. The
explorer hides companion clips by default; the **Full episodes only** checkbox
turns them back on.

---

## 🛠️ Building the Network JSON

`tools/build_network.py` builds `catalog_cocktails.json` and `sample_network.json`.

**Inputs are read-only; outputs are fully derived.** Nothing the builder writes
is ever read back on a later run, so a name it guessed today cannot be mistaken
for a curated fact tomorrow. Delete both output files, rebuild, and they come
back identical.

```
data/seed_curated.json   episodes and guests from the original spreadsheet
data/verified.json       human corrections, applied last and always
data/topics.json         the topic taxonomy
the RSS feed             the full back catalogue, fetched fresh each run
        │
        ▼
catalog_cocktails.json   output
sample_network.json      output — what the site fetches
```

To correct something the extractor got wrong, edit `data/verified.json`. Never
edit the output files: the next run overwrites them.

```bash
python3 tools/build_network.py --feed --report          # try the known URLs
python3 tools/build_network.py --feed <URL> --report    # or name one
```

With no URL, `--feed` uses the show's own canonical feed, taken from the
`<atom:link rel="self">` inside the feed itself.

**The feed is paginated.** Page 1 carries only the most recent episodes and
links the rest through `<atom:link rel="next">`; the builder follows every page
and de-duplicates by GUID. Fetching one page would look like a successful run
while silently dropping most of the back catalogue.

Companion clips are identified two ways, because neither is reliable alone:
older ones carry `<itunes:episodeType>full</itunes:episodeType>` and are only
recognisable by their `TAKEAWAYS - ` title prefix, while the newest drop the
separator (`TAKEAWAY Foo`) and are only recognisable by `episodeType`. A real
episode titled *Takeaways from Gartner Data & Analytics Rants* is neither, and
is treated as a full episode. If the URL turns out to serve a
web page rather than a feed, the builder reads that page's
`<link rel="alternate" type="application/rss+xml">` pointer and follows it once,
over https, refusing to downgrade the connection or follow an unusual scheme.
The run log prints `USING FEED: <url>` so the working URL can be pinned.

A rebuild can only add episodes. If one would drop more than a handful, the
builder refuses to write and exits non-zero rather than publish a truncated or
unrelated feed. The number it compares against is the episode count in the last
published graph, not the one in the seed — the seed is a small curated file the
feed has long since grown past, so checking against it would let a truncated
feed drop most of the back catalogue and still look fine.

Guest and company links from the spreadsheet survive every rebuild. Everything
the feed adds beyond them is auto-extracted, and any episode where no guest
could be identified is listed under `meta.episodes_needing_review` so it can be
checked by hand rather than silently dropped.

### Provenance

Every node carries a `source`, and every link a `provenance`, so the graph says
which parts you know and which parts a regex guessed.

| value | meaning |
| --- | --- |
| `curated` | from the spreadsheet, or from `data/topics.json` |
| `verified` | a decision in `data/verified.json` touched it |
| `feed` | an episode only the RSS feed knows about — a fact, not a guess |
| `inferred` | read out of a title or description by a heuristic |

A node inherits the **strongest** provenance of any edge that touches it, ranked
`verified` > `curated` / `feed` > `inferred`. A guest who is curated on one
episode and guessed on another is a curated person with one guessed edge:

```json
{ "id": "person_aakriti_agrawal", "name": "Aakriti Agrawal",
  "type": "person", "source": "curated" }

{ "source": "person_aakriti_agrawal", "target": "ep_20230608_takeaways_with_aakriti_a",
  "type": "GUEST_ON", "provenance": "inferred" }
```

The key on a link is `provenance`, not `source` — a link's `source` is its
origin node and stays that way.

`meta.provenance` and `meta.link_provenance` count it up, and `--report` prints
it. Link provenance is broken out per type on purpose: lumping it together
buries how many *guest* edges are known under the topic edges, which are keyword
matches by design.

```
  episode          388   curated=202  feed=186
  organization      98   curated=60  inferred=37  verified=1
  person           182   curated=141  inferred=38  verified=3
  topic             22   curated=22
  AFFILIATED_WITH  103   curated=61  inferred=41  verified=1
  COVERS           564   inferred=564
  GUEST_ON         259   curated=146  inferred=110  verified=3
  TAKEAWAY_OF       62   inferred=62
```

### Correcting the data

`data/verified.json` is where human judgement enters the pipeline, and the only
file you should hand-edit. It is applied after extraction on every run, so a
correction made once stays made.

| key | effect |
| --- | --- |
| `drop_person` | an extracted "person" that is really a topic, role or fragment |
| `drop_organization` | same, for companies |
| `rename_person` | fold a variant or misspelling into the real name |
| `rename_organization` | same, for companies |
| `person_org` | set an employer by hand; beats every guess |
| `hosts_only` | episodes Juan and Tim recorded alone, by exact title |
| `hosts_only_patterns` | regexes for the recurring host-only formats |

An episode marked `hosts_only` carries `"hosts_only": true` and no guest, rather
than an invented guest called "Juan and Tim", and drops out of the review queue.
The flag only applies when no guest was found, so a pattern can never displace a
real guest.

Anything under `_unresolved` in that file is a note, not a rule. It is not
applied.

Guest extraction from a title recovers roughly half of all guests; the rest are
named only in the episode description. It is a starting point that needs review,
not a replacement for a curated list.

If the feed parses badly, capture the raw XML so the problem can be reproduced
without network access:

```bash
python3 tools/build_network.py --feed --save-feed /tmp/feed.xml --report
python3 tools/build_network.py --feed-file /tmp/feed.xml --report
```

`data/seed_curated.json` is the frozen spreadsheet-derived data and the
builder's starting point. It is never written to.

Run the checks after changing anything in `tools/`:

```bash
python3 tools/test_build_network.py
```

### Automatic refresh

`.github/workflows/refresh-network.yml` runs the online build weekly and commits
the result if the graph changed. GitHub's runners have open outbound network
access, so the site keeps itself current with no local setup. Trigger it by hand
from the repository's **Actions** tab via **Run workflow**.

---

## 📊 Use Cases

### 1. **Guest Network Analysis**
- Which guests return most frequently?
- Which organizations are most represented?
- Are there clusters of guests from the same industry?

### 2. **Content Planning**
- Identify gaps in your guest diversity
- Find one-time guests worth inviting back
- Track organizational representation over time

### 3. **Social Proof**
- Showcase the breadth of your guest network
- Highlight prominent organizations you've featured
- Demonstrate growth and evolution of your show

### 4. **Research & Discovery**
- Find connections between guests and organizations
- Track career movements (guests changing companies)
- Identify influential nodes in your podcast network

---

## 🎨 Customization

### Colors
Edit the `colorMap` in `explore.html`:
```javascript
const colorMap = {
    'episode': '#ff6b6b',      // Red
    'person': '#4ecdc4',        // Cyan
    'organization': '#ffe66d'   // Yellow
};
```

### Node Sizes
Edit the `sizeMap`:
```javascript
const sizeMap = {
    'episode': 7,
    'person': 11,
    'organization': 15
};
```

### Physics
Adjust force simulation parameters:
```javascript
.force('charge', d3.forceManyBody()
    .strength(d => {
        if (d.type === 'organization') return -1200;  // Org repulsion
        if (d.type === 'person') return -500;         // Guest repulsion
        return -200;                                  // Episode repulsion
    }))
```

---

## 🧰 Tech Stack

- **D3.js v7** - Force-directed graph layout
- **Vanilla JavaScript** - No frameworks, no build step
- **HTML5 + CSS3** - Modern, responsive design
- **JSON** - Simple, portable data format

**Why no backend?**
- Runs entirely in the browser
- No server needed
- Easy to host on GitHub Pages, Netlify, etc.
- Privacy-friendly (your data never leaves your machine)

---

## 📖 Documentation

### Filter Logic

Filters cascade intelligently:

1. **Episode filters** (year, search, visibility) apply first
2. **Guest filters** only show guests connected to visible episodes
3. **Organization filters** only show orgs connected to visible guests

This prevents orphaned nodes and maintains network coherence.

**Connection thresholds** (e.g., "min 2 episodes per guest") use the **full dataset**, not the filtered view. This ensures consistent filtering behavior.

### Performance

GraphGarnish handles networks with:
- ✅ Up to 500 episodes
- ✅ Up to 300 guests
- ✅ Up to 150 organizations

For larger networks (1000+ nodes), consider:
- Reducing physics iterations
- Disabling labels on episodes
- Pre-filtering data before loading

---

## 🤝 Contributing

Contributions welcome! Here's how:

1. **Fork** the repo
2. **Create a branch**: `git checkout -b feature/your-feature`
3. **Commit changes**: `git commit -m 'Add some feature'`
4. **Push**: `git push origin feature/your-feature`
5. **Open a Pull Request**

### Ideas for Contributions

- [ ] CSV export functionality
- [ ] Timeline slider (animate growth over time)
- [ ] Community detection algorithms
- [ ] Dark mode toggle
- [ ] Mobile gesture controls
- [ ] Embed code generator
- [ ] Multi-dataset comparison view

---

## 📄 License

MIT License - see [LICENSE](LICENSE) file for details

**TL;DR:** Use it, modify it, share it. Just keep the license notice.

---

## 🙏 Acknowledgments

- **D3.js** for the incredible force simulation library
- **Catalog & Cocktails** podcast for the demo dataset
- The data visualization community for inspiration

---

## 📬 Contact

**Built by:** Dagny Taggart  
**Website:** [yourwebsite.com](https://yourwebsite.com)  
**GitHub:** [@dagny099](https://github.com/dagny099)

**Questions? Ideas? Found a bug?**  
Open an issue on GitHub or reach out on LinkedIn.

---

## 🎯 Roadmap

### v1.0 (Current)
- ✅ Interactive network visualization
- ✅ Smart filtering system
- ✅ File upload interface
- ✅ Responsive design

### v1.1 (In progress)
- [x] Topic layer connecting episodes across years and guests
- [x] Search across every node type, not just episode titles
- [x] Companion-clip handling so guest counts are honest
- [x] Scheduled rebuild from the podcast RSS feed
- [ ] Export filtered network as image
- [ ] Save/load filter presets
- [ ] Keyboard shortcuts
- [ ] Tutorial overlay for first-time users

### v2.0 (Future)
- [ ] Backend API for automatic network generation
- [ ] Multi-podcast comparison mode
- [ ] Statistical analysis dashboard
- [ ] Integration with podcast hosting platforms

---

**⭐ If you find GraphGarnish useful, please star the repo!**

---

*"The perfect garnish for your podcast knowledge graph."*
