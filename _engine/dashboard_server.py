#!/usr/bin/env python3
"""Private local runtime for the interactive OSRS Dashboard."""

import json
import os
import platform
import sys
import threading
import time
import webbrowser
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

import console
from version import APP_VERSION, ISSUES_URL, RELEASES_API

# How long a cached copy of the releases feed is treated as current. What's New
# is reference material, not live data, so an hour keeps GitHub's rate limit
# comfortable even if someone reloads the dashboard repeatedly.
RELEASES_CACHE_SECONDS = 3600
RELEASES_TIMEOUT = 2.5
RELEASES_KEEP = 10


# The page the browser lands on while the dashboard is being built. It is
# deliberately self-contained with no fonts, no Chart.js, and no images: it has
# to render instantly and it may be the only thing on screen if the build
# fails, so it cannot depend on anything the build produces.
BUILDING_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Building your dashboard</title>
<style>
  :root { --gold:#c8a45a; --gold-bright:#f0c040; --bg:#0a0804; --card:#120e08;
          --border:#3a2d18; --text:#d4c4a0; --dim:#8f7d5e; --red:#c96a5a; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
         background:var(--bg); color:var(--text);
         font-family:Georgia,'Times New Roman',serif; }
  .wrap { width:min(620px,100%); }
  h1 { margin:0 0 6px; font-size:1.4rem; color:var(--gold-bright); letter-spacing:.5px; font-weight:600; }
  .sub { margin:0 0 20px; color:var(--dim); font-size:.9rem; }
  .card { background:var(--card); border:1px solid var(--border); padding:16px 18px; }
  .bar { height:2px; background:#241c0e; overflow:hidden; margin-bottom:14px; }
  .bar i { display:block; height:100%; width:38%; background:var(--gold);
           animation:slide 1.15s ease-in-out infinite; }
  @keyframes slide { 0%{transform:translateX(-100%);} 100%{transform:translateX(300%);} }
  #log { margin:0; max-height:290px; overflow-y:auto; font-family:ui-monospace,Consolas,monospace;
         font-size:.76rem; line-height:1.65; color:var(--dim); white-space:pre-wrap; word-break:break-word; }
  #log b { color:var(--text); font-weight:400; }
  .failed h1 { color:var(--red); }
  .failed .bar { display:none; }
  .hint { margin:14px 0 0; font-size:.82rem; color:var(--dim); line-height:1.55; }
  code { color:var(--gold); font-family:ui-monospace,Consolas,monospace; font-size:.78rem;
         word-break:break-all; }
</style></head>
<body><div class="wrap" id="wrap">
  <h1 id="title">Building your dashboard</h1>
  <p class="sub" id="sub">Reading your screenshots. The first run on a large account takes a minute.</p>
  <div class="card">
    <div class="bar"><i></i></div>
    <pre id="log">Starting up...</pre>
  </div>
  <p class="hint" id="hint"></p>
</div>
<script>
var seen = 0, logEl = document.getElementById('log'), lines = [];
var clientId = (window.crypto && crypto.randomUUID)
  ? crypto.randomUUID() : 'building-' + Date.now();

// Hold the same presence connection the dashboard uses, so closing this page
// stops the service on the existing grace period rather than stranding it.
(async function presence() {
  var delay = 2000;
  while (true) {
    try {
      var r = await fetch('/api/presence?client_id=' + encodeURIComponent(clientId), {cache:'no-store'});
      if (!r.ok || !r.body) throw new Error('unavailable');
      delay = 2000;
      var reader = r.body.getReader();
      while (!(await reader.read()).done) { /* keep open */ }
    } catch (e) { /* retry */ }
    await new Promise(function (res) { setTimeout(res, delay); });
    delay = Math.min(delay * 2, 15000);
  }
})();

function render() {
  logEl.textContent = lines.join('\\n');
  logEl.scrollTop = logEl.scrollHeight;
}

async function poll() {
  var data;
  try {
    var r = await fetch('/api/build?after=' + seen, {cache:'no-store'});
    data = await r.json();
  } catch (e) {
    setTimeout(poll, 1200);
    return;
  }
  if (data.lines && data.lines.length) {
    data.lines.forEach(function (l) { lines.push(l.text); });
    if (lines.length > 400) lines = lines.slice(-400);
    seen = data.latest;
    render();
  }
  if (data.state === 'ready') {
    document.getElementById('title').textContent = 'Ready';
    document.getElementById('sub').textContent = 'Opening your dashboard...';
    window.location.replace('/');
    return;
  }
  if (data.state === 'failed') {
    document.getElementById('wrap').className = 'wrap failed';
    document.getElementById('title').textContent = 'Something went wrong';
    document.getElementById('sub').textContent = data.message || 'The dashboard could not be built.';
    document.getElementById('hint').innerHTML =
      'The full log was saved to <code>' + (data.log || '') + '</code>. ' +
      'You can attach it to a report at ' +
      '<a href="ISSUES_URL_PLACEHOLDER" target="_blank" rel="noopener" style="color:var(--gold)">Issues</a>.';
    return;
  }
  setTimeout(poll, 900);
}
poll();
</script></body></html>
""".replace("ISSUES_URL_PLACEHOLDER", ISSUES_URL)


# Shown on a first run, when no character has been chosen yet. This is what
# removing the console window depends on: without it a packaged first run has
# no way to ask, and a new user's double-click does nothing at all.
SETUP_PAGE = """<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Choose your character</title>
<style>
  :root { --gold:#c8a45a; --gold-bright:#f0c040; --bg:#0a0804; --card:#120e08;
          --border:#3a2d18; --bright:#6a4f28; --text:#d4c4a0; --dim:#8f7d5e; --red:#c96a5a; }
  * { box-sizing:border-box; }
  body { margin:0; min-height:100vh; display:grid; place-items:center; padding:24px;
         background:var(--bg); color:var(--text); font-family:Georgia,'Times New Roman',serif; }
  .wrap { width:min(560px,100%); }
  h1 { margin:0 0 6px; font-size:1.4rem; color:var(--gold-bright); font-weight:600; letter-spacing:.5px; }
  .sub { margin:0 0 20px; color:var(--dim); font-size:.9rem; line-height:1.5; }
  .card { background:var(--card); border:1px solid var(--border); padding:8px; }
  button.pick { display:flex; align-items:center; justify-content:space-between; width:100%;
    padding:13px 14px; margin:0; background:transparent; border:1px solid transparent;
    color:var(--text); font-family:inherit; font-size:1rem; text-align:left; cursor:pointer; }
  button.pick:hover { background:#18150e; border-color:#3f331e; color:var(--gold-bright); }
  button.pick span { color:var(--dim); font-size:.78rem; }
  button.pick:disabled { opacity:.5; cursor:wait; }
  .manual { margin-top:18px; }
  .manual label { display:block; font-size:.8rem; color:var(--dim); margin-bottom:6px; }
  .row { display:flex; gap:8px; }
  input { flex:1; background:var(--bg); border:1px solid var(--border); color:var(--text);
          font-family:inherit; font-size:.9rem; padding:9px 10px; }
  input:focus { outline:none; border-color:var(--gold); }
  .go { padding:0 16px; background:#1c160a; border:1px solid var(--bright); color:var(--gold-bright);
        font-family:inherit; font-size:.85rem; cursor:pointer; }
  .go:hover { border-color:var(--gold); }
  .err { color:var(--red); font-size:.84rem; margin:12px 0 0; min-height:1em; }
  .hint { color:var(--dim); font-size:.8rem; margin:16px 0 0; line-height:1.55; }
</style></head>
<body><div class="wrap">
  <h1>Choose your character</h1>
  <p class="sub" id="sub">Loading...</p>
  <div class="card" id="list" style="display:none"></div>
  <div class="manual" id="manual" style="display:none">
    <label for="path">Or paste the full path to your character&#39;s screenshot folder</label>
    <div class="row">
      <input id="path" type="text" spellcheck="false" placeholder="C:\\Users\\you\\.runelite\\screenshots\\YourName">
      <button class="go" onclick="submitPath()">Use this</button>
    </div>
  </div>
  <p class="err" id="err"></p>
  <p class="hint" id="hint"></p>
</div>
<script>
var errEl = document.getElementById('err');

async function load() {
  var data;
  try {
    data = await (await fetch('/api/setup', {cache:'no-store'})).json();
  } catch (e) {
    document.getElementById('sub').textContent = 'Could not reach the local service.';
    return;
  }
  var list = document.getElementById('list');
  var chars = data.characters || [];
  if (chars.length) {
    document.getElementById('sub').textContent =
      'These are the characters RuneLite has saved screenshots for. This is remembered, so you only pick once.';
    chars.forEach(function (name) {
      var b = document.createElement('button');
      b.className = 'pick';
      b.innerHTML = '';
      b.appendChild(document.createTextNode(name));
      var s = document.createElement('span');
      s.textContent = 'Use this';
      b.appendChild(s);
      b.onclick = function () { choose({folder: name}, b); };
      list.appendChild(b);
    });
    list.style.display = '';
    document.getElementById('hint').textContent =
      'Wrong one? You can change it later under Settings in the dashboard.';
  } else {
    document.getElementById('sub').textContent = data.base_exists
      ? 'No character folders with screenshots were found in ' + data.base + '.'
      : 'RuneLite\\u2019s screenshots folder was not in the usual place.';
    document.getElementById('hint').textContent =
      'In RuneLite, the screenshot location is shown in the Screenshot plugin settings.';
  }
  document.getElementById('manual').style.display = '';
}

async function choose(body, button) {
  errEl.textContent = '';
  if (button) { button.disabled = true; }
  var result;
  try {
    var r = await fetch('/api/setup', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body)
    });
    result = await r.json();
  } catch (e) {
    errEl.textContent = 'Could not reach the local service.';
    if (button) { button.disabled = false; }
    return;
  }
  if (result.ok) { window.location.replace('/'); return; }
  errEl.textContent = result.message || 'That did not work.';
  if (button) { button.disabled = false; }
}

function submitPath() {
  var value = document.getElementById('path').value.trim();
  if (!value) { document.getElementById('path').focus(); return; }
  choose({path: value}, null);
}

document.getElementById('path').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') submitPath();
});
load();
</script></body></html>
"""


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, engine, root):
        super().__init__(address, handler)
        self.engine = engine
        self.root = Path(root).resolve()
        self.merge_roots = {}
        self.set_roots(root, getattr(engine, "MERGE_FOLDERS", ()))
        self.refresh_lock = threading.Lock()
        # Set below in set_roots; declared here so the attribute always exists.
        self.favorites_lock = threading.Lock()
        self.client_lock = threading.Lock()
        self.clients = set()
        self.had_client = False
        self.client_generation = 0
        self.releases_lock = threading.Lock()
        # Summary of the most recent successful build. Diagnostics are read
        # from here rather than rescanning, so opening the report dialog costs
        # nothing even on an account with thousands of screenshots.
        self.last_build = {}
        self.build_state = {"state": "building", "message": ""}
        self.build_lock = threading.Lock()
        # Set when the service comes up before a character has been chosen.
        # on_account_chosen is supplied by the launcher and binds the engine.
        self.setup_base = None
        self.on_account_chosen = None

    def set_build_setup(self):
        with self.build_lock:
            self.build_state = {"state": "setup", "message": ""}

    def set_build_building(self):
        with self.build_lock:
            self.build_state = {"state": "building", "message": ""}

    def set_build_ready(self):
        with self.build_lock:
            self.build_state = {"state": "ready", "message": ""}

    def set_build_failed(self, message):
        with self.build_lock:
            self.build_state = {"state": "failed", "message": str(message)}

    def get_build_state(self):
        with self.build_lock:
            return dict(self.build_state)

    def set_roots(self, root, merge_folders=()):
        """Declare the folders this account is allowed to serve files from.

        The primary folder plus whatever the user declared to be the same
        account under a former name — nothing else, and never a folder simply
        because it sits beside them. Rebuilt as a whole and swapped in, so a
        request being served while an account switch happens sees either the
        old set or the new one and never a half-built map.
        """
        resolved_root = Path(root).resolve()
        roots = {}
        for folder in merge_folders or []:
            try:
                candidate = Path(folder).resolve()
            except OSError:
                continue
            if candidate == resolved_root or not candidate.is_dir():
                continue
            # Keyed by folder name because that is what survives the browser's
            # URL normalisation. Lowercased for lookup only: Windows paths are
            # case-insensitive, and the stored value is the real resolved path.
            roots[candidate.name.lower()] = candidate
        self.root = resolved_root
        self.merge_roots = roots

    def resolve_declared(self, parts):
        """Map already-split path segments onto a real file inside a declared root.

        Returns the resolved path, or None if it does not land inside the
        primary folder or one of the declared merge folders. The final
        containment check is done against resolved paths rather than against
        the request text, so a link or junction cannot be used to step outside
        a folder that was legitimately declared.
        """
        safe = _safe_segments(parts)
        if not safe:
            return None

        candidates = []
        merge_root = self.merge_roots.get(safe[0].lower())
        if merge_root is not None and len(safe) > 1:
            candidates.append((merge_root, safe[1:]))
        candidates.append((self.root, safe))

        for base, segments in candidates:
            try:
                target = (base / Path(*segments)).resolve()
                target.relative_to(base)
            except (ValueError, OSError):
                continue
            if target.is_file():
                return target
        return None

    def resolve_merged_url(self, url_path):
        """A static GET for a merged folder, or None to fall through to normal serving."""
        if not self.merge_roots:
            return None
        raw = unquote(urlparse(url_path).path)
        parts = [p for p in raw.split("/") if p]
        if not parts or parts[0].lower() not in self.merge_roots:
            return None
        target = self.resolve_declared(parts)
        return str(target) if target is not None else None

    def register_client(self, client_id):
        with self.client_lock:
            first_ever = not self.had_client
            self.clients.add(client_id)
            self.had_client = True
            self.client_generation += 1
            count = len(self.clients)
        if first_ever:
            print("Dashboard tab connected.")
        return count

    def disconnect_client(self, client_id):
        with self.client_lock:
            self.clients.discard(client_id)
            self.client_generation += 1
            generation = self.client_generation
            should_schedule = self.had_client and not self.clients
        if should_schedule:
            timer = threading.Timer(5.0, self.shutdown_if_idle, args=(generation,))
            timer.daemon = True
            timer.start()
        return len(self.clients)

    def shutdown_if_idle(self, expected_generation):
        with self.client_lock:
            should_stop = (
                self.had_client
                and not self.clients
                and self.client_generation == expected_generation
            )
        if should_stop:
            print("\nDashboard tab closed. Stopping the local service...")
            self.shutdown()


def _safe_segments(parts):
    """Path segments with nothing that could climb out of a folder.

    Anything empty, `.`, `..`, or carrying a separator is rejected outright
    rather than cleaned up. A request is data, and the only segments worth
    accepting are ones that are already plain names.
    """
    safe = []
    for part in parts:
        if not part or part in (".", "..") or "/" in part or "\\" in part:
            return None
        safe.append(part)
    return safe


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    server_version = "OSRSDashboard/1.0"

    def log_message(self, _format, *_args):
        return

    def translate_path(self, path):
        """Resolve static files against the server's current root.

        SimpleHTTPRequestHandler normally captures a directory at construction.
        Reading it from the server instead means the root can change once, when
        a first-run user picks their character, without restarting the service
        or moving them to a different port.

        Merged screenshots are the second case. Their `src` in the HTML is a
        real `../OldName/...` path so the file still opens with no service
        running, but a browser resolves a relative reference before it sends
        the request and discards leading `..` segments that climb past the
        root (RFC 3986 remove_dot_segments). So what actually arrives here is
        `/OldName/...`, and the folder it names has to be routed explicitly.
        Only folders this account declared are routable — the name is matched
        against that list, never trusted as a path.
        """
        self.directory = str(self.server.root)
        merged = self.server.resolve_merged_url(path)
        if merged is not None:
            return merged
        return super().translate_path(path)

    def _send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            return None
        if length <= 0 or length > 64 * 1024:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None

    def _favorites_payload(self):
        return {
            "version": 1,
            "player": self.server.engine.PLAYER_NAME,
            "favorites": sorted(self.server.engine.load_favorite_paths()),
        }

    def _write_favorites(self, favorites):
        path = Path(self.server.engine.FAVORITES_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "player": self.server.engine.PLAYER_NAME,
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "favorites": sorted(favorites),
        }
        temp = path.with_name(path.name + ".tmp")
        with temp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)

    def _diagnostics(self):
        """Facts a bug report needs, none of which identify the user.

        Everything here comes from the last build summary or the interpreter
        itself. No scanning, no network, and deliberately no player name or
        file path — a public issue should not carry either.
        """
        build = self.server.last_build or {}
        return {
            "version": APP_VERSION,
            "packaged": bool(getattr(sys, "frozen", False)),
            "os": f"{platform.system()} {platform.release()}",
            "python": platform.python_version(),
            "screenshots": build.get("screenshots"),
            "hiscores_ok": build.get("hiscores"),
            "built_at": build.get("generated_at"),
        }

    def _settings_payload(self):
        """Current account plus the other characters we could switch to."""
        import settings as user_settings

        base = self.server.root.parent
        options = []
        try:
            for entry in sorted(base.iterdir(), key=lambda p: p.name.lower()):
                if entry.is_dir() and next(entry.rglob("*.png"), None) is not None:
                    options.append(entry.name)
        except OSError:
            pass
        current = self.server.root.name
        if current not in options:
            options.insert(0, current)
        remembered = user_settings.last_account()
        return {
            "ok": True,
            "current": current,
            "player": self.server.engine.PLAYER_NAME,
            "options": options,
            "remembered": remembered["primary_folder"] if remembered else None,
            "log": str(self.server.root / "dashboard_log.txt"),
            "settings_file": str(user_settings.settings_path()),
        }

    def _releases_cache_path(self):
        return self.server.root / "releases_cache.json"

    def _load_releases_cache(self):
        try:
            with self._releases_cache_path().open(encoding="utf-8") as f:
                cached = json.load(f)
        except (OSError, json.JSONDecodeError):
            return None
        return cached if isinstance(cached, dict) else None

    def _store_releases_cache(self, payload):
        path = self._releases_cache_path()
        temp = path.with_name(path.name + ".tmp")
        try:
            with temp.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.write("\n")
            os.replace(temp, path)
        except OSError:
            # A cache that cannot be written is not worth failing a page load
            # over. The panel still renders from the response we just fetched.
            pass

    def _fetch_releases(self):
        request = Request(
            RELEASES_API,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"osrs-dashboard/{APP_VERSION}",
            },
        )
        with urlopen(request, timeout=RELEASES_TIMEOUT) as response:
            raw = json.loads(response.read().decode("utf-8"))
        if not isinstance(raw, list):
            raise ValueError("Unexpected releases response.")
        releases = []
        for entry in raw[:RELEASES_KEEP]:
            if not isinstance(entry, dict) or entry.get("draft"):
                continue
            releases.append({
                "tag": entry.get("tag_name") or "",
                "name": entry.get("name") or entry.get("tag_name") or "",
                "published_at": (entry.get("published_at") or "")[:10],
                "notes": entry.get("body") or "",
                "url": entry.get("html_url") or "",
                "prerelease": bool(entry.get("prerelease")),
            })
        return {
            "version": 1,
            "fetched_at": time.time(),
            "current": APP_VERSION,
            "releases": releases,
        }

    def _releases_payload(self):
        """Serve the releases list, preferring a fresh fetch, falling back to disk.

        Offline is an ordinary outcome here, not an error: the panel is
        reference material and the dashboard's core has to render with no
        network at all. A stale cache beats an error message, and no cache at
        all yields an empty list the page hides.
        """
        cached = self._load_releases_cache()
        if cached and (time.time() - float(cached.get("fetched_at") or 0)) < RELEASES_CACHE_SECONDS:
            return {**cached, "stale": False, "current": APP_VERSION}
        try:
            fresh = self._fetch_releases()
        except Exception:  # noqa: BLE001 - offline, rate limited, or changed shape
            if cached:
                return {**cached, "stale": True, "current": APP_VERSION}
            return {"version": 1, "releases": [], "stale": True, "current": APP_VERSION}
        self._store_releases_cache(fresh)
        return {**fresh, "stale": False}

    def _valid_screenshot_path(self, value):
        if not isinstance(value, str) or not value.strip():
            return None
        pure = PurePosixPath(value.replace("\\", "/"))
        if pure.is_absolute() or pure.suffix.lower() != ".png":
            return None

        parts = list(pure.parts)
        # A favorite is stored exactly as the dashboard references the image,
        # so a merged screenshot arrives as `../OldName/...`. Exactly one
        # leading `..` is allowed, and only when the folder it names is one
        # this account declared — every other `..` is still refused outright.
        if parts and parts[0] == "..":
            if len(parts) < 3 or parts[1].lower() not in self.server.merge_roots:
                return None
            parts = parts[1:]
        if ".." in parts:
            return None

        if self.server.resolve_declared(parts) is None:
            return None
        # Return the path as given, not as resolved: favorites are keyed by the
        # same string the HTML uses, so the heart lights up on the next rebuild.
        return pure.as_posix()

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        if route == "/api/presence":
            # Connection-based presence: the page holds this response open for
            # its whole life. When the tab goes away — close, crash, reload,
            # browser exit — the socket drops, the next ping write fails, and
            # the client is unregistered. Unlike unload-event beacons, this
            # cannot be lost, and unlike heartbeat timers it is immune to
            # background-tab throttling.
            client_id = (parse_qs(parsed.query).get("client_id") or [""])[0]
            if not (1 <= len(client_id) <= 128):
                self._send_json(400, {"ok": False, "message": "Invalid dashboard client."})
                return
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.server.register_client(client_id)
            try:
                while True:
                    self.wfile.write(b"ping\n")
                    self.wfile.flush()
                    time.sleep(1.0)
            except OSError:
                pass
            finally:
                self.server.disconnect_client(client_id)
            return
        if route == "/api/status":
            self._send_json(200, {
                "ok": True,
                "player": self.server.engine.PLAYER_NAME,
                "interactive": True,
                "diagnostics": self._diagnostics(),
            })
            return
        if route == "/api/releases":
            with self.server.releases_lock:
                self._send_json(200, self._releases_payload())
            return
        if route == "/api/setup":
            base = self.server.setup_base
            found = []
            if base is not None:
                try:
                    for entry in sorted(Path(base).iterdir(), key=lambda p: p.name.lower()):
                        if entry.is_dir() and next(entry.rglob("*.png"), None) is not None:
                            found.append(entry.name)
                except OSError:
                    pass
            self._send_json(200, {
                "ok": True,
                "base": str(base) if base else "",
                "base_exists": bool(base and Path(base).is_dir()),
                "characters": found,
            })
            return
        if route == "/api/settings":
            self._send_json(200, self._settings_payload())
            return
        if route == "/api/build":
            try:
                after = int((parse_qs(parsed.query).get("after") or ["0"])[0])
            except ValueError:
                after = 0
            state = self.server.get_build_state()
            state.update(console.recent(after=after))
            state["log"] = str(self.server.root / "dashboard_log.txt")
            self._send_json(200, state)
            return
        if route == "/api/favorites":
            with self.server.favorites_lock:
                self._send_json(200, self._favorites_payload())
            return
        if route == "/":
            # Serve the dashboard once it exists; until then, the progress page
            # is what the browser lands on. Checking the file rather than only
            # the state means a returning user with a dashboard already on disk
            # never sees a progress screen they do not need.
            target = self.server.root / "osrs_dashboard.html"
            state = self.server.get_build_state()["state"]
            if state == "setup":
                body = SETUP_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
            if state == "ready" and target.is_file():
                self.path = "/osrs_dashboard.html"
            else:
                body = BUILDING_PAGE.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
                return
        super().do_GET()

    def do_POST(self):
        # An unhandled exception in here kills the connection without a
        # response, and the page cannot tell that apart from the service being
        # gone — it reports "could not reach the local service" and the real
        # cause is only in the log. Always answer, even when answering is the
        # last thing this handler does.
        try:
            self._do_POST()
        except Exception as exc:  # noqa: BLE001 - the response is the diagnosis
            import traceback
            traceback.print_exc()
            try:
                self._send_json(500, {
                    "ok": False,
                    "message": f"{type(exc).__name__}: {exc}",
                })
            except Exception:  # noqa: BLE001 - connection already unusable
                pass

    def _do_POST(self):
        route = urlparse(self.path).path
        payload = self._read_json()
        if payload is None:
            self._send_json(400, {"ok": False, "message": "Invalid request."})
            return

        if route in {"/api/client/connect", "/api/client/disconnect"}:
            client_id = payload.get("client_id")
            if not isinstance(client_id, str) or not (1 <= len(client_id) <= 128):
                self._send_json(400, {"ok": False, "message": "Invalid dashboard client."})
                return
            if route.endswith("/connect"):
                count = self.server.register_client(client_id)
            else:
                count = self.server.disconnect_client(client_id)
            self._send_json(200, {"ok": True, "clients": count})
            return

        if route == "/api/favorite":
            screenshot_path = self._valid_screenshot_path(payload.get("path"))
            favorite = payload.get("favorite")
            if screenshot_path is None or not isinstance(favorite, bool):
                self._send_json(400, {"ok": False, "message": "Invalid screenshot favorite."})
                return
            with self.server.favorites_lock:
                favorites = self.server.engine.load_favorite_paths()
                if favorite:
                    favorites.add(screenshot_path)
                else:
                    favorites.discard(screenshot_path)
                try:
                    self._write_favorites(favorites)
                except OSError as exc:
                    self._send_json(500, {"ok": False, "message": f"Could not save favorites: {exc}"})
                    return
            self._send_json(200, {"ok": True, "favorite": favorite, "count": len(favorites)})
            return

        if route == "/api/setup":
            if self.server.get_build_state()["state"] != "setup":
                self._send_json(409, {"ok": False, "message": "Setup has already finished."})
                return
            base = self.server.setup_base
            folder = payload.get("folder")
            raw_path = payload.get("path")
            target = None
            if isinstance(folder, str) and folder.strip() and base is not None:
                # A name from the list we ourselves produced: confirm it is a
                # real child of the base rather than trusting the round trip.
                if folder not in (".", "..") and "/" not in folder and "\\" not in folder:
                    candidate = Path(base) / folder
                    if candidate.is_dir():
                        target = candidate
            elif isinstance(raw_path, str) and raw_path.strip():
                # Typed by the user because auto-detection found nothing. This
                # is the one place an arbitrary path is legitimate: they are
                # naming their own screenshot folder on their own machine.
                candidate = Path(raw_path.strip().strip('"'))
                if candidate.is_dir():
                    target = candidate
            if target is None:
                self._send_json(400, {"ok": False, "message": "That folder could not be found."})
                return
            target = target.resolve()
            try:
                self.server.on_account_chosen(target)
            except Exception as exc:  # noqa: BLE001 - report instead of dying
                import traceback
                traceback.print_exc()
                self._send_json(500, {"ok": False, "message": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(200, {"ok": True, "character": target.name})
            return

        if route == "/api/settings/character":
            import settings as user_settings

            choice = payload.get("character")
            if not isinstance(choice, str) or not choice.strip():
                self._send_json(400, {"ok": False, "message": "No character given."})
                return
            # Only a real sibling folder is acceptable. This is the same class
            # of check as the screenshot traversal guard: never take a path
            # from the page, only a name we can independently confirm.
            target = self.server.root.parent / choice
            if choice in (".", "..") or "/" in choice or "\\" in choice or not target.is_dir():
                self._send_json(400, {"ok": False, "message": "That character folder was not found."})
                return
            user_settings.remember_account(target.name, display_name=target.name)
            self._send_json(200, {
                "ok": True,
                "character": target.name,
                # Account paths are bound when the engine starts, so switching
                # takes effect on the next launch rather than mid-session.
                # Saying so plainly beats pretending it applied.
                "restart_required": target.name != self.server.root.name,
            })
            return

        if route == "/api/refresh":
            if not self.server.refresh_lock.acquire(blocking=False):
                self._send_json(409, {"ok": False, "message": "A refresh is already running."})
                return
            force_boss_data = payload.get("boss_data") is True
            previous = getattr(self.server.engine, "FORCE_BOSS_DATA_REFRESH", False)
            try:
                if force_boss_data:
                    self.server.engine.FORCE_BOSS_DATA_REFRESH = True
                result = self.server.engine.generate_dashboard()
            except Exception as exc:  # noqa: BLE001 - surface the error in the page
                result = {"ok": False, "message": f"Refresh failed: {exc}"}
            finally:
                self.server.engine.FORCE_BOSS_DATA_REFRESH = previous
                self.server.refresh_lock.release()
            if result.get("ok"):
                self.server.last_build = result
            self._send_json(200 if result.get("ok") else 500, result)
            return

        self._send_json(404, {"ok": False, "message": "Not found."})


def serve_dashboard(engine, open_browser=True, build_first=True,
                    setup_base=None, on_account_chosen=None):
    """Serve immediately, then build, so the browser is the interface throughout.

    The old order was build-then-serve, which meant a full screenshot scan of
    dead air before anything appeared. With the console window gone there is
    nothing on screen during that gap at all, so the order is inverted: the
    service comes up, the browser opens on a progress page, and the build runs
    behind it reporting as it goes.

    Tab lifecycle is unchanged and deliberately reused. The progress page holds
    the same presence connection the dashboard does, so closing it mid-build
    stops the service on the existing five-second grace, and the swap from
    progress page to dashboard is just the reload case that grace already
    covers.
    """
    root = Path(engine.SCREENSHOTS_PATH).resolve()
    handler = partial(DashboardRequestHandler, directory=str(root))
    server = DashboardHTTPServer(("127.0.0.1", 0), handler, engine, root)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

    if not build_first:
        # Caller already has a dashboard on disk and only wants it served.
        server.build_state = {"state": "ready", "message": ""}
        return _run_server(server, url, open_browser)

    if setup_base is not None:
        # Nothing remembered: come up in setup and let the browser ask. This
        # is what allows the console window to go away, since a packaged first
        # run otherwise has no way to put the question anywhere.
        chosen = threading.Event()
        server.setup_base = Path(setup_base)
        server.set_build_setup()

        def account_chosen(path):
            on_account_chosen(path)
            # Re-declare both the root and the merge folders together. Setting
            # the root alone would leave the previous account's folders
            # routable, which is the leak the engine-side reset also guards.
            server.set_roots(engine.SCREENSHOTS_PATH, getattr(engine, "MERGE_FOLDERS", ()))
            server.set_build_building()
            chosen.set()

        server.on_account_chosen = account_chosen

    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    serving.start()
    print(f"\nInteractive dashboard is running locally.\n  {url}\n")
    if open_browser:
        webbrowser.open(url)

    if setup_base is not None:
        print("Waiting for a character to be chosen in your browser...")
        while not chosen.is_set():
            if not serving.is_alive():
                # The window was closed before anything was picked.
                server.server_close()
                print("No character chosen. Nothing was built.")
                return None
            chosen.wait(timeout=0.25)

    try:
        result = engine.generate_dashboard()
    except Exception as exc:  # noqa: BLE001 - the page is the error channel now
        import traceback
        traceback.print_exc()
        server.set_build_failed(f"{type(exc).__name__}: {exc}")
        result = None
    else:
        if result.get("ok"):
            server.last_build = result
            server.set_build_ready()
        else:
            server.set_build_failed(result.get("message", "Dashboard generation failed."))

    # Keep serving until the last tab closes, exactly as before. A failed build
    # still serves, so the progress page can explain what went wrong.
    try:
        serving.join()
    except KeyboardInterrupt:
        server.shutdown()
        serving.join(timeout=5)
    finally:
        server.server_close()
        print("Dashboard service stopped.")
    return result


def _run_server(server, url, open_browser):
    """Serve an already-built dashboard. Retained for callers that pre-build."""
    port = server.server_address[1]

    print("\nInteractive dashboard is running locally.")
    print(f"  {url}")
    print("This window will close automatically after the dashboard tab closes.")
    print("Saved favorites remain on disk.\n")
    if open_browser:
        webbrowser.open(url)

    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        print("Dashboard service stopped.")


if __name__ == "__main__":
    import osrs_dashboard

    # This is the entry point `1 - Refresh Dashboard.bat` uses when running
    # from source, so install the tee here too. Otherwise the log only ever
    # exists for people running the packaged app, which is the opposite of
    # useful during development.
    console.install(log_path=Path(osrs_dashboard.SCREENSHOTS_PATH) / "dashboard_log.txt")
    serve_dashboard(osrs_dashboard)
