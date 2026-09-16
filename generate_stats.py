"""
generate_stats.py
=================
Fetches GitHub statistics for a given user via the GitHub REST API
and generates a two-panel SVG statistics card saved to assets/github-stats.svg.

HOW EACH STATISTIC IS CALCULATED
---------------------------------
- Total Stars        : Sum of `stargazers_count` across all non-forked public repos.
- Total Commits      : Uses the GitHub search API (commits endpoint) to count commits
                       authored by the user. This reflects the searchable commit index
                       and may differ slightly from the contribution graph on the profile.
- Total PRs          : Uses the GitHub search API (type:pr author:USERNAME) to count
                       all pull requests (open + closed + merged) authored by the user.
- Total Issues       : Uses the GitHub search API (type:issue author:USERNAME) to count
                       issues created by the user. PRs are explicitly excluded.
- Contributed to     : Number of distinct repos (not owned by the user) found in the
                       user's public event stream. Best-effort; limited to recent events.
- Languages          : Aggregated byte-counts from /repos/{owner}/{repo}/languages for
                       every non-forked public repo, then converted to percentages.

CONFIGURABLE CONSTANTS
-----------------------
See the "--- CONFIGURATION ---" section below.
"""

import os
import sys
import html
import math
import time
import requests
from collections import defaultdict

# ── CONFIGURATION ─────────────────────────────────────────────────────────────

GITHUB_USERNAME   = "PragathiJayasinghe"

# Visual grade shown inside the circular indicator (purely cosmetic).
GRADE             = "A+"
GRADE_PROGRESS    = 75          # percentage of the ring that is filled (0-100)

# Card dimensions
CARD_WIDTH        = 1000
CARD_HEIGHT       = 300

# Colour palette
COLOR_BG          = "#0D1117"   # overall card background
COLOR_PANEL       = "#161B22"   # inner panel background
COLOR_BORDER      = "#30363D"   # border lines
COLOR_TITLE       = "#C9D1D9"   # panel titles
COLOR_ICON        = "#7EE787"   # stat row icons
COLOR_LABEL       = "#B1BAC4"   # stat row labels
COLOR_VALUE       = "#C9D1D9"   # stat row values
COLOR_RING_TRACK  = "#30363D"   # circular-indicator background ring
COLOR_RING_ARC    = "#C9D1D9"   # circular-indicator progress arc

# Top-N languages to show in the legend
MAX_LANGUAGES     = 5

# Set True to count commits in the current calendar year only.
COMMITS_CURRENT_YEAR_ONLY = False

# Include forked repositories in language / star counts?
INCLUDE_FORKS     = False

# Output path (relative to repository root)
OUTPUT_PATH       = "assets/github-stats.svg"

# Language colour map (extend as needed)
LANG_COLORS = {
    "Python":            "#3572A5",
    "JavaScript":        "#F1E05A",
    "TypeScript":        "#2B7489",
    "Java":              "#B07219",
    "C":                 "#555555",
    "C++":               "#F34B7D",
    "C#":                "#178600",
    "Go":                "#00ADD8",
    "Rust":              "#DEA584",
    "Ruby":              "#701516",
    "PHP":               "#4F5D95",
    "Swift":             "#FFAC45",
    "Kotlin":            "#A97BFF",
    "Dart":              "#00B4AB",
    "HTML":              "#E44B23",
    "CSS":               "#563D7C",
    "Shell":             "#89E051",
    "R":                 "#198CE7",
    "Scala":             "#C22D40",
    "Jupyter Notebook":  "#DA5B0B",
    "Vue":               "#41B883",
    "Text":              "#888888",
    "Markdown":          "#083FA1",
    "Other":             "#6E7681",
}

# ── API HELPERS ────────────────────────────────────────────────────────────────

def _headers():
    """Return request headers. Uses GH_TOKEN env var when available."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    h = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


def _get(url, params=None, retries=3):
    """GET a GitHub API URL with basic retry / rate-limit handling."""
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=_headers(), params=params, timeout=20)
            if r.status_code == 200:
                return r.json()
            if r.status_code == 403:
                reset = int(r.headers.get("X-RateLimit-Reset", time.time() + 60))
                wait  = max(reset - int(time.time()), 1)
                print(f"  Rate-limited. Sleeping {min(wait, 60)}s ...", file=sys.stderr)
                time.sleep(min(wait, 60))
                continue
            if r.status_code == 422:
                return None   # Search index unavailable / validation error
            print(f"  HTTP {r.status_code} for {url}", file=sys.stderr)
        except requests.RequestException as exc:
            print(f"  Request error ({exc}), attempt {attempt + 1}/{retries}", file=sys.stderr)
            time.sleep(2 ** attempt)
    return None


def _paginate(url, params=None):
    """Collect all pages of a GitHub list endpoint."""
    results = []
    p = dict(params or {})
    p.setdefault("per_page", 100)
    page = 1
    while True:
        p["page"] = page
        data = _get(url, p)
        if not data:
            break
        results.extend(data)
        if len(data) < p["per_page"]:
            break
        page += 1
    return results


def _search_count(query):
    """Return total_count from the GitHub search/issues API for a query string."""
    data = _get("https://api.github.com/search/issues",
                {"q": query, "per_page": 1})
    if data and "total_count" in data:
        return data["total_count"]
    return 0

# ── DATA FETCHING ──────────────────────────────────────────────────────────────

def get_repositories():
    """Return public repos owned by the user (optionally excluding forks)."""
    repos = _paginate(
        f"https://api.github.com/users/{GITHUB_USERNAME}/repos",
        {"type": "owner", "sort": "pushed"},
    )
    if not INCLUDE_FORKS:
        repos = [r for r in repos if not r.get("fork")]
    return repos


def get_total_stars(repos):
    """Sum stargazers_count across supplied repositories."""
    return sum(r.get("stargazers_count", 0) for r in repos)


def get_total_commits():
    """
    Count commits authored by the user via the GitHub commits search API.
    Uses a special preview Accept header required by that endpoint.
    If COMMITS_CURRENT_YEAR_ONLY is True, restricts to the current year.
    """
    from datetime import date
    query = f"author:{GITHUB_USERNAME}"
    if COMMITS_CURRENT_YEAR_ONLY:
        year  = date.today().year
        query += f" committer-date:{year}-01-01..{year}-12-31"

    headers = _headers()
    headers["Accept"] = "application/vnd.github.cloak-preview+json"
    try:
        r = requests.get(
            "https://api.github.com/search/commits",
            headers=headers,
            params={"q": query, "per_page": 1},
            timeout=20,
        )
        if r.status_code == 200:
            return r.json().get("total_count", 0)
        print(f"  Commit search HTTP {r.status_code}", file=sys.stderr)
    except requests.RequestException as exc:
        print(f"  Commit search error: {exc}", file=sys.stderr)
    return 0


def get_total_pull_requests():
    """Count all pull requests (open + closed + merged) authored by the user."""
    return _search_count(f"type:pr author:{GITHUB_USERNAME}")


def get_total_issues():
    """
    Count issues created by the user, explicitly excluding pull requests.
    (type:issue in GitHub search excludes PRs automatically.)
    """
    return _search_count(f"type:issue author:{GITHUB_USERNAME}")


def get_contributed_repositories():
    """
    Approximate number of distinct repositories (not owned by the user) that
    appear in the user's recent public events stream.
    The public events API returns up to 300 events; this is a best-effort metric.
    """
    events = _paginate(
        f"https://api.github.com/users/{GITHUB_USERNAME}/events/public"
    )
    contributed = set()
    for ev in events:
        repo = ev.get("repo", {}).get("name", "")
        if repo and not repo.startswith(f"{GITHUB_USERNAME}/"):
            contributed.add(repo)
    return len(contributed)


def get_language_statistics(repos):
    """
    Fetch per-language byte counts for every repo and return a dict of
    {language: percentage} sorted by percentage descending.
    """
    totals = defaultdict(int)
    for repo in repos:
        owner = repo["owner"]["login"]
        name  = repo["name"]
        data  = _get(f"https://api.github.com/repos/{owner}/{name}/languages")
        if isinstance(data, dict):
            for lang, count in data.items():
                totals[lang] += count

    grand_total = sum(totals.values())
    if grand_total == 0:
        return {}

    return {
        lang: round(count / grand_total * 100, 2)
        for lang, count in sorted(totals.items(), key=lambda x: x[1], reverse=True)
    }

# ── SVG GENERATION ─────────────────────────────────────────────────────────────

def _lang_color(lang):
    return LANG_COLORS.get(lang, LANG_COLORS["Other"])


def _e(text):
    """HTML-escape a value for safe SVG embedding."""
    return html.escape(str(text))


def _fmt(n):
    """Format large integers with K / M suffixes."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def _ring_svg(cx, cy, r, progress, grade, track_color, arc_color):
    """Return SVG markup for a circular progress ring centred at (cx, cy)."""
    circ   = 2 * math.pi * r
    offset = circ * (1 - progress / 100)
    rotate = f"rotate(-90 {cx} {cy})"
    return f"""
    <circle cx="{cx}" cy="{cy}" r="{r}"
            fill="none" stroke="{track_color}" stroke-width="6"/>
    <circle cx="{cx}" cy="{cy}" r="{r}"
            fill="none" stroke="{arc_color}" stroke-width="6"
            stroke-linecap="round"
            stroke-dasharray="{circ:.2f}"
            stroke-dashoffset="{offset:.2f}"
            transform="{rotate}"/>
    <text x="{cx}" y="{cy}"
          dominant-baseline="middle" text-anchor="middle"
          font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
          font-size="18" font-weight="700" fill="{COLOR_TITLE}">{_e(grade)}</text>
"""


def _stat_row(x, y, icon, label, value, value_x):
    """Return SVG for one statistics row."""
    return f"""
    <text x="{x}" y="{y}"
          font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
          font-size="14" fill="{COLOR_ICON}">{_e(icon)}</text>
    <text x="{x + 22}" y="{y}"
          font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
          font-size="14" fill="{COLOR_LABEL}">{_e(label)}</text>
    <text x="{value_x}" y="{y}"
          font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
          font-size="14" font-weight="600" fill="{COLOR_VALUE}"
          text-anchor="end">{_e(value)}</text>
"""


def generate_svg(stats):
    """Build and return the complete SVG string from a stats dict."""

    W, H    = CARD_WIDTH, CARD_HEIGHT
    pad     = 16
    gap     = 10
    panel_w = (W - 2 * pad - gap) // 2
    panel_h = H - 2 * pad

    # Left panel
    lp_x        = pad
    lp_y        = pad
    title_y     = lp_y + 36
    row_start_y = title_y + 34
    row_step    = 32
    ring_cx     = lp_x + panel_w - 58
    ring_cy     = lp_y + panel_h // 2 + 8
    ring_r      = 38
    value_x     = ring_cx - ring_r - 12

    # Right panel
    rp_x     = pad + panel_w + gap
    rp_y     = pad
    bar_y    = rp_y + 72
    bar_h    = 14
    bar_w    = panel_w - 40
    legend_y = bar_y + bar_h + 28

    # ── Language bar ─────────────────────────────────────────
    languages = stats.get("languages", {})
    top_langs = dict(list(languages.items())[:MAX_LANGUAGES])
    other_pct = max(0.0, 100.0 - sum(top_langs.values()))
    if other_pct > 0.5:
        top_langs["Other"] = round(other_pct, 2)

    bar_segs = ""
    cur_x = rp_x + 20
    for lang, pct in top_langs.items():
        seg_w = bar_w * pct / 100
        if seg_w < 1:
            continue
        bar_segs += (
            f'<rect x="{cur_x:.2f}" y="{bar_y}" '
            f'width="{seg_w:.2f}" height="{bar_h}" '
            f'fill="{_lang_color(lang)}"/>'
        )
        cur_x += seg_w

    # ── Language legend ───────────────────────────────────────
    col_w   = (panel_w - 40) // 2
    legend  = ""
    for i, (lang, pct) in enumerate(top_langs.items()):
        col  = i % 2
        row  = i // 2
        lx   = rp_x + 20 + col * col_w
        ly   = legend_y + row * 26
        clr  = _lang_color(lang)
        name = lang if len(lang) <= 18 else lang[:16] + "..."
        legend += f"""
    <circle cx="{lx + 6}" cy="{ly - 5}" r="5" fill="{clr}"/>
    <text x="{lx + 16}" y="{ly}"
          font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
          font-size="12" fill="{COLOR_LABEL}">{_e(name)}</text>
    <text x="{lx + col_w - 4}" y="{ly}"
          font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
          font-size="12" font-weight="600" fill="{COLOR_VALUE}"
          text-anchor="end">({pct:.2f}%)</text>"""

    # ── Stat rows ─────────────────────────────────────────────
    rows = [
        ("⭐", "Total Stars",    _fmt(stats.get("stars",   0))),
        ("◷",  "Total Commits",  _fmt(stats.get("commits", 0))),
        ("⑂",  "Total PRs",      _fmt(stats.get("prs",     0))),
        ("ⓘ",  "Total Issues",   _fmt(stats.get("issues",  0))),
        ("▣",  "Contributed to", _fmt(stats.get("contrib", 0))),
    ]
    rows_svg = ""
    for idx, (icon, label, value) in enumerate(rows):
        y = row_start_y + idx * row_step
        rows_svg += _stat_row(lp_x + 22, y, icon, label, value, value_x)

    ring = _ring_svg(ring_cx, ring_cy, ring_r,
                     GRADE_PROGRESS, GRADE,
                     COLOR_RING_TRACK, COLOR_RING_ARC)

    # ── Assemble ──────────────────────────────────────────────
    return f"""<svg xmlns="http://www.w3.org/2000/svg"
     viewBox="0 0 {W} {H}" width="{W}" height="{H}"
     role="img" aria-label="GitHub statistics for {_e(GITHUB_USERNAME)}">

  <title>GitHub Statistics — {_e(GITHUB_USERNAME)}</title>

  <!-- Card background -->
  <rect width="{W}" height="{H}" rx="12" ry="12"
        fill="{COLOR_BG}" stroke="{COLOR_BORDER}" stroke-width="1"/>

  <!-- ══ LEFT PANEL ══ -->
  <rect x="{lp_x}" y="{lp_y}" width="{panel_w}" height="{panel_h}"
        rx="10" ry="10" fill="{COLOR_PANEL}"
        stroke="{COLOR_BORDER}" stroke-width="1"/>

  <text x="{lp_x + 22}" y="{title_y}"
        font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
        font-size="16" font-weight="700" fill="{COLOR_TITLE}">&#x1F4CA; GitHub Statistics</text>

  {rows_svg}
  {ring}

  <!-- ══ RIGHT PANEL ══ -->
  <rect x="{rp_x}" y="{rp_y}" width="{panel_w}" height="{panel_h}"
        rx="10" ry="10" fill="{COLOR_PANEL}"
        stroke="{COLOR_BORDER}" stroke-width="1"/>

  <text x="{rp_x + 20}" y="{rp_y + 36}"
        font-family="'Segoe UI','Helvetica Neue',Arial,sans-serif"
        font-size="16" font-weight="700" fill="{COLOR_TITLE}">My Programming Languages</text>

  <!-- Language bar background track -->
  <rect x="{rp_x + 20}" y="{bar_y}" width="{bar_w}" height="{bar_h}"
        rx="6" ry="6" fill="{COLOR_BORDER}"/>

  <!-- Stacked language bar segments (clipped) -->
  <clipPath id="bar-clip">
    <rect x="{rp_x + 20}" y="{bar_y}" width="{bar_w}" height="{bar_h}"
          rx="6" ry="6"/>
  </clipPath>
  <g clip-path="url(#bar-clip)">
    {bar_segs}
  </g>

  <!-- Legend -->
  {legend}

</svg>"""

# ── MAIN ───────────────────────────────────────────────────────────────────────

def main():
    print(f"Fetching GitHub stats for: {GITHUB_USERNAME}")

    print("  -> repositories ...")
    repos   = get_repositories()
    print(f"     {len(repos)} repositories found")

    print("  -> stars ...")
    stars   = get_total_stars(repos)

    print("  -> commits ...")
    commits = get_total_commits()

    print("  -> pull requests ...")
    prs     = get_total_pull_requests()

    print("  -> issues ...")
    issues  = get_total_issues()

    print("  -> contributions ...")
    contrib = get_contributed_repositories()

    print("  -> language statistics ...")
    languages = get_language_statistics(repos)

    stats = {
        "stars":     stars,
        "commits":   commits,
        "prs":       prs,
        "issues":    issues,
        "contrib":   contrib,
        "languages": languages,
    }

    print("\nStats collected:")
    for k, v in stats.items():
        if k != "languages":
            print(f"  {k:<12}: {v}")
    if languages:
        print("  languages    :")
        for lang, pct in list(languages.items())[:MAX_LANGUAGES]:
            print(f"    {lang:<20}: {pct:.2f}%")

    print("\nGenerating SVG ...")
    svg = generate_svg(stats)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as fh:
        fh.write(svg)
    print(f"SVG saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
