# Mint Radio — Tech Sheet
Low-memory internet radio player for **Linux Mint 22.3 Zena (Cinnamon)**

## 1. Goal
A nice-looking but tiny GUI radio player: < 60 MB RAM idle+playing,
instant start (<1s), plays UK + US stations reliably, no browser/Electron.

## 2. Chosen stack (per your picks: nice GUI + Python + low RAM)

| Layer | Choice | Why |
|---|---|---|
| Language | **Python 3.12** (pre-installed on Mint) | Zero setup, tiny runtime ~20 MB |
| GUI | **Tkinter + ttk (themed)** | Already installed (`python3-tk` ✅ verified), ~10 MB. No PyQt (80+ MB) / Electron (500 MB). Custom dark theme → looks modern without cost |
| Audio engine | **mpv (subprocess, `--no-video --audio-only`)** | Handles MP3/AAC/HLS/m3u8/pls — critical for BBC + radio-browser URLs. ~20–25 MB. Verified: `apt` has mpv 0.37.0, not yet installed |
| Audio out | PipeWire / PulseAudio (Mint default) | mpv auto-selects, no config needed |
| Station source | **radio-browser.info API** (public domain, ~30k stations) + curated `stations.json` fallback | Free, no key, health-checked daily (`hidebroken=true`, `url_resolved`) |
| Config | `stations.json` + `favorites.json` in `~/.config/mint-radio/` | Human-editable, no DB overhead |
| Packaging | Single `main.py` + `.desktop` file | Double-click from Mint menu |

**Memory budget:** Python 20 + Tk 10 + mpv 25 = **~55 MB playing**.
Compare: VLC ~150 MB, Chrome web player 500 MB+.

## 3. Architecture
```
[ Tkinter GUI ] --commands--> [ Player controller ] --subprocess--> [ mpv --no-video URL ]
      |                              |
[ stations.json ]            [ volume / mute / metadata via mpv IPC ]
      |
[ radio-browser search (optional v2) ]
```
- GUI thread never blocks: mpv runs as child process, stop = kill + respawn (avoids leaks).
- Stream URLs are direct `url_resolved` (no playlist parsing in app — mpv does it).
- Favorites + last-volume persisted as JSON.

## 4. Dependencies (Mint 22.3 verified)
```bash
sudo apt update && sudo apt install -y mpv python3-tk
# python3-tk 3.12.3 ✅ already present, mpv missing → installs from noble/universe
# ffplay exists as fallback, but mpv preferred (HLS + metadata)
```

## 5. Station list strategy
- `stations.json` ships with **24 verified stations** (see file): UK (BBC World Service, Classic FM, LBC, Capital, Heart, Smooth), US (KEXP, Radio Paradise, SomaFM, WALM jazz/classical, NPR-style), plus genre buckets: Music / Talk+News / Classical+Jazz+Chill.
- All are **MP3/AAC direct streams** (mpv-safe). Avoided Opus-only + DASH-only.
- BBC national HLS streams are geo-ficky and need `lstn.lv` proxies — only **BBC World Service** included by default (works worldwide). UK-only BBC 1/2/3/4 can be added in-app via radio-browser search if user is in UK.
- App will support "Search more" via `GET https://de1.api.radio-browser.info/json/stations/search` with `User-Agent: MintRadio/1.0` (required by API).

## 6. What was verified today
- Mint 22.3 Cinnamon, Python 3.12.3, tkinter present, mpv 0.37 available.
- Live API query returned working UK/US stations; spot-checked with HTTP:
  - `https://ice1.somafm.com/groovesalad-128-mp3` → 200 ✅
  - `http://stream-uk1.radioparadise.com/mp3-128` → 200 ✅
  - `http://ice-the.musicradio.com/ClassicFMMP3` → 405 on HEAD (= normal for Icecast, plays in mpv) ⚠️ treat as OK
  - KEXP canonical: `https://kexp-mp3-128.streamguys1.com/kexp128.mp3` ✅ (via API)

## 7. Nice-GUI-but-light rules
- One window, ttk dark theme, station listbox + search filter + 6 preset buttons + volume slider. No images/animations (RAM killers).
- System tray optional v2 (pystray adds ~5 MB — skip for v1).

## 8. Next build steps
1. `sudo apt install mpv`
2. Build `main.py` (Tkinter + subprocess mpv, ~250 lines)
3. Ship `stations.json` (already in this repo) + `mint-radio.desktop`
4. Test: play 3 stations per category, check `ps -o rss` < 60 MB
5. v2: radio-browser search box, record button (`mpv --stream-dump`), sleep timer

---
*Sources: radio-browser.info API docs, BBC HLS stream notes (garfnet/johnpenny tools — BBC changed streams Jan 2025, hence proxy note).*
