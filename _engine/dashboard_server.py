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

from version import APP_VERSION, RELEASES_API

# How long a cached copy of the releases feed is treated as current. What's New
# is reference material, not live data, so an hour keeps GitHub's rate limit
# comfortable even if someone reloads the dashboard repeatedly.
RELEASES_CACHE_SECONDS = 3600
RELEASES_TIMEOUT = 2.5
RELEASES_KEEP = 10


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
        if route == "/api/favorites":
            with self.server.favorites_lock:
                self._send_json(200, self._favorites_payload())
            return
        if route == "/":
            self.path = "/osrs_dashboard.html"
        super().do_GET()

    def do_POST(self):
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

        if route == "/api/refresh":
            if not self.server.refresh_lock.acquire(blocking=False):
                self._send_json(409, {"ok": False, "message": "A refresh is already running."})
                return
            try:
                result = self.server.engine.generate_dashboard()
            except Exception as exc:  # noqa: BLE001 - surface the error in the page
                result = {"ok": False, "message": f"Refresh failed: {exc}"}
            finally:
                self.server.refresh_lock.release()
            if result.get("ok"):
                self.server.last_build = result
            self._send_json(200 if result.get("ok") else 500, result)
            return

        self._send_json(404, {"ok": False, "message": "Not found."})


def serve_dashboard(engine, open_browser=True):
    """Build once, open the dashboard, and serve until its last tab closes."""
    result = engine.generate_dashboard()
    if not result.get("ok"):
        raise RuntimeError(result.get("message", "Dashboard generation failed."))

    root = Path(engine.SCREENSHOTS_PATH).resolve()
    handler = partial(DashboardRequestHandler, directory=str(root))
    server = DashboardHTTPServer(("127.0.0.1", 0), handler, engine, root)
    server.last_build = result
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"

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

    serve_dashboard(osrs_dashboard)
