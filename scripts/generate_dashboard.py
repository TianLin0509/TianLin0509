#!/usr/bin/env python3
"""Generate TianLin0509's GitHub stats dashboard SVG from live data.

Pulls public repo stats via the REST API and the contribution calendar via the
GraphQL API, computes metrics, and renders assets/github-stats-dashboard.svg.

Only the Python standard library is used (no pip install needed in CI).

Environment:
  GH_PAT  classic personal access token with `read:user` scope (required for the
          GraphQL contribution calendar). Also used to raise the REST rate limit.

Run:
  GH_PAT=xxxx python scripts/generate_dashboard.py
"""

import json
import math
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

USERNAME = "TianLin0509"
API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
OUT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "assets",
    "github-stats-dashboard.svg",
)

# Dark-theme palette (matches the existing dashboard).
COLORS = {
    "blue": "#58a6ff",
    "green": "#3fb950",
    "cyan": "#39c5cf",
    "amber": "#d29922",
    "violet": "#a371f7",
    "red": "#f85149",
}
ACCENTS = ["blue", "amber", "green", "cyan", "violet", "red"]
# GitHub-style green scale for the contribution heatmap (dark theme).
HEAT = ["#161b22", "#0e4429", "#006d32", "#26a641", "#39d353"]


# --------------------------------------------------------------------------- #
# HTTP helpers
# --------------------------------------------------------------------------- #
def _token():
    return os.environ.get("GH_PAT", "").strip()


def gh_get(url):
    """GET a REST endpoint. Returns (parsed_json, headers)."""
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", USERNAME + "-dashboard")
    tok = _token()
    if tok:
        req.add_header("Authorization", "Bearer " + tok)
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        return data, dict(resp.headers)


def gh_graphql(query, variables):
    """POST a GraphQL query. Returns the `data` object. Raises on errors."""
    tok = _token()
    if not tok:
        raise RuntimeError("GH_PAT is required for the GraphQL contribution calendar")
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(GRAPHQL, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + tok)
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", USERNAME + "-dashboard")
    with urllib.request.urlopen(req, timeout=30) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if payload.get("errors"):
        raise RuntimeError("GraphQL errors: " + json.dumps(payload["errors"]))
    return payload["data"]


# --------------------------------------------------------------------------- #
# Data fetching
# --------------------------------------------------------------------------- #
def fetch_user():
    data, _ = gh_get("%s/users/%s" % (API, USERNAME))
    return data


def fetch_repos():
    repos = []
    page = 1
    while True:
        url = "%s/users/%s/repos?per_page=100&type=owner&page=%d" % (API, USERNAME, page)
        batch, _ = gh_get(url)
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return repos


def fetch_commit_count(repo):
    """Default-branch commit count via the Link header `last` page trick."""
    branch = repo.get("default_branch") or "main"
    url = "%s/repos/%s/%s/commits?sha=%s&per_page=1" % (
        API, USERNAME, repo["name"], branch,
    )
    try:
        data, headers = gh_get(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 409:  # empty repository
            return 0
        raise
    link = headers.get("Link") or headers.get("link") or ""
    for part in link.split(","):
        if 'rel="last"' in part:
            seg = part[part.find("<") + 1: part.find(">")]
            if "page=" in seg:
                return int(seg.rsplit("page=", 1)[1].split("&")[0])
    return len(data)  # 0 or 1 commit, no pagination


def fetch_calendar():
    query = """
    query($login: String!) {
      user(login: $login) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks {
              contributionDays { date weekday contributionCount }
            }
          }
        }
      }
    }
    """
    data = gh_graphql(query, {"login": USERNAME})
    cal = data["user"]["contributionsCollection"]["contributionCalendar"]
    if not cal["weeks"]:
        raise RuntimeError("contribution calendar is empty")
    return cal


# --------------------------------------------------------------------------- #
# Metric computation
# --------------------------------------------------------------------------- #
def _parse_dt(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def heat_levels(weeks):
    """Map each day's count to a 0-4 level using quartiles of positive days."""
    positives = sorted(
        d["contributionCount"]
        for w in weeks for d in w["contributionDays"]
        if d["contributionCount"] > 0
    )
    if not positives:
        thresholds = [1, 2, 3]
    else:
        def q(p):
            return positives[min(len(positives) - 1, int(len(positives) * p))]
        thresholds = [q(0.25), q(0.50), q(0.75)]

    def level(c):
        if c <= 0:
            return 0
        if c <= thresholds[0]:
            return 1
        if c <= thresholds[1]:
            return 2
        if c <= thresholds[2]:
            return 3
        return 4

    return level


def compute_streaks(days):
    """days: chronological list of {date, contributionCount}. Returns (cur, longest)."""
    longest = run = 0
    for d in days:
        if d["contributionCount"] > 0:
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    # current streak counts back from the last day (allow today to still be 0)
    cur = 0
    for d in reversed(days):
        if d["contributionCount"] > 0:
            cur += 1
        elif cur == 0 and d is days[-1]:
            continue  # today not yet contributed; keep looking back
        else:
            break
    return cur, longest


def compute_metrics(user, repos, cal):
    now = datetime.now(timezone.utc)
    for r in repos:
        r["commits"] = fetch_commit_count(r)

    total_stars = sum(r["stargazers_count"] for r in repos)
    total_forks = sum(r["forks_count"] for r in repos)
    total_commits = sum(r["commits"] for r in repos)
    active = sum(
        1 for r in repos
        if r.get("pushed_at") and (now - _parse_dt(r["pushed_at"])) <= timedelta(days=365)
    )

    kpis = [
        ("blue", "{:,}".format(user["public_repos"]), "公开仓库"),
        ("amber", "{:,}".format(total_stars), "Total Stars"),
        ("green", "{:,}".format(total_commits), "默认分支 Commits"),
        ("cyan", "{:,}".format(total_forks), "Forks"),
        ("violet", "{:,}".format(user["followers"]), "Followers"),
        ("red", "{:,}".format(active), "近 365 天活跃仓库"),
    ]

    star_rank = sorted(repos, key=lambda r: (-r["stargazers_count"], r["name"]))[:6]
    star_rank = [(r["name"], r["stargazers_count"]) for r in star_rank]
    commit_rank = sorted(repos, key=lambda r: (-r["commits"], r["name"]))[:6]
    commit_rank = [(r["name"], r["commits"]) for r in commit_rank]

    # Language distribution (by primary language; null -> 未标注).
    lang_count = {}
    lang_stars = {}
    lang_commits = {}
    for r in repos:
        lang = r.get("language") or "未标注"
        lang_count[lang] = lang_count.get(lang, 0) + 1
        lang_stars[lang] = lang_stars.get(lang, 0) + r["stargazers_count"]
        lang_commits[lang] = lang_commits.get(lang, 0) + r["commits"]

    dist = sorted(lang_count.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(dist) > 5:
        head = dist[:4]
        rest = sum(c for _, c in dist[4:])
        dist = head + [("其他", rest)]

    contrib = sorted(
        ((lang, lang_stars[lang], lang_commits[lang]) for lang in lang_stars),
        key=lambda t: (-t[1], -t[2], t[0]),
    )[:4]

    recent = sorted(
        (r for r in repos if r.get("pushed_at")),
        key=lambda r: r["pushed_at"], reverse=True,
    )[:8]
    recent = [r["name"] for r in recent]

    weeks = cal["weeks"]
    days = [d for w in weeks for d in w["contributionDays"]]
    cur_streak, longest_streak = compute_streaks(days)
    last30 = days[-30:]

    return {
        "kpis": kpis,
        "star_rank": star_rank,
        "commit_rank": commit_rank,
        "dist": dist,
        "total_repos": len(repos),
        "contrib": contrib,
        "recent": recent,
        "weeks": weeks,
        "total_contrib": cal["totalContributions"],
        "cur_streak": cur_streak,
        "longest_streak": longest_streak,
        "last30": last30,
        "updated": (now + timedelta(hours=8)).strftime("%Y-%m-%d"),
    }


# --------------------------------------------------------------------------- #
# SVG rendering
# --------------------------------------------------------------------------- #
def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def clip(name, n=20):
    name = str(name)
    return name if len(name) <= n else name[: n - 1] + "…"


def scaled(value, max_value, max_width):
    if max_value <= 0:
        return 0
    return max(2, round(value / max_value * max_width))


def render_bars(rank, x0, label_x, bar_x, max_width):
    """Render a 6-row ranking column (Star/Commit)."""
    out = []
    max_value = max((v for _, v in rank), default=1) or 1
    for i, (name, value) in enumerate(rank):
        ty = 76 + i * 36
        accent = ACCENTS[i % len(ACCENTS)]
        w = scaled(value, max_value, max_width)
        out.append('<text class="text" x="%d" y="%d" font-size="12" font-weight="700">%s</text>'
                    % (label_x, ty, esc(clip(name, 22))))
        out.append('<rect class="%s" x="%d" y="%d" width="%d" height="18" rx="4"/>'
                   '<text class="text mono" x="%d" y="%d" font-size="12">%s</text>'
                   % (accent, bar_x, ty - 14, w, bar_x + w + 8, ty, "{:,}".format(value)))
    return "\n    ".join(out)


def render_donut(dist, total, cx, cy, r):
    circ = 2 * math.pi * r
    total_shown = sum(c for _, c in dist) or 1
    out = ['<circle cx="%d" cy="%d" r="%d" fill="none" stroke="#30363d" stroke-width="34"/>'
           % (cx, cy, r)]
    offset = 0.0
    for i, (_, count) in enumerate(dist):
        frac = count / total_shown
        dash = frac * circ
        col = COLORS[ACCENTS[i % len(ACCENTS)]]
        out.append('<circle cx="%d" cy="%d" r="%d" fill="none" stroke="%s" stroke-width="34" '
                   'stroke-dasharray="%.2f %.2f" stroke-dashoffset="%.2f" '
                   'transform="rotate(-90 %d %d)"/>'
                   % (cx, cy, r, col, dash, circ - dash, -offset, cx, cy))
        offset += dash
    out.append('<text class="text mono" x="%d" y="%d" text-anchor="middle" font-size="28" '
               'font-weight="800">%d</text>' % (cx, cy - 3, total))
    out.append('<text class="muted" x="%d" y="%d" text-anchor="middle" font-size="12">Repos</text>'
               % (cx, cy + 20))
    lx, ly = cx + 126, 72
    for i, (lang, count) in enumerate(dist):
        accent = ACCENTS[i % len(ACCENTS)]
        y = ly + i * 34
        out.append('<rect class="%s" x="%d" y="%d" width="12" height="12" rx="2"/>'
                   '<text class="text" x="%d" y="%d" font-size="13">%s · %d</text>'
                   % (accent, lx, y, lx + 20, y + 11, esc(clip(lang, 14)), count))
    return "\n    ".join(out)


def render_contrib(contrib):
    out = []
    max_stars = max((s for _, s, _ in contrib), default=1) or 1
    max_commits = max((c for _, _, c in contrib), default=1) or 1
    for i, (lang, stars, commits) in enumerate(contrib):
        base = 62 + i * 50
        ws = scaled(stars, max_stars, 202)
        wc = scaled(commits, max_commits, 202)
        out.append('<text class="text" x="474" y="%d" font-size="12" font-weight="700">%s</text>'
                   % (base + 16, esc(clip(lang, 16))))
        out.append('<rect class="amber" x="574" y="%d" width="%d" height="16" rx="4"/>'
                   '<text class="text mono" x="%d" y="%d" font-size="12">%s stars</text>'
                   % (base, ws, 574 + ws + 8, base + 14, "{:,}".format(stars)))
        out.append('<rect class="blue" x="574" y="%d" width="%d" height="10" rx="3"/>'
                   '<text class="muted mono" x="%d" y="%d" font-size="12">%s commits</text>'
                   % (base + 24, wc, 574 + wc + 8, base + 34, "{:,}".format(commits)))
    return "\n    ".join(out)


def render_heatmap(weeks, total):
    level = heat_levels(weeks)
    out = ['<text class="text" x="18" y="30" font-size="18" font-weight="800">贡献热力图</text>']
    out.append('<text class="muted" x="870" y="30" text-anchor="end" font-size="12">'
               '%s contributions in the last year</text>' % "{:,}".format(total))
    gx, gy, step, cell = 46, 52, 15, 11
    # weekday labels
    for row, lbl in ((1, "Mon"), (3, "Wed"), (5, "Fri")):
        out.append('<text class="muted" x="18" y="%d" font-size="9">%s</text>'
                   % (gy + row * step + 9, lbl))
    # month labels
    months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
              "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    last_month = None
    for c, w in enumerate(weeks):
        first = w["contributionDays"][0]["date"]
        m = int(first[5:7])
        if m != last_month:
            out.append('<text class="muted" x="%d" y="%d" font-size="9">%s</text>'
                       % (gx + c * step, gy - 6, months[m - 1]))
            last_month = m
    # cells
    for c, w in enumerate(weeks):
        for d in w["contributionDays"]:
            row = d["weekday"]
            x = gx + c * step
            y = gy + row * step
            out.append('<rect x="%d" y="%d" width="%d" height="%d" rx="2" fill="%s"/>'
                       % (x, y, cell, cell, HEAT[level(d["contributionCount"])]))
    # legend
    lx = 700
    out.append('<text class="muted" x="%d" y="178" font-size="11">Less</text>' % (lx - 28))
    for i, col in enumerate(HEAT):
        out.append('<rect x="%d" y="169" width="11" height="11" rx="2" fill="%s"/>'
                   % (lx + i * 15, col))
    out.append('<text class="muted" x="%d" y="178" font-size="11">More</text>'
               % (lx + len(HEAT) * 15 + 4))
    return "\n    ".join(out)


def render_streak(cur, longest):
    return ("\n    ".join([
        '<text class="text" x="18" y="30" font-size="18" font-weight="800">连续提交</text>',
        '<text class="green mono" x="40" y="82" font-size="36" font-weight="800">%d</text>' % cur,
        '<text class="muted" x="40" y="104" font-size="12">当前连续（天）</text>',
        '<text class="amber mono" x="244" y="82" font-size="36" font-weight="800">%d</text>' % longest,
        '<text class="muted" x="244" y="104" font-size="12">最长连续（天）</text>',
    ]))


def render_trend(last30):
    out = ['<text class="text" x="474" y="30" font-size="18" font-weight="800">近 30 天提交趋势</text>']
    counts = [d["contributionCount"] for d in last30]
    mx = max(counts) if counts else 1
    mx = mx or 1
    x0, baseline, max_h, step, bw = 478, 100, 56, 12, 8
    total = sum(counts)
    for i, c in enumerate(counts):
        h = round(c / mx * max_h) if c > 0 else 1
        x = x0 + i * step
        out.append('<rect class="blue" x="%d" y="%d" width="%d" height="%d" rx="1"/>'
                   % (x, baseline - h, bw, h))
    out.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#30363d" stroke-width="1"/>'
               % (x0 - 2, baseline + 2, x0 + 30 * step, baseline + 2))
    out.append('<text class="muted" x="474" y="%d" font-size="12">近 30 天共 %d 次贡献</text>'
               % (baseline + 22, total))
    return "\n    ".join(out)


def render_recent(recent):
    out = ['<text class="text" x="18" y="30" font-size="18" font-weight="800">最近活跃仓库</text>']
    n = len(recent)
    if n:
        x0, x1 = 190, 842
        out.append('<line x1="184" y1="38" x2="842" y2="38" stroke="#30363d" stroke-width="2"/>')
        spacing = (x1 - x0) / (n - 1) if n > 1 else 0
        for i, name in enumerate(recent):
            cx = round(x0 + i * spacing)
            accent = ACCENTS[i % len(ACCENTS)]
            out.append('<circle class="%s" cx="%d" cy="38" r="10"/>'
                       '<text class="muted" x="%d" y="62" text-anchor="middle" font-size="11">%s</text>'
                       % (accent, cx, cx, esc(clip(name, 12))))
    return "\n    ".join(out)


def render_kpis(kpis):
    out = []
    for i, (accent, value, label) in enumerate(kpis):
        x = i * 150
        out.append('<rect class="panel" x="%d" y="0" width="136" height="88" rx="8"/>' % x)
        out.append('<text class="%s mono" x="%d" y="38" font-size="30" font-weight="800">%s</text>'
                   % (accent, x + 16, value))
        out.append('<text class="muted" x="%d" y="66" font-size="13">%s</text>'
                   % (x + 16, esc(label)))
    return "\n    ".join(out)


def render_svg(m):
    return """<svg xmlns="http://www.w3.org/2000/svg" width="980" height="1284" viewBox="0 0 980 1284" role="img" aria-label="TianLin0509 GitHub 统计仪表盘">
  <title>TianLin0509 GitHub 统计</title>
  <desc>实时生成的 GitHub 统计仪表盘，每日自动更新。</desc>
  <defs>
    <style>
      .bg{{fill:#0d1117}}.panel{{fill:#161b22;stroke:#30363d;stroke-width:1}}.text{{fill:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}.muted{{fill:#8b949e;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei",Arial,sans-serif}}.mono{{font-family:Consolas,"SFMono-Regular","Cascadia Mono",monospace}}.blue{{fill:#58a6ff}}.green{{fill:#3fb950}}.cyan{{fill:#39c5cf}}.amber{{fill:#d29922}}.violet{{fill:#a371f7}}.red{{fill:#f85149}}
    </style>
  </defs>
  <rect class="bg" width="980" height="1284" rx="12"/>

  <text class="text" x="48" y="70" font-size="40" font-weight="800">TianLin0509 · GitHub 统计</text>
  <text class="muted" x="932" y="54" font-size="13" text-anchor="end">GitHub 实时统计</text>
  <text class="muted mono" x="932" y="76" font-size="13" text-anchor="end">更新于 {updated}</text>

  <g transform="translate(48 104)">
    {kpis}
  </g>

  <g transform="translate(48 218)">
    <rect class="panel" x="0" y="0" width="432" height="290" rx="8"/>
    <text class="text" x="18" y="34" font-size="20" font-weight="800">Star 排行</text>
    {star_rank}

    <rect class="panel" x="456" y="0" width="432" height="290" rx="8"/>
    <text class="text" x="474" y="34" font-size="20" font-weight="800">Commit 排行</text>
    {commit_rank}
  </g>

  <g transform="translate(48 532)">
    <rect class="panel" x="0" y="0" width="432" height="252" rx="8"/>
    <text class="text" x="18" y="34" font-size="20" font-weight="800">语言分布</text>
    {donut}

    <rect class="panel" x="456" y="0" width="432" height="252" rx="8"/>
    <text class="text" x="474" y="34" font-size="20" font-weight="800">语言贡献</text>
    {contrib}
  </g>

  <g transform="translate(48 808)">
    <rect class="panel" x="0" y="0" width="888" height="190" rx="8"/>
    {heatmap}
  </g>

  <g transform="translate(48 1018)">
    <rect class="panel" x="0" y="0" width="432" height="120" rx="8"/>
    {streak}
    <rect class="panel" x="456" y="0" width="432" height="120" rx="8"/>
    {trend}
  </g>

  <g transform="translate(48 1158)">
    <rect class="panel" x="0" y="0" width="888" height="74" rx="8"/>
    {recent}
  </g>

  <text class="muted" x="490" y="1264" font-size="12" text-anchor="middle">口径：公开 owner repos；commit 为各仓库默认分支 commit 数汇总；贡献日历含私有贡献。</text>
</svg>
""".format(
        updated=m["updated"],
        kpis=render_kpis(m["kpis"]),
        star_rank=render_bars(m["star_rank"], 0, 18, 170, 236),
        commit_rank=render_bars(m["commit_rank"], 456, 474, 626, 236),
        donut=render_donut(m["dist"], m["total_repos"], 138, 136, 74),
        contrib=render_contrib(m["contrib"]),
        heatmap=render_heatmap(m["weeks"], m["total_contrib"]),
        streak=render_streak(m["cur_streak"], m["longest_streak"]),
        trend=render_trend(m["last30"]),
        recent=render_recent(m["recent"]),
    )


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def main():
    try:
        user = fetch_user()
        repos = fetch_repos()
        cal = fetch_calendar()
        metrics = compute_metrics(user, repos, cal)
        svg = render_svg(metrics)
    except Exception as exc:  # fail loud, never write a broken SVG
        print("ERROR: failed to generate dashboard: %s" % exc, file=sys.stderr)
        return 1
    with open(OUT_PATH, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(svg)
    print("wrote %s (%d repos, %d contributions)"
          % (OUT_PATH, len(repos), metrics["total_contrib"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
