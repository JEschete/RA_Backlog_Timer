# RA_Backlog_Timer

A Python tool that syncs your RetroAchievements "Want to Play" list with HowLongToBeat completion times.

Plan your retro gaming backlog by seeing how long each game takes to beat and 100% complete.

## Important Note

**These times are NOT RetroAchievements mastery times.**

HowLongToBeat tracks how long it takes to *play through* a game, not how long it takes to earn all achievements. RetroAchievements sets often include challenges that go far beyond a normal playthrough, such as:

- No-damage runs
- Speedrun challenges
- Collectible hunts with no in-game tracking
- Multiple playthroughs on different difficulties
- Challenge modes and post-game content
- Missable achievements requiring careful planning

As a result, the time to master a game on RetroAchievements can be significantly longer (sometimes 2-5x or more) than the HLTB completionist time.

**This tool provides a general estimate of base game length to help you plan your backlog.** Actual mastery time will vary based on the achievement set difficulty, your familiarity with the game, and your skill level.

## Features

- Pulls your Want to Play list directly from the RetroAchievements API
- Fetches beat and completionist times from HowLongToBeat
- GUI login dialog with secure credential storage
- Progress caching (safe to interrupt and resume)
- Fuzzy match detection with quality indicators
- Exports to Excel with all data and match notes
- Automatic handling of RA-specific title prefixes (~Hack~, ~Homebrew~, etc.)

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

### Output

The tool creates an Excel file with the following columns:

| Column | Description |
|--------|-------------|
| Title | Game title from RetroAchievements |
| System | Console/platform |
| Achievements | Number of achievements available |
| Points | Total achievement points |
| RA_ID | RetroAchievements game ID |
| Beat | Main story completion time (hours) |
| Complete | 100% completion time (hours) |
| Comments | Match quality notes (see below) |

### Match Quality

The Comments column indicates how well the game matched on HowLongToBeat:

| Comment | Meaning |
|---------|---------|
| *(empty)* | Exact title match |
| `Fuzzy match: [name]` | High similarity (80%+) but not exact |
| `Loose match (X%): [name]` | Moderate similarity (50-79%) |
| `Poor match (X%): [name] - VERIFY` | Low similarity, needs manual review |
| `No HLTB match found` | Game not found in HowLongToBeat database |

## Files Created

The tool creates several cache files in the working directory:

- `HowLongToBeat.xlsx` - Your output file (or custom name via -o)
- `ra_wanttoplay_cache.json` - Cached Want to Play list from RA
- `hltb_progress.json` - HLTB lookup progress (for resuming)
- `.ra_credentials.json` - Credentials file (only if keyring unavailable)

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
- `retroachievements.org` - to fetch your Want to Play list
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

## Contributing

Pull requests are welcome. For major changes, please open an issue first to discuss what you would like to change.

## License

[MIT](LICENSE)

## Acknowledgments

- [RetroAchievements](https://retroachievements.org) for the amazing retro gaming community and API
- [HowLongToBeat](https://howlongtobeat.com) for game completion time data
- [howlongtobeatpy](https://github.com/ScrappyCocco/HowLongToBeat-PythonAPI) for the Python HLTB library
