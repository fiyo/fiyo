"""RaccoonX 个人页统计图表生成器。

纯 Python 标准库：抓取公开 GitHub 数据 -> 生成 5 张 SVG -> 由 GitHub Actions 每日提交回仓库。
不依赖任何第三方服务（不使用 github-readme-stats 之类的外部卡片），图表随仓库自托管。

用法：
    python scripts/update_stats.py             # 抓真实公开数据
    python scripts/update_stats.py --demo     # 用示例数据生成本地预览，不联网
    python scripts/update_stats.py --user fiyo

环境变量：
    PROFILE_USER   目标 GitHub 用户名（默认 fiyo）
    GH_TOKEN       GitHub Actions 自动注入；本地可选，用于提高 API 限额
"""

import argparse
import collections
import datetime as dt
import html
import json
import os
import random
import re
import time
import urllib.parse
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "stats"
DEMO_OUT = ROOT / "assets" / "stats-demo"

# RaccoonX 官网同款暖色系（raccoonx.cn）
BG = "#FFF8F3"
BORDER = "#F1D6C6"
TRACK = "#F6E5D9"
TEXT = "#2A0E08"
MUTED = "#8C6B5D"
COLORS = ["#F0511E", "#E23B2E", "#FFB454", "#C2410C", "#F4773C"]
LEVELS = ["#F6E5D9", "#FBD3B4", "#F9AC7C", "#F4773C", "#F0511E"]
FONT = "Arial, 'Microsoft YaHei', 'PingFang SC', sans-serif"
WIDTH = 1000

# 图表展示别名：真实仓库名仍完整保留在 data.json 中，仅展示层统一用 RaccoonX 品牌名
DISPLAY_NAMES = {
    "DBCheck": "RaccoonX 主仓库",
    "RaccoonX-website": "RaccoonX 官网",
    "DBCheck-website": "官网（旧站）",
}


def fetch(url, as_json=True):
    headers = {
        "User-Agent": "RaccoonX-Profile-Stats",
        "Accept": "application/vnd.github+json",
    }
    token = os.environ.get("GH_TOKEN")
    if token and url.startswith("https://api.github.com/"):
        headers["Authorization"] = f"Bearer {token}"

    content = None
    last_error = None
    for attempt in range(3):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=45) as response:
                content = response.read().decode("utf-8")
            break
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(3 * (attempt + 1))

    if content is None:
        # 本地代理对 urllib 的 CONNECT 不友好时，退回系统 curl（GitHub Actions 上通常走不到这里）
        import subprocess
        cmd = ["curl", "-fsSL", "--max-time", "45"]
        for key, value in headers.items():
            cmd += ["-H", f"{key}: {value}"]
        cmd.append(url)
        try:
            result = subprocess.run(cmd, capture_output=True, check=True, timeout=60)
            content = result.stdout.decode("utf-8")
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"抓取失败: {url} -> urllib: {last_error}; curl: {exc}") from exc

    return json.loads(content) if as_json else content


class CalendarParser(HTMLParser):
    """兼容 GitHub 贡献日历的两种输出：td + tool-tip 文本，或 rect/td 的 data-level。"""

    def __init__(self):
        super().__init__()
        self.cell_ids = {}
        self.counts = {}
        self.levels = {}
        self.tip_for = None
        self.tip_text = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "td" and "data-date" in attrs and attrs.get("id"):
            self.cell_ids[attrs["id"]] = attrs["data-date"]
        if tag == "tool-tip" and attrs.get("for") in self.cell_ids:
            self.tip_for = attrs["for"]
            self.tip_text = []
        if "data-date" in attrs and "data-level" in attrs:
            try:
                self.levels[attrs["data-date"]] = int(attrs["data-level"])
            except ValueError:
                pass

    def handle_data(self, data):
        if self.tip_for:
            self.tip_text.append(data)

    def handle_endtag(self, tag):
        if tag == "tool-tip" and self.tip_for:
            date = self.cell_ids[self.tip_for]
            content = "".join(self.tip_text).strip()
            if content.lower().startswith("no contributions"):
                self.counts[date] = 0
            else:
                match = re.search(r"([\d,]+)\s+contribution", content)
                if match:
                    self.counts[date] = int(match.group(1).replace(",", ""))
            self.tip_for = None


def search(endpoint, query, per_page=1, page=1):
    params = urllib.parse.urlencode({"q": query, "per_page": per_page, "page": page})
    result = fetch(f"https://api.github.com/search/{endpoint}?{params}")
    if not isinstance(result, dict):
        raise RuntimeError(f"搜索接口返回异常: {endpoint}")
    if result.get("incomplete_results"):
        raise RuntimeError("GitHub 返回了不完整的搜索结果，保留现有图表不覆盖")
    return result


def collect(user):
    page = fetch(f"https://github.com/users/{user}/contributions", as_json=False)
    parser = CalendarParser()
    parser.feed(page)

    days = dict(parser.counts)
    precision = "每日精确贡献数"
    if len(days) < 300:
        if len(parser.levels) >= 300:
            days = dict(parser.levels)
            precision = "GitHub 热度等级 0-4（页面结构变更后降级）"
        else:
            raise RuntimeError("贡献日历解析失败：GitHub 页面结构可能已变更，保留现有图表不覆盖")

    days = dict(sorted(days.items())[-365:])
    start, end = min(days), max(days)
    period = f"{start}..{end}"

    commits_query = f"author:{user} committer-date:{period} is:public"
    commits = search("commits", commits_query, per_page=100)
    total_commits = commits.get("total_count", 0)
    items = list(commits.get("items", []))
    # GitHub 搜索最多返回 1000 条；最多取 10 页做有界采样，并在脚注注明
    for page_no in range(2, min((total_commits + 99) // 100, 10) + 1):
        time.sleep(2)
        items.extend(search("commits", commits_query, per_page=100, page=page_no).get("items", []))
    unique = {(item["repository"]["full_name"], item["sha"]) for item in items}

    activity = {"提交 Commits": total_commits}
    for label, kind in [("合并请求 PR", "pr"), ("问题 Issues", "issue")]:
        query = f"author:{user} type:{kind} created:{period} is:public"
        activity[label] = search("issues", query).get("total_count", 0)

    repositories = []
    page_no = 1
    while True:
        batch = fetch(f"https://api.github.com/users/{user}/repos?type=owner&per_page=100&page={page_no}")
        if not isinstance(batch, list):
            raise RuntimeError("仓库列表返回异常")
        repositories.extend(batch)
        if len(batch) < 100:
            break
        page_no += 1
    owned = [repo for repo in repositories if not repo["fork"] and not repo["archived"]]
    languages = collections.Counter(repo["language"] for repo in owned if repo["language"])
    # author: 搜索会按提交者邮箱匹配，别人 fork 本账号仓库的提交也会命中。
    # 活跃仓库只统计本人名下的仓库，外部命中单独记录供审计。
    active_all = collections.Counter(name for name, _ in unique)
    active = collections.Counter({
        name: count for name, count in active_all.most_common()
        if name.lower().startswith(user.lower() + "/")
    })
    external_matches = sum(active_all.values()) - sum(active.values())
    if not active:
        active = active_all

    return {
        "user": user,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "start": start,
        "end": end,
        "days": days,
        "calendar_precision": precision,
        "activity": activity,
        "languages": dict(languages.most_common()),
        "language_repos": sum(languages.values()),
        "unclassified_repos": sum(repo["language"] is None for repo in owned),
        "active_repositories": dict(active.most_common(5)),
        "commit_sample": len(unique),
        "commit_total": total_commits,
        "external_commit_matches": external_matches,
        "sources": [
            f"https://github.com/users/{user}/contributions",
            f"https://api.github.com/users/{user}/repos?type=owner&per_page=100",
            "https://api.github.com/search/commits?q=" + urllib.parse.quote(commits_query),
            "https://api.github.com/search/issues?q=" + urllib.parse.quote(
                f"author:{user} created:{period} is:public"),
        ],
        "definitions": {
            "calendar": "GitHub 公开贡献日历最近 365 天，遵循 GitHub 贡献计入规则，不含私有仓库贡献。",
            "activity": "公开搜索统计：提交按 committer-date，PR / Issue 按创建时间，受搜索索引规则影响。",
            "languages": "公开、自有、非 fork、未归档仓库按主语言计数，无主语言的仓库不计入。",
            "active_repositories": "公开提交搜索命中的 (仓库, SHA) 去重计数前五，仅统计本人名下仓库，最多采样 1000 条。",
        },
    }


def demo_data(user):
    """离线预览用的示例数据（固定随机种子，结果可复现）。"""
    rng = random.Random(20261115)
    today = dt.date.today()
    days = {}
    for offset in range(364, -1, -1):
        day = today - dt.timedelta(days=offset)
        progress = (365 - offset) / 365
        chance = 0.18 + 0.62 * progress ** 2
        if day.weekday() >= 5:
            chance *= 0.6
        days[day.isoformat()] = rng.choice([1, 1, 2, 2, 3, 4, 6, 8, 12]) if rng.random() < chance else 0
    languages = {"Python": 24, "JavaScript": 6, "TypeScript": 4, "HTML": 3,
                 "Shell": 2, "Go": 1, "Vue": 1}
    start, end = min(days), max(days)
    return {
        "user": user,
        "updated": "示例数据（占位）· 待 Actions 首次运行替换",
        "start": start,
        "end": end,
        "days": days,
        "calendar_precision": "示例数据",
        "activity": {"提交 Commits": 428, "合并请求 PR": 37, "问题 Issues": 26},
        "languages": languages,
        "language_repos": sum(languages.values()),
        "unclassified_repos": 3,
        "active_repositories": {
            f"{user}/raccoonx-core": 218,
            f"{user}/raccoonx.cn": 96,
            f"{user}/raccoonx-docs": 54,
            f"{user}/plugin-kit": 31,
            f"{user}/agents-lab": 19,
        },
        "commit_sample": 428,
        "commit_total": 428,
        "external_commit_matches": 0,
        "sources": ["示例数据，未联网"],
        "definitions": {},
    }


def text(x, y, content, size=14, fill=TEXT, anchor="start", weight="400"):
    return (f'<text x="{x}" y="{y}" fill="{fill}" font-size="{size}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{html.escape(str(content))}</text>')


def card(title, subtitle, body, height, data):
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" '
        f'viewBox="0 0 {WIDTH} {height}" role="img">'
        f'<title>{html.escape(title)}</title><desc>{html.escape(subtitle)}</desc>'
        f'<g font-family="{FONT}">'
        f'<rect x="1" y="1" width="{WIDTH-2}" height="{height-2}" rx="18" '
        f'fill="{BG}" stroke="{BORDER}"/>'
        + text(32, 40, title, 23, TEXT, weight="700")
        + text(32, 67, subtitle, 13, MUTED)
        + body
        + text(968, height - 16, "数据更新 " + data["updated"], 11, MUTED, "end")
        + '</g></svg>'
    )


def display_repo(name, user):
    short = name.removeprefix(user + "/")
    return DISPLAY_NAMES.get(short, short)


def bars(title, subtitle, rows, data, footnote=""):
    height = max(240, 120 + 47 * len(rows) + (25 if footnote else 0))
    maximum = max((value for _, value in rows), default=1) or 1
    body = ""
    for index, (label, value) in enumerate(rows):
        y = 111 + index * 47
        body += text(32, y + 5, label, 15)
        body += f'<rect x="285" y="{y-10}" width="580" height="18" rx="4" fill="{TRACK}"/>'
        if value:
            width = 580 * value / maximum
            body += (f'<rect x="285" y="{y-10}" width="{width:.2f}" height="18" '
                     f'rx="4" fill="{COLORS[index % len(COLORS)]}"/>')
        body += text(955, y + 5, f"{value:,}", 16, TEXT, "end")
    if not rows:
        body += text(32, 130, "该周期内没有公开记录", 16, MUTED)
    if footnote:
        body += text(32, height - 43, footnote, 12, MUTED)
    return card(title, subtitle, body, height, data)


def render_calendar(data):
    days = data["days"]
    total = sum(days.values())
    active_days = sum(value > 0 for value in days.values())
    peak = max(days.values()) or 1
    body = text(32, 114, f"{total:,} 次贡献", 25, COLORS[0], weight="700")
    body += text(370, 114, f"{active_days} 个活跃日", 20, COLORS[2])
    body += text(700, 114, f"单日峰值 {peak:,}", 20, COLORS[4])

    first = dt.date.fromisoformat(data["start"])
    first_sunday = first - dt.timedelta(days=(first.weekday() + 1) % 7)
    months_seen = set()
    for day, count in days.items():
        date = dt.date.fromisoformat(day)
        week = (date - first_sunday).days // 7
        weekday = (date.weekday() + 1) % 7
        x, y = 70 + week * 17, 161 + weekday * 17
        level = 0 if count == 0 else min(4, 1 + (count - 1) * 4 // peak)
        body += (f'<rect x="{x}" y="{y}" width="13" height="13" rx="3" '
                 f'fill="{LEVELS[level]}"><title>{day} · {count} 次贡献</title></rect>')
        month = day[:7]
        if month not in months_seen:
            if date.day <= 7 or not months_seen:
                body += text(x, 149, f"{date.month}月", 11, MUTED)
            months_seen.add(month)
    for label, row in [("一", 1), ("三", 3), ("五", 5)]:
        body += text(31, 171 + row * 17, label, 10, MUTED)
    body += text(32, 315, f'{data["start"]} — {data["end"]} · 遵循 GitHub 贡献规则', 12, MUTED)
    body += text(730, 315, "少", 11, MUTED)
    for i, color in enumerate(LEVELS):
        body += f'<rect x="{765 + i*18}" y="304" width="13" height="13" rx="3" fill="{color}"/>'
    body += text(865, 315, "多", 11, MUTED)
    return card("贡献日历", "最近 365 天的每日公开贡献", body, 355, data)


def render_monthly(data):
    monthly = collections.Counter()
    for day, count in data["days"].items():
        monthly[day[:7]] += count
    months = sorted(monthly)
    peak = max(monthly.values()) or 1
    body = ""
    for tick in range(5):
        value = peak * tick / 4
        y = 258 - tick * 37
        body += f'<path d="M70 {y}H948" stroke="{BORDER}" stroke-width="1" fill="none"/>'
        body += text(56, y + 4, f"{value:g}", 11, MUTED, "end")
    points = []
    for i, month in enumerate(months):
        x = 78 + i * 860 / max(len(months) - 1, 1)
        y = 258 - monthly[month] / peak * 148
        points.append((x, y, month))
    body += ('<polyline points="' + " ".join(f"{x:.1f},{y:.1f}" for x, y, _ in points)
             + f'" fill="none" stroke="{COLORS[0]}" stroke-width="3"/>')
    for x, y, month in points:
        body += f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{COLORS[0]}"/>'
        body += text(round(x, 1), round(y - 12, 1), monthly[month], 12, TEXT, "middle")
        body += text(round(x, 1), 283, month[2:], 11, MUTED, "middle")
    body += text(32, 316, "每月贡献数 · 首尾月份可能不完整", 12, MUTED)
    return card("贡献趋势", f'{data["start"]} — {data["end"]} · 按月汇总', body, 355, data)


def render(data, out_dir=None):
    out = out_dir or OUT
    assets = {
        "calendar.svg": render_calendar(data),
        "monthly.svg": render_monthly(data),
        "activity.svg": bars(
            "公开活动",
            f'{data["start"]} — {data["end"]} · GitHub 公开搜索统计',
            list(data["activity"].items()), data,
            footnote="提交按 committer date，PR / Issue 按创建时间，受搜索索引规则影响"),
    }
    languages = list(data["languages"].items())
    if len(languages) > 6:
        languages = languages[:5] + [("其他", sum(value for _, value in languages[5:]))]
    assets["languages.svg"] = bars(
        "仓库语言分布",
        f'{data["language_repos"]} 个公开非 fork、未归档仓库的主语言',
        languages, data,
        footnote=f'按主语言统计仓库数 · {data["unclassified_repos"]} 个无主语言仓库未计入')

    sample_note = f'{data["commit_sample"]:,} 条去重提交命中 · 仅统计本人名下仓库'
    if data["commit_total"] > 1000:
        sample_note += f'，从 {data["commit_total"]:,} 条结果中采样（GitHub 搜索上限）'
    rows = [(display_repo(name, data["user"]), count)
            for name, count in data["active_repositories"].items()]
    assets["repositories.svg"] = bars(
        "活跃仓库 Top 5",
        f'{data["start"]} — {data["end"]} · 按公开提交搜索命中数',
        rows, data, footnote=sample_note + " · 可能包含 fork 历史")

    out.mkdir(parents=True, exist_ok=True)
    for name, content in assets.items():
        (out / name).write_text(content + "\n", encoding="utf-8")
    (out / "data.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return assets, out


def main():
    parser = argparse.ArgumentParser(description="RaccoonX 个人页统计图表生成器")
    parser.add_argument("--demo", action="store_true", help="用示例数据生成预览，不联网")
    parser.add_argument("--from-json", action="store_true", help="用现有 data.json 重绘，不联网（改配色后用）")
    parser.add_argument("--user", default=os.environ.get("PROFILE_USER", "fiyo"))
    args = parser.parse_args()

    if args.demo:
        data = demo_data(args.user)
        out_dir = DEMO_OUT
    elif args.from_json:
        data = json.loads((OUT / "data.json").read_text(encoding="utf-8"))
        out_dir = None
    else:
        data = collect(args.user)
        out_dir = None

    assets, out = render(data, out_dir)
    print(json.dumps({
        "user": data["user"],
        "updated": data["updated"],
        "days": len(data["days"]),
        "contributions": sum(data["days"].values()),
        "charts": list(assets),
        "output": str(out.relative_to(ROOT)).replace("\\", "/"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
