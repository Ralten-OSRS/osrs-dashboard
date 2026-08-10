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
  /* Four children now, so the row is laid out explicitly rather than leaning on
     space-between, which only worked while there were exactly two. */
  button.pick { justify-content:flex-start; gap:10px; }
  button.pick .pick-name { flex:1; color:inherit; font-size:1rem; text-align:left; }
  button.pick .tag { flex:none; color:var(--gold); font-size:.7rem;
    border:1px solid var(--bright); padding:1px 6px; letter-spacing:.4px; }
  button.pick .pick-count { flex:none; }
  button.pick .go-mark { flex:none; color:var(--bright); font-size:1.1rem; line-height:1;
    transition:color .15s, transform .15s; }
  button.pick:hover .go-mark { color:var(--gold-bright); transform:translateX(2px); }
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
  .step { margin-top:22px; }
  .step h2 { font-size:.95rem; color:var(--gold-bright); font-weight:600; margin:0 0 4px; }
  /* Step 1 needs a question of its own. Without it the two folder lists read as
     one control with two overflow links, because only the second was headed. */
  .step-head { font-size:.95rem; color:var(--gold-bright); font-weight:600; margin:0 0 8px; }
  .step p { margin:0 0 10px; color:var(--dim); font-size:.82rem; line-height:1.55; }
  button.pick.chosen { background:#1c160a; border-color:var(--bright); color:var(--gold-bright); }
  label.opt { display:flex; align-items:center; gap:10px; padding:10px 14px; cursor:pointer;
    border:1px solid transparent; color:var(--text); font-size:.92rem; }
  label.opt:hover { background:#18150e; border-color:#3f331e; }
  /* Rebuilt rather than tinted: accent-color leaves the unchecked box white,
     which is the one bit of browser chrome loud enough to break this palette. */
  input[type=checkbox] { appearance:none; -webkit-appearance:none; flex:none;
    width:15px; height:15px; border:1px solid var(--bright); background:var(--bg);
    cursor:pointer; position:relative; transition:border-color .15s, background .15s; }
  input[type=checkbox]:hover { border-color:var(--gold); }
  input[type=checkbox]:checked { border-color:var(--gold); background:#5c4620; }
  input[type=checkbox]:checked::after { content:""; position:absolute; left:4px; top:0;
    width:4px; height:9px; border:solid var(--gold-bright); border-width:0 2px 2px 0;
    transform:rotate(45deg); }
  input[type=checkbox]:focus-visible { outline:1px solid var(--gold-bright); outline-offset:2px; }
  label.opt .meta { margin-left:auto; color:var(--dim); font-size:.75rem; }
  label.opt .tag { color:var(--gold); font-size:.7rem; border:1px solid var(--bright);
    padding:1px 6px; letter-spacing:.4px; }
  .disclose { margin-top:10px; }
  .disclose summary { cursor:pointer; color:var(--dim); font-size:.8rem; padding:8px 14px;
    list-style:none; }
  .disclose summary::-webkit-details-marker { display:none; }
  .disclose summary:hover { color:var(--gold-bright); }
  .warn { border:1px solid var(--red); background:#1a0d0a; padding:12px 14px; margin-top:14px;
    font-size:.82rem; line-height:1.6; }
  .warn strong { color:var(--red); display:block; margin-bottom:4px; }
  .warn ul { margin:6px 0 0; padding-left:18px; }
  .warn label { display:flex; gap:8px; align-items:flex-start; margin-top:10px;
    color:var(--text); cursor:pointer; }
  .actions { margin-top:18px; display:flex; gap:10px; align-items:center; }
  .primary { padding:11px 20px; background:#1c160a; border:1px solid var(--bright);
    color:var(--gold-bright); font-family:inherit; font-size:.9rem; cursor:pointer; }
  .primary:hover:not(:disabled) { border-color:var(--gold); }
  .primary:disabled { opacity:.45; cursor:not-allowed; }
</style></head>
<body><div class="wrap">
  <h1>Choose your character</h1>
  <p class="sub" id="sub">Loading...</p>
  <h2 class="step-head" id="pickHead" style="display:none">Which character do you play now?</h2>
  <div class="card" id="list" style="display:none"></div>
  <details class="disclose" id="pickModeWrap" style="display:none">
    <summary id="pickModeSummary">Show other game modes</summary>
    <div class="card" id="pickModes"></div>
  </details>

  <div class="step" id="step2" style="display:none">
    <h2>Has this character had other names?</h2>
    <p>RuneLite makes a new folder when you change your name, so an older name&#39;s
       screenshots sit apart from your current ones. Tick any that were the same
       character and they become one story: one dashboard, one history, one timeline.
       Leave them unticked if you are not sure &mdash; you can change this later.</p>
    <div class="card" id="others"></div>
    <details class="disclose" id="modeWrap" style="display:none">
      <summary id="modeSummary">Show other game modes</summary>
      <div class="card" id="modes"></div>
    </details>
    <div class="warn" id="warn" style="display:none">
      <strong>These are different game modes.</strong>
      <span id="warnText"></span>
      <ul>
        <li>Wealth totals will include loot that was never on your main.</li>
        <li>Pace and Road to Max will be distorted by borrowed XP.</li>
        <li>Hiscores only ever answer for your current main character.</li>
      </ul>
      Your screenshots, Chronicle and Gallery stay accurate &mdash; so this is a
      reasonable way to browse everything you have ever done, as long as you do not
      read the numbers as your main account&#39;s.
      <label><input type="checkbox" id="ack"> I understand these numbers will be mixed.</label>
    </div>
  </div>

  <div class="actions" id="actions" style="display:none">
    <button class="primary" id="go" onclick="submitChoice()">Build my dashboard</button>
    <span class="meta" id="summary" style="color:var(--dim);font-size:.8rem"></span>
  </div>

  <details class="manual disclose" id="manual" style="display:none">
    <summary>My screenshot folder is not listed</summary>
    <label for="path">Paste the full path to your character&#39;s screenshot folder</label>
    <div class="row">
      <input id="path" type="text" spellcheck="false" placeholder="C:\\Users\\you\\.runelite\\screenshots\\YourName">
      <button class="go" onclick="submitPath()">Use this</button>
    </div>
  </details>
  <p class="err" id="err"></p>
  <p class="hint" id="hint"></p>
</div>
<script>
var errEl = document.getElementById('err');
var FOLDERS = [];
var current = null;
var picked = {};

function byName(name) {
  for (var i = 0; i < FOLDERS.length; i++) { if (FOLDERS[i].name === name) return FOLDERS[i]; }
  return null;
}
function shotLabel(n) { return n === 1 ? '1 screenshot' : n.toLocaleString() + ' screenshots'; }

function selectedModes() {
  var modes = {}, names = [current];
  for (var n in picked) { if (picked[n]) names.push(n); }
  names.forEach(function (n) { var f = byName(n); if (f) modes[f.mode] = true; });
  return Object.keys(modes);
}

function option(folder, container) {
  var label = document.createElement('label');
  label.className = 'opt';
  var box = document.createElement('input');
  box.type = 'checkbox';
  box.checked = !!picked[folder.name];
  box.onchange = function () { picked[folder.name] = box.checked; refresh(); };
  label.appendChild(box);
  label.appendChild(document.createTextNode(folder.name));
  if (folder.mode) {
    var tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = folder.mode;
    label.appendChild(tag);
  }
  var meta = document.createElement('span');
  meta.className = 'meta';
  meta.textContent = shotLabel(folder.shots);
  label.appendChild(meta);
  container.appendChild(label);
}

function refresh() {
  var modes = selectedModes();
  var mixed = modes.length > 1;
  document.getElementById('warn').style.display = mixed ? '' : 'none';
  if (mixed) {
    document.getElementById('warnText').textContent =
      'You have selected folders from ' + modes.length + ' different game modes.';
  } else {
    document.getElementById('ack').checked = false;
  }
  var chosen = Object.keys(picked).filter(function (n) { return picked[n]; });
  var total = (byName(current) || {shots: 0}).shots;
  chosen.forEach(function (n) { var f = byName(n); if (f) total += f.shots; });
  document.getElementById('summary').textContent =
    chosen.length
      ? current + ' plus ' + chosen.length + ' other folder' + (chosen.length > 1 ? 's' : '') +
        ' — ' + shotLabel(total)
      : current + ' — ' + shotLabel(total);
  document.getElementById('go').disabled = mixed && !document.getElementById('ack').checked;
}

function pickCurrent(name, button) {
  current = name;
  picked = {};
  Array.prototype.forEach.call(document.querySelectorAll('button.pick'), function (b) {
    b.classList.toggle('chosen', b === button);
  });
  var others = document.getElementById('others');
  var modes = document.getElementById('modes');
  others.innerHTML = '';
  modes.innerHTML = '';
  var modeCount = 0, otherCount = 0;
  FOLDERS.forEach(function (f) {
    if (f.name === name) return;
    if (f.mode) { option(f, modes); modeCount++; } else { option(f, others); otherCount++; }
  });
  if (!otherCount) {
    var none = document.createElement('p');
    none.style.cssText = 'margin:0;padding:12px 14px;color:var(--dim);font-size:.82rem';
    none.textContent = 'No other folders found under a different name.';
    others.appendChild(none);
  }
  document.getElementById('modeWrap').style.display = modeCount ? '' : 'none';
  document.getElementById('modeSummary').textContent =
    'Show other game modes (' + modeCount + ')';
  document.getElementById('step2').style.display = '';
  document.getElementById('actions').style.display = '';
  refresh();
}

async function load() {
  var data;
  try {
    data = await (await fetch('/api/setup', {cache:'no-store'})).json();
  } catch (e) {
    document.getElementById('sub').textContent = 'Could not reach the local service.';
    return;
  }
  var list = document.getElementById('list');
  FOLDERS = data.folders || [];
  if (FOLDERS.length) {
    document.getElementById('sub').textContent =
      'This app tells one account\\u2019s story. Pick the character you play now \\u2014 ' +
      'if it has had other names, you can add those next so nothing is left out. ' +
      'This is remembered, so you only do it once.';
    // Game modes are collapsed here too, not just in the "other names" step.
    // Picking a league folder as your character is allowed and produces a
    // coherent league dashboard, but it is never the common answer, and on a
    // machine with a dozen folders it buries the one the user wants.
    var modeHolder = document.getElementById('pickModes');
    var modeCount = 0;
    FOLDERS.forEach(function (f) {
      var b = document.createElement('button');
      b.className = 'pick';
      var nameEl = document.createElement('span');
      nameEl.className = 'pick-name';
      nameEl.textContent = f.name;
      b.appendChild(nameEl);
      if (f.mode) {
        var tag = document.createElement('span');
        tag.className = 'tag';
        tag.textContent = f.mode;
        b.appendChild(tag);
      }
      var s = document.createElement('span');
      s.className = 'pick-count';
      s.textContent = shotLabel(f.shots);
      b.appendChild(s);
      // Without this the rows read as a list rather than as choices. The count
      // alone gave no signal that anything here was clickable.
      var go = document.createElement('span');
      go.className = 'go-mark';
      go.textContent = '\\u203A';
      b.appendChild(go);
      b.onclick = function () { pickCurrent(f.name, b); };
      if (f.mode) { modeHolder.appendChild(b); modeCount++; }
      else { list.appendChild(b); }
    });
    document.getElementById('pickModeWrap').style.display = modeCount ? '' : 'none';
    document.getElementById('pickModeSummary').textContent =
      'Show other game modes (' + modeCount + ')';
    document.getElementById('pickHead').style.display = '';
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

function submitChoice() {
  if (!current) return;
  var also = Object.keys(picked).filter(function (n) { return picked[n]; });
  choose({
    folder: current,
    also_folders: also,
    acknowledged: document.getElementById('ack').checked
  }, document.getElementById('go'));
}

function submitPath() {
  var value = document.getElementById('path').value.trim();
  if (!value) { document.getElementById('path').focus(); return; }
  choose({path: value}, null);
}

document.getElementById('path').addEventListener('keydown', function (e) {
  if (e.key === 'Enter') submitPath();
});
document.getElementById('ack').addEventListener('change', refresh);
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
        # Folders the setup page declared as former names, handed to the
        # launcher's callback so the engine is bound to the whole account
        # rather than only the folder that was clicked.
        self.pending_also_folders = []
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
        # Favourite keys name their own folder, including the primary one, so
        # they resolve against a slightly wider map than static URLs do. The
        # primary folder is not routable as a URL prefix — the browser already
        # serves it from the root — but it is a legitimate key prefix.
        self.favorite_roots = dict(roots)
        self.favorite_roots[resolved_root.name.lower()] = resolved_root

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


def _count_screenshots(folder, cap=100000):
    total = 0
    for _ in folder.rglob("*.png"):
        total += 1
        if total >= cap:
            break
    return total


def folder_inventory(base):
    """Every screenshot folder under `base`, with a mode hint where one is evident.

    RuneLite names a folder `<account>-<world type>`, so `Ralten-Beta` and
    `Ralten-Raging Echoes League` are the same character in a different game
    mode. This does not hardcode that vocabulary — there is no list of league
    names here, deliberately, because the naming belongs to RuneLite and this
    project has already been broken once by depending on it.

    Instead the hint is drawn from the folder set itself: a name is treated as
    a possible mode only when the part before its first hyphen is *also* a
    real folder. That is evidence rather than a guess, and when it is wrong or
    absent the user simply sees an ordinary folder and ticks it themselves.
    Nothing stored depends on this; it only decides what the page collapses.
    """
    base = Path(base) if base else None
    if base is None or not base.is_dir():
        return []
    try:
        folders = [p for p in sorted(base.iterdir(), key=lambda p: p.name.lower()) if p.is_dir()]
    except OSError:
        return []

    known = {p.name.lower() for p in folders}
    inventory = []
    for folder in folders:
        shots = _count_screenshots(folder)
        if not shots:
            continue
        family, mode = folder.name, ""
        head, sep, tail = folder.name.partition("-")
        if sep and tail.strip() and head.lower() in known:
            family, mode = head, tail.strip()
        inventory.append({
            "name": folder.name,
            "shots": shots,
            "family": family,
            "mode": mode,
        })
    return inventory


def _modes_in(selection, inventory):
    """How many distinct game modes a chosen set of folders spans."""
    by_name = {item["name"]: item for item in inventory}
    return {by_name[name]["mode"] for name in selection if name in by_name}


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

    def _validated_composition(self, target, payload, base):
        """Check the declared merge list. Returns (folders, error_message).

        Two things are enforced here rather than only in the page. Every name
        must be a real sibling folder, because a name arriving over the wire is
        data. And a set spanning more than one game mode must carry an explicit
        acknowledgement, because that combination knowingly breaks wealth, pace
        and hiscores — DESIGN.md says the user may do it, and equally that they
        must be told first. A page that skipped the warning would otherwise be
        able to skip the consequence.
        """
        raw = payload.get("also_folders")
        if raw in (None, []):
            return [], None
        if not isinstance(raw, list):
            return None, "Invalid folder selection."

        also = []
        for name in raw:
            if not isinstance(name, str) or not name.strip():
                return None, "Invalid folder selection."
            name = name.strip()
            if name in (".", "..") or "/" in name or "\\" in name:
                return None, "Invalid folder selection."
            if name == target.name or name in also:
                continue
            if not (base / name).is_dir():
                return None, f"The folder '{name}' could not be found."
            also.append(name)

        inventory = folder_inventory(base)
        if len(_modes_in([target.name] + also, inventory)) > 1 and not payload.get("acknowledged"):
            return None, ("Combining different game modes changes what the numbers mean. "
                          "Please confirm you understand before continuing.")
        return also, None

    def _all_favorites(self):
        engine = self.server.engine
        return engine.load_all_favorites(engine.SCREENSHOTS_PATH, engine.MERGE_FOLDERS)

    def _favorites_payload(self):
        return {
            "version": self.server.engine.FAVORITES_SCHEMA_VERSION,
            "player": self.server.engine.PLAYER_NAME,
            "favorites": sorted(self._all_favorites()),
        }

    def _toggle_favorite(self, key, favorite):
        """Add or remove one favourite, in the folder that owns the screenshot.

        A favourite key names its own folder, so the write goes there rather
        than to whichever folder happens to be primary. That is what makes
        un-favouriting work at all once folders are merged: writing every
        change to the primary would leave a removal fighting a union that
        keeps re-reading the original folder's file and putting it back.
        """
        folder = self.server.favorite_roots.get(key.split("/", 1)[0].lower())
        if folder is None:
            return None
        engine = self.server.engine
        owned = engine.load_favorite_paths(
            engine.favorites_file_for(folder), primary_folder=folder.name
        )
        if favorite:
            owned.add(key)
        else:
            owned.discard(key)
        self._write_favorites(owned, engine.favorites_file_for(folder))
        return self._all_favorites()

    def _write_favorites(self, favorites, path=None):
        path = Path(path or self.server.engine.FAVORITES_FILE)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            # Version 2 keys every favourite by its own folder, so the record
            # survives the account being recomposed. The engine upgrades a
            # version 1 file on read; this is where the upgrade lands on disk,
            # the first time the user toggles anything.
            "version": self.server.engine.FAVORITES_SCHEMA_VERSION,
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

        # A favourite key names its own folder first: `Ralten/Boss Kills/x.png`.
        # That is what makes it survive the account being recomposed, and it
        # means the folder must be one this account actually declared rather
        # than any name the page cares to send.
        parts = _safe_segments(pure.parts)
        if not parts or len(parts) < 2:
            return None
        folder = self.server.favorite_roots.get(parts[0].lower())
        if folder is None:
            return None

        try:
            target = (folder / Path(*parts[1:])).resolve()
            target.relative_to(folder)
        except (ValueError, OSError):
            return None
        if not target.is_file():
            return None

        # Canonicalise the folder segment to the real directory name so two
        # spellings of the same screenshot cannot become two favourites.
        return "/".join([folder.name] + list(parts[1:]))

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
            inventory = folder_inventory(base)
            self._send_json(200, {
                "ok": True,
                "base": str(base) if base else "",
                "base_exists": bool(base and Path(base).is_dir()),
                # Kept for older generated pages that read a flat name list.
                "characters": [item["name"] for item in inventory],
                "folders": inventory,
                "current": None,
                "also": [],
            })
            return
        if route == "/api/composition":
            # The same question the setup screen asks, reachable afterwards so
            # the account stays recomposable. Reports what is in the dashboard
            # right now alongside everything available.
            import settings as user_settings
            base = self.server.root.parent
            record = user_settings.get_account(user_settings.load(), self.server.root.name)
            self._send_json(200, {
                "ok": True,
                "base": str(base),
                "base_exists": base.is_dir(),
                "folders": folder_inventory(base),
                "current": self.server.root.name,
                "also": list(record["also_folders"]) if record else [],
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
                try:
                    favorites = self._toggle_favorite(screenshot_path, favorite)
                except OSError as exc:
                    self._send_json(500, {"ok": False, "message": f"Could not save favorites: {exc}"})
                    return
                if favorites is None:
                    self._send_json(400, {"ok": False, "message": "Invalid screenshot favorite."})
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

            also, error = self._validated_composition(target, payload, Path(base) if base else target.parent)
            if error:
                self._send_json(400, {"ok": False, "message": error})
                return
            self.server.pending_also_folders = also

            try:
                self.server.on_account_chosen(target)
            except Exception as exc:  # noqa: BLE001 - report instead of dying
                import traceback
                traceback.print_exc()
                self._send_json(500, {"ok": False, "message": f"{type(exc).__name__}: {exc}"})
                return
            self._send_json(200, {"ok": True, "character": target.name})
            return

        if route == "/api/composition":
            import settings as user_settings

            base = self.server.root.parent
            also, error = self._validated_composition(self.server.root, payload, base)
            if error:
                self._send_json(400, {"ok": False, "message": error})
                return

            record = user_settings.describe_account(self.server.root.name, also_folders=also)
            folders = user_settings.account_folders(record, base)
            merges = [str(path) for path in folders[1:]]

            # Applied live rather than deferred to the next launch. The engine
            # reads these at build time, and the page already has a Refresh
            # button, so re-pointing both here turns "change what is included"
            # into one extra click instead of a restart.
            self.server.engine.MERGE_FOLDERS = merges
            self.server.set_roots(self.server.engine.SCREENSHOTS_PATH, merges)

            self._send_json(200, {
                "ok": True,
                "current": self.server.root.name,
                "also": list(record["also_folders"]),
                "refresh_required": True,
            })
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
            on_account_chosen(path, server.pending_also_folders)
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
