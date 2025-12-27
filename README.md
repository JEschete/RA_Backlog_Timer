# RA_Backlog_Timer

A Python tool that syncs your RetroAchievements "Want to Play" list with completion time data from both HowLongToBeat and RetroAchievements player statistics.

Plan your retro gaming backlog with accurate time estimates for both casual playthroughs and full mastery. 

**Note: This is rate limited to not overload RA or HLTB servers**

## Time Data Sources

This tool pulls timing data from **two sources**:

| Source | Data | What it measures |
|--------|------|------------------|
| **RetroAchievements** | RA_Beat, RA_Master | Actual median times from RA players earning achievements |
| **HowLongToBeat** | HLTB_Beat, HLTB_Complete | General playthrough times (not achievement-focused) |

**RA Mastery times are authoritative** - they come from actual player data and reflect how long it takes to earn all achievements, including challenge runs, collectibles, and multiple playthroughs.

HLTB times are useful as a baseline comparison but typically underestimate mastery time by 2-5x depending on the achievement set difficulty.

## Features

- Pulls your Want to Play list directly from the RetroAchievements API
- Fetches **actual RA mastery times** from player statistics (API_GetGameProgression)
- Fetches beat and completionist times from HowLongToBeat for comparison
- **Efficiency metric** (points per hour) to prioritize your backlog
- **Smart title matching** for better HLTB results
- GUI login dialog with secure credential storage
- Progress caching (safe to interrupt and resume)
- Exports to Excel with all data and match notes

### Smart Title Matching

The HLTB search uses intelligent matching to handle RetroAchievements naming conventions:

- **Pokemon handling**: Normalizes `é` to `e`, strips "Version" suffix
- **Alternate titles**: Searches both sides of pipe separators (e.g., `HeartGold | SoulSilver`)
- **Sequel detection**: Penalizes numbered sequels when searching for the original (prevents `Aladdin` matching `Aladdin III`)
- **Title cleanup**: Removes `~Hack~`, `[Subset]`, region codes `(USA)`, version info `(Rev 1)`, etc.
- **Special characters**: Normalizes `ō` to `o` (Okami), `ü` to `u`, and other diacritics

## Installation

### Requirements

- Python 3.10 or higher
- A RetroAchievements account with a Want to Play list
- Your RA API key (get it from [retroachievements.org/settings](https://retroachievements.org/settings))

### Install dependencies

```bash
pip install howlongtobeatpy pandas openpyxl aiohttp keyring
```

Note: `keyring` is optional but recommended for secure credential storage. Without it, credentials are stored in a local file.

## Usage

### Basic usage

```bash
python ra_backlog_timer.py
```

On first run, a login dialog will appear asking for your RetroAchievements username and API key. These are saved securely for future runs.

### Command line options

```bash
# Specify output file
python ra_backlog_timer.py -o MyBacklog.xlsx

# Re-fetch your Want to Play list from RetroAchievements
python ra_backlog_timer.py --refresh

# Clear stored credentials and enter new ones
python ra_backlog_timer.py --reset-creds
```

### Sample Terminal Output

```
======================================================================
RetroAchievements + HowLongToBeat Scraper
======================================================================
Fetching Want to Play list for 'USER' from RetroAchievements...
  Fetched 345/345 games...
  Total: 345 games in Want to Play list

Processing 345 games...

----------------------------------------------------------------------
[318/345] Crash Twinsanity (PlayStation 2)... [319/345] Drakengard (PlayStation 2)... [320/345] Spyro: A Hero's Tail (PlayStation 2)... [321/345] Spyro: Enter the Dragonfly (PlayStation 2)... → .hack//Infection [HLTB: 17.1h, RA Master: 23.0h]
[322/345] Unlimited Saga (PlayStation 2)... → Crash Twinsanity [HLTB: 5.1h, RA Master: 15.5h]
[323/345] Monster Hunter (PlayStation 2)... → Drakengard [HLTB: 10.3h, RA Master: 81.8h]
→ Unlimited Saga [HLTB: 29.9h]
[324/345] Rayman 3: Hoodlum Havoc (PlayStation 2)... [325/345] Silent Hill: Shattered Memories (PlayStation 2)... → Spyro: A Hero's Tail [HLTB: 9.9h, RA Master: 23.2h]
→ Spyro: Enter the Dragonfly [HLTB: 9.3h, RA Master: 14.0h]
→ Monster Hunter [HLTB: 42.6h, RA Master: 35.2h]
→ Silent Hill: Shattered Memories [HLTB: 6.8h, RA Master: 19.0h]
→ Rayman 3: Hoodlum Havoc [HLTB: 8.7h, RA Master: 17.4h]
----------------------------------------------------------------------

Complete!
  Processed: 345
  Skipped (already had data): 0
  Games with HLTB data: 323
  Games with RA mastery data: 298

HLTB Match quality:
  Exact matches: 267
  Fuzzy matches: 30
  Loose matches: 8
  Poor matches (needs review): 0
  No match found: 18

RA vs HLTB Comparison (285 games with both):
  Avg HLTB Completionist: 45.8 hours
  Avg RA Mastery: 68.2 hours
  RA takes 1.5x longer on average

RA Mastery Time estimates:
  Total Mastery time: 18450.3 hours (768.8 days)
  Average Mastery: 61.9 hours

Games by system:
  PlayStation 2: 83
  PlayStation: 53
  SNES/Super Famicom: 45
  PlayStation Portable: 26
  Game Boy Advance: 24
  Nintendo 64: 23
  Nintendo DS: 23
  Genesis/Mega Drive: 21
  NES/Famicom: 17
  GameCube: 14

Most Efficient Games (highest points per hour of mastery):
  125.0 pts/hr -Ings (250 pts, 2.0h RA)
  98.5 pts/hr - Super Mario Bros. (394 pts, 4.0h RA)
  ...

Results saved to: HowLongToBeat.xlsx

Games without HLTB data (22):
  - Pokemon FireRed Version
  - Pokemon LeafGreen Version
  - Kirby no Kirakira Kids | Kirby's Super Star Stacker
  - Dragon Quest I & II
  ... and 18 more
```

### Output

The tool creates an Excel file with the following columns:

| Column | Description |
|--------|-------------|
| Title | Game title from RetroAchievements |
| System | Console/platform |
| Achievements | Number of achievements available |
| Points | Total achievement points |
| RA_ID | RetroAchievements game ID |
| HLTB_Beat | HowLongToBeat main story time (hours) |
| HLTB_Complete | HowLongToBeat completionist time (hours) |
| RA_Beat | RetroAchievements median time to beat (hours) |
| RA_Master | RetroAchievements median time to master (hours) |
| RA_Players | Number of distinct players on RetroAchievements |
| Points_Per_Hour | Efficiency metric (Points / RA_Master time) |
| Comments | HLTB match quality notes |

### Efficiency Metric

The `Points_Per_Hour` column helps you prioritize your backlog by showing which games give you the most RetroAchievements points for your time investment.

**Higher values = more "rewarding" games**

This is calculated using RA_Master time when available (actual player data), falling back to HLTB_Complete if RA data is missing.

Sort by this column descending to find quick wins that will boost your RA rank efficiently.

### HLTB Match Quality

The Comments column indicates how well the game matched on HowLongToBeat:

| Comment | Meaning |
|---------|---------|
| *(empty)* | Exact title match |
| `Fuzzy match: [name]` | High confidence match with minor differences |
| `Loose match (X%): [name]` | Moderate confidence, worth verifying |
| `Poor match (X%): [name] - VERIFY` | Low confidence, needs manual review |
| `No HLTB match found` | Game not found in HowLongToBeat database |

## Files Created

The tool creates several cache files in the working directory:

- `HowLongToBeat.xlsx` - Your output file (or custom name via -o)
- `ra_wanttoplay_cache.json` - Cached Want to Play list from RA
- `hltb_progress.json` - Lookup progress cache (for resuming)
- `.ra_credentials.json` - Credentials file (only if keyring unavailable)

### Re-fetching Data

To re-run HLTB matching (e.g., after a matching algorithm update):
```bash
rm hltb_progress.json
python ra_backlog_timer.py
```

To refresh your Want to Play list from RetroAchievements:
```bash
python ra_backlog_timer.py --refresh
```

## Security

### Credential Storage

Your RetroAchievements API key is sensitive and should be protected.

**With keyring installed (recommended):**
- Credentials are stored in your operating system's secure credential storage
- Windows: Credential Manager
- macOS: Keychain
- Linux: Secret Service (GNOME Keyring or KWallet)

**Without keyring:**
- Credentials are stored in `.ra_credentials.json` in the working directory
- File permissions are set to 600 (owner read/write only) on Unix systems

### API Key Safety

- Never commit your API key to version control
- The `.ra_credentials.json` file is a hidden file but is NOT encrypted
- If you suspect your API key is compromised, regenerate it at [retroachievements.org/settings](https://retroachievements.org/settings)

### Network Requests

This tool makes HTTPS requests to:
- `retroachievements.org` - to fetch your Want to Play list and game progression data
- `howlongtobeat.com` - to fetch game completion times

No data is sent to any other servers.

## Troubleshooting

### "Invalid API key or unauthorized"

- Verify your API key at [retroachievements.org/settings](https://retroachievements.org/settings)
- Run with `--reset-creds` to re-enter your credentials
- Make sure you can access your own Want to Play list (must be your account or mutual followers)

### "No games found in Want to Play list"

- Add some games to your Want to Play list on RetroAchievements
- Check that you are querying your own username

### GUI does not appear

- Make sure you have tkinter installed (included with most Python distributions)
- On Linux, you may need: `sudo apt install python3-tk`

### High DPI display issues

- The dialog should auto-scale on Windows 10/11
- If text appears too small, try running from a terminal with DPI awareness enabled

### Missing RA_Master times

- Not all games have enough player data for median times
- Newer or less popular games may not have mastery statistics yet
- The tool will still show HLTB times as a fallback

### Wrong HLTB match

- Delete `hltb_progress.json` and re-run to get fresh matches
- Check the Comments column for match quality indicators
- Some games (especially romhacks, subsets, or regional variants) may not exist on HLTB

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](LICENSE)

## Acknowledgments

- [RetroAchievements](https://retroachievements.org) for the amazing retro gaming community and API
- [HowLongToBeat](https://howlongtobeat.com) for game completion time data
- [howlongtobeatpy](https://github.com/ScrappyCocco/HowLongToBeat-PythonAPI) for the Python HLTB library
