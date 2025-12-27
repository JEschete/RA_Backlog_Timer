#!/usr/bin/env python3
"""
HowLongToBeat Scraper for RetroAchievements Want to Play List

Pulls your Want to Play list directly from RetroAchievements API,
then fetches HowLongToBeat times for each game.

Features:
- GUI prompt for credentials on first run
- Securely stores credentials in system keyring
- Caches RA game list locally
- Tracks HLTB lookup progress (safe to interrupt)

Usage:
    pip install howlongtobeatpy pandas openpyxl aiohttp keyring
    python hltb_scraper.py

Options:
    --output, -o      Output Excel file (default: HowLongToBeat.xlsx)
    --refresh         Re-fetch the Want to Play list from RA
    --reset-creds     Clear stored credentials and prompt again
"""

import asyncio
import pandas as pd
from pathlib import Path
from howlongtobeatpy import HowLongToBeat, SearchModifiers
import sys
import json
import argparse
import aiohttp
import tkinter as tk
from tkinter import ttk, messagebox

# Try to import keyring, fall back to file-based storage if unavailable
try:
    import keyring
    KEYRING_AVAILABLE = True
except ImportError:
    KEYRING_AVAILABLE = False
    print("Note: 'keyring' not installed. Credentials will be stored in a local file.")
    print("      Install keyring for secure storage: pip install keyring")

# Constants
DELAY_BETWEEN_REQUESTS = 0.3  # Reduced since we're limiting concurrency
MAX_CONCURRENT_REQUESTS = 5   # Number of simultaneous HLTB lookups
RA_API_BASE = "https://retroachievements.org/API"
PROGRESS_FILE = 'hltb_progress.json'
RA_CACHE_FILE = 'ra_wanttoplay_cache.json'
CREDS_FILE = '.ra_credentials.json'  # Fallback if keyring unavailable
KEYRING_SERVICE = 'RAHLTBScraper'

# ANSI color codes for terminal output
class Colors:
    RED = '\033[91m'
    ORANGE = '\033[93m'
    GREEN = '\033[92m'
    RESET = '\033[0m'

# Regex patterns for title normalization
import re

def normalize_title(title: str) -> str:
    """
    Aggressively normalize RA titles for better HLTB matching.
    
    Removes:
    - RA tags: ~Hack~, ~Homebrew~, ~Prototype~, ~Demo~, ~Unlicensed~, etc.
    - Subset markers: [Subset - Bonus], [Subset - Multi], etc.
    - Region codes: (USA), (Europe), (Japan), (En,Fr,De), etc.
    - Version info: (Rev 1), (v1.1), (Beta), etc.
    - Platform tags: (Virtual Console), (PSN), etc.
    - Articles for sorting: ", The" at end becomes "The " at start
    - Normalizes special characters (é -> e for Pokemon)
    """
    clean = title
    
    # Remove ~Tag~ prefixes (Hack, Homebrew, Prototype, Demo, Unlicensed, Translation, etc.)
    clean = re.sub(r'^~[^~]+~\s*', '', clean)
    
    # Remove [Subset - ...] markers
    clean = re.sub(r'\[Subset\s*-\s*[^\]]+\]', '', clean)
    
    # Remove other bracket tags [!], [T+Eng], [T-En], etc.
    clean = re.sub(r'\[[^\]]*\]', '', clean)
    
    # Remove region codes (USA), (Europe), (Japan), (J), (U), (E), (En,Fr,De), (World), etc.
    clean = re.sub(r'\((?:USA|Europe|Japan|World|En|Fr|De|Es|It|J|U|E|En,\s*[A-Za-z,\s]+)\)', '', clean, flags=re.IGNORECASE)
    
    # Remove version/revision info (Rev 1), (Rev A), (v1.0), (V1.1), (Beta), (Proto), (Sample)
    clean = re.sub(r'\((?:Rev\s*[A-Z0-9]*|v\d+\.\d+|Beta|Proto|Sample|Virtual Console|PSN|XBLA)\)', '', clean, flags=re.IGNORECASE)
    
    # Remove (Disc 1), (Disc 2), etc.
    clean = re.sub(r'\(Disc\s*\d+\)', '', clean, flags=re.IGNORECASE)
    
    # Handle ", The" at end -> "The " at start (for alphabetized titles)
    if clean.endswith(', The'):
        clean = 'The ' + clean[:-5]
    
    # Normalize special characters for better matching
    # Pokemon uses é but HLTB might use e
    clean = clean.replace('é', 'e').replace('É', 'E')
    clean = clean.replace('ō', 'o').replace('Ō', 'O')  # Okami uses ō
    clean = clean.replace('ü', 'u').replace('Ü', 'U')
    
    # Remove double spaces and trim
    clean = re.sub(r'\s+', ' ', clean).strip()
    
    return clean


async def fetch_game_progression(session: aiohttp.ClientSession, api_key: str, game_id: int) -> dict:
    """
    Fetch progression/timing data for a specific game from RetroAchievements.
    
    Returns median times in hours for beat and mastery.
    Times from RA are in seconds, we convert to hours.
    """
    result = {
        'ra_beat_time': None,
        'ra_master_time': None,
        'ra_beat_hardcore': None,
        'ra_master_hardcore': None,
        'distinct_players': None
    }
    
    try:
        url = f"{RA_API_BASE}/API_GetGameProgression.php"
        params = {'y': api_key, 'i': game_id}
        
        async with session.get(url, params=params) as resp:
            if resp.status != 200:
                return result
            
            data = await resp.json()
        
        # Convert seconds to hours (round to 1 decimal)
        if data.get('MedianTimeToBeat') and data['MedianTimeToBeat'] > 0:
            result['ra_beat_time'] = round(data['MedianTimeToBeat'] / 3600, 1)
        
        if data.get('MedianTimeToMaster') and data['MedianTimeToMaster'] > 0:
            result['ra_master_time'] = round(data['MedianTimeToMaster'] / 3600, 1)
        
        if data.get('MedianTimeToBeatHardcore') and data['MedianTimeToBeatHardcore'] > 0:
            result['ra_beat_hardcore'] = round(data['MedianTimeToBeatHardcore'] / 3600, 1)
        
        if data.get('MedianTimeToMasterHardcore') and data['MedianTimeToMasterHardcore'] > 0:
            result['ra_master_hardcore'] = round(data['MedianTimeToMasterHardcore'] / 3600, 1)
        
        result['distinct_players'] = data.get('NumDistinctPlayers', 0)
        
    except Exception as e:
        pass  # Silently fail, we'll just have None values
    
    return result


class CredentialManager:
    """Manages RetroAchievements credentials with secure storage."""
    
    @staticmethod
    def get_credentials() -> tuple[str, str] | None:
        """Retrieve stored credentials. Returns (username, api_key) or None."""
        if KEYRING_AVAILABLE:
            username = keyring.get_password(KEYRING_SERVICE, 'username')
            api_key = keyring.get_password(KEYRING_SERVICE, 'api_key')
            if username and api_key:
                return (username, api_key)
        else:
            # Fallback to file
            creds_path = Path(CREDS_FILE)
            if creds_path.exists():
                try:
                    with open(creds_path, 'r') as f:
                        data = json.load(f)
                        return (data.get('username'), data.get('api_key'))
                except:
                    pass
        return None
    
    @staticmethod
    def save_credentials(username: str, api_key: str):
        """Store credentials securely."""
        if KEYRING_AVAILABLE:
            keyring.set_password(KEYRING_SERVICE, 'username', username)
            keyring.set_password(KEYRING_SERVICE, 'api_key', api_key)
        else:
            # Fallback to file (less secure but functional)
            with open(CREDS_FILE, 'w') as f:
                json.dump({'username': username, 'api_key': api_key}, f)
            # Try to set file permissions (Unix only)
            try:
                import os
                os.chmod(CREDS_FILE, 0o600)
            except:
                pass
    
    @staticmethod
    def clear_credentials():
        """Remove stored credentials."""
        if KEYRING_AVAILABLE:
            try:
                keyring.delete_password(KEYRING_SERVICE, 'username')
                keyring.delete_password(KEYRING_SERVICE, 'api_key')
            except:
                pass
        else:
            creds_path = Path(CREDS_FILE)
            if creds_path.exists():
                creds_path.unlink()


class CredentialDialog:
    """GUI dialog for entering RetroAchievements credentials."""
    
    def __init__(self, existing_username: str = ""):
        self.result = None
        self.root = tk.Tk()
        self.root.title("RetroAchievements Login")
        
        # Enable DPI awareness on Windows
        try:
            from ctypes import windll
            windll.shcore.SetProcessDpiAwareness(2)  # Per-monitor DPI aware
        except:
            pass
        
        # Enable scaling for Tk
        self.root.tk.call('tk', 'scaling', self.root.winfo_fpixels('1i') / 72.0)
        
        # Get scaling factor
        scale = self.root.winfo_fpixels('1i') / 96.0  # 96 DPI is baseline
        scale = max(1.0, scale)  # Don't scale below 1.0
        
        # Scaled dimensions
        def s(value):
            return int(value * scale)
        
        # Configure styles with scaled fonts
        style = ttk.Style()
        default_font_size = s(10)
        title_font_size = s(14)
        
        style.configure('TLabel', font=('Segoe UI', default_font_size))
        style.configure('TButton', font=('Segoe UI', default_font_size), padding=s(5))
        style.configure('TCheckbutton', font=('Segoe UI', default_font_size))
        style.configure('TEntry', font=('Segoe UI', default_font_size))
        style.configure('Title.TLabel', font=('Segoe UI', title_font_size, 'bold'))
        style.configure('Info.TLabel', font=('Segoe UI', default_font_size), foreground='gray')
        
        # Main frame with scaled padding
        main_frame = ttk.Frame(self.root, padding=s(20))
        main_frame.grid(row=0, column=0, sticky="nsew")
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(0, weight=1)
        
        # Title
        title_label = ttk.Label(main_frame, text="RetroAchievements Credentials", 
                                style='Title.TLabel')
        title_label.grid(row=0, column=0, columnspan=2, pady=(0, s(10)))
        
        # Info text with clickable link
        info_frame = ttk.Frame(main_frame)
        info_frame.grid(row=1, column=0, columnspan=2, pady=(0, s(15)))
        
        info_label1 = ttk.Label(info_frame, text="Enter your RA username and API key.", style='Info.TLabel')
        info_label1.pack()
        
        # Frame for the "Get your API key from:" line with clickable link
        link_frame = ttk.Frame(info_frame)
        link_frame.pack()
        
        info_label2 = ttk.Label(link_frame, text="Get your API key from: ", style='Info.TLabel')
        info_label2.pack(side='left')
        
        # Clickable link
        link_label = tk.Label(link_frame, text="retroachievements.org/settings", 
                              fg='#0066CC', cursor='hand2', 
                              font=('Segoe UI', default_font_size, 'underline'))
        link_label.pack(side='left')
        link_label.bind('<Button-1>', lambda e: self._open_url('https://retroachievements.org/settings'))
        link_label.bind('<Enter>', lambda e: link_label.config(fg='#0099FF'))
        link_label.bind('<Leave>', lambda e: link_label.config(fg='#0066CC'))
        
        # Username
        ttk.Label(main_frame, text="Username:").grid(row=2, column=0, sticky='e', padx=(0, s(10)))
        self.username_entry = ttk.Entry(main_frame, width=s(35), font=('Segoe UI', default_font_size))
        self.username_entry.grid(row=2, column=1, pady=s(5), sticky='ew')
        if existing_username:
            self.username_entry.insert(0, existing_username)
        
        # API Key
        ttk.Label(main_frame, text="API Key:").grid(row=3, column=0, sticky='e', padx=(0, s(10)))
        self.apikey_entry = ttk.Entry(main_frame, width=s(35), show='•', font=('Segoe UI', default_font_size))
        self.apikey_entry.grid(row=3, column=1, pady=s(5), sticky='ew')
        
        # Show/Hide API key checkbox
        self.show_key_var = tk.BooleanVar()
        show_key_cb = ttk.Checkbutton(main_frame, text="Show API key", 
                                       variable=self.show_key_var, 
                                       command=self._toggle_key_visibility)
        show_key_cb.grid(row=4, column=1, sticky='w', pady=(0, s(10)))
        
        # Buttons
        button_frame = ttk.Frame(main_frame)
        button_frame.grid(row=5, column=0, columnspan=2, pady=(s(10), 0))
        
        ttk.Button(button_frame, text="Cancel", command=self._cancel).pack(side='left', padx=s(5))
        ttk.Button(button_frame, text="Save & Continue", command=self._submit).pack(side='left', padx=s(5))
        
        # Make column 1 expandable
        main_frame.columnconfigure(1, weight=1)
        
        # Bind Enter key
        self.root.bind('<Return>', lambda e: self._submit())
        self.root.bind('<Escape>', lambda e: self._cancel())
        
        # Focus on first empty field
        if existing_username:
            self.apikey_entry.focus()
        else:
            self.username_entry.focus()
        
        # Let tkinter calculate size, then center
        self.root.update_idletasks()
        
        # Add some padding to calculated size
        width = self.root.winfo_reqwidth() + s(40)
        height = self.root.winfo_reqheight() + s(20)
        
        # Center on screen
        screen_width = self.root.winfo_screenwidth()
        screen_height = self.root.winfo_screenheight()
        x = (screen_width - width) // 2
        y = (screen_height - height) // 2
        
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(width, height)
        self.root.resizable(False, False)
    
    def _toggle_key_visibility(self):
        if self.show_key_var.get():
            self.apikey_entry.config(show='')
        else:
            self.apikey_entry.config(show='•')
    
    def _open_url(self, url: str):
        """Open a URL in the default web browser."""
        import webbrowser
        webbrowser.open(url)
    
    def _submit(self):
        username = self.username_entry.get().strip()
        api_key = self.apikey_entry.get().strip()
        
        if not username:
            messagebox.showerror("Error", "Please enter your username")
            self.username_entry.focus()
            return
        
        if not api_key:
            messagebox.showerror("Error", "Please enter your API key")
            self.apikey_entry.focus()
            return
        
        self.result = (username, api_key)
        self.root.destroy()
    
    def _cancel(self):
        self.result = None
        self.root.destroy()
    
    def show(self) -> tuple[str, str] | None:
        """Display the dialog and return (username, api_key) or None if cancelled."""
        self.root.mainloop()
        return self.result


def get_credentials(reset: bool = False) -> tuple[str, str]:
    """
    Get credentials, prompting with GUI if needed.
    Returns (username, api_key) or exits if cancelled.
    """
    if reset:
        CredentialManager.clear_credentials()
        print("Credentials cleared.")
    
    # Try to get stored credentials
    creds = CredentialManager.get_credentials()
    
    if creds and creds[0] and creds[1]:
        print(f"Using stored credentials for user: {creds[0]}")
        return creds
    
    # Need to prompt for credentials
    print("No stored credentials found. Opening login dialog...")
    
    existing_username = creds[0] if creds else ""
    dialog = CredentialDialog(existing_username)
    result = dialog.show()
    
    if not result:
        print("Login cancelled.")
        sys.exit(0)
    
    username, api_key = result
    
    # Save credentials
    CredentialManager.save_credentials(username, api_key)
    storage_type = "system keyring" if KEYRING_AVAILABLE else f"local file ({CREDS_FILE})"
    print(f"Credentials saved to {storage_type}")
    
    return (username, api_key)


async def fetch_want_to_play_list(username: str, api_key: str, use_cache: bool = True) -> list:
    """Fetch the user's Want to Play list from RetroAchievements API."""
    cache_path = Path(RA_CACHE_FILE)
    
    if use_cache and cache_path.exists():
        with open(cache_path, 'r') as f:
            cached = json.load(f)
            if cached.get('username', '').lower() == username.lower():
                print(f"Using cached Want to Play list ({len(cached['games'])} games)")
                print("  (Use --refresh to re-fetch from RetroAchievements)")
                return cached['games']
    
    print(f"Fetching Want to Play list for '{username}' from RetroAchievements...")
    
    all_games = []
    offset = 0
    page_size = 500
    
    async with aiohttp.ClientSession() as session:
        while True:
            url = f"{RA_API_BASE}/API_GetUserWantToPlayList.php"
            params = {'y': api_key, 'u': username, 'c': page_size, 'o': offset}
            
            async with session.get(url, params=params) as resp:
                if resp.status == 401:
                    print("Error: Invalid API key or unauthorized")
                    print("Use --reset-creds to re-enter your credentials")
                    sys.exit(1)
                elif resp.status != 200:
                    print(f"Error: API returned status {resp.status}")
                    sys.exit(1)
                
                data = await resp.json()
            
            results = data.get('Results', [])
            total = data.get('Total', 0)
            
            if not results:
                break
            
            all_games.extend(results)
            print(f"  Fetched {len(all_games)}/{total} games...")
            
            offset += page_size
            if offset >= total:
                break
    
    print(f"  Total: {len(all_games)} games in Want to Play list")
    
    with open(cache_path, 'w') as f:
        json.dump({'username': username, 'games': all_games}, f)
    
    return all_games


def convert_ra_to_dataframe(ra_games: list) -> pd.DataFrame:
    """Convert RetroAchievements game list to DataFrame."""
    rows = []
    for game in ra_games:
        rows.append({
            'Title': game['Title'],
            'System': game['ConsoleName'],
            'Achievements': game.get('AchievementsPublished', 0),
            'Points': game.get('PointsTotal', 0),
            'RA_ID': game['ID'],
            # HLTB times
            'HLTB_Beat': None,
            'HLTB_Complete': None,
            # RA actual times (from player data)
            'RA_Beat': None,
            'RA_Master': None,
            'RA_Players': None,
            # Efficiency
            'Points_Per_Hour': None,
            'Comments': None
        })
    return pd.DataFrame(rows)


def calculate_efficiency(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate efficiency metrics for prioritizing games.
    
    Points_Per_Hour = Points / RA_Master time (or HLTB_Complete as fallback)
    Higher = more "rewarding" games (more points for less time)
    """
    for idx, row in df.iterrows():
        # Prefer RA mastery time (actual data), fallback to HLTB
        time_val = row.get('RA_Master') or row.get('HLTB_Complete') or row.get('HLTB_Beat')
        points = row.get('Points', 0)
        
        if time_val and time_val > 0 and points and points > 0:
            df.at[idx, 'Points_Per_Hour'] = round(points / time_val, 1)
    
    return df


async def search_game(game_title: str, system: str = None) -> dict:
    """Search for a game on HowLongToBeat with improved matching."""
    result = {
        'beat': None, 'complete': None, 'hltb_name': None,
        'similarity': 0.0, 'error': None, 'comment': None
    }
    
    # Aggressive title normalization for better HLTB matching
    clean_title = normalize_title(game_title)
    
    # Build list of search variants to try
    search_variants = []
    
    # Handle pipe-separated alternate titles (e.g., "Game A | Game B")
    if ' | ' in clean_title:
        parts = [p.strip() for p in clean_title.split(' | ')]
        search_variants.extend(parts)
    else:
        search_variants.append(clean_title)
    
    # Add variant without "Version" suffix (Pokemon FireRed Version -> Pokemon FireRed)
    for variant in search_variants.copy():
        if variant.endswith(' Version'):
            search_variants.append(variant[:-8])
    
    # Add base title (before colon/dash) as lower priority fallback
    for sep in [':', ' - ']:
        if sep in clean_title:
            base = clean_title.split(sep)[0].strip()
            if base not in search_variants:
                search_variants.append(base)
            break
    
    # Map RA system names to HLTB-friendly search terms
    system_map = {
        'Genesis/Mega Drive': 'Genesis',
        'SNES/Super Famicom': 'SNES',
        'NES/Famicom': 'NES',
        'Game Boy Advance': 'GBA',
        'Game Boy Color': 'GBC',
        'Game Boy': 'Game Boy',
        'Nintendo 64': 'N64',
        'Nintendo DS': 'DS',
        'PlayStation': 'PlayStation',
        'PlayStation 2': 'PS2',
        'PlayStation Portable': 'PSP',
        'GameCube': 'GameCube',
    }
    mapped_system = system_map.get(system, system) if system else None
    
    try:
        hltb = HowLongToBeat(0.0)  # Get ALL results, we'll filter ourselves
        
        best_match = None
        best_score = -999
        
        for search_term in search_variants:
            results = await hltb.async_search(search_term, search_modifiers=SearchModifiers.HIDE_DLC)
            
            if not results:
                continue
            
            for game in results:
                score = 0
                game_name_lower = game.game_name.lower().strip()
                search_lower = search_term.lower().strip()
                
                # Exact match is best
                if game_name_lower == search_lower:
                    score = 1000
                # Check if search term is contained in game name or vice versa
                elif search_lower in game_name_lower or game_name_lower in search_lower:
                    score = 500 + (game.similarity * 100)
                else:
                    score = game.similarity * 100
                
                # Penalty for numbered sequels when we didn't ask for one
                # e.g., searching "Aladdin" shouldn't match "Aladdin III"
                search_has_number = bool(re.search(r'\b(II|III|IV|V|VI|VII|VIII|IX|X|\d+)\b', search_term, re.IGNORECASE))
                result_has_number = bool(re.search(r'\b(II|III|IV|V|VI|VII|VIII|IX|X|\d+)\b', game.game_name, re.IGNORECASE))
                
                if result_has_number and not search_has_number:
                    score -= 300
                
                # Penalty for results with extra significant words
                search_words = set(search_lower.split())
                result_words = set(game_name_lower.split())
                extra_words = result_words - search_words
                common_words = {'the', 'a', 'an', 'of', 'and', '&', '-', 'edition', 'remastered', 'hd', 'definitive'}
                significant_extra = extra_words - common_words
                score -= len(significant_extra) * 15
                
                if score > best_score:
                    best_score = score
                    best_match = game
        
        if not best_match:
            result['error'] = 'No results'
            result['comment'] = 'No HLTB match found'
            return result
        
        result['hltb_name'] = best_match.game_name
        result['similarity'] = best_match.similarity
        
        # Get times
        if best_match.main_story and best_match.main_story > 0:
            result['beat'] = round(best_match.main_story, 1)
        elif best_match.main_extra and best_match.main_extra > 0:
            result['beat'] = round(best_match.main_extra, 1)
        
        if best_match.completionist and best_match.completionist > 0:
            result['complete'] = round(best_match.completionist, 1)
        
        # Generate match quality comment
        clean_lower = clean_title.lower().strip()
        match_lower = best_match.game_name.lower().strip()
        
        # Check all variants for exact match
        is_exact = any(v.lower().strip() == match_lower for v in search_variants)
        
        if is_exact:
            result['comment'] = None
        elif best_score >= 500:
            result['comment'] = f"Fuzzy match: {best_match.game_name}"
        elif best_score >= 200:
            result['comment'] = f"Loose match ({best_match.similarity:.0%}): {best_match.game_name}"
        else:
            result['comment'] = f"Poor match ({best_match.similarity:.0%}): {best_match.game_name} - VERIFY"
        
    except Exception as e:
        result['error'] = str(e)
        result['comment'] = f"Error: {str(e)}"
    
    return result


async def process_single_game(
    idx: int, 
    row: pd.Series, 
    total: int,
    session: aiohttp.ClientSession, 
    api_key: str,
    semaphore: asyncio.Semaphore,
    progress: dict,
    progress_path: Path
) -> dict:
    """Process a single game with semaphore-controlled concurrency."""
    title = row['Title']
    system = row.get('System', '')
    ra_id = row.get('RA_ID')
    cache_key = f"{title}|{system}"
    
    async with semaphore:
        # Small delay to be polite
        await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
        
        print(f"[{idx + 1}/{total}] {title} ({system})...", end=" ", flush=True)
        
        # Fetch HLTB data
        hltb_result = await search_game(title, system)
        
        # Fetch RA progression data
        ra_result = {}
        if ra_id:
            ra_result = await fetch_game_progression(session, api_key, ra_id)
        
        # Merge results
        combined = {**hltb_result, **ra_result}
        
        # Print result
        if hltb_result['hltb_name'] or ra_result.get('ra_master_time'):
            parts = []
            if hltb_result['beat']:
                parts.append(f"HLTB: {hltb_result['beat']}h")
            if ra_result.get('ra_master_time'):
                parts.append(f"RA Master: {ra_result['ra_master_time']}h")
            
            match_name = hltb_result.get('hltb_name', 'N/A')
            times_str = f"[{', '.join(parts)}]" if parts else '[No times]'
            
            # Color based on match quality
            if hltb_result['hltb_name'] and hltb_result['similarity'] < 0.6:
                print(f"→ {Colors.ORANGE}{match_name}{Colors.RESET} {times_str}")
            else:
                print(f"→ {match_name} {times_str}")
        else:
            print(f"{Colors.RED}✗ {hltb_result['error'] or 'No data'}{Colors.RESET}")
        
        return {
            'idx': idx,
            'cache_key': cache_key,
            'hltb_result': hltb_result,
            'ra_result': ra_result,
            'combined': combined
        }


async def process_games(df: pd.DataFrame, excel_path: Path, api_key: str):
    """Process all games with concurrent HLTB lookups and RA progression data."""
    progress = {}
    progress_path = Path(PROGRESS_FILE)
    if progress_path.exists():
        with open(progress_path, 'r') as f:
            progress = json.load(f)
        print(f"Resuming from progress file ({len(progress)} games cached)")
    
    total = len(df)
    skipped = 0
    from_cache = 0
    
    # First pass: apply cached results
    for idx, row in df.iterrows():
        title = row['Title']
        system = row.get('System', '')
        cache_key = f"{title}|{system}"
        
        # Skip if already has all data in dataframe
        if (pd.notna(row.get('HLTB_Beat')) and pd.notna(row.get('HLTB_Complete')) and
            pd.notna(row.get('RA_Master'))):
            skipped += 1
            continue
        
        # Apply from progress cache
        if cache_key in progress:
            cached = progress[cache_key]
            if cached.get('beat') is not None:
                df.at[idx, 'HLTB_Beat'] = cached['beat']
            if cached.get('complete') is not None:
                df.at[idx, 'HLTB_Complete'] = cached['complete']
            if cached.get('ra_beat_time') is not None:
                df.at[idx, 'RA_Beat'] = cached['ra_beat_time']
            if cached.get('ra_master_time') is not None:
                df.at[idx, 'RA_Master'] = cached['ra_master_time']
            if cached.get('distinct_players') is not None:
                df.at[idx, 'RA_Players'] = cached['distinct_players']
            if cached.get('comment') is not None:
                df.at[idx, 'Comments'] = cached['comment']
            from_cache += 1
    
    # Build list of games that need processing
    games_to_process = []
    for idx, row in df.iterrows():
        title = row['Title']
        system = row.get('System', '')
        cache_key = f"{title}|{system}"
        
        if cache_key not in progress and not (
            pd.notna(row.get('HLTB_Beat')) and pd.notna(row.get('HLTB_Complete')) and
            pd.notna(row.get('RA_Master'))):
            games_to_process.append((idx, row))
    
    print(f"\nProcessing {total} games ({from_cache} from cache, {skipped} skipped, {len(games_to_process)} to fetch)...\n")
    print(f"Using {MAX_CONCURRENT_REQUESTS} concurrent requests")
    print("-" * 70)
    
    if games_to_process:
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_REQUESTS)
        
        async with aiohttp.ClientSession() as session:
            # Process in batches to allow periodic saves
            batch_size = 25
            for batch_start in range(0, len(games_to_process), batch_size):
                batch = games_to_process[batch_start:batch_start + batch_size]
                
                tasks = [
                    process_single_game(
                        idx, row, total, session, api_key, 
                        semaphore, progress, progress_path
                    )
                    for idx, row in batch
                ]
                
                results = await asyncio.gather(*tasks, return_exceptions=True)
                
                # Apply results to dataframe and save progress
                for result in results:
                    if isinstance(result, Exception):
                        print(f"Error: {result}")
                        continue
                    
                    idx = result['idx']
                    hltb_result = result['hltb_result']
                    ra_result = result['ra_result']
                    
                    if hltb_result['beat'] is not None:
                        df.at[idx, 'HLTB_Beat'] = hltb_result['beat']
                    if hltb_result['complete'] is not None:
                        df.at[idx, 'HLTB_Complete'] = hltb_result['complete']
                    if ra_result.get('ra_beat_time') is not None:
                        df.at[idx, 'RA_Beat'] = ra_result['ra_beat_time']
                    if ra_result.get('ra_master_time') is not None:
                        df.at[idx, 'RA_Master'] = ra_result['ra_master_time']
                    if ra_result.get('distinct_players') is not None:
                        df.at[idx, 'RA_Players'] = ra_result['distinct_players']
                    if hltb_result['comment'] is not None:
                        df.at[idx, 'Comments'] = hltb_result['comment']
                    
                    progress[result['cache_key']] = result['combined']
                
                # Save progress after each batch
                with open(progress_path, 'w') as f:
                    json.dump(progress, f)
                df.to_excel(excel_path, index=False)
                
                if batch_start + batch_size < len(games_to_process):
                    print(f"    [Saved progress: {batch_start + len(batch)}/{len(games_to_process)} fetched]")
    
    df.to_excel(excel_path, index=False)
    
    # Calculate efficiency metrics
    df = calculate_efficiency(df)
    df.to_excel(excel_path, index=False)
    
    # Summary
    print("-" * 70)
    print(f"\nComplete!")
    print(f"  Total games: {total}")
    print(f"  From cache: {from_cache}")
    print(f"  Fetched: {len(games_to_process)}")
    print(f"  Skipped (already had data): {skipped}")
    print(f"  Games with HLTB data: {df['HLTB_Beat'].notna().sum()}")
    print(f"  Games with RA mastery data: {df['RA_Master'].notna().sum()}")
    
    comments = df['Comments'].fillna('')
    exact = (comments == '').sum() - df['HLTB_Beat'].isna().sum()
    fuzzy = comments.str.contains('Fuzzy match', case=False).sum()
    loose = comments.str.contains('Loose match', case=False).sum()
    poor = comments.str.contains('Poor match', case=False).sum()
    none = comments.str.contains('No HLTB match', case=False).sum()
    
    print(f"\nHLTB Match quality:")
    print(f"  Exact matches: {exact}")
    print(f"  Fuzzy matches: {fuzzy}")
    print(f"  Loose matches: {loose}")
    print(f"  Poor matches (needs review): {poor}")
    print(f"  No match found: {none}")
    
    # Time comparison
    if df['RA_Master'].notna().any() and df['HLTB_Complete'].notna().any():
        both_have_data = df[(df['RA_Master'].notna()) & (df['HLTB_Complete'].notna())]
        if len(both_have_data) > 0:
            avg_ra = both_have_data['RA_Master'].mean()
            avg_hltb = both_have_data['HLTB_Complete'].mean()
            ratio = avg_ra / avg_hltb if avg_hltb > 0 else 0
            print(f"\nRA vs HLTB Comparison ({len(both_have_data)} games with both):")
            print(f"  Avg HLTB Completionist: {avg_hltb:.1f} hours")
            print(f"  Avg RA Mastery: {avg_ra:.1f} hours")
            print(f"  RA takes {ratio:.1f}x longer on average")
    
    if df['RA_Master'].notna().any():
        print(f"\nRA Mastery Time estimates:")
        print(f"  Total Mastery time: {df['RA_Master'].sum():.1f} hours ({df['RA_Master'].sum()/24:.1f} days)")
        print(f"  Average Mastery: {df['RA_Master'].mean():.1f} hours")
    
    print(f"\nGames by system:")
    for system, count in df.groupby('System').size().sort_values(ascending=False).head(10).items():
        print(f"  {system}: {count}")
    
    # Show most efficient games (best points per hour based on RA mastery time)
    if df['Points_Per_Hour'].notna().any():
        print(f"\nMost Efficient Games (highest points per hour of mastery):")
        # Ensure numeric dtype for nlargest
        df['Points_Per_Hour'] = pd.to_numeric(df['Points_Per_Hour'], errors='coerce')
        efficient = df[df['Points_Per_Hour'].notna()].nlargest(10, 'Points_Per_Hour')
        for _, row in efficient.iterrows():
            time_used = row['RA_Master'] or row['HLTB_Complete'] or row['HLTB_Beat']
            time_src = "RA" if pd.notna(row['RA_Master']) else "HLTB"
            print(f"  {row['Points_Per_Hour']:.1f} pts/hr - {row['Title']} ({row['Points']} pts, {time_used:.1f}h {time_src})")
    
    print(f"\nResults saved to: {excel_path}")
    
    missing = df[df['HLTB_Beat'].isna() & df['RA_Master'].isna()]['Title'].tolist()
    if missing:
        print(f"\nGames without any time data ({len(missing)}):")
        for title in missing[:15]:
            print(f"  - {title}")
        if len(missing) > 15:
            print(f"  ... and {len(missing) - 15} more")
    
    return df


async def lookup_single_game(api_key: str):
    """Look up a single game by name or RA ID."""
    print("\n" + "=" * 70)
    print("Single Game Lookup")
    print("=" * 70)
    
    query = input("\nEnter game name or RA ID (or 'back' to return): ").strip()
    
    if query.lower() == 'back' or not query:
        return
    
    # Check if it's an RA ID (numeric)
    if query.isdigit():
        ra_id = int(query)
        print(f"\nLooking up RA ID: {ra_id}...")
        
        async with aiohttp.ClientSession() as session:
            # Fetch game info from RA
            url = f"{RA_API_BASE}/API_GetGame.php"
            params = {'y': api_key, 'i': ra_id}
            
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    print(f"{Colors.RED}Error: Could not fetch game data{Colors.RESET}")
                    return
                data = await resp.json()
            
            if not data or not data.get('Title'):
                print(f"{Colors.RED}Error: Game not found with ID {ra_id}{Colors.RESET}")
                return
            
            title = data['Title']
            system = data.get('ConsoleName', 'Unknown')
            points = data.get('points_total', 0)
            achievements = data.get('achievements_published', 0)
            
            print(f"\nFound: {title} ({system})")
            print(f"  Achievements: {achievements} | Points: {points}")
            
            # Get RA progression data
            ra_result = await fetch_game_progression(session, api_key, ra_id)
        
        # Search HLTB
        print(f"  Searching HLTB...")
        hltb_result = await search_game(title, system)
        
    else:
        # Search by name
        title = query
        print(f"\nSearching for: {title}...")
        
        # Search HLTB first
        hltb_result = await search_game(title, None)
        ra_result = {}
    
    # Display results
    print("\n" + "-" * 50)
    print(f"Results for: {title}")
    print("-" * 50)
    
    if hltb_result.get('hltb_name'):
        match_note = ""
        if hltb_result.get('comment'):
            match_note = f" ({Colors.ORANGE}{hltb_result['comment']}{Colors.RESET})"
        print(f"\nHLTB Match: {hltb_result['hltb_name']}{match_note}")
        if hltb_result.get('beat'):
            print(f"  Beat: {hltb_result['beat']} hours")
        if hltb_result.get('complete'):
            print(f"  Completionist: {hltb_result['complete']} hours")
    else:
        print(f"\n{Colors.RED}No HLTB match found{Colors.RESET}")
    
    if ra_result.get('ra_master_time'):
        print(f"\nRA Player Data:")
        if ra_result.get('ra_beat_time'):
            print(f"  Median Beat: {ra_result['ra_beat_time']} hours")
        print(f"  Median Mastery: {ra_result['ra_master_time']} hours")
        if ra_result.get('distinct_players'):
            print(f"  Distinct Players: {ra_result['distinct_players']:,}")
    
    input("\nPress Enter to continue...")


def show_backlog_summary(output_path: Path):
    """Display summary statistics from existing Excel file."""
    print("\n" + "=" * 70)
    print("Backlog Summary")
    print("=" * 70)
    
    if not output_path.exists():
        print(f"\n{Colors.RED}No data file found at {output_path}{Colors.RESET}")
        print("Run a scan first to generate data.")
        input("\nPress Enter to continue...")
        return
    
    df = pd.read_excel(output_path)
    
    # Ensure numeric columns
    for col in ['RA_Master', 'HLTB_Complete', 'HLTB_Beat', 'Points', 'Points_Per_Hour']:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors='coerce')
    
    print(f"\nTotal games: {len(df)}")
    print(f"Games with RA mastery data: {df['RA_Master'].notna().sum()}")
    print(f"Games with HLTB data: {df['HLTB_Beat'].notna().sum()}")
    
    if df['RA_Master'].notna().any():
        total_hours = df['RA_Master'].sum()
        avg_hours = df['RA_Master'].mean()
        print(f"\nRA Mastery Time:")
        print(f"  Total: {total_hours:,.1f} hours ({total_hours/24:,.1f} days)")
        print(f"  Average: {avg_hours:.1f} hours per game")
    
    if df['Points'].notna().any():
        total_points = df['Points'].sum()
        print(f"\nTotal Points Available: {total_points:,}")
    
    # Games by system
    print(f"\nGames by System:")
    for system, count in df.groupby('System').size().sort_values(ascending=False).head(10).items():
        system_hours = df[df['System'] == system]['RA_Master'].sum()
        if pd.notna(system_hours) and system_hours > 0:
            print(f"  {system}: {count} games ({system_hours:.1f}h)")
        else:
            print(f"  {system}: {count} games")
    
    # Top 5 longest games
    if df['RA_Master'].notna().any():
        print(f"\nTop 5 Longest Games (by RA Mastery):")
        longest = df[df['RA_Master'].notna()].nlargest(5, 'RA_Master')
        for _, row in longest.iterrows():
            print(f"  {row['RA_Master']:.1f}h - {row['Title']}")
    
    # Top 5 most efficient
    if df['Points_Per_Hour'].notna().any():
        print(f"\nTop 5 Most Efficient (points per hour):")
        efficient = df[df['Points_Per_Hour'].notna()].nlargest(5, 'Points_Per_Hour')
        for _, row in efficient.iterrows():
            print(f"  {row['Points_Per_Hour']:.1f} pts/hr - {row['Title']}")
    
    input("\nPress Enter to continue...")


def estimate_completion_time(output_path: Path):
    """Estimate how long it will take to complete the backlog."""
    print("\n" + "=" * 70)
    print("Completion Time Estimator")
    print("=" * 70)
    
    if not output_path.exists():
        print(f"\n{Colors.RED}No data file found at {output_path}{Colors.RESET}")
        print("Run a scan first to generate data.")
        input("\nPress Enter to continue...")
        return
    
    df = pd.read_excel(output_path)
    df['RA_Master'] = pd.to_numeric(df['RA_Master'], errors='coerce')
    
    total_hours = df['RA_Master'].sum()
    
    if pd.isna(total_hours) or total_hours == 0:
        print(f"\n{Colors.RED}No mastery time data available{Colors.RESET}")
        input("\nPress Enter to continue...")
        return
    
    print(f"\nTotal backlog: {total_hours:,.1f} hours ({len(df)} games)")
    
    try:
        hours_per_week = float(input("\nHow many hours per week can you play? ").strip())
        if hours_per_week <= 0:
            print("Please enter a positive number.")
            input("\nPress Enter to continue...")
            return
    except ValueError:
        print("Invalid number.")
        input("\nPress Enter to continue...")
        return
    
    weeks = total_hours / hours_per_week
    years = weeks / 52
    
    print(f"\n" + "-" * 50)
    print(f"At {hours_per_week} hours per week:")
    print(f"  Weeks to complete: {weeks:,.1f}")
    print(f"  Months to complete: {weeks/4.33:,.1f}")
    print(f"  Years to complete: {years:,.2f}")
    
    from datetime import datetime, timedelta
    completion_date = datetime.now() + timedelta(weeks=weeks)
    print(f"\n  Estimated completion: {completion_date.strftime('%B %Y')}")
    
    if years > 5:
        print(f"\n  {Colors.ORANGE}That's a lot of gaming! Maybe prioritize by efficiency?{Colors.RESET}")
    elif years > 1:
        print(f"\n  {Colors.ORANGE}A solid multi-year project!{Colors.RESET}")
    else:
        print(f"\n  {Colors.GREEN}Very achievable!{Colors.RESET}")
    
    input("\nPress Enter to continue...")


async def run_scan(username: str, api_key: str, output_path: Path, fresh: bool = False, 
                   systems_filter: list = None, systems_exclude: list = None):
    """Run a scan (update or fresh)."""
    print("\n" + "=" * 70)
    print("RetroAchievements + HowLongToBeat Scraper")
    print("=" * 70)
    
    if not fresh and output_path.exists():
        print(f"\nFound existing {output_path}")
        print("Loading and checking for new games...")
        df = pd.read_excel(output_path)
        
        ra_games = await fetch_want_to_play_list(username, api_key, use_cache=False)
        ra_df = convert_ra_to_dataframe(ra_games)
        
        existing_ids = set(df['RA_ID'].dropna().astype(int)) if 'RA_ID' in df.columns else set()
        new_games = ra_df[~ra_df['RA_ID'].isin(existing_ids)]
        
        if len(new_games) > 0:
            print(f"Found {len(new_games)} new games to add!")
            df = pd.concat([df, new_games], ignore_index=True)
        else:
            print("No new games found in Want to Play list")
    else:
        ra_games = await fetch_want_to_play_list(username, api_key, use_cache=False)
        
        if not ra_games:
            print("No games found in Want to Play list!")
            print("Make sure your Want to Play list is accessible.")
            return
        
        df = convert_ra_to_dataframe(ra_games)
    
    # Apply system filters
    if systems_filter:
        df = df[df['System'].isin(systems_filter)]
        print(f"Filtered to systems: {', '.join(systems_filter)} ({len(df)} games)")
    
    if systems_exclude:
        df = df[~df['System'].isin(systems_exclude)]
        print(f"Excluded systems: {', '.join(systems_exclude)} ({len(df)} games remaining)")
    
    if len(df) == 0:
        print("No games to process after filtering!")
        return
    
    await process_games(df, output_path, api_key)


def get_system_selection(df_or_path, mode='filter'):
    """Let user select systems to filter or exclude."""
    if isinstance(df_or_path, Path):
        if not df_or_path.exists():
            return None
        df = pd.read_excel(df_or_path)
    else:
        df = df_or_path
    
    systems = df['System'].value_counts()
    
    print(f"\nAvailable systems:")
    system_list = list(systems.items())
    for i, (system, count) in enumerate(system_list, 1):
        print(f"  {i}. {system} ({count} games)")
    
    action = "include" if mode == 'filter' else "exclude"
    print(f"\nEnter numbers to {action} (comma-separated), or 'all' for all, or 'back' to cancel:")
    
    selection = input("> ").strip().lower()
    
    if selection == 'back' or not selection:
        return None
    
    if selection == 'all':
        return [s for s, _ in system_list]
    
    try:
        indices = [int(x.strip()) - 1 for x in selection.split(',')]
        selected = [system_list[i][0] for i in indices if 0 <= i < len(system_list)]
        return selected if selected else None
    except (ValueError, IndexError):
        print("Invalid selection")
        return None


def export_to_csv(output_path: Path):
    """Export Excel data to CSV."""
    if not output_path.exists():
        print(f"\n{Colors.RED}No data file found at {output_path}{Colors.RESET}")
        input("\nPress Enter to continue...")
        return
    
    csv_path = output_path.with_suffix('.csv')
    df = pd.read_excel(output_path)
    df.to_csv(csv_path, index=False)
    print(f"\n{Colors.GREEN}Exported to: {csv_path}{Colors.RESET}")
    input("\nPress Enter to continue...")


def print_menu(username: str = None):
    """Print the main menu."""
    print("\n" + "=" * 70)
    print("  RA Backlog Timer - Main Menu")
    print("=" * 70)
    
    if username:
        print(f"  Logged in as: {Colors.GREEN}{username}{Colors.RESET}")
    else:
        print(f"  {Colors.ORANGE}Not logged in{Colors.RESET}")
    
    print("\n  SCANNING")
    print("    1. Update scan (check for new games)")
    print("    2. Fresh scan (re-fetch everything)")
    print("    3. Scan specific systems only")
    print("    4. Scan excluding specific systems")
    
    print("\n  TOOLS")
    print("    5. Look up single game")
    print("    6. View backlog summary")
    print("    7. Estimate completion time")
    print("    8. Export to CSV")
    
    print("\n  ACCOUNT")
    print("    9. View username")
    print("   10. Clear cached credentials")
    
    print("\n    0. Exit")
    print("=" * 70)


async def interactive_menu(output_path: Path):
    """Run the interactive menu."""
    username = None
    api_key = None
    
    # Try to load existing credentials
    creds = CredentialManager.get_credentials()
    if creds and creds[0] and creds[1]:
        username, api_key = creds
    
    while True:
        print_menu(username)
        
        choice = input("\nEnter choice: ").strip()
        
        if choice == '0':
            print("\nGoodbye!")
            break
        
        elif choice == '1':
            # Update scan
            if not username:
                username, api_key = get_credentials()
            await run_scan(username, api_key, output_path, fresh=False)
        
        elif choice == '2':
            # Fresh scan
            if not username:
                username, api_key = get_credentials()
            
            # Warn about what will be deleted/overwritten
            files_to_clear = []
            progress_path = Path(PROGRESS_FILE)
            ra_cache_path = Path(RA_CACHE_FILE)
            
            if progress_path.exists():
                files_to_clear.append(f"  - {PROGRESS_FILE} (HLTB lookup cache)")
            if ra_cache_path.exists():
                files_to_clear.append(f"  - {RA_CACHE_FILE} (RA game list cache)")
            if output_path.exists():
                files_to_clear.append(f"  - {output_path} (will be overwritten)")
            
            if files_to_clear:
                print(f"\n{Colors.ORANGE}WARNING: Fresh scan will delete/overwrite:{Colors.RESET}")
                for f in files_to_clear:
                    print(f)
                confirm = input("\nAre you sure? (y/n): ").strip().lower()
                if confirm != 'y':
                    print("Cancelled.")
                    continue
                
                # Clear the cache files
                if progress_path.exists():
                    progress_path.unlink()
                if ra_cache_path.exists():
                    ra_cache_path.unlink()
                print("Cache files cleared.")
            
            await run_scan(username, api_key, output_path, fresh=True)
        
        elif choice == '3':
            # Scan specific systems
            if not username:
                username, api_key = get_credentials()
            
            # Need to fetch game list first to show systems
            ra_games = await fetch_want_to_play_list(username, api_key, use_cache=True)
            temp_df = convert_ra_to_dataframe(ra_games)
            
            systems = get_system_selection(temp_df, mode='filter')
            if systems:
                await run_scan(username, api_key, output_path, fresh=False, systems_filter=systems)
        
        elif choice == '4':
            # Scan excluding systems
            if not username:
                username, api_key = get_credentials()
            
            ra_games = await fetch_want_to_play_list(username, api_key, use_cache=True)
            temp_df = convert_ra_to_dataframe(ra_games)
            
            systems = get_system_selection(temp_df, mode='exclude')
            if systems:
                await run_scan(username, api_key, output_path, fresh=False, systems_exclude=systems)
        
        elif choice == '5':
            # Single game lookup
            if not username:
                username, api_key = get_credentials()
            await lookup_single_game(api_key)
        
        elif choice == '6':
            # Backlog summary
            show_backlog_summary(output_path)
        
        elif choice == '7':
            # Completion estimate
            estimate_completion_time(output_path)
        
        elif choice == '8':
            # Export to CSV
            export_to_csv(output_path)
        
        elif choice == '9':
            # View username
            if username:
                print(f"\n  Current username: {Colors.GREEN}{username}{Colors.RESET}")
            else:
                print(f"\n  {Colors.ORANGE}No credentials stored{Colors.RESET}")
            input("\nPress Enter to continue...")
        
        elif choice == '10':
            # Clear credentials
            confirm = input("\nClear stored credentials? (y/n): ").strip().lower()
            if confirm == 'y':
                CredentialManager.clear_credentials()
                username = None
                api_key = None
                print(f"{Colors.GREEN}Credentials cleared.{Colors.RESET}")
            input("\nPress Enter to continue...")
        
        else:
            print(f"\n{Colors.RED}Invalid choice{Colors.RESET}")


async def main_async(args):
    """Main async entry point."""
    output_path = Path(args.output)
    
    if args.menu or (not args.refresh and not args.reset_creds and not hasattr(args, 'run_direct')):
        # Interactive menu mode
        await interactive_menu(output_path)
    else:
        # Direct execution mode (legacy CLI)
        username, api_key = get_credentials(reset=args.reset_creds)
        await run_scan(username, api_key, output_path, fresh=args.refresh)


def main():
    parser = argparse.ArgumentParser(
        description='Fetch RetroAchievements Want to Play list and get HowLongToBeat times'
    )
    parser.add_argument('-o', '--output', default='HowLongToBeat.xlsx',
                        help='Output Excel file (default: HowLongToBeat.xlsx)')
    parser.add_argument('--refresh', action='store_true',
                        help='Re-fetch Want to Play list from RetroAchievements')
    parser.add_argument('--reset-creds', action='store_true',
                        help='Clear stored credentials and prompt again')
    parser.add_argument('--menu', action='store_true',
                        help='Show interactive menu (default behavior)')
    parser.add_argument('--no-menu', action='store_true',
                        help='Run scan directly without menu')
    
    args = parser.parse_args()
    
    # If --no-menu is specified, mark for direct run
    if args.no_menu:
        args.run_direct = True
        args.menu = False
    
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()