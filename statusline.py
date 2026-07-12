#!/usr/bin/env python3
"""Claude Code status line — context window + plan/rate-limit usage.

Renders one (responsive) line:

    <dir> [branch] | <model> | <ctx bar> <pct>% · <tok>k | 5h <p>% · wk <p>% · <Model> <p>%

- Context bar: reads the JSON blob Claude Code pipes in on stdin, scans the
  session transcript for the most recent MAIN-thread assistant usage, and shows
  how full the context window is (self-correcting across model/window changes).
- Plan usage: 5-hour, weekly, and any per-model rate-limit percentages from the
  same account endpoint `/usage` uses. Cached, background-refreshed, fail-soft —
  a network/token failure just drops that segment and keeps the context bar.

No dependencies beyond the Python 3 stdlib + `curl`. Pure stdout; safe to fail.

Environment overrides:
  CLAUDE_CONFIG_DIR         config dir for the token/cache (default ~/.claude)
  CLAUDE_STATUSLINE_USAGE=0 disable the network plan-usage segment entirely
"""
import sys, json, os, subprocess, time, re

# ---- ANSI helpers ---------------------------------------------------------
RESET = "\033[0m"
DIM = "\033[90m"
BOLD = "\033[1m"
CYAN = "\033[36m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"

# ---- plan-usage (Anthropic account rate-limit) ----------------------------
# Fetched from the undocumented account endpoint that /usage itself uses.
# Cached to disk and refreshed in a detached background process so the status
# line render never blocks on the network.
# Honor CLAUDE_CONFIG_DIR (Claude Code's own override) so this works for users
# who don't keep their config in ~/.claude. Falls back to ~/.claude.
CONFIG_DIR = os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR", "~/.claude"))
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
USAGE_CACHE = os.path.join(CONFIG_DIR, ".usage-cache.json")
USAGE_LOCK = USAGE_CACHE + ".lock"
USAGE_TTL = 60  # seconds a cached response is served before a bg refresh fires
KEYCHAIN_SERVICE = "Claude Code-credentials"  # macOS: OAuth creds in login Keychain
CREDENTIALS_FILE = os.path.join(CONFIG_DIR, ".credentials.json")  # Linux/cloud: file
# Opt out of the network plan-usage segment entirely (offline, privacy, or CI):
# set CLAUDE_STATUSLINE_USAGE=0. The context-window bar always renders locally.
USAGE_ENABLED = os.environ.get("CLAUDE_STATUSLINE_USAGE", "1") not in ("0", "false", "no")


def read_stdin_json():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}


def last_usage_tokens(path):
    """Return context tokens from the last non-sidechain assistant turn."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            tail = 3_000_000  # only inspect the last few MB for speed
            if size > tail:
                fh.seek(size - tail)
                fh.readline()  # drop the partial first line
            lines = fh.read().decode("utf-8", "replace").splitlines()
    except Exception:
        return 0

    for line in reversed(lines):
        line = line.strip()
        if not line or '"usage"' not in line:
            continue
        try:
            obj = json.loads(line)
        except Exception:
            continue
        if obj.get("isSidechain"):
            continue  # subagent turns don't reflect the main context
        usage = (obj.get("message") or {}).get("usage")
        if not usage:
            continue
        return (
            usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0)
        )
    return 0


def git_branch(cwd):
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=0.3,
        )
        b = out.stdout.strip()
        return b if out.returncode == 0 and b and b != "HEAD" else ""
    except Exception:
        return ""


def bar(pct, width=10):
    filled = max(0, min(width, round(pct / 100 * width)))
    color = GREEN if pct < 50 else YELLOW if pct < 80 else RED
    return f"{color}{'█' * filled}{DIM}{'░' * (width - filled)}{RESET}"


def context_window(data, model_id, used):
    """Best-effort context-window size, most-trusted signal first — so the bar
    stays honest as new models/windows ship, ideally without editing this file.

      1. An explicit window the harness hands us. FUTURE-PROOF: the day Claude
         Code exposes the window in the status-line JSON, it's adopted
         automatically and every layer below becomes irrelevant.
      2. An explicit "1m" marker in the model id (the original heuristic).
      3. Behavioural ratchet: a 200k-window model is compacted before it can
         exceed ~200k, so any usage past 200k *proves* a 1M window is live.
         Self-correcting and model-agnostic.
      4. The one manual knob — known 1M-window model families. Update THIS line
         (only) when a new 1M model ships that hasn't yet crossed 200k of use.
      5. Classic 200k default. Safe direction: over-reports fill, never hides it.
    """
    model = data.get("model") if isinstance(data.get("model"), dict) else {}
    for src in (model, data):
        for key in ("context_window", "context_length", "max_context_tokens",
                    "context_limit", "max_input_tokens"):
            w = src.get(key) if isinstance(src, dict) else None
            if isinstance(w, (int, float)) and not isinstance(w, bool) and w > 1000:
                return int(w)
    if "1m" in model_id:
        return 1_000_000
    if used > 200_000:
        return 1_000_000
    if re.search(r"opus-4-[89]|opus-[5-9]|sonnet-[5-9]", model_id):
        return 1_000_000
    return 200_000


# ---- plan-usage helpers ---------------------------------------------------
def _oauth_token():
    """Read the Claude Code OAuth access token, cross-platform.

    Linux/cloud stores it as a plaintext file (~/.claude/.credentials.json);
    macOS keeps it in the login Keychain. Try the file first, then the Keychain,
    so the same script works unmodified on both.
    """
    try:
        with open(CREDENTIALS_FILE) as fh:
            tok = (json.load(fh).get("claudeAiOauth") or {}).get("accessToken")
            if tok:
                return tok
    except Exception:
        pass
    try:
        out = subprocess.run(
            ["security", "find-generic-password", "-s", KEYCHAIN_SERVICE, "-w"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return (json.loads(out.stdout).get("claudeAiOauth") or {}).get("accessToken")
    except Exception:
        pass
    return None


def fetch_usage():
    """Hit the (undocumented) account usage endpoint. Returns raw dict or None."""
    tok = _oauth_token()
    if not tok:
        return None
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "3", USAGE_URL,
             "-H", f"Authorization: Bearer {tok}",
             "-H", "anthropic-beta: oauth-2025-04-20",
             "-H", "Content-Type: application/json"],
            capture_output=True, text=True, timeout=4,
        )
        data = json.loads(out.stdout)
        return data if isinstance(data, dict) and "limits" in data else None
    except Exception:
        return None


def refresh_usage():
    """Fetch and atomically write the cache; run in a detached bg process."""
    data = fetch_usage()
    if data is not None:
        try:
            tmp = USAGE_CACHE + ".tmp"
            with open(tmp, "w") as fh:
                json.dump({"ts": time.time(), "data": data}, fh)
            os.replace(tmp, USAGE_CACHE)
        except Exception:
            pass
    try:
        os.remove(USAGE_LOCK)
    except Exception:
        pass


def load_usage():
    """Return cached usage data, kicking off a bg refresh when it's stale.

    Never blocks on the network: a stale/missing cache spawns a detached
    `--refresh-usage` process (guarded by a short-lived lock) and this render
    uses whatever we already had (possibly None on the very first run).
    """
    ts, data = 0, None
    try:
        with open(USAGE_CACHE) as fh:
            c = json.load(fh)
        ts, data = c.get("ts", 0), c.get("data")
    except Exception:
        pass

    if time.time() - ts >= USAGE_TTL:
        try:
            lock_fresh = (os.path.exists(USAGE_LOCK)
                          and time.time() - os.path.getmtime(USAGE_LOCK) < 15)
            if not lock_fresh:
                open(USAGE_LOCK, "w").close()
                subprocess.Popen(
                    [sys.executable, os.path.abspath(__file__), "--refresh-usage"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL, start_new_session=True,
                )
        except Exception:
            pass
    return data


def usage_segment(data):
    """Render '5h <bar> 7% · wk <bar> 10% · Fable <bar> 7%' from limits[].

    Session (5h) and weekly-all are shown whenever present. Any weekly_scoped
    per-model limit (e.g. the temporary Fable promo) is shown only while the API
    returns it, labelled by its own display_name — so it disappears cleanly when
    the promo ends and any future scoped model appears automatically.
    """
    if not data:
        return ""
    limits = data.get("limits") or []

    def pick(kind):
        return next((l for l in limits if l.get("kind") == kind), None)

    ordered = []
    session = pick("session")
    weekly = pick("weekly_all")
    if session:
        ordered.append(("5h", session))
    if weekly:
        ordered.append(("wk", weekly))
    for l in limits:
        if l.get("kind") == "weekly_scoped":
            name = ((l.get("scope") or {}).get("model") or {}).get("display_name")
            if name:
                ordered.append((name, l))

    sep = f" {DIM}·{RESET} "
    parts = []
    for label, l in ordered:
        pct = int(round(l.get("percent") or 0))
        color = GREEN if pct < 50 else YELLOW if pct < 80 else RED
        parts.append(f"{DIM}{label}{RESET} {color}{pct}%{RESET}")
    return sep.join(parts)


# ---- responsive wrapping --------------------------------------------------
# Claude Code captures our stdout (not a TTY), so tput/ioctl can't read the
# width; instead it sets COLUMNS to the terminal width before running us.
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
WRAP_MARGIN = 2  # small buffer for Claude Code's built-in spacing/padding


def _vlen(s):
    """Visible width of a string, ignoring ANSI color codes."""
    return len(ANSI_RE.sub("", s))


def flow(chunks, width, sep):
    """Pack chunks into as few rows as fit within `width` visible columns."""
    sep_len = _vlen(sep)
    lines, cur, cur_len = [], "", 0
    for c in chunks:
        cl = _vlen(c)
        if not cur:
            cur, cur_len = c, cl
        elif cur_len + sep_len + cl <= width:
            cur, cur_len = cur + sep + c, cur_len + sep_len + cl
        else:
            lines.append(cur)
            cur, cur_len = c, cl
    if cur:
        lines.append(cur)
    return lines


def main():
    data = read_stdin_json()

    model = data.get("model") or {}
    model_name = model.get("display_name") or model.get("id") or "?"
    model_id = (model.get("id") or "").lower()

    ws = data.get("workspace") or {}
    cwd = ws.get("current_dir") or data.get("cwd") or os.getcwd()
    dirname = os.path.basename(cwd.rstrip("/")) or cwd
    branch = git_branch(cwd)

    used = 0
    tpath = data.get("transcript_path")
    if tpath and os.path.exists(tpath):
        used = last_usage_tokens(tpath)
    # Window resolved from the harness / markers / behaviour — not a hard 200k.
    window = context_window(data, model_id, used)
    pct = min(100, round(used / window * 100)) if window else 0

    seg = []
    seg.append(f"{BOLD}{CYAN}{dirname}{RESET}")
    if branch:
        seg.append(f"{DIM} {branch}{RESET}")  # branch glyph (nerd-font); falls back gracefully
    left = " ".join(seg)

    tok = f"{DIM}{used // 1000}k{RESET}" if used else ""
    ctx = f"{bar(pct)} {pct}%"
    if tok:
        ctx += f" {DIM}·{RESET} {tok}"

    chunks = [left, model_name, ctx]
    if USAGE_ENABLED:
        useg = usage_segment(load_usage())
        if useg:
            chunks.append(useg)

    sep = f" {DIM}|{RESET} "
    try:
        width = int(os.environ.get("COLUMNS", "0")) - WRAP_MARGIN
    except ValueError:
        width = 0
    if width <= 0:
        print(sep.join(chunks))  # no COLUMNS (old CC / non-tty) → single line
    else:
        print("\n".join(flow(chunks, width, sep)))


if __name__ == "__main__":
    if "--refresh-usage" in sys.argv:
        refresh_usage()
    else:
        main()
