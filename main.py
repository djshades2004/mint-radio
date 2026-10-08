#!/usr/bin/env python3
"""Mint Radio — low-memory internet radio player (Tkinter + mpv). Stdlib only.

Copyright (C) 2026 djshades2004
This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version. See LICENSE for details.
"""
import json
import os
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

APP = "Mint Radio"
VERSION = "1.5.2"
UA = "MintRadio/1.0"
API = "https://de1.api.radio-browser.info"
IPC_SOCK = "/tmp/mint-radio-mpv.sock"

BASE_DIR = Path(__file__).resolve().parent
CONFIG_DIR = Path.home() / ".config" / "mint-radio"
FAV_FILE = CONFIG_DIR / "favorites.json"
SET_FILE = CONFIG_DIR / "settings.json"
CUSTOM_FILE = CONFIG_DIR / "custom.json"
RECORD_DIR = Path.home() / "Music" / "MintRadio"

CATEGORIES = ["All", "music", "talk", "classical", "jazz", "chill"]
SLEEP_OPTIONS = ["Off", "5 min", "15 min", "30 min", "60 min", "90 min"]

# glassy modern navy palette (also used by dialogs, which lack App.PAL)
NAVY = {"bg": "#0b1a33", "bg2": "#13294d", "bg3": "#1e3c6e",
        "field": "#0e2242", "fg": "#e9f1fc", "muted": "#8ba3c7",
        "acc": "#4da3ff", "ice": "#8fd8ff", "row_alt": "#0d2140",
        "sel": "#2f6fdd", "off": "#3a557f"}


# ---------------- storage ----------------
import concurrent.futures
import html as htmlmod
def load_json(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path, data):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        pass


def bundled_stations():
    data = load_json(BASE_DIR / "stations.json", {})
    stations = data.get("stations", []) if isinstance(data, dict) else []
    out = []
    for s in stations:
        out.append({
            "name": s.get("name", "Unknown"),
            "country": s.get("country", "?"),
            "category": s.get("category", "music"),
            "codec": s.get("codec", "MP3"),
            "bitrate": s.get("bitrate", 128),
            "url": s.get("url", ""),
            "custom": False,
        })
    customs = load_json(CUSTOM_FILE, [])
    for s in customs:
        s = dict(s)
        s["custom"] = True
        out.append(s)
    return out


# ---------------- mpv player ----------------
class Player:
    """Owns a single mpv child process. Never blocks the Tk mainloop."""

    def __init__(self, on_meta, on_state):
        self.proc = None
        self.url = None
        self.volume = 70
        self.muted = False
        self._pre_mute_vol = 70
        self.on_meta = on_meta
        self.on_state = on_state
        self._watcher = None

    def _ipc_send(self, cmd):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(IPC_SOCK)
            sock.sendall((json.dumps(cmd) + "\n").encode())
            sock.close()
            return True
        except Exception:
            return False

    def _watch(self):
        try:
            assert self.proc is not None and self.proc.stderr is not None
            for line in self.proc.stderr:
                try:
                    text = line.decode(errors="replace")
                except Exception:
                    continue
                m = re.search(r"icy-title:\s*(.+)", text, re.I)
                if m:
                    self.on_meta(m.group(1).strip()[:160])
        except Exception:
            pass

    def play(self, url, volume=None):
        if volume is not None:
            self.volume = volume
        self.stop(silent=True)
        try:
            if os.path.exists(IPC_SOCK):
                os.unlink(IPC_SOCK)
        except Exception:
            pass
        mpv = shutil.which("mpv")
        if not mpv:
            self.on_state("mpv not found — run install.sh", False)
            return False
        cmd = [mpv, "--no-video",
               f"--input-ipc-server={IPC_SOCK}",
               f"--volume={self.volume}", "--really-quiet",
               "--msg-level=all=info", "--", url]
        try:
            self.proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE, stdin=subprocess.DEVNULL)
        except Exception as e:
            self.on_state(f"start failed: {e}", False)
            return False
        self.url = url
        self.on_state("Playing", True)
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()
        # re-apply mute state on fresh process
        if self.muted:
            self._ipc_send({"command": ["set_property", "mute", True]})
        threading.Thread(target=self._reap, daemon=True).start()
        return True

    def _reap(self):
        p = self.proc
        if p is None:
            return
        rc = p.wait()
        if self.proc is p:  # not replaced by a new play()
            self.proc = None
            if rc not in (0, None):
                self.on_state("Stream ended / error — try another", False)
            else:
                self.on_state("Stopped", False)

    def stop(self, silent=False):
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                except Exception:
                    pass
        if not silent:
            self.on_state("Stopped", False)

    def set_volume(self, v):
        self.volume = int(v)
        if self.muted:
            self._pre_mute_vol = self.volume
            return
        if self.proc and self.proc.poll() is None:
            if not self._ipc_send({"command": ["set_property", "volume", self.volume]}):
                self.play(self.url)  # IPC failed → respawn with new volume

    def toggle_mute(self):
        self.muted = not self.muted
        if self.muted:
            self._pre_mute_vol = self.volume
        ok = False
        if self.proc and self.proc.poll() is None:
            ok = self._ipc_send({"command": ["set_property", "mute", self.muted]})
        return self.muted


# ---------------- network helpers ----------------
def check_stream(url, timeout=10):
    """GET a few bytes (never HEAD — Icecast 405s on HEAD). Returns (ok, note)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": UA, "Icy-MetaData": "1"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            chunk = r.read(2048)
            ctype = r.headers.get_content_type()
            if len(chunk) > 0:
                return True, f"OK ({ctype})"
            return False, "empty response"
    except Exception as e:
        return False, str(e)[:90]


def api_search(params, timeout=15):
    qs = urllib.parse.urlencode(params)
    req = urllib.request.Request(API + "/json/stations/search?" + qs,
                                 headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for s in data:
        url = s.get("url_resolved") or s.get("url") or ""
        codec = (s.get("codec") or "").upper()
        if codec in ("OGG", "OPUS") or ".mpd" in url:
            continue  # keep mpv-safe MP3/AAC only
        out.append({
            "name": s.get("name", "Unknown")[:60],
            "country": s.get("country", "?")[:24],
            "category": (s.get("tags") or "music").split(",")[0][:20] or "music",
            "codec": codec or "?",
            "bitrate": s.get("bitrate") or 128,
            "url": url,
        })
    return out


# ---------------- Shoutcast directory (no key needed) ----------------
# Same JSON endpoint the shoutcast.com website itself uses for searches.
SHOUTCAST_SEARCH = "https://directory.shoutcast.com/Search/UpdateAdvancedSearch"
SHOUTCAST_TUNEIN = "http://yp.shoutcast.com/sbin/tunein-station.m3u?id={sid}"


def shoutcast_search(genre="", name="", limit=20, timeout=20):
    """Search Shoutcast's public directory. Returns MP3/AAC stations, most listeners first."""
    form = urllib.parse.urlencode(
        {"genre": genre, "station": name, "type": "",
         "artist": "", "song": ""}).encode()
    req = urllib.request.Request(
        SHOUTCAST_SEARCH, data=form,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) MintRadio/1.0",
                 "X-Requested-With": "XMLHttpRequest"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8", "replace"))
    out = []
    for s in data:
        fmt = (s.get("Format") or "").lower()
        if "mpeg" in fmt:
            codec = "MP3"
        elif "aac" in fmt:
            codec = "AAC"
        else:
            continue
        try:
            bitrate = int(s.get("Bitrate") or 0) or 128
        except (TypeError, ValueError):
            bitrate = 128
        cat = ((s.get("Genre") or genre or "music").split() or ["music"])[0].lower()[:20]
        out.append({
            "name": str(s.get("Name", "Unknown"))[:60],
            "country": "Web",
            "category": cat,
            "codec": codec,
            "bitrate": bitrate,
            "url": SHOUTCAST_TUNEIN.format(sid=s.get("ID")),
            "listeners": s.get("Listeners") or 0,
            "source": "Shoutcast",
        })
    out.sort(key=lambda s: s["listeners"], reverse=True)
    return out[:limit]


# Shoutcast serves max ~200 rows per query, so big scans fan out across genres.
SHOUTCAST_GENRES = ["pop", "rock", "jazz", "classical", "news", "talk", "dance",
                    "chill", "oldies", "country", "hip hop", "latin", "electronic",
                    "ambient", "blues", "reggae", "metal", "folk", "sports", "world"]


def shoutcast_scan(genres=None, per_genre=200, max_total=1000, timeout=20):
    """Fan out across genres to cover far more than one query's ~200 rows."""
    merged = {}
    for g in (genres or SHOUTCAST_GENRES):
        try:
            for s in shoutcast_search(genre=g, limit=per_genre, timeout=timeout):
                key = s["url"]  # tune-in URL embeds the unique station ID
                if key not in merged:
                    merged[key] = s
        except Exception:
            continue  # one bad genre must not kill the whole scan
        if len(merged) >= max_total:
            break
    out = sorted(merged.values(), key=lambda s: s["listeners"], reverse=True)
    return out[:max_total]


def resolve_stream(url, timeout=10):
    """Resolve a playlist/tune-in URL to the direct stream URL (mpv plays either)."""
    PLAYLIST_TYPES = ("audio/x-mpegurl", "audio/x-scpls",
                      "application/vnd.apple.mpegurl", "application/x-mpegurl")
    try:
        low = url.lower()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            ctype = r.headers.get_content_type()
            if (ctype.startswith("audio/")
                    and ctype not in PLAYLIST_TYPES
                    and not low.endswith((".m3u", ".pls", ".xspf"))
                    and "tunein" not in low):
                return url  # already a direct stream
            body = r.read(16384).decode("utf-8", "replace")
        for line in body.splitlines():
            m = re.search(r"https?://[^\s\"']+", line)
            if m and "yp.shoutcast.com" not in m.group(0):
                return m.group(0)
    except Exception:
        pass
    return url


# ---------------- Icecast directory via internet-radio.com (no key needed) ----------------
# Mostly Icecast servers; server-rendered pages with direct stream URLs embedded.
IR_BASE = "https://www.internet-radio.com"
IR_UA = "Mozilla/5.0 (X11; Linux x86_64) MintRadio/1.0"
IR_GENRES = ["pop", "rock", "jazz", "classical", "dance", "chillout", "oldies",
             "country", "talk", "news", "ambient", "reggae"]


def icecast_search(genre="", query="", pages=3, limit=200, timeout=20):
    """Scrape internet-radio.com genre/search pages (sorted by listeners)."""
    paths = []
    if genre and genre != "All genres":
        g = genre.lower().replace(" ", "")
        paths = [f"/stations/{g}/?sortby=listeners"] + \
                [f"/stations/{g}/page{n}?sortby=listeners" for n in range(2, pages + 1)]
    else:
        q = urllib.parse.quote(query or "music")
        paths = [f"/search/?radio={q}"]
    out, seen = [], set()
    for path in paths:
        try:
            req = urllib.request.Request(IR_BASE + path, headers={"User-Agent": IR_UA})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                page = htmlmod.unescape(r.read().decode("utf-8", "replace"))
        except Exception:
            continue
        for row in re.findall(r"<tr>(.*?)</tr>", page, re.S):
            m = re.search(r"playlistgenerator/\?u=(http[^&\"']+)", row)
            if not m:
                continue
            url = urllib.parse.unquote(m.group(1)).split()[0]
            if not url or url in seen:
                continue
            seen.add(url)
            nm = re.search(r"<h4[^>]*>(?:<a[^>]*>)?([^<]{2,80})(?:</a>)?</h4>", row)
            name = nm.group(1).strip() if nm else "Unknown"
            li = re.search(r"(\d+)\s+Listeners", row)
            br = re.search(r"(\d+)\s+Kbps", row)
            gm = re.search(r"Genres:(.*?)</td>", row, re.S)
            cat = "music"
            if gm:
                words = re.findall(r"[A-Za-z]+", re.sub(r"<[^>]+>", " ", gm.group(1)))
                if words:
                    cat = words[0].lower()[:20]
            try:
                bitrate = int(br.group(1)) if br else 128
            except (TypeError, ValueError):
                bitrate = 128
            out.append({
                "name": name[:60], "country": "Web", "category": cat,
                "codec": "MP3", "bitrate": bitrate, "url": url,
                "listeners": int(li.group(1)) if li else 0,
                "source": "Icecast",
            })
            if len(out) >= limit:
                return out
    out.sort(key=lambda s: s["listeners"], reverse=True)
    return out[:limit]


def icecast_scan(genres=None, pages_per_genre=3, max_total=500, timeout=20):
    """Fan out across genres for a wide Icecast trawl."""
    merged = {}
    for g in (genres or IR_GENRES):
        for s in icecast_search(genre=g, pages=pages_per_genre,
                                limit=max_total, timeout=timeout):
            if s["url"] not in merged:
                merged[s["url"]] = s
        if len(merged) >= max_total:
            break
    return sorted(merged.values(),
                  key=lambda s: s["listeners"], reverse=True)[:max_total]


# ---------------- tray (Mint/Cinnamon native XApp, optional, no extra deps) ----------------
try:
    import gi as _gi
    _gi.require_version("XApp", "1.0")
    from gi.repository import XApp as _XApp, Gtk as _Gtk, GLib as _GLib
    _HAVE_TRAY = True
except Exception:
    _HAVE_TRAY = False


# ---------------- GUI ----------------
import tkinter as tk
from tkinter import messagebox, ttk


class SearchDialog(tk.Toplevel):
    def __init__(self, master, on_add):
        super().__init__(master)
        self.title("Search radio-browser.info")
        self.geometry("620x420")
        self.on_add = on_add
        self.results = []
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Query:").pack(side="left")
        self.q = tk.StringVar(value="jazz")
        ttk.Entry(top, textvariable=self.q, width=22).pack(side="left", padx=6)
        ttk.Label(top, text="Country:").pack(side="left")
        self.cc = tk.StringVar(value="GB")
        ttk.Combobox(top, textvariable=self.cc, width=6,
                     values=["", "GB", "US", "DE", "FR", "NL", "CA", "AU", "JP"]).pack(side="left", padx=6)
        ttk.Button(top, text="Search", command=self.run).pack(side="left")
        self.list = tk.Listbox(self, font=("", 10), bg=NAVY["field"], fg=NAVY["fg"],
                               selectbackground=NAVY["sel"], selectforeground="#ffffff",
                               highlightbackground=NAVY["bg3"], relief="flat",
                               activestyle="none")
        self.list.pack(fill="both", expand=True, padx=8)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.status = tk.StringVar(value="Public-domain directory, ~30k stations.")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Button(bar, text="Add selected", command=self.add).pack(side="right")

    def run(self):
        self.status.set("Searching…")
        self.update_idletasks()
        params = {"name": self.q.get(), "hidebroken": "true",
                  "order": "votes", "reverse": "true", "limit": 40}
        if self.cc.get().strip():
            params["countrycode"] = self.cc.get().strip().upper()
        try:
            self.results = api_search(params)
        except Exception as e:
            self.status.set(f"Search failed: {e}"[:100])
            return
        self.list.delete(0, "end")
        for s in self.results:
            self.list.insert("end", f"{s['name']} [{s['country']}] {s['codec']} {s['bitrate']}k")
        self.status.set(f"{len(self.results)} found (MP3/AAC only).")

    def add(self):
        sel = self.list.curselection()
        if not sel or not self.results:
            return
        self.on_add(self.results[sel[0]])
        self.status.set("Added to your stations.")


class ScanDialog(tk.Toplevel):
    """Bulk scan: top-voted stations by country/tag, stream-test each, import working ones."""

    def __init__(self, master, on_import):
        super().__init__(master)
        self.title("Scan for stations")
        self.geometry("640x460")
        self.on_import = on_import
        self.working = []
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Label(top, text="Source:").pack(side="left")
        self.src = tk.StringVar(value="radio-browser")
        ttk.Combobox(top, textvariable=self.src, width=13, state="readonly",
                     values=["radio-browser", "Shoutcast", "Icecast"]).pack(side="left", padx=4)
        ttk.Label(top, text="Country:").pack(side="left")
        self.cc = tk.StringVar(value="GB")
        ttk.Combobox(top, textvariable=self.cc, width=5,
                     values=["GB", "US", "DE", "FR", "NL", "IE", "CA", "AU", "ES", "IT", "JP", ""]
                     ).pack(side="left", padx=4)
        ttk.Label(top, text="Genre:").pack(side="left")
        self.tag = tk.StringVar(value="pop")
        ttk.Combobox(top, textvariable=self.tag, width=10,
                     values=["All genres", "pop", "rock", "jazz", "classical", "news", "talk",
                             "dance", "chill", "oldies", "folk", "sports"]
                     ).pack(side="left", padx=4)
        ttk.Label(top, text="Top:").pack(side="left")
        self.limit = tk.StringVar(value="100")
        ttk.Combobox(top, textvariable=self.limit, width=4,
                     values=["20", "50", "100", "200", "500"], state="readonly").pack(side="left", padx=4)
        ttk.Button(top, text="Scan", command=self.run).pack(side="left", padx=6)
        self.list = tk.Listbox(self, font=("", 10), selectmode="extended",
                               bg=NAVY["field"], fg=NAVY["fg"],
                               selectbackground=NAVY["sel"], selectforeground="#ffffff",
                               highlightbackground=NAVY["bg3"], relief="flat",
                               activestyle="none")
        self.list.pack(fill="both", expand=True, padx=8)
        bar = ttk.Frame(self, padding=8)
        bar.pack(fill="x")
        self.status = tk.StringVar(value="Picks top-voted MP3/AAC streams and tests each one.")
        ttk.Label(bar, textvariable=self.status).pack(side="left")
        ttk.Button(bar, text="Add selected", command=self.add).pack(side="right")

    def run(self):
        self.status.set("Fetching candidates…")
        self.update_idletasks()
        is_sc = self.src.get() == "Shoutcast"
        is_ir = self.src.get() == "Icecast"
        want = int(self.limit.get())
        try:
            if is_sc:
                # Shoutcast has no country field — Country box is ignored here
                genre = "" if self.tag.get().strip() == "All genres" else self.tag.get().strip()
                if genre:
                    cands = shoutcast_search(genre=genre, limit=want)
                else:
                    cands = shoutcast_scan(max_total=want)
                    self.status.set(f"Scanned all genres: {len(cands)} candidates…")
                    self.update_idletasks()
            elif is_ir:
                # Icecast directory: single genre pages, or fan out for All genres
                genre = self.tag.get().strip()
                if genre == "All genres":
                    cands = icecast_scan(max_total=want)
                    self.status.set(f"Scanned all genres: {len(cands)} candidates…")
                    self.update_idletasks()
                else:
                    cands = icecast_search(genre=genre or "pop",
                                           pages=max(1, min(10, (want + 19) // 20)),
                                           limit=want)
            else:
                params = {"order": "votes", "reverse": "true", "hidebroken": "true",
                          "limit": self.limit.get()}
                if self.tag.get().strip():
                    params["tag"] = self.tag.get().strip()
                if self.cc.get().strip():
                    params["countrycode"] = self.cc.get().strip().upper()
                cands = api_search(params)
        except Exception as e:
            self.status.set(f"Scan failed: {e}"[:100])
            return
        # drop URLs we already have
        have = {s["url"] for s in self.master.stations}
        cands = [c for c in cands if c["url"] not in have][:want]
        if not cands:
            self.status.set("No new candidates — try another country/tag.")
            return
        self.status.set(f"Testing {len(cands)} streams…")
        self.update_idletasks()

        def test(c):
            if is_sc or is_ir:
                # tune-in / playlist links resolve to direct stream URLs — store those
                c["url"] = resolve_stream(c["url"])
            ok, note = check_stream(c["url"], timeout=8)
            if ok:
                m = re.search(r"\(([^)]+)\)", note)
                ctype = m.group(1) if m else ""
                if "aac" in ctype:
                    c["codec"] = "AAC"
                elif "ogg" in ctype or "vorbis" in ctype or "opus" in ctype:
                    c["codec"] = "OGG"
                elif "mpeg" in ctype:
                    c["codec"] = "MP3"
            return c if ok else None

        self.working = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            for res in ex.map(test, cands):
                if res:
                    self.working.append(res)
                self.status.set(f"Testing… {len(self.working)} working so far")
                self.update_idletasks()
        self.list.delete(0, "end")
        for s in self.working:
            self.list.insert("end",
                             f"{s['name']} [{s['country']}] {s['codec']} {s['bitrate']}k")
        self.list.select_set(0, "end")
        self.status.set(f"{len(self.working)}/{len(cands)} working — selected all, adjust then Add.")

    def add(self):
        sel = self.list.curselection()
        if not sel or not self.working:
            return
        picked = [self.working[i] for i in sel]
        self.on_import(picked)
        self.status.set(f"Added {len(picked)} stations.")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{APP} {VERSION}")
        self.geometry("860x600")
        self.minsize(700, 480)
        try:
            icon_png = BASE_DIR / "assets" / "icon.png"
            if icon_png.is_file():
                self._icon = tk.PhotoImage(file=str(icon_png))
                self.iconphoto(True, self._icon)
        except Exception:
            pass
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        RECORD_DIR.mkdir(parents=True, exist_ok=True)

        self.stations = bundled_stations()
        self.favs = set(load_json(FAV_FILE, []))
        self.settings = load_json(SET_FILE, {})
        self.volume = tk.IntVar(value=self.settings.get("volume", 70))
        self.playing_name = tk.StringVar(value="Select a station and press Play")
        self.meta = tk.StringVar(value="")
        self.status = tk.StringVar(value="Ready")
        self.playing_url = None
        self.playing_flag = False
        self.rec_proc = None
        self.sleep_deadline = None
        self.sort_col = "country"
        self.sort_reverse = False

        self.player = Player(self._on_meta, self._on_state)
        self._build_style()
        self._build_widgets()
        self._refresh_tree()
        self._bind_keys()
        # restore last station selection
        last = self.settings.get("last_station")
        if last:
            for iid in self.tree.get_children():
                if self.tree.set(iid, "url") == last:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break
        self.after(1000, self._tick)

    # ----- layout -----
    def _build_style(self):
        st = ttk.Style(self)
        try:
            st.theme_use("clam")
        except Exception:
            pass
        # glassy modern navy
        bg, bg2, bg3 = NAVY["bg"], NAVY["bg2"], NAVY["bg3"]
        field, fg, muted = NAVY["field"], NAVY["fg"], NAVY["muted"]
        acc, ice = NAVY["acc"], NAVY["ice"]
        self.configure(bg=bg)
        self.PAL = dict(NAVY)
        st.configure(".", background=bg, foreground=fg, fieldbackground=field,
                     bordercolor=bg3, lightcolor=bg3, darkcolor=bg)
        st.configure("TFrame", background=bg)
        st.configure("TLabel", background=bg, foreground=fg)
        st.configure("Muted.TLabel", background=bg, foreground=muted)
        st.configure("TButton", background=bg2, foreground=fg, padding=(8, 5),
                     borderwidth=1, relief="flat", focusthickness=0)
        st.map("TButton",
               background=[("active", bg3), ("pressed", "#2a4f8f")],
               foreground=[("active", ice)],
               bordercolor=[("active", acc)])
        st.configure("TEntry", fieldbackground=field, foreground=fg,
                     insertcolor=acc, bordercolor=bg3)
        st.configure("TCombobox", fieldbackground=field, foreground=fg,
                     arrowcolor=acc, bordercolor=bg3)
        st.map("TCombobox", fieldbackground=[("readonly", field)],
               foreground=[("readonly", fg)])
        st.configure("TCheckbutton", background=bg, foreground=fg)
        st.map("TCheckbutton", background=[("active", bg)])
        st.configure("Treeview", background=bg2, foreground=fg,
                     fieldbackground=bg2, rowheight=26, borderwidth=0)
        st.configure("Treeview.Heading", background=bg3, foreground=ice,
                     relief="flat", padding=4, font=("", 9, "bold"))
        st.configure("Rule.TFrame", background=acc)
        st.map("Treeview.Heading", background=[("active", "#2a4f8f")])
        st.map("Treeview", background=[("selected", self.PAL["sel"])],
               foreground=[("selected", "#ffffff")])
        self.tree_tag_colors = True
        st.configure("Horizontal.TScale", background=bg, troughcolor=field,
                     bordercolor=bg3)
        st.configure("Vertical.TScrollbar", background=bg2, troughcolor=bg,
                     bordercolor=bg, arrowcolor=acc)
        st.map("Vertical.TScrollbar", background=[("active", bg3)])
        self.accent = acc

    def _build_widgets(self):
        # now playing header
        head = ttk.Frame(self, padding=(10, 8, 10, 2))
        head.pack(fill="x")
        self.dot = tk.Label(head, text="●", fg=NAVY["off"], bg=NAVY["bg"], font=("", 14))
        self.dot.pack(side="left", padx=(0, 8))
        now = ttk.Frame(head)
        now.pack(side="left", fill="x", expand=True)
        ttk.Label(now, textvariable=self.playing_name, font=("", 12, "bold")).pack(anchor="w")
        ttk.Label(now, textvariable=self.meta, font=("", 9)).pack(anchor="w")
        # icy accent rule under the header
        ttk.Frame(self, height=2, style="Rule.TFrame").pack(fill="x", padx=10)

        # transport
        tr = ttk.Frame(self, padding=(10, 4))
        tr.pack(fill="x")
        self.play_btn = ttk.Button(tr, text="▶ Play", command=self.play_selected, width=9)
        self.play_btn.pack(side="left", padx=2)
        ttk.Button(tr, text="⏹ Stop", command=self.stop).pack(side="left", padx=2)
        ttk.Button(tr, text="★ Fav", command=self.toggle_fav).pack(side="left", padx=2)
        ttk.Button(tr, text="⏺ Rec", command=self.toggle_record).pack(side="left", padx=2)
        ttk.Button(tr, text="⧉ Copy URL", command=self.copy_url).pack(side="left", padx=2)
        ttk.Button(tr, text="✓ Test", command=self.test_selected).pack(side="left", padx=2)

        # filters
        flt = ttk.Frame(self, padding=(10, 4))
        flt.pack(fill="x")
        ttk.Label(flt, text="Search:").pack(side="left")
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_a: self._refresh_tree())
        ttk.Entry(flt, textvariable=self.search_var, width=24).pack(side="left", padx=6)
        ttk.Label(flt, text="Category:").pack(side="left")
        self.cat_var = tk.StringVar(value="All")
        ttk.Combobox(flt, textvariable=self.cat_var, width=9,
                     values=CATEGORIES, state="readonly").pack(side="left", padx=6)
        self.cat_var.trace_add("write", lambda *_a: self._refresh_tree())
        ttk.Label(flt, text="Country:").pack(side="left")
        self.cty_var = tk.StringVar(value="All")
        ttk.Combobox(flt, textvariable=self.cty_var, width=7,
                     values=["All", "UK", "US", "IE", "Web"]).pack(side="left", padx=6)
        self.cty_var.trace_add("write", lambda *_a: self._refresh_tree())
        self.fav_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(flt, text="★ only", variable=self.fav_only,
                        command=self._refresh_tree).pack(side="left", padx=6)
        ttk.Button(flt, text="+ Add station", command=self.add_custom).pack(side="right")
        ttk.Button(flt, text="🔍 Scan", command=self.open_scan).pack(side="right", padx=4)
        ttk.Button(flt, text="🌐 Search online", command=self.open_search).pack(side="right", padx=4)

        # station table
        mid = ttk.Frame(self, padding=(10, 4))
        mid.pack(fill="both", expand=True)
        cols = ("name", "country", "category", "codec", "bitrate", "url")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", selectmode="browse")
        for c, w in (("name", 260), ("country", 60), ("category", 80),
                     ("codec", 60), ("bitrate", 60), ("url", 200)):
            self.tree.heading(c, text=c.title(),
                              command=lambda c=c: self._sort_by(c))
            self.tree.column(c, width=w, anchor="w" if c in ("name", "url") else "center")
        self.tree.column("url", width=200, stretch=True)
        self.tree.tag_configure("odd", background=NAVY["row_alt"])
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self.play_selected())
        self.tree.bind("<Return>", lambda _e: self.play_selected())
        self.tree.bind("<Button-3>", self._preset_assign_click)

        # presets
        pr = ttk.Frame(self, padding=(10, 4))
        pr.pack(fill="x")
        ttk.Label(pr, text="Presets (right-click station to assign):").pack(side="left", padx=(0, 6))
        self.preset_btns = []
        presets = self.settings.get("presets", [None] * 6)
        while len(presets) < 6:
            presets.append(None)
        for i in range(6):
            b = ttk.Button(pr, text=f"P{i+1}", width=5,
                           command=lambda i=i: self.play_preset(i))
            b.pack(side="left", padx=2)
            self.preset_btns.append(b)
        self._update_preset_labels()

        # status bar + sleep/volume controls
        sb2 = ttk.Frame(self, padding=(10, 4))
        sb2.pack(fill="x")
        ttk.Label(sb2, textvariable=self.status).pack(side="left", fill="x", expand=True)
        ttk.Label(sb2, text="Sleep:").pack(side="left", padx=(6, 2))
        self.sleep_var = tk.StringVar(value="Off")
        ttk.Combobox(sb2, textvariable=self.sleep_var, width=7,
                     values=SLEEP_OPTIONS, state="readonly").pack(side="left")
        ttk.Label(sb2, text="Vol:").pack(side="left", padx=(6, 2))
        ttk.Scale(sb2, from_=0, to=130, variable=self.volume,
                  orient="horizontal", length=110,
                  command=lambda _e: self.player.set_volume(self.volume.get())
                  ).pack(side="left")
        self.mute_btn = ttk.Button(sb2, text="🔊", width=4, command=self.mute)
        self.mute_btn.pack(side="left", padx=4)

        # menu
        m = tk.Menu(self, bg=NAVY["bg2"], fg=NAVY["fg"],
                    activebackground=NAVY["acc"], activeforeground=NAVY["bg"])
        mf = tk.Menu(m, tearoff=0, bg=NAVY["bg2"], fg=NAVY["fg"],
                     activebackground=NAVY["acc"], activeforeground=NAVY["bg"])
        mf.add_command(label="Add custom station…", command=self.add_custom)
        mf.add_command(label="Remove selected custom", command=self.remove_custom)
        mf.add_separator()
        self.tray_var = tk.BooleanVar(
            value=bool(self.settings.get("tray", True)) and _HAVE_TRAY)
        mf.add_checkbutton(label="Minimize to tray", variable=self.tray_var,
                           command=self._apply_tray_pref,
                           state="normal" if _HAVE_TRAY else "disabled")
        self.close_to_tray_var = tk.BooleanVar(
            value=bool(self.settings.get("close_to_tray", False)) and _HAVE_TRAY)
        mf.add_checkbutton(label="Close button hides to tray", variable=self.close_to_tray_var,
                           command=self._apply_tray_pref,
                           state="normal" if _HAVE_TRAY else "disabled")
        mf.add_command(label="Hide to tray now", command=self._hide_to_tray,
                       accelerator="Ctrl+H",
                       state="normal" if _HAVE_TRAY else "disabled")
        mf.add_separator()
        mf.add_command(label="Quit", command=self.on_quit)
        m.add_cascade(label="File", menu=mf)
        ms = tk.Menu(m, tearoff=0, bg=NAVY["bg2"], fg=NAVY["fg"],
                     activebackground=NAVY["acc"], activeforeground=NAVY["bg"])
        ms.add_command(label="Search radio-browser…", command=self.open_search)
        ms.add_command(label="Scan for stations to add…", command=self.open_scan)
        ms.add_command(label="Reload list", command=lambda: [self._reload(), self._refresh_tree()])
        m.add_cascade(label="Stations", menu=ms)
        m.add_command(label="About", command=lambda: messagebox.showinfo(
            APP, f"{APP} {VERSION}\nLow-memory radio: Python + Tk + mpv.\n~55 MB playing.\nGPL-3.0 — see LICENSE."))
        self.configure(menu=m)
        self.protocol("WM_DELETE_WINDOW", self._on_close_button)
        self._tray = None
        self._tray_loop = None
        self._hiding = False
        self.bind("<Unmap>", self._on_unmap)

    def _bind_keys(self):
        self.bind("<space>", lambda _e: self.play_selected() if not self.playing_flag else self.stop())
        self.bind("m", lambda _e: self.mute())
        self.bind("f", lambda _e: self.toggle_fav())
        self.bind("/", lambda _e: self.focus_search())
        self.bind("<Control-h>", lambda _e: self._hide_to_tray())
        for i in range(6):
            self.bind(str(i + 1), lambda _e, i=i: self.play_preset(i))

    # ----- data -----
    def _reload(self):
        self.stations = bundled_stations()

    def _filtered(self):
        q = self.search_var.get().lower()
        cat, cty = self.cat_var.get(), self.cty_var.get()
        out = []
        for s in self.stations:
            if cat != "All" and s.get("category") != cat:
                continue
            if cty != "All" and s.get("country") != cty:
                continue
            if self.fav_only.get() and s["url"] not in self.favs:
                continue
            if q and q not in (s["name"] + " " + s.get("country", "")).lower():
                continue
            out.append(s)
        return out

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col, self.sort_reverse = col, False
        self._refresh_tree()

    def _sorted(self, stations):
        col = self.sort_col
        def key(s):
            v = s.get(col, "")
            if col == "bitrate":
                try:
                    return int(v)
                except (TypeError, ValueError):
                    return 0
            return str(v).lower()
        # country sort groups by country, then name — same for category
        if col in ("country", "category"):
            return sorted(stations,
                          key=lambda s: (str(s.get(col, "")).lower(),
                                         s.get("name", "").lower()),
                          reverse=self.sort_reverse)
        return sorted(stations, key=key, reverse=self.sort_reverse)

    def _refresh_tree(self):
        sel_url = None
        sel = self.tree.selection() if hasattr(self, "tree") else ()
        if sel:
            sel_url = self.tree.set(sel[0], "url")
        for c in ("name", "country", "category", "codec", "bitrate", "url"):
            arrow = " ▲" if (c == self.sort_col and not self.sort_reverse) else \
                    " ▼" if c == self.sort_col else ""
            self.tree.heading(c, text=c.title() + arrow,
                              command=lambda c=c: self._sort_by(c))
        self.tree.delete(*self.tree.get_children())
        for i, s in enumerate(self._sorted(self._filtered())):
            star = "★ " if s["url"] in self.favs else ""
            name = s["name"]
            src = s.get("source")
            if src and f"[{src}]" not in name:
                name += f" [{src}]"
            self.tree.insert("", "end", values=(
                star + name, s.get("country", "?"), s.get("category", "?"),
                s.get("codec", "?"), s.get("bitrate", "?"), s["url"]),
                tags=("odd",) if i % 2 else ())
        if sel_url:
            for iid in self.tree.get_children():
                if self.tree.set(iid, "url") == sel_url:
                    self.tree.selection_set(iid)
                    break
        self.status.set(f"{len(self.tree.get_children())} stations")

    def _current(self):
        sel = self.tree.selection()
        if not sel:
            return None
        vals = self.tree.item(sel[0], "values")
        return {"name": vals[0].lstrip("★ "), "country": vals[1],
                "category": vals[2], "codec": vals[3],
                "bitrate": vals[4], "url": vals[5]}

    # ----- transport -----
    def play_selected(self):
        s = self._current()
        if not s:
            self.status.set("Select a station first")
            return
        self._play_station(s)

    def _play_station(self, s):
        self.meta.set("")
        self.playing_name.set(s["name"])
        self.playing_url = s["url"]
        self.settings["last_station"] = s["url"]
        save_json(SET_FILE, {**self.settings, "volume": self.volume.get()})
        ok = self.player.play(s["url"], self.volume.get())
        if ok:
            self.playing_flag = True
            self.dot.configure(fg=self.accent)
            self.play_btn.configure(text="⏸ Playing")
            self.status.set(f"Playing {s['name']} …")
            self._tray_tip()
            if self.sleep_var.get() != "Off":
                mins = int(self.sleep_var.get().split()[0])
                self.sleep_deadline = time.time() + mins * 60

    def stop(self):
        self.player.stop()
        self.playing_flag = False
        self.dot.configure(fg=NAVY["off"])
        self.play_btn.configure(text="▶ Play")
        self.sleep_deadline = None
        self._tray_tip()

    def mute(self):
        m = self.player.toggle_mute()
        self.mute_btn.configure(text="🔇" if m else "🔊")

    def focus_search(self):
        for w in self.winfo_children():
            pass
        self.focus_set()

    # ----- player callbacks (thread-safe via after) -----
    def _on_meta(self, title):
        self.after(0, lambda: self.meta.set("♪ " + title))

    def _on_state(self, msg, playing):
        self.after(0, lambda: [self.status.set(msg),
                               self.dot.configure(fg=self.accent if playing else NAVY["off"])])

    # ----- favorites / presets -----
    def toggle_fav(self):
        s = self._current()
        if not s:
            return
        if s["url"] in self.favs:
            self.favs.discard(s["url"])
        else:
            self.favs.add(s["url"])
        save_json(FAV_FILE, sorted(self.favs))
        self._refresh_tree()

    def play_preset(self, i):
        presets = self.settings.get("presets", [None] * 6)
        url = presets[i] if i < len(presets) else None
        if not url:
            self.status.set(f"P{i+1} empty — right-click a station to assign")
            return
        for s in self.stations:
            if s["url"] == url:
                self._play_station(s)
                return
        self._play_station({"name": f"Preset {i+1}", "url": url})

    def _preset_assign_click(self, event):
        iid = self.tree.identify_row(event.y)
        if not iid:
            return
        self.tree.selection_set(iid)
        menu = tk.Menu(self, tearoff=0, bg=NAVY["bg2"], fg=NAVY["fg"],
                         activebackground=NAVY["acc"], activeforeground=NAVY["bg"])
        vals = self.tree.item(iid, "values")
        for i in range(6):
            menu.add_command(label=f"Assign to P{i+1}",
                             command=lambda i=i, url=vals[5]: self._assign_preset(i, url))
        menu.post(event.x_root, event.y_root)

    def _assign_preset(self, i, url):
        presets = self.settings.get("presets", [None] * 6)
        while len(presets) < 6:
            presets.append(None)
        presets[i] = url
        self.settings["presets"] = presets
        save_json(SET_FILE, {**self.settings, "volume": self.volume.get()})
        self._update_preset_labels()
        self.status.set(f"P{i+1} assigned")

    def _update_preset_labels(self):
        presets = self.settings.get("presets", [None] * 6)
        for i, b in enumerate(self.preset_btns):
            url = presets[i] if i < len(presets) else None
            name = None
            if url:
                for s in self.stations:
                    if s["url"] == url:
                        name = s["name"][:10]
                        break
            b.configure(text=f"P{i+1}" + (f":{name}" if name else ""))

    # ----- custom stations -----
    def add_custom(self, preset=None):
        d = tk.Toplevel(self)
        d.title("Add station")
        d.geometry("400x260")
        f = ttk.Frame(d, padding=12)
        f.pack(fill="both", expand=True)
        vn, vu = tk.StringVar(), tk.StringVar()
        vc, vv = tk.StringVar(value="music"), tk.IntVar(value=128)
        if preset:
            vn.set(preset.get("name", ""))
            vu.set(preset.get("url", ""))
        for lbl, var in (("Name:", vn), ("Stream URL:", vu)):
            ttk.Label(f, text=lbl).pack(anchor="w")
            ttk.Entry(f, textvariable=var, width=48).pack(fill="x", pady=(0, 8))
        row = ttk.Frame(f)
        row.pack(fill="x")
        ttk.Label(row, text="Category:").pack(side="left")
        ttk.Combobox(row, textvariable=vc, width=10,
                     values=["music", "talk", "classical", "jazz", "chill"]).pack(side="left", padx=6)

        def save():
            name, url = vn.get().strip(), vu.get().strip()
            if not name or not url:
                messagebox.showerror("Error", "Name and URL required", parent=d)
                return
            customs = load_json(CUSTOM_FILE, [])
            customs = [c for c in customs if c.get("url") != url]
            customs.append({"name": name, "country": "Custom",
                            "category": vc.get(), "codec": "MP3",
                            "bitrate": vv.get(), "url": url})
            save_json(CUSTOM_FILE, customs)
            self._reload()
            self._refresh_tree()
            d.destroy()
            self.status.set(f"Added {name}")
        ttk.Button(f, text="Save", command=save).pack(pady=8)

    def remove_custom(self):
        s = self._current()
        if not s:
            return
        customs = load_json(CUSTOM_FILE, [])
        urls = {c.get("url") for c in customs}
        if s["url"] not in urls:
            self.status.set("Only custom stations can be removed")
            return
        save_json(CUSTOM_FILE, [c for c in customs if c.get("url") != s["url"]])
        self._reload()
        self._refresh_tree()

    def open_search(self):
        SearchDialog(self, lambda s: [self.add_custom_via_search(s)])

    def open_scan(self):
        ScanDialog(self, self.import_scanned)

    def import_scanned(self, stations):
        customs = load_json(CUSTOM_FILE, [])
        have = {c.get("url") for c in customs} | {s["url"] for s in self.stations}
        added = 0
        first_new = None
        for s in stations:
            if s["url"] not in have:
                customs.append({**s, "country": s.get("country", "?")})
                have.add(s["url"])
                added += 1
                if first_new is None:
                    first_new = s["url"]
        save_json(CUSTOM_FILE, customs)
        self._reload()
        # reset filters so the new stations are actually visible
        self.search_var.set("")
        self.cat_var.set("All")
        self.cty_var.set("All")
        self.fav_only.set(False)
        self._refresh_tree()
        self._update_preset_labels()
        if first_new:
            for iid in self.tree.get_children():
                if self.tree.set(iid, "url") == first_new:
                    self.tree.selection_set(iid)
                    self.tree.see(iid)
                    break
        self.status.set(f"Added {added} new stations")

    def add_custom_via_search(self, s):
        customs = load_json(CUSTOM_FILE, [])
        if all(c.get("url") != s["url"] for c in customs):
            customs.append({**s, "country": s.get("country", "?")})
            save_json(CUSTOM_FILE, customs)
            self._reload()
            self._refresh_tree()

    # ----- misc features -----
    def copy_url(self):
        s = self._current()
        if not s:
            return
        self.clipboard_clear()
        self.clipboard_append(s["url"])
        self.status.set("URL copied to clipboard")

    def test_selected(self):
        s = self._current()
        if not s:
            return
        self.status.set(f"Testing {s['name']}…")
        threading.Thread(target=self._test_bg, args=(s,), daemon=True).start()

    def _test_bg(self, s):
        ok, note = check_stream(s["url"])
        self.after(0, lambda: self.status.set(
            f"{'✅' if ok else '❌'} {s['name']}: {note}"))

    def toggle_record(self):
        if self.rec_proc and self.rec_proc.poll() is None:
            self.rec_proc.terminate()
            self.rec_proc = None
            self.status.set("Recording stopped")
            return
        if not self.playing_url:
            self.status.set("Play a station first, then record")
            return
        ext = "aac" if ".aac" in self.playing_url else "mp3"
        ts = time.strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9-_]+", "_", self.playing_name.get())[:40]
        dest = RECORD_DIR / f"{ts}_{safe}.{ext}"
        try:
            req = urllib.request.Request(self.playing_url, headers={"User-Agent": UA})
            src = urllib.request.urlopen(req, timeout=10)
            fh = open(dest, "wb")

            def pump():
                try:
                    while True:
                        chunk = src.read(65536)
                        if not chunk:
                            break
                        fh.write(chunk)
                        if self.rec_proc is None:
                            break
                except Exception:
                    pass
                finally:
                    try:
                        fh.close()
                    except Exception:
                        pass
            self.rec_proc = subprocess.Popen(["sleep", "1000000"])  # handle flag
            threading.Thread(target=pump, daemon=True).start()
            self.status.set(f"⏺ Recording → {dest.name}")
        except Exception as e:
            self.status.set(f"Record failed: {e}"[:100])

    def _tick(self):
        # sleep timer countdown
        if self.sleep_deadline and self.playing_flag:
            left = self.sleep_deadline - time.time()
            if left <= 0:
                self.stop()
                self.sleep_var.set("Off")
                self.status.set("Sleep timer — stopped")
            elif int(left) % 30 == 0 or left < 60:
                self.status.set(f"Playing — sleep in {int(left//60)}m {int(left%60)}s")
        # persist volume occasionally
        self.settings["volume"] = self.volume.get()
        self.after(1000, self._tick)

    # ----- system tray (XApp) -----
    def _ensure_tray(self):
        """Create the tray icon + menu once (on the Tk thread). Returns True if ready."""
        if self._tray is not None:
            return True
        try:
            if self._tray_loop is None:
                self._tray_loop = _GLib.MainLoop()
                threading.Thread(target=self._tray_loop.run, daemon=True).start()
            icon = _XApp.StatusIcon()
            icon.set_icon_name("mint-radio")
            icon.set_tooltip_text(APP)
            menu = _Gtk.Menu()
            for label, fn in (("Show / Hide", self._tray_toggle),
                              ("Play / Stop", self._tray_playstop),
                              ("Quit", self._tray_quit)):
                it = _Gtk.MenuItem.new_with_label(label)
                # menu callbacks arrive on the GLib thread — hop to Tk thread
                it.connect("activate", lambda _w, f=fn: self.after(0, f))
                it.show()
                menu.append(it)
            icon.set_primary_menu(menu)
            self._tray = icon
            return True
        except Exception:
            self._tray = None
            return False

    def _tray_tip(self):
        if self._tray is None:
            return
        try:
            state = "▶ " + self.playing_name.get() if self.playing_flag else APP
            self._tray.set_tooltip_text(state[:120])
        except Exception:
            pass

    def _on_unmap(self, _event=None):
        if self._hiding or not self.tray_var.get() or not _HAVE_TRAY:
            return
        # state() hasn't transitioned yet when Unmap fires — re-check shortly
        self.after(200, self._maybe_hide_to_tray)

    def _maybe_hide_to_tray(self):
        if self._hiding or not self.tray_var.get() or not _HAVE_TRAY:
            return
        try:
            # iconic = minimized; withdrawn = already in tray (leave it)
            if self.state() != "iconic":
                return
        except Exception:
            return
        self._hide_to_tray()

    def _hide_to_tray(self):
        if not self._ensure_tray():
            return  # no tray available — stay minimized normally
        self._toast("Minimized to tray", "Left-click the tray icon to bring Mint Radio back.")
        self._hiding = True
        try:
            self.withdraw()
            self._tray.set_visible(True)
            self._tray_tip()
        finally:
            self._hiding = False

    def _toast(self, title, sub="", hold_ms=1600):
        """Small borderless banner, bottom-right, fading out on its own."""
        try:
            old = getattr(self, "_toast_win", None)
            if old is not None:
                try:
                    old.destroy()
                except Exception:
                    pass
            tip = tk.Toplevel(self)
            tip.overrideredirect(True)
            tip.attributes("-topmost", True)
            tip.configure(bg=NAVY["bg"])
            bar = tk.Frame(tip, bg=NAVY["acc"], width=4)
            bar.pack(side="left", fill="y")
            body = ttk.Frame(tip, padding=(12, 10))
            body.pack(side="left", fill="both", expand=True)
            ttk.Label(body, text=title, font=("", 10, "bold")).pack(anchor="w")
            if sub:
                ttk.Label(body, text=sub, font=("", 9),
                          style="Muted.TLabel").pack(anchor="w")
            tip.update_idletasks()
            w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
            x = self.winfo_screenwidth() - w - 24
            y = self.winfo_screenheight() - h - 64  # clear of the panel
            tip.geometry(f"+{x}+{y}")
            try:
                tip.attributes("-alpha", 0.96)
                faded = True
            except Exception:
                faded = False  # no compositor — show plain, then close
            self._toast_win = tip
            if faded:
                tip.after(hold_ms, lambda: self._fade_toast(tip, 0.96))
            else:
                self.after(hold_ms, tip.destroy)
        except Exception:
            pass

    def _fade_toast(self, tip, alpha):
        try:
            if alpha <= 0:
                tip.destroy()
                return
            tip.attributes("-alpha", alpha)
            tip.after(70, lambda: self._fade_toast(tip, round(alpha - 0.08, 2)))
        except Exception:
            try:
                tip.destroy()
            except Exception:
                pass

    def _tray_toggle(self):
        try:
            if self.state() == "withdrawn" or not self.winfo_viewable():
                self.deiconify()
                self.lift()
                self.focus_force()
            else:
                self._hide_to_tray()
        except Exception:
            pass

    def _tray_playstop(self):
        if self.playing_flag:
            self.stop()
        else:
            self.play_selected()
        self._tray_tip()

    def _tray_quit(self):
        self.on_quit()

    def _apply_tray_pref(self):
        self.settings["tray"] = bool(self.tray_var.get())
        self.settings["close_to_tray"] = bool(self.close_to_tray_var.get())
        save_json(SET_FILE, {**self.settings, "volume": self.volume.get()})
        if not self.tray_var.get() and self._tray is not None:
            try:
                self._tray.set_visible(False)
            except Exception:
                pass
            try:
                if self.state() == "withdrawn":
                    self.deiconify()
            except Exception:
                pass

    def _on_close_button(self):
        if self.close_to_tray_var.get() and _HAVE_TRAY and self._ensure_tray():
            self._hide_to_tray()
        else:
            self.on_quit()

    def on_quit(self):
        try:
            if self._tray is not None:
                self._tray.set_visible(False)
        except Exception:
            pass
        try:
            if self._tray_loop is not None:
                self._tray_loop.quit()
        except Exception:
            pass
        try:
            if self.rec_proc and self.rec_proc.poll() is None:
                self.rec_proc.terminate()
        except Exception:
            pass
        save_json(SET_FILE, {**self.settings, "volume": self.volume.get()})
        save_json(FAV_FILE, sorted(self.favs))
        self.player.stop(silent=True)
        self.destroy()


def main():
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()

