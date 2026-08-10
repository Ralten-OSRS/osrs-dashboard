#!/usr/bin/env python3
"""Single source of truth for the app version and its GitHub coordinates.

This used to live in `dashboard_app.py`, which meant the engine that writes the
dashboard HTML had no way to know what version produced it. The in-app Report
Issue button needs that number in every report, so it moved here where both the
launcher and the generator can read it.

Bump APP_VERSION in the same commit that gets tagged for a release. A stale
value tells users they are current when they are not, and now also stamps the
wrong build onto every issue they file.
"""

APP_VERSION = "1.2.0"

REPO = "Ralten-OSRS/osrs-dashboard"
REPO_URL = f"https://github.com/{REPO}"
RELEASES_API = f"https://api.github.com/repos/{REPO}/releases"
RELEASES_LATEST_API = f"{RELEASES_API}/latest"
RELEASES_PAGE = f"{REPO_URL}/releases/latest"
NEW_ISSUE_URL = f"{REPO_URL}/issues/new"
ISSUES_URL = f"{REPO_URL}/issues"
