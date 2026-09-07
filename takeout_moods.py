"""Google Takeout (YouTube / YouTube Music) -> iTunes previews -> Essentia mood features.

The Takeout CSV schema drifts between export versions and between file types
(`music-library-songs.csv` carries Title/Album/Artist, playlist exports often carry
only a video id), so columns are detected by fuzzy header matching rather than
hardcoded, and what was detected is reported back to the caller.
"""
from __future__ import annotations

import csv
import difflib
import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile

import pandas as pd

# --------------------------------------------------------------------------- #
# Column detection
# --------------------------------------------------------------------------- #

def _norm_header(h: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(h).lower())

# ordered by preference: the first candidate found wins
COLUMN_CANDIDATES = {
    "title": ["songtitle", "tracktitle", "title", "song", "track", "videotitle", "name"],
    "artist": ["artistnames", "artistname", "artist", "artists", "albumartist", "channeltitle", "channelname"],
    "video_id": ["videoid", "videoids", "id", "youtubevideoid"],
    "album": ["albumtitle", "album"],
}

def detect_columns(headers) -> dict:
    """Map logical field -> actual header, by normalized fuzzy match."""
    norm = {_norm_header(h): h for h in headers}
    found = {}
    for field, candidates in COLUMN_CANDIDATES.items():
        for cand in candidates:
            if cand in norm:
                found[field] = norm[cand]
                break
    return found

# --------------------------------------------------------------------------- #
# Normalization  (the single biggest lever on match rate for YouTube-sourced data)
# --------------------------------------------------------------------------- #

# junk that YouTube titles carry and iTunes titles never do
NOISE_RE = re.compile(
    r"""\s*[\(\[]\s*(?:
        official(?:\s+(?:music\s+)?(?:video|audio|visualizer|lyric\s*video|lyrics))?
      | music\s+video | lyric[s]?(?:\s+video)? | audio\s+only | audio
      | visuali[sz]er | m/?v | hd | hq | 4k | 8k | full\s+album
      | (?:\d{4}\s+)?remaster(?:ed)?(?:\s+version)?(?:\s*\d{4})? | reissue
      | explicit | clean\s+version | color\s+coded.*
    )\s*[\)\]]""",
    re.IGNORECASE | re.VERBOSE,
)
FEAT_RE = re.compile(r"\s*[\(\[]\s*(?:feat|ft|featuring)\b\.?\s*[^\)\]]*[\)\]]", re.IGNORECASE)
TRAILING_DASH_NOISE_RE = re.compile(
    r"\s+-\s+(?:official.*|lyric[s]?.*|topic|audio|video|visuali[sz]er)$", re.IGNORECASE
)
# un-bracketed trailing noise ("... Official MV"). Requires the word "official"
# (or a bare M/V) so a song genuinely titled "Video" survives.
NOISE_BARE_RE = re.compile(
    r"\s+(?:official\s+(?:m/?v|music\s+video|video|audio|lyric[s]?(?:\s+video)?)|m/v)\s*$",
    re.IGNORECASE,
)
TOPIC_RE = re.compile(r"\s+-\s+topic\s*$", re.IGNORECASE)
# only explicit featuring markers - never split on ',', '&' or 'and', which are
# part of plenty of real band names (Earth, Wind & Fire / Simon & Garfunkel)
ARTIST_FEAT_RE = re.compile(r"\s+(?:feat|ft|featuring|with)\b\.?\s+.*$", re.IGNORECASE)

def normalize_title(s: str, drop_feat: bool = True) -> str:
    s = str(s or "")
    s = NOISE_RE.sub(" ", s)
    if drop_feat:
        s = FEAT_RE.sub(" ", s)
    s = TRAILING_DASH_NOISE_RE.sub("", s)
    s = NOISE_BARE_RE.sub("", s)
    s = s.replace("’", "'").replace("‘", "'")
    return re.sub(r"\s+", " ", s).strip(" -–—|")

def normalize_artist(s: str) -> str:
    """YouTube Music surfaces auto-generated channels as 'Artist - Topic'."""
    s = str(s or "")
    s = TOPIC_RE.sub("", s)
    s = ARTIST_FEAT_RE.sub("", s)
    s = s.replace("’", "'")
    return re.sub(r"\s+", " ", s).strip()

def split_artist_title(raw_title: str):
    """YouTube titles are often 'Artist - Title' in one field."""
    cleaned = normalize_title(raw_title)
    parts = re.split(r"\s+[-–—]\s+", cleaned, maxsplit=1)
    if len(parts) == 2 and all(p.strip() for p in parts):
        return parts[0].strip(), parts[1].strip()
    return "", cleaned

def _match_key(artist: str, title: str) -> str:
    s = f"{normalize_artist(artist)} {normalize_title(title)}".lower()
    return re.sub(r"[^a-z0-9 ]", "", s).strip()

# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_takeout_csv(path: str, playlist_name: str | None = None) -> pd.DataFrame:
    """Read one Takeout CSV into playlist/title/artist/video_id, whatever its schema."""
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return pd.DataFrame(columns=["playlist", "title", "artist", "video_id", "source_file"])

    cols = detect_columns(rows[0].keys())
    playlist = playlist_name or os.path.splitext(os.path.basename(path))[0]

    out = []
    for r in rows:
        title = (r.get(cols["title"]) or "").strip() if "title" in cols else ""
        artist = (r.get(cols["artist"]) or "").strip() if "artist" in cols else ""
        vid = (r.get(cols["video_id"]) or "").strip() if "video_id" in cols else ""
        if not title and not vid:
            continue
        # single combined 'Artist - Title' field
        if title and not artist:
            guessed_artist, guessed_title = split_artist_title(title)
            if guessed_artist:
                artist, title = guessed_artist, guessed_title
        out.append(dict(playlist=playlist, title=title, artist=artist,
                        video_id=vid, source_file=os.path.basename(path)))
    df = pd.DataFrame(out)
    df.attrs["detected_columns"] = cols
    return df

def load_takeout(path: str) -> pd.DataFrame:
    """Accept a CSV, a directory of CSVs (Takeout playlists folder), or a Takeout .zip."""
    if path.lower().endswith(".zip"):
        extract_dir = path[:-4] + "_extracted"
        with zipfile.ZipFile(path) as z:
            z.extractall(extract_dir)
        path = extract_dir

    if os.path.isfile(path):
        return load_takeout_csv(path)

    frames, detected = [], {}
    for root, _dirs, files in os.walk(path):
        for fn in sorted(files):
            if not fn.lower().endswith(".csv"):
                continue
            fp = os.path.join(root, fn)
            df = load_takeout_csv(fp)
            if len(df):
                detected[fn] = df.attrs.get("detected_columns", {})
                frames.append(df)
    if not frames:
        raise FileNotFoundError(f"no non-empty CSV found under {path}")
    out = pd.concat(frames, ignore_index=True)
    out.attrs["detected_columns"] = detected
    return out

# --------------------------------------------------------------------------- #
# Video-ID-only playlists -> titles, via the YouTube Data API
# --------------------------------------------------------------------------- #

def resolve_video_ids(video_ids, api_key: str, chunk: int = 50) -> dict:
    """videos.list costs 1 quota unit per call and takes 50 ids at a time,
    so a 5000-track library resolves for ~100 units of the 10000/day budget.

    NOTE: not exercised in this repo's demo run (needs your own API key)."""
    resolved = {}
    ids = [v for v in video_ids if v]
    for i in range(0, len(ids), chunk):
        batch = ids[i:i + chunk]
        url = ("https://www.googleapis.com/youtube/v3/videos?part=snippet&id="
               + ",".join(batch) + "&key=" + urllib.parse.quote(api_key))
        with urllib.request.urlopen(url, timeout=30) as r:
            data = json.load(r)
        for item in data.get("items", []):
            sn = item["snippet"]
            resolved[item["id"]] = {
                "raw_title": sn.get("title", ""),
                "channel": sn.get("channelTitle", ""),
            }
        time.sleep(0.1)
    return resolved

# --------------------------------------------------------------------------- #
# iTunes matching
# --------------------------------------------------------------------------- #

def itunes_search(artist: str, title: str, limit: int = 5, timeout: int = 20):
    term = " ".join(x for x in (normalize_artist(artist), normalize_title(title)) if x)
    if not term.strip():
        return []
    url = ("https://itunes.apple.com/search?media=music&limit=" + str(limit)
           + "&term=" + urllib.parse.quote(term))
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.load(r).get("results", [])

def _tokens(s: str) -> set:
    return set(re.findall(r"[a-z0-9]+", s.lower()))


def score_candidate(artist: str, title: str, cand: dict) -> float:
    """Similarity of an iTunes candidate to the wanted (artist, title).

    Artist and title are scored separately rather than as one concatenated
    key. iTunes credits every collaborator in `artistName` ("Daft Punk,
    Pharrell Williams & Nile Rodgers") where a Takeout row carries only the
    primary artist, so a single combined ratio penalises the *correct* match
    for being more fully credited — badly enough that a remix can outrank it.
    Scoring the fields apart, and treating a subset artist credit as a match
    rather than a partial one, removes that failure mode.

    Title carries more weight than artist: the artist is usually already
    constrained by the search query, whereas the title is what distinguishes
    the original from a remix, a live cut or an edit.
    """
    a_want, t_want = normalize_artist(artist), normalize_title(title)
    a_got = normalize_artist(cand.get("artistName", ""))
    t_got = normalize_title(cand.get("trackName", ""))
    if not (a_want and t_want) or not (a_got and t_got):
        return 0.0

    def _clean(s: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()

    def _ratio(x: str, y: str) -> float:
        return difflib.SequenceMatcher(None, _clean(x), _clean(y)).ratio()

    aw, ag = _tokens(a_want), _tokens(a_got)
    a_score = 1.0 if aw and aw <= ag else _ratio(a_want, a_got)
    t_score = _ratio(t_want, t_got)
    return 0.4 * a_score + 0.6 * t_score


def match_track(artist: str, title: str, threshold: float = 0.72,
                 retries: int = 3, pause: float = 0.4) -> dict:
    """Best-scoring iTunes result, with backoff (iTunes rate-limits aggressively)."""
    results = []
    for attempt in range(retries):
        try:
            results = itunes_search(artist, title)
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError):
            if attempt == retries - 1:
                return {"status": "error", "score": 0.0}
            time.sleep(pause * (2 ** attempt))

    scored = [(score_candidate(artist, title, c), c) for c in results]
    scored = [(s, c) for s, c in scored if c.get("previewUrl")]
    if not scored:
        return {"status": "no_result", "score": 0.0}

    score, best = max(scored, key=lambda t: t[0])
    return {
        "status": "matched" if score >= threshold else "low_confidence",
        "score": round(float(score), 3),
        "itunes_artist": best.get("artistName"),
        "itunes_track": best.get("trackName"),
        "itunes_track_id": best.get("trackId"),
        "preview_url": best.get("previewUrl"),
    }

def match_library(df: pd.DataFrame, threshold: float = 0.72, pause: float = 0.4,
                   progress_every: int = 25) -> pd.DataFrame:
    rows = []
    for i, rec in enumerate(df.to_dict("records"), 1):
        res = match_track(rec["artist"], rec["title"], threshold=threshold, pause=pause)
        rows.append({**rec, **res})
        if progress_every and i % progress_every == 0:
            print(f"  {i}/{len(df)} matched so far")
        time.sleep(pause)
    return pd.DataFrame(rows)

def coverage_report(matched: pd.DataFrame) -> pd.DataFrame:
    counts = matched["status"].value_counts()
    total = len(matched)
    report = pd.DataFrame({
        "tracks": counts,
        "share": (counts / total * 100).round(1),
    })
    report.index.name = "status"
    return report

# --------------------------------------------------------------------------- #
# Audio + features
# --------------------------------------------------------------------------- #

def fetch_preview(preview_url: str, track_id, audio_dir: str) -> str:
    os.makedirs(audio_dir, exist_ok=True)
    m4a = os.path.join(audio_dir, f"{track_id}.m4a")
    wav = os.path.join(audio_dir, f"{track_id}.wav")
    if not os.path.exists(wav):
        urllib.request.urlretrieve(preview_url, m4a)
        try:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", m4a,
                            "-ac", "1", "-ar", "16000", wav],
                           check=True, timeout=60, stdin=subprocess.DEVNULL)
        except FileNotFoundError:
            raise RuntimeError(
                "ffmpeg is not on PATH. It is a system dependency, not a pip "
                "package: iTunes previews are .m4a and Essentia needs mono "
                "16 kHz wav. Install it with `brew install ffmpeg` (macOS) or "
                "`apt install ffmpeg` (Debian), then re-run."
            ) from None
    return wav
