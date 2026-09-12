# RA_Backlog_Timer

Syncs your RetroAchievements **Want to Play** list with completion times from both HowLongToBeat and RetroAchievements player statistics, then helps you decide what to actually play next.

Runs as a browser dashboard, a terminal menu, or a headless command — same data, same database.

**Rate limited by design** so it doesn't hammer RA or HLTB.

## Time data sources

| Source | Columns | What it measures |
|--------|---------|------------------|
| **RetroAchievements** | `RA_Beat`, `RA_Master`, `RA_Beat_HC`, `RA_Master_HC` | Median times from RA players actually earning achievements |
| **HowLongToBeat** | `HLTB_Beat`, `HLTB_Complete` | General playthrough times, not achievement-focused |

**RA mastery times are authoritative** — they come from real player data and reflect what it takes to earn every achievement, including challenge runs, collectibles and repeat playthroughs. The `_HC` columns are the hardcore-mode equivalents.

HLTB times are a useful baseline but typically underestimate mastery by 2–5× depending on the achievement set.

## Features

- Pulls your Want to Play list straight from the RetroAchievements API
- Fetches real RA mastery times (`API_GetGameProgression`), softcore and hardcore
- Fetches beat and completionist times from HowLongToBeat for comparison
- Tracks achievements you've **already earned**, so estimates are time *remaining*, not time from scratch
- **Points per hour** efficiency metric to prioritise the backlog
- **Session planner** — "what's the most points I can earn in 20 hours?" (exact knapsack, not a greedy guess)
- Smart title matching tuned for RetroAchievements naming
- Resumable: interrupt any scan and pick up where you left off
- Exports a formatted Excel workbook or CSV

## Installation

### Requirements

- Python 3.10 or newer
- A RetroAchievements account with a Want to Play list
- An RA API key from [retroachievements.org/settings](https://retroachievements.org/settings)

### Install

```bash
git clone https://github.com/JEschete/RA_Backlog_Timer
cd RA_Backlog_Timer
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e .
```

That gives you the `ra-backlog` command. If you'd rather not install the package:

```bash
pip install -r requirements.txt
python ra_backlog_timer.py
```

On Linux, tkinter ships separately and is needed for the first-run credential prompt: `sudo apt install python3-tk`.

## Usage

### Browser dashboard (default)

```bash
ra-backlog
```

Opens `http://127.0.0.1:8000` in your browser. From there you can run scans with live progress, sort and filter the whole backlog, run the planner, and export. Everything stays on your machine — the server binds to localhost only.

```bash
ra-backlog web --port 9000 --no-browser
```

### Terminal menu

```bash
ra-backlog menu
```

Numbered menu covering scans, summary, planner, exports and credentials.

### Headless

```bash
ra-backlog scan                              # update: fetch what's missing
ra-backlog scan --fresh                      # re-check everything
ra-backlog scan --systems "Nintendo 64" SNES/Super Famicom
ra-backlog scan --exclude "PlayStation 2"
ra-backlog scan --no-user-progress           # skip earned-achievement lookup

ra-backlog export --format xlsx -o MyBacklog.xlsx
ra-backlog export --format csv

ra-backlog credentials                       # set or replace
ra-backlog credentials --clear
```

Global flags: `--db PATH`, `-v/--verbose`, `-q/--quiet`, `--log-file PATH`.

### Terminal summary

```
  Games:                345
  With HLTB data:       338
  With RA mastery data: 334
  Without time data:    1
  Total mastery time:   15226.9 h (634.5 days)
  Average per game:     44.3 h

  Most efficient games:
      135.2 pts/hr  Golden Axe (284 pts, 2.1h)
      115.6 pts/hr  Mighty Morphin Power Rangers: The Movie (416 pts, 3.6h)
       92.3 pts/hr  Alien Soldier (323 pts, 3.5h)
```

### Session planner

Given the hours you actually have, it picks the set of games maximising achievement points — solved exactly rather than by sorting on efficiency and taking the top N, which gets the answer wrong whenever a slightly-less-efficient game fits the remaining time better.

```
  Hours available: 20

  1799 points in 19.5 h  (92.3 pts/hr)

       2.1h    284p  Golden Axe
       3.6h    416p  Mighty Morphin Power Rangers: The Movie
       3.5h    323p  Alien Soldier
       5.5h    445p  Castlevania: The Adventure
       4.8h    331p  Streets of Rage 2
```

Note that this isn't simply the top five by efficiency — `Castlevania: The Adventure` (80.9 pts/hr) and `Streets of Rage 2` (69.0) are chosen over higher-ranked games because they pack more points into the hours left over. That's the difference between solving the problem and sorting a column.

## Output columns

Excel and CSV exports contain:

| Column | Description |
|--------|-------------|
| `Title` | Game title from RetroAchievements |
| `System` | Console / platform |
| `Achievements` | Achievements published |
| `Earned` | Achievements you've already earned |
| `Points` | Total achievement points |
| `RA_ID` | RetroAchievements game ID |
| `HLTB_Beat` | HowLongToBeat main story (hours) |
| `HLTB_Complete` | HowLongToBeat completionist (hours) |
| `RA_Beat` | RA median time to beat (hours) |
| `RA_Master` | RA median time to master (hours) |
| `RA_Beat_HC` | RA median time to beat, hardcore |
| `RA_Master_HC` | RA median time to master, hardcore |
| `RA_Players` | Distinct players on RA |
| `Points_Per_Hour` | Efficiency metric |
| `Remaining_Hours` | Estimated time left, scaled by what you've already earned |
| `Match_Quality` | `exact` / `fuzzy` / `loose` / `poor` / `none` |
| `HLTB_Name` | The HowLongToBeat entry that matched |

The workbook ships with a frozen header, autofilter, a colour scale on `Points_Per_Hour`, and a **Summary** sheet with totals and a per-system breakdown.

### Efficiency metric

`Points_Per_Hour` = points ÷ mastery time. Higher means more RA points per hour of your life.

It prefers `RA_Master` (real player data) and falls back to `HLTB_Complete`, then `HLTB_Beat`. Sort descending to find quick wins.

### Match quality

| Value | Meaning |
|-------|---------|
| `exact` | Title matched exactly |
| `fuzzy` | High confidence, minor differences |
| `loose` | Moderate confidence — worth a look |
| `poor` | Low confidence — verify manually |
| `none` | Not found on HowLongToBeat |

### Smart title matching

RetroAchievements names games differently from HowLongToBeat, so titles are normalised before searching:

- **Articles**: RA alphabetises as `Legend of Zelda, The: A Link to the Past`. The article is moved to the front, including when it sits before a subtitle colon.
- **Tags**: strips `~Hack~`, `~Homebrew~`, `~Prototype~`, `[Subset - Bonus]`, `[T+Eng]`
- **Region and version**: `(USA)`, `(Europe)`, `(En,Fr,De)`, `(Rev 1)`, `(v1.1)`, `(Disc 1)`
- **Diacritics**: full Unicode folding, so `Pokémon` → `Pokemon`, `Ōkami` → `Okami`
- **Alternate titles**: searches both sides of `HeartGold | SoulSilver`
- **Sequel guard**: searching `Aladdin` won't return `Aladdin III`
- **Fallbacks**: the pre-subtitle base title is tried last and weighted down, so `Castlevania: Symphony of the Night` can't collapse to plain `Castlevania`

## Files

| File | Purpose |
|------|---------|
| `backlog.db` | SQLite database — the source of truth |
| `HowLongToBeat.xlsx` | Generated export (not read back) |
| `.ra_credentials.json` | Only created when `keyring` is unavailable |

Excel is an **output format**, not the database. That means a scan can't be derailed by having the workbook open, and exports are free to be formatted for reading.

### Migrating from an earlier version

Nothing to do. On first run, any existing `HowLongToBeat.xlsx`, `hltb_progress.json` and `ra_wanttoplay_cache.json` are imported into `backlog.db` automatically. The old files are left on disk untouched.

One deliberate exception: cached *failures* in `hltb_progress.json` are not imported, so those games get retried instead of inheriting a stale error.

### Re-running lookups

```bash
ra-backlog scan --fresh     # re-check every game
```

Failed lookups retry themselves after 24 hours. To force them sooner, use option 8 in the terminal menu ("Retry failed lookups").

Removing a game from your RA Want to Play list doesn't delete its data — it's flagged instead, so re-adding it costs no lookups.

## Security

### Credential storage

**With `keyring` (recommended, installed by default):** credentials go to your OS credential store — Windows Credential Manager, macOS Keychain, or Secret Service on Linux.

**Without it:** they're written to `.ra_credentials.json` with `600` permissions on Unix. The file is not encrypted.

If credentials are found in the fallback file while `keyring` is available, they're migrated into the keyring and the file is removed.

### Network requests

HTTPS to `retroachievements.org` and `howlongtobeat.com`. Nothing else. The web dashboard binds to `127.0.0.1` and is not reachable from your network.

If you think your API key is compromised, regenerate it at [retroachievements.org/settings](https://retroachievements.org/settings).

## Troubleshooting

**"Unauthorized (401)"** — verify your key, then `ra-backlog credentials` to re-enter it. You can only read your own Want to Play list (or a mutual follower's).

**No games found** — add some games to your Want to Play list, and check you're querying your own username.

**Credential dialog doesn't appear** — needs tkinter (`sudo apt install python3-tk` on Linux). Without a display it falls back to a terminal prompt.

**Missing RA mastery times** — not every game has enough player data for a median. Newer or obscure sets often have none; HLTB times are used as the fallback.

**Wrong HLTB match** — check `Match_Quality`. Romhacks, subsets and regional variants frequently have no HLTB entry at all. `ra-backlog scan --fresh` re-runs matching after any tuning.

**Export says the file is open** — close the workbook in Excel and retry. The scan itself is unaffected; only the export needs the file.

**Rate limited** — requests retry with exponential backoff automatically. For a gentler scan, lower `MAX_CONCURRENT_REQUESTS` in `ra_backlog/config.py`.

## Development

```
ra_backlog/
  matching.py       title normalization + HLTB scoring   (pure, heavily tested)
  efficiency.py     derived metrics + session planner    (pure)
  scanner.py        scan orchestration and concurrency
  storage/          SQLite schema, queries, migration, export
  clients/          RA API, HLTB, shared retry/backoff
  cli/              argument parsing, terminal menu, credential dialog
  web/              FastAPI app, SSE progress, dashboard
tests/
```

```bash
pip install -e ".[dev]"
pytest
```

`matching.py` and `efficiency.py` have no I/O, which is what makes them worth testing properly — the test suite covers title normalization against real RetroAchievements naming, the metric fallback chain, planner optimality and budget limits, and the lookup cache's retention rules.

## Contributing

Pull requests welcome. For anything substantial, open an issue first.

## License

[MIT](LICENSE)

## Acknowledgments

- [RetroAchievements](https://retroachievements.org) for the community and the API
- [HowLongToBeat](https://howlongtobeat.com) for completion time data
- [howlongtobeatpy](https://github.com/ScrappyCocco/HowLongToBeat-PythonAPI) for the Python HLTB library
