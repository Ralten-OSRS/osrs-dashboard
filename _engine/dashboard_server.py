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
from urllib.parse import parse_qs, urlparse
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


class DashboardHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, engine, root):
        super().__init__(address, handler)
        self.engine = engine
        self.root = Path(root).resolve()
        self.refresh_lock = threading.Lock()
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

    def set_build_ready(self):
        with self.build_lock:
            self.build_state = {"state": "ready", "message": ""}

    def set_build_failed(self, message):
        with self.build_lock:
            self.build_state = {"state": "failed", "message": str(message)}

    def get_build_state(self):
        with self.build_lock:
            return dict(self.build_state)

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


class DashboardRequestHandler(SimpleHTTPRequestHandler):
    server_version = "OSRSDashboard/1.0"

    def log_message(self, _format, *_args):
        return

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
        if pure.is_absolute() or ".." in pure.parts or pure.suffix.lower() != ".png":
            return None
        candidate = (self.server.root / Path(*pure.parts)).resolve()
        try:
            candidate.relative_to(self.server.root)
        except ValueError:
            return None
        if not candidate.is_file():
            return None
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
            if self.server.get_build_state()["state"] == "ready" and target.is_file():
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


def serve_dashboard(engine, open_browser=True, build_first=True):
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

    serving = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
    serving.start()
    print(f"\nInteractive dashboard is running locally.\n  {url}\n")
    if open_browser:
        webbrowser.open(url)

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
