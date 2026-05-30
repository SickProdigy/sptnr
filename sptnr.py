with open("VERSION", "r") as file:
    __version__ = file.read().strip()

import argparse
import base64
import json
import math
import logging
import os
import re
import sys
import time
import urllib.parse
import random
import tempfile

from dotenv import load_dotenv
import requests
from colorama import init, Fore, Style
from tqdm import tqdm
from datetime import timedelta

# Load environment variables from .env file if it exists
if os.path.exists(".env"):
    load_dotenv()

# Record the start time
start_time = time.time()

LOCK_DIR = "logs"
LOCK_FILENAME = "song_update_lock.json"
LOCK_FILE = LOCK_DIR + "/" + LOCK_FILENAME

# Config
NAV_BASE_URL = os.getenv("NAV_BASE_URL")
NAV_USER = os.getenv("NAV_USER")
NAV_PASS = os.getenv("NAV_PASS")
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY")
LASTFM_API_URL = "https://ws.audioscrobbler.com/2.0/"
LASTFM_ARTIST_TOP_TRACKS = {}
MUSICBRAINZ_API_URL = "https://musicbrainz.org/ws/2/"
MUSICBRAINZ_LAST_REQUEST_AT = 0

# Colors
LIGHT_PURPLE = Fore.MAGENTA + Style.BRIGHT
LIGHT_GREEN = Fore.GREEN + Style.BRIGHT
LIGHT_RED = Fore.RED + Style.BRIGHT
LIGHT_BLUE = Fore.BLUE + Style.BRIGHT
LIGHT_CYAN = Fore.CYAN + Style.BRIGHT
LIGHT_YELLOW = Fore.YELLOW + Style.BRIGHT
BOLD = Style.BRIGHT
RESET = Style.RESET_ALL

# Setup logs
LOG_DIR = "logs"
if not os.path.exists(LOG_DIR):
    os.makedirs(LOG_DIR)

LOGFILE = os.path.join(LOG_DIR, f"spotify-popularity_{int(time.time())}.log")

assert NAV_PASS is not None
HEX_ENCODED_PASS = NAV_PASS.encode().hex()
TOKEN_AUTH = base64.b64encode(
    f"{SPOTIFY_CLIENT_ID}:{SPOTIFY_CLIENT_SECRET}".encode()
).decode()
TOKEN_URL = "https://accounts.spotify.com/api/token"

class SpotifyTokenManager:
    def __init__(self, client_id, client_secret, token_url):
        self.client_id = client_id
        self.client_secret = client_secret
        self.token_url = token_url
        self.token = None
        self.expires_at = 0
        self._authenticate()

    def _authenticate(self):
        token_auth = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()
        response = requests.post(
            self.token_url,
            headers={"Authorization": f"Basic {token_auth}"},
            data={"grant_type": "client_credentials"},
        )
        if response.status_code != 200:
            error_info = response.json()
            error_description = error_info.get("error_description", "Unknown error")
            logging.error(
                f"{LIGHT_RED}Spotify Authentication Error: {error_description}{RESET}"
            )
            sys.exit(1)
        token_data = response.json()
        self.token = token_data["access_token"]
        self.expires_at = time.time() + token_data["expires_in"] - 60  # refresh 1 min early

    def get_token(self):
        if time.time() >= self.expires_at:
            self._authenticate()
        return self.token

class SafeAsciiFormatter(logging.Formatter):
    ansi_escape = re.compile(r"\x1B\[[0-?]*[ -/]*[@-~]")

    def format(self, record):
        rendered = super().format(record)
        rendered = self.ansi_escape.sub("", rendered)
        return rendered.encode("ascii", "backslashreplace").decode("ascii")


def load_lock():
    if not os.path.exists(LOCK_FILE):
        return {}
    try:
        with open(LOCK_FILE, "r") as f:
            return json.load(f)
    except json.JSONDecodeError:
        logging.error(f"{LIGHT_RED}Lock file '{LOCK_FILE}' is corrupt or not valid JSON. Starting with an empty lock.{RESET}")
        os.rename(LOCK_FILE, LOCK_FILE + ".corrupt")
        return {}
    except Exception as e:
        logging.error(f"{LIGHT_RED}Error loading lock file '{LOCK_FILE}': {e}{RESET}")
        return {}

def save_lock(lock):
    # Write to a temp file first, then atomically replace the lock file
    dir_name = os.path.dirname(LOCK_FILE) or "."
    with tempfile.NamedTemporaryFile("w", dir=dir_name, delete=False) as tf:
        json.dump(lock, tf)
        tempname = tf.name
    os.replace(tempname, LOCK_FILE)

def should_update(song_id):
    lock_expiry_seconds = get_lock_expiry()
    if lock_expiry_seconds == 0:
        return True
    last_update_ts = LOCK.get(song_id)
    if not last_update_ts:
        return True
    return (time.time() - last_update_ts) > lock_expiry_seconds


def get_lock_expiry():
    if (BASE_LOCK_DURATION == 0):
        return 0  # No lock duration, force update every time

    base_expiry = timedelta(days=BASE_LOCK_DURATION)
    jitter = timedelta(hours=random.uniform(-LOCK_JITTER/2, LOCK_JITTER/2))
    expiry = base_expiry + jitter
    # Ensure expiry is at least 1 day
    expiry = max(expiry, timedelta(days=1))
    return expiry.total_seconds()

# Set up the stream handler (console logging) without timestamp
console_handler = logging.StreamHandler()
console_handler.setFormatter(SafeAsciiFormatter("%(message)s"))
logging.basicConfig(level=logging.INFO, handlers=[console_handler])

# Set up the file handler (file logging) with timestamp
file_handler = logging.FileHandler(LOGFILE, "a", encoding="ascii", errors="backslashreplace")
file_handler.setFormatter(SafeAsciiFormatter("[%(asctime)s] %(message)s"))
logging.getLogger().addHandler(file_handler)

# Authentication
spotify_token_manager = None
SPOTIFY_TOKEN = None

init(autoreset=True)

# Default flags
PREVIEW = 0
START = 0
LIMIT = 0
ARTIST_IDs = []
ALBUM_IDs = []

# Variables
ARTISTS_PROCESSED = 0
TOTAL_TRACKS = 0
FOUND_AND_UPDATED = 0
NOT_FOUND = 0
SKIPPED_RATED = 0
UNMATCHED_TRACKS = []

# Parse arguments
description_text = "process command-line flags for sync"
parser = argparse.ArgumentParser()

parser.add_argument(
    "-p",
    "--preview",
    action="store_true",
    help="execute script in preview mode (no changes made)",
)
parser.add_argument(
    "-a",
    "--artist",
    action="append",
    help="process the artist using the Navidrome artist ID (ignores START and LIMIT)",
    type=str,
)
parser.add_argument(
    "-b",
    "--album",
    action="append",
    help="process the album using the Navidrome album ID (ignores START and LIMIT)",
    type=str,
)
parser.add_argument(
    "-s",
    "--start",
    default=0,
    type=int,
    help="start processing from artist at index [NUM] (0-based index, so 0 is the first artist)",
)
parser.add_argument(
    "-l",
    "--limit",
    default=0,
    type=int,
    help="limit to processing [NUM] artists from the start index",
)
parser.add_argument(
    "-d",
    "--lock-duration",
    type=int,
    default=7,
    help="Number of days to lock song updates (0 to force update every time)",
)
parser.add_argument(
    "-j",
    "--lock-jitter",
    type=int,
    default=24,
    help="Number of hours to add random jitter to the lock duration",
)
parser.add_argument(
    "--provider",
    choices=["spotify", "lastfm", "musicbrainz"],
    default="spotify",
    help="Popularity provider to use for updates",
)
parser.add_argument(
    "--unrated-only",
    action="store_true",
    help="Only update songs that do not already have a Navidrome rating",
)

parser.add_argument(
    "-v", "--version", action="version", version=f"%(prog)s {__version__}"
)


args = parser.parse_args()

ARTIST_IDs = args.artist if args.artist else []
ALBUM_IDs = args.album if args.album else []
START = args.start
LIMIT = args.limit
BASE_LOCK_DURATION = args.lock_duration
LOCK_JITTER = args.lock_jitter
PROVIDER = args.provider
UNRATED_ONLY = args.unrated_only

# Build only the provider-specific client we actually need.
if PROVIDER == "spotify":
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        logging.error(f"{LIGHT_RED}Config Error: SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET are required when using --provider spotify.{RESET}")
        sys.exit(1)
    spotify_token_manager = SpotifyTokenManager(
        SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, TOKEN_URL
    )
    assert spotify_token_manager is not None
    SPOTIFY_TOKEN = spotify_token_manager.get_token()
elif PROVIDER == "lastfm":
    if not LASTFM_API_KEY:
        logging.error(f"{LIGHT_RED}Config Error: LASTFM_API_KEY is required when using --provider lastfm.{RESET}")
        sys.exit(1)

logging.info(f"{BOLD}Version:{RESET} {LIGHT_YELLOW}sptnr v{__version__}{RESET}")

LOCK = load_lock()

SHOULD_DELAY = False

if args.preview:
    logging.info(f"{LIGHT_YELLOW}Preview mode, no changes will be made.{RESET}")
    PREVIEW = 1

# Check if both ARTIST_ID and START/LIMIT are provided
if ARTIST_IDs and (START != 0 or LIMIT != 0):
    START = 0
    LIMIT = 0
    logging.info(
        f"{LIGHT_YELLOW}Warning: The --artist flag overrides --start and --limit. Ignoring these settings.{RESET}"
    )

if not args.preview:
    logging.info(
        f"{BOLD}Syncing {PROVIDER.title()} {LIGHT_CYAN}popularity{RESET}{BOLD} with Navidrome {LIGHT_BLUE}rating{RESET}...{RESET}"
    )


def validate_url(url):
    if not re.match(r"https?://", url):
        logging.error(
            f"{LIGHT_RED}Config Error: URL must start with 'http://' or 'https://'.{RESET}"
        )
        return False
    if url.endswith("/"):
        logging.error(
            f"{LIGHT_RED}Config Error: URL must not end with a trailing slash.{RESET}"
        )
        return False
    return True


def url_encode(string):
    return urllib.parse.quote_plus(string)


# Convert the popularity-like score into Navidrome's 0-5 rating buckets.
def get_rating_from_popularity(provider_popularity):
    provider_popularity = float(provider_popularity)
    if provider_popularity < 16.66:
        return 0
    elif provider_popularity < 33.33:
        return 1
    elif provider_popularity < 50:
        return 2
    elif provider_popularity < 66.66:
        return 3
    elif provider_popularity < 83.33:
        return 4
    else:
        return 5


# Read the current Navidrome rating for this track so unrated-only mode can skip already-rated songs.
def get_existing_rating(track_id):
    nav_url = f"{NAV_BASE_URL}/rest/getSong?id={track_id}&u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=myapp&f=json"
    try:
        response = requests.get(nav_url, timeout=5)
        response.raise_for_status()
        data = response.json()
        song = data["subsonic-response"]["song"]
        rating_value = song.get("rating", song.get("userRating", 0))
        if rating_value in (None, ""):
            return 0
        return int(rating_value)
    except (requests.exceptions.RequestException, ValueError, KeyError, TypeError) as e:
        logging.warning(f"{LIGHT_YELLOW}Unable to read existing rating for {track_id}: {e}{RESET}")
        return None


# Process one track end-to-end: skip locked or already-rated songs, look up popularity, then write the Navidrome rating.
def process_track(track_id, artist_name, album, track_name):

    # Declare global variables
    global FOUND_AND_UPDATED, UNMATCHED_TRACKS, NOT_FOUND, TOTAL_TRACKS, SKIPPED_RATED

    # If the user asked for unrated-only, skip anything that already has a Navidrome score.
    existing_rating = get_existing_rating(track_id)
    if UNRATED_ONLY and existing_rating not in (None, 0):
        logging.info(
            f"    {LIGHT_YELLOW}Skipping{RESET} {track_name} (Navidrome Rating: {existing_rating})"
        )
        SKIPPED_RATED += 1
        TOTAL_TRACKS += 1
        return


    if not should_update(track_id):
        print(f"Skipping {track_name}, recently updated.")
        return

    def search_spotify(query, max_retries=3):
        # search_spotify: query Spotify's search API and return the track object.
        # The returned track includes a 0-100 `popularity` field, so no extra
        # follow-up lookup is required for Spotify ratings.
        global SHOULD_DELAY
        SHOULD_DELAY = True

        assert spotify_token_manager is not None
        SPOTIFY_TOKEN = spotify_token_manager.get_token()

        spotify_url = f"https://api.spotify.com/v1/search?q={query}&type=track&limit=1"
        headers = {"Authorization": f"Bearer {SPOTIFY_TOKEN}"}

        for attempt in range(max_retries):
            try:
                response = requests.get(spotify_url, headers=headers, timeout=10)
                
                # Handle rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get('Retry-After', 5))
                    logging.warning(f"Rate limited. Retrying after {retry_after} seconds...")
                    time.sleep(retry_after)
                    continue
                
                # Handle server errors with retry
                if response.status_code >= 500:
                    wait_time = (attempt + 1) * 2  # Exponential backoff factor
                    logging.warning(f"Spotify server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...")
                    time.sleep(wait_time)
                    continue
                
                response.raise_for_status()
                return response.json()
                
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait_time = (attempt + 1) * 2
                logging.warning(f"Connection error: {e}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"Request failed: {e}")
                break
        
        # If we get here, all retries failed
        logging.error(f"Failed after {max_retries} attempts for query: {query}")
        return None

    def search_lastfm(track_artist, track_title, max_retries=3):
        # search_lastfm: call Last.fm's track.getInfo and return the track info.
        # Last.fm provides `playcount` and `listeners` in the track info; we blend
        # those into a 0-100 popularity-like value - no separate rating lookup needed.
        for attempt in range(max_retries):
            try:
                response = requests.get(
                    LASTFM_API_URL,
                    params={
                        "method": "track.getInfo",
                        "api_key": LASTFM_API_KEY,
                        "artist": track_artist,
                        "track": track_title,
                        "autocorrect": 1,
                        "format": "json",
                    },
                    timeout=10,
                )
                data = response.json()

                if data.get("error") == 29:
                    retry_after = (attempt + 1) * 2
                    logging.warning(
                        f"Last.fm rate limit exceeded. Attempt {attempt + 1}/{max_retries}. Waiting {retry_after}s..."
                    )
                    time.sleep(retry_after)
                    continue

                if data.get("error"):
                    logging.warning(
                        f"Last.fm error {data.get('error')}: {data.get('message', 'Unknown error')}"
                    )
                    return None

                return data

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait_time = (attempt + 1) * 2
                logging.warning(f"Connection error: {e}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"Request failed: {e}")
                break

        logging.error(f"Failed after {max_retries} attempts for query: {track_artist} - {track_title}")
        return None

    def normalize_lastfm_title(title):
        normalized = re.sub(r"[^\w\s]", " ", title.casefold())
        return re.sub(r"\s+", " ", normalized).strip()

    def get_lastfm_artist_top_tracks(track_artist, max_retries=3):
        if track_artist in LASTFM_ARTIST_TOP_TRACKS:
            return LASTFM_ARTIST_TOP_TRACKS[track_artist]

        for attempt in range(max_retries):
            try:
                response = requests.get(
                    LASTFM_API_URL,
                    params={
                        "method": "artist.getTopTracks",
                        "api_key": LASTFM_API_KEY,
                        "artist": track_artist,
                        "autocorrect": 1,
                        "limit": 500,
                        "format": "json",
                    },
                    timeout=10,
                )
                data = response.json()

                if data.get("error") == 29:
                    retry_after = (attempt + 1) * 2
                    logging.warning(
                        f"Last.fm rate limit exceeded. Attempt {attempt + 1}/{max_retries}. Waiting {retry_after}s..."
                    )
                    time.sleep(retry_after)
                    continue

                if data.get("error"):
                    logging.warning(
                        f"Last.fm artist top tracks error {data.get('error')}: {data.get('message', 'Unknown error')}"
                    )
                    LASTFM_ARTIST_TOP_TRACKS[track_artist] = {}
                    return {}

                tracks = data.get("toptracks", {}).get("track", [])
                rank_by_title = {}
                for index, track in enumerate(tracks):
                    track_name = track.get("name")
                    if not track_name:
                        continue
                    normalized_title = normalize_lastfm_title(track_name)
                    if normalized_title in rank_by_title:
                        continue
                    rank_by_title[normalized_title] = {
                        "rank": index + 1,
                        "name": track_name,
                        "listeners": max(0, int(track.get("listeners", 0))),
                        "playcount": max(0, int(track.get("playcount", 0))),
                    }
                LASTFM_ARTIST_TOP_TRACKS[track_artist] = rank_by_title
                return rank_by_title

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait_time = (attempt + 1) * 2
                logging.warning(f"Connection error: {e}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s...")
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"Request failed: {e}")
                break

        logging.error(f"Failed after {max_retries} attempts for Last.fm top tracks: {track_artist}")
        LASTFM_ARTIST_TOP_TRACKS[track_artist] = {}
        return {}

    # MusicBrainz is using first recording/release here: we search recordings/release-group by artist/title,
    # optionally narrow the search by album, and use the first recording/release that
    # comes back for the rating lookup. The album only helps narrow the match;
    # it does not use release-group's recordings/releases average ratings.
    # search_musicbrainz: search MusicBrainz recordings and return metadata (including MBID).
    # Note: the search response does NOT include ratings; use lookup_musicbrainz_rating()
    # with the returned recording id to fetch the recording's rating value.
    def search_musicbrainz(track_artist, track_title, track_album=None, max_retries=3):
        def escape_lucene(value):
            return value.replace('"', '\\"')

        query_parts = [
            f'recording:"{escape_lucene(track_title)}"',
            f'artist:"{escape_lucene(track_artist)}"',
        ]
        if track_album:
            query_parts.append(f'release:"{escape_lucene(track_album)}"')

        query = " AND ".join(query_parts)
        search_context = f"artist={track_artist!r}, title={track_title!r}"
        if track_album:
            search_context += f", album={track_album!r}"
        headers = {
            "User-Agent": f"sptnr/{__version__} (https://github.com/krestaino/sptnr)",
            "Accept": "application/json",
        }

        for attempt in range(max_retries):
            try:
                global MUSICBRAINZ_LAST_REQUEST_AT
                elapsed = time.time() - MUSICBRAINZ_LAST_REQUEST_AT
                if elapsed < 1:
                    time.sleep(1 - elapsed)
                MUSICBRAINZ_LAST_REQUEST_AT = time.time()

                response = requests.get(
                    f"{MUSICBRAINZ_API_URL}recording",
                    params={"query": query, "fmt": "json", "limit": 1},
                    headers=headers,
                    timeout=10,
                )

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 2
                    logging.warning(
                        f"MusicBrainz rate limit exceeded. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue

                if response.status_code >= 500:
                    wait_time = (attempt + 1) * 2
                    logging.warning(
                        f"MusicBrainz server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()
                recordings = data.get("recordings", [])
                if recordings:
                    return data
                return None

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait_time = (attempt + 1) * 2
                logging.warning(
                    f"Connection error while searching MusicBrainz ({search_context}): {e}. "
                    f"Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s..."
                )
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"Request failed: {e}")
                break

        logging.error(
            f"Failed after {max_retries} attempts for MusicBrainz search ({search_context})"
        )
        return None

    # lookup_musicbrainz_rating: fetch the recording/{id}?inc=ratings endpoint to
    # retrieve the recording's rating (value is 0-5). This is separate from the
    # search step because MusicBrainz intentionally exposes ratings on the
    # recording lookup endpoint only.
    def lookup_musicbrainz_rating(recording_id, max_retries=3):
        rating_context = f"recording_id={recording_id!r}"
        headers = {
            "User-Agent": f"sptnr/{__version__} (https://github.com/krestaino/sptnr)",
            "Accept": "application/json",
        }

        for attempt in range(max_retries):
            try:
                global MUSICBRAINZ_LAST_REQUEST_AT
                elapsed = time.time() - MUSICBRAINZ_LAST_REQUEST_AT
                if elapsed < 1:
                    time.sleep(1 - elapsed)
                MUSICBRAINZ_LAST_REQUEST_AT = time.time()

                response = requests.get(
                    f"{MUSICBRAINZ_API_URL}recording/{recording_id}",
                    params={"inc": "ratings", "fmt": "json"},
                    headers=headers,
                    timeout=10,
                )

                if response.status_code == 429:
                    wait_time = (attempt + 1) * 2
                    logging.warning(
                        f"MusicBrainz rate limit exceeded. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue

                if response.status_code >= 500:
                    wait_time = (attempt + 1) * 2
                    logging.warning(
                        f"MusicBrainz server error {response.status_code}. Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue

                response.raise_for_status()
                recording_data = response.json()
                recording = recording_data.get("recording", recording_data)
                if not isinstance(recording, dict):
                    return None

                for key in ("rating", "user-rating", "user_rating", "userRating"):
                    value = recording.get(key)
                    if isinstance(value, dict):
                        for nested_key in ("value", "rating", "user-rating", "user_rating"):
                            nested_value = value.get(nested_key)
                            if nested_value not in (None, ""):
                                return nested_value
                    elif value not in (None, ""):
                        return value

                return None

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
                wait_time = (attempt + 1) * 2
                logging.warning(
                    f"Connection error while looking up MusicBrainz rating ({rating_context}): {e}. "
                    f"Attempt {attempt + 1}/{max_retries}. Waiting {wait_time}s..."
                )
                time.sleep(wait_time)
                continue
            except requests.exceptions.RequestException as e:
                logging.error(f"Request failed: {e}")
                break

        logging.error(
            f"Failed after {max_retries} attempts for MusicBrainz rating lookup ({rating_context})"
        )
        return None

    def remove_parentheses_content(s):
        # Only remove parentheses if they do NOT contain important keywords
        keywords = ["remix", "instrumental", "edit", "version", "mix", "karaoke", "live", "acoustic", "demo"]
        def replacer(match):
            content = match.group(1).lower()
            if any(k in content for k in keywords):
                return f"({match.group(1)})"  # Keep it
            return ""
        return re.sub(r"\((.*?)\)", replacer, s).strip()

    search_attempts = [
        # Primary attempt with all info
        lambda: f"{url_encode(track_name)}%20artist:{url_encode(artist_name)}%20album:{url_encode(album)}",
        
        # Secondary attempt without album
        lambda: f"{url_encode(remove_parentheses_content(track_name))}%20artist:{url_encode(artist_name)}",
        
        # Tertiary attempt with modified track name
        lambda: f"{url_encode(track_name.replace('Part', 'Pt.'))}%20artist:{url_encode(artist_name)}"
    ]

    spotify_popularity = None
    lastfm_popularity = None
    musicbrainz_rating = None
    navidrome_rating = None
    sp_track_name = track_name

    if PROVIDER == "spotify":
        # Spotify gives us popularity directly, so we only need to find the best matching track.
        provider_data = None
        for attempt in search_attempts:
            provider_data = search_spotify(attempt())
            if provider_data and provider_data.get("tracks", {}).get("items"):
                break

        if provider_data and provider_data.get("tracks", {}).get("items"):
            track = provider_data["tracks"]["items"][0]
            spotify_popularity = track.get("popularity", 0)
            sp_track_name = track["name"]
    elif PROVIDER == "lastfm":
        # Last.fm exposes listeners and playcount instead of a Spotify-style popularity score.
        # Artist top-track position drives the main score, global listener reach keeps scale
        # across artists, and plays per listener adds a small capped engagement bonus.
        lastfm_attempts = [
            (artist_name, track_name),
            (artist_name, remove_parentheses_content(track_name)),
            (artist_name, track_name.replace("Part", "Pt.")),
        ]
        provider_data = None
        for attempt_artist, attempt_title in lastfm_attempts:
            provider_data = search_lastfm(attempt_artist, attempt_title)
            if provider_data and provider_data.get("track"):
                break

        if provider_data and provider_data.get("track"):
            track = provider_data["track"]
            playcount = max(0, int(track.get("playcount", 0)))
            listeners = max(0, int(track.get("listeners", 0)))
            top_tracks = get_lastfm_artist_top_tracks(artist_name)
            matched_title = track.get("name", track_name)
            top_track = top_tracks.get(normalize_lastfm_title(matched_title))
            if not top_track:
                top_track = top_tracks.get(normalize_lastfm_title(track_name))

            top_track_position = None
            if top_track:
                top_track_position = top_track["rank"]
                matched_title = top_track["name"]
                listeners = max(listeners, top_track["listeners"])
                playcount = max(playcount, top_track["playcount"])

            top_track_position_score = 0
            if top_track_position:
                top_track_position_score = max(0, 100 - (math.log2(top_track_position) * 10))

            if listeners == 0 or playcount == 0:
                reach_score = 0
                engagement_bonus = 0
            else:
                plays_per_listener = playcount / listeners
                reach_score = min(90, math.log10(listeners + 1) * 13)
                engagement_bonus = min(12, math.log2(plays_per_listener) * 4)
            lastfm_popularity = round(
                min(
                    100,
                    (top_track_position_score * 0.50)
                    + (reach_score * 0.40)
                    + (engagement_bonus * 0.10),
                )
            )
            sp_track_name = matched_title
    else:      # MusicBrainz ratings come from a lookup on the matched recording MBID.
        mb_attempts = [
            (artist_name, track_name, album),
            (artist_name, remove_parentheses_content(track_name), album),
            (artist_name, track_name.replace("Part", "Pt."), album),
            (artist_name, track_name, None),
        ]
        provider_data = None
        for attempt_artist, attempt_title, attempt_album in mb_attempts:
            provider_data = search_musicbrainz(attempt_artist, attempt_title, attempt_album)
            if provider_data and provider_data.get("recordings"):
                break

        if provider_data and provider_data.get("recordings"):
            track = provider_data["recordings"][0]
            musicbrainz_rating = lookup_musicbrainz_rating(track.get("id"))
            musicbrainz_rating = max(0.0, min(5.0, float(musicbrainz_rating or 0)))
            navidrome_rating = 0 if musicbrainz_rating == 0 else max(1, int(musicbrainz_rating + 0.5))
            sp_track_name = track.get("title", track_name)

    provider_value = (
        spotify_popularity
        if spotify_popularity is not None
        else lastfm_popularity
        if lastfm_popularity is not None
        else musicbrainz_rating
    )

    if provider_value is not None:
        # MusicBrainz ratings are already 0-5, so use directly; others are 0-100 and need mapping.
        navidrome_rating = navidrome_rating if PROVIDER == "musicbrainz" else get_rating_from_popularity(provider_value)
        provider_value_str = f"{provider_value} " if 0 <= provider_value <= 9 else str(provider_value)
        source_label = "s" if PROVIDER == "musicbrainz" else "p"

        if navidrome_rating == 0:
            logging.info(f"    {source_label}:{LIGHT_CYAN}{provider_value_str}{RESET} | Skipping {track_name} (Navidrome Rating: 0)")
        else:
            logging.info(f"    {source_label}:{LIGHT_CYAN}{provider_value_str}{RESET} -> r:{LIGHT_BLUE}{navidrome_rating}{RESET} | {LIGHT_GREEN}{track_name} - {sp_track_name}{RESET}")

        if navidrome_rating == 0:
            pass
        elif PREVIEW != 1:
            try:
                nav_url = f"{NAV_BASE_URL}/rest/setRating?u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=myapp&id={track_id}&rating={navidrome_rating}"
                requests.get(nav_url, timeout=5)
                FOUND_AND_UPDATED += 1
                LOCK[track_id] = time.time()
                save_lock(LOCK)
            except requests.exceptions.RequestException as e:
                logging.error(f"Failed to update rating in Navidrome: {e}")
    else:
        logging.info(f"    p:{LIGHT_RED}??{RESET} -> r:{LIGHT_BLUE}0{RESET} | {LIGHT_RED}(not found) {track_name}{RESET}")
        UNMATCHED_TRACKS.append(f"{artist_name} - {album} - {track_name}")
        NOT_FOUND += 1

        if PREVIEW != 1:
            try:
                nav_url = f"{NAV_BASE_URL}/rest/setRating?u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=myapp&id={track_id}&rating=0"
                requests.get(nav_url, timeout=5)
                LOCK[track_id] = time.time()
                save_lock(LOCK)
            except requests.exceptions.RequestException as e:
                logging.error(f"Failed to update rating in Navidrome: {e}")


    TOTAL_TRACKS += 1

def process_album(album_id):

    global SHOULD_DELAY

    if SHOULD_DELAY:
        # sleep for a short time to avoid hitting rate limits too quickly
        time.sleep(4)

    SHOULD_DELAY = False
    nav_url = f"{NAV_BASE_URL}/rest/getAlbum?id={album_id}&u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=spotify_sync&f=json"
    response = requests.get(nav_url).json()

    album_info = response["subsonic-response"]["album"]
    album_artist = album_info["artist"]
    tracks = [
        (song["id"], album_artist, song["album"], song["title"])
        for song in album_info.get("song", [])
    ]

    for track in tracks:
        process_track(*track)


def process_artist(artist_id):
    nav_url = f"{NAV_BASE_URL}/rest/getArtist?id={artist_id}&u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=spotify_sync&f=json"
    response = requests.get(nav_url).json()

    albums = [
        (album["id"], album["name"])
        for album in response["subsonic-response"]["artist"].get("album", [])
    ]

    for album_id, album_name in albums:
        logging.info(f"  Album: {LIGHT_YELLOW}{album_name}{RESET} ({album_id})")
        process_album(album_id)


def fetch_data(url):
    try:
        response = requests.get(url)
        response_data = json.loads(response.text)

        if "subsonic-response" not in response_data:
            logging.error(
                f"{LIGHT_RED}Unexpected response format from Navidrome.{RESET}"
            )
            sys.exit(1)

        nav_response = response_data["subsonic-response"]

        if "error" in nav_response:
            error_message = nav_response["error"].get("message", "Unknown error")
            logging.error(f"{LIGHT_RED}Navidrome Error: {error_message}{RESET}")
            sys.exit(1)

        return nav_response

    except requests.exceptions.ConnectionError:
        logging.error(
            f"{LIGHT_RED}Connection Error: Failed to connect to the provided URL. Please check if the URL is correct and the server is reachable.{RESET}"
        )
        sys.exit(1)
    except requests.exceptions.RequestException as e:
        logging.error(
            f"{LIGHT_RED}Connection Error: An error occurred while trying to connect to Navidrome: {e}{RESET}"
        )
        sys.exit(1)
    except json.JSONDecodeError:
        logging.error(
            f"{LIGHT_RED}JSON Parsing Error: Failed to parse JSON response from Navidrome. Please check if the provided URL is a valid Navidrome server.{RESET}"
        )
        sys.exit(1)


try:
    validate_url(NAV_BASE_URL)
except ValueError as e:
    logging.error(f"{LIGHT_RED}{e}{RESET}")
    sys.exit(1)

if ARTIST_IDs:
    for ARTIST_ID in ARTIST_IDs:
        url = f"{NAV_BASE_URL}/rest/getArtist?id={ARTIST_ID}&u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=spotify_sync&f=json"
        data = fetch_data(url)
        ARTIST_NAME = data["artist"]["name"]

        logging.info("")
        logging.info(f"Artist: {LIGHT_PURPLE}{ARTIST_NAME}{RESET} ({ARTIST_ID})")
        process_artist(ARTIST_ID)

elif ALBUM_IDs:
    for ALBUM_ID in ALBUM_IDs:
        url = f"{NAV_BASE_URL}/rest/getAlbum?id={ALBUM_ID}&u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=spotify_sync&f=json"
        data = fetch_data(url)
        ARTIST_NAME = data["album"]["artist"]
        ARTIST_ID = data["album"]["artistId"]
        ALBUM_NAME = data["album"]["name"]

        logging.info("")
        logging.info(f"Artist: {LIGHT_PURPLE}{ARTIST_NAME}{RESET} ({ARTIST_ID})")
        logging.info(f"  Album: {LIGHT_YELLOW}{ALBUM_NAME}{RESET} ({ALBUM_ID})")
        process_album(ALBUM_ID)

else:
    url = f"{NAV_BASE_URL}/rest/getArtists?u={NAV_USER}&p=enc:{HEX_ENCODED_PASS}&v=1.12.0&c=spotify_sync&f=json"
    data = fetch_data(url)
    ARTIST_DATA = [
        (artist["id"], artist["name"])
        for index_entry in data["artists"]["index"]
        for artist in index_entry["artist"]
    ]

    if START == 0 and LIMIT == 0:
        data_slice = ARTIST_DATA
        total_count = len(ARTIST_DATA)
    else:
        if LIMIT == 0:
            data_slice = ARTIST_DATA[START:]
        else:
            data_slice = ARTIST_DATA[START : START + LIMIT]
        total_count = len(data_slice)

    logging.info(f"Total artists to process: {LIGHT_GREEN}{total_count}{RESET}")

    for index, ARTIST_ENTRY in tqdm(
        enumerate(data_slice), total=total_count, leave=False
    ):
        ARTIST_ID, ARTIST_NAME = ARTIST_ENTRY

        logging.info("")
        logging.info(
            f"Artist: {LIGHT_PURPLE}{ARTIST_NAME}{RESET} ({ARTIST_ID})[{index+args.start}]"
        )
        process_artist(ARTIST_ID)

        ARTISTS_PROCESSED += 1


# Display the results
logging.info("")
processable_tracks = TOTAL_TRACKS - SKIPPED_RATED if UNRATED_ONLY else TOTAL_TRACKS
MATCH_PERCENTAGE = (FOUND_AND_UPDATED / processable_tracks) * 100 if processable_tracks != 0 else 0
FORMATTED_MATCH_PERCENTAGE = round(MATCH_PERCENTAGE, 2)  # Rounding to 2 decimal places
TOTAL_BLOCKS = 20

color_found = LIGHT_GREEN if FOUND_AND_UPDATED == processable_tracks else LIGHT_YELLOW
color_found_white = LIGHT_GREEN if FOUND_AND_UPDATED == processable_tracks else BOLD
color_not_found = LIGHT_GREEN if NOT_FOUND == 0 else LIGHT_RED

if processable_tracks == 0:
    blocks_found = ""
    blocks_not_found = ""
else:
    blocks_found = "#" * round(FOUND_AND_UPDATED * TOTAL_BLOCKS / processable_tracks)
    blocks_not_found = "-" * (TOTAL_BLOCKS - len(blocks_found))
full_blocks_found = f"{color_found_white}{blocks_found}{RESET}"
full_blocks_not_found = f"{color_not_found}{blocks_not_found}{RESET}"

# Calculate elapsed time
elapsed_time = time.time() - start_time
hours, remainder = divmod(elapsed_time, 3600)
minutes, seconds = divmod(remainder, 60)

parts = []
if hours:
    parts.append(f"{int(hours)}h")
if minutes:
    parts.append(f"{int(minutes)}m")
if seconds or not parts:  # Show seconds if it's the only value, even if it's 0
    parts.append(f"{int(seconds)}s")

formatted_elapsed_time = " ".join(parts)

# logging.info(f"Processing completed in {int(hours):02}:{int(minutes):02}:{int(seconds):02}")
summary_line = (
    f"Tracks: {LIGHT_PURPLE}{TOTAL_TRACKS}{RESET} | "
    f"Found: {color_found}{FOUND_AND_UPDATED}{RESET}"
)
if SKIPPED_RATED:
    summary_line += f" | Skipped: {LIGHT_YELLOW}{SKIPPED_RATED}{RESET}"
summary_line += (
    f" | Not Found: {color_not_found}{NOT_FOUND}{RESET} | "
    f"Match: {color_found}{FORMATTED_MATCH_PERCENTAGE}%{RESET} | "
    f"Time: {LIGHT_PURPLE}{formatted_elapsed_time}{RESET}"
)
logging.info(summary_line)
