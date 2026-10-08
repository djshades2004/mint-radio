# AGENTS.md — Mint Radio

Low-memory internet radio player for Linux Mint 22.3 Zena (Cinnamon).
Stack (decided, see `TECH_SHEET.md`): Python 3.12 + Tkinter/ttk + mpv via subprocess. No PyQt/Electron.

## Commands
- Deps: `sudo apt install -y mpv python3-tk` (tkinter pre-installed; mpv 0.37 in noble/universe, not installed by default; ffplay exists as fallback only)
- Run (planned): `python3 main.py` — single file + `stations.json` + `.desktop`, no build step, no venv needed
- Verify stream: GET bytes, never HEAD — Icecast returns `405` on HEAD for working streams:
  `python3 -c "import urllib.request; print(len(urllib.request.urlopen(urllib.request.Request('URL', headers={'User-Agent':'MintRadio/1.0'}), timeout=10).read(2048)))"`
- Check RAM budget (<60 MB playing): `ps -o rss=,comm= -C python3 -C mpv`

## Conventions
- `stations.json` schema: `{app, source, version, stations: [{name, country, category, codec, bitrate, url}]}`. Categories: `music|talk|classical|jazz|chill`. Keep MP3/AAC direct streams only — no Opus-only, no DASH.
- Playback: always `mpv --no-video <url>` (`--audio-only` does not exist in mpv 0.37 — it aborts), spawned as child process from GUI thread; stop = kill + respawn. Never block Tk mainloop. Let mpv resolve playlists.
- radio-browser.info API: send `User-Agent: MintRadio/1.0` (required), query `https://de1.api.radio-browser.info/...`, use `url_resolved` field with `hidebroken=true`.
- Shoutcast directory needs no API key: POST form `{genre, station, type, artist, song}` to `https://directory.shoutcast.com/Search/UpdateAdvancedSearch` (browser UA + `X-Requested-With: XMLHttpRequest`), returns JSON with `ID/Name/Format/Bitrate/Genre/Listeners`. One query caps at ~200 rows — `shoutcast_scan()` fans out over `SHOUTCAST_GENRES` and dedupes by tune-in URL for 1000+ candidates. Play via `http://yp.shoutcast.com/sbin/tunein-station.m3u?id=ID`, but store the resolved direct URL (`resolve_stream()`) so Test/Record work. Imported stations get `country: Web`. Never commit a Shoutcast dev key — none is needed.
- Icecast: official dir.xiph.org is empty (beta has no data, yp.xml returns `<directory></directory>`) — use internet-radio.com instead: GET `/stations/<genre>/?sortby=listeners` + `/page<N>` (20 rows/page, server-rendered table; stream URL in `playlistgenerator/?u=` param, usually a `.pls` needing `resolve_stream()`). `icecast_scan()` fans out over `IR_GENRES`. Name regex must handle plain `<h4>` as well as `<h4><a>` rows. Codec comes from the stream-check content-type, not the listing.
- BBC nationals (Radio 1/2/3/4) are geo-blocked HLS since Jan 2025 — don't add raw BBC URLs; only BBC World Service is safe default. UK-only additions need `lstn.lv` proxy form.
- Tray: Mint-native `XApp.StatusIcon` via `gi` (no pip deps; `gir1.2-xapp-1.0` preinstalled). Gtk objects are created on the Tk thread, only the `GLib.MainLoop` runs on a daemon thread — menu callbacks must hop back via `after(0, …)`. No XApp custom signals are exposed; left-click shows the primary menu automatically. Guard `withdraw()` with `_hiding` so `<Unmap>` doesn't loop. `<Unmap>` must defer the `state()=="iconic"` check ~200ms (state hasn't transitioned when the event fires). Don't rely on minimize interception alone — `Ctrl+H` / close-to-tray setting are the guaranteed paths. Toggle persisted as `settings.tray`.

## Gotchas
- No tests, CI, or lint — `main.py` + installer exist and work, but everything is still uncommitted. Don't assume entrypoints exist.
- Target env is Mint/Cinnamon with PipeWire/Pulse — mpv needs no audio config; don't add ALSA hardcoding.
