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
DELAY_BETWEEN_REQUESTS = 0.5
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
        
        # Info text
        info_text = "Enter your RA username and API key.\nGet your API key from: retroachievements.org/settings"
        info_label = ttk.Label(main_frame, text=info_text, style='Info.TLabel')
        info_label.grid(row=1, column=0, columnspan=2, pady=(0, s(15)))
        
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


async def process_games(df: pd.DataFrame, excel_path: Path, api_key: str):
    """Process all games with HLTB lookups and RA progression data."""
    progress = {}
    progress_path = Path(PROGRESS_FILE)
    if progress_path.exists():
        with open(progress_path, 'r') as f:
            progress = json.load(f)
        print(f"Resuming from progress file ({len(progress)} games cached)")
    
    total = len(df)
    updated = 0
    skipped = 0
    
    print(f"\nProcessing {total} games...\n")
    print("-" * 70)
    
    async with aiohttp.ClientSession() as session:
        for idx, row in df.iterrows():
            title = row['Title']
            system = row.get('System', '')
            ra_id = row.get('RA_ID')
            
            # Skip if already has all data
            if (pd.notna(row.get('HLTB_Beat')) and pd.notna(row.get('HLTB_Complete')) and
                pd.notna(row.get('RA_Master'))):
                skipped += 1
                continue
            
            cache_key = f"{title}|{system}"
            
            # Check progress cache
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
                updated += 1
                continue
            
            print(f"[{idx + 1}/{total}] {title} ({system})...", end=" ", flush=True)
            
            # Fetch HLTB data
            hltb_result = await search_game(title, system)
            
            # Fetch RA progression data
            ra_result = {}
            if ra_id:
                ra_result = await fetch_game_progression(session, api_key, ra_id)
            
            # Merge results for caching
            combined = {**hltb_result, **ra_result}
            
            # Update dataframe
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
            
            # Save to progress cache
            progress[cache_key] = combined
            with open(progress_path, 'w') as f:
                json.dump(progress, f)
            
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
                    # Orange for low confidence matches
                    print(f"→ {Colors.ORANGE}{match_name}{Colors.RESET} {times_str}")
                else:
                    print(f"→ {match_name} {times_str}")
            else:
                # Red for no match
                print(f"{Colors.RED}✗ {hltb_result['error'] or 'No data'}{Colors.RESET}")
            
            updated += 1
            await asyncio.sleep(DELAY_BETWEEN_REQUESTS)
            
            if updated % 25 == 0:
                df.to_excel(excel_path, index=False)
                print(f"    [Auto-saved progress to {excel_path}]")
    
    df.to_excel(excel_path, index=False)
    
    # Calculate efficiency metrics
    df = calculate_efficiency(df)
    df.to_excel(excel_path, index=False)
    
    # Summary
    print("-" * 70)
    print(f"\nComplete!")
    print(f"  Processed: {updated}")
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


async def main_async(args):
    """Main async entry point."""
    username, api_key = get_credentials(reset=args.reset_creds)
    output_path = Path(args.output)
    
    print("=" * 70)
    print("RetroAchievements + HowLongToBeat Scraper")
    print("=" * 70)
    
    if output_path.exists() and not args.refresh:
        print(f"\nFound existing {output_path}")
        print("Loading and checking for new games...")
        df = pd.read_excel(output_path)
        
        ra_games = await fetch_want_to_play_list(username, api_key, use_cache=not args.refresh)
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
            sys.exit(1)
        
        df = convert_ra_to_dataframe(ra_games)
    
    await process_games(df, output_path, api_key)


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
    
    args = parser.parse_args()
    asyncio.run(main_async(args))


if __name__ == '__main__':
    main()