#!/usr/bin/env python3
"""Generate language-by-recent-change and peak-public-commit-day SVGs.

This script intentionally uses GitHub's **repository commit API**, not the
limited account events stream. Data scope: public, non-fork, public repositories,
default-branch history. Language shares are from changed lines (additions plus
deletions), not claimed proficiency or the full repository's code composition.

No third-party Python dependencies are required.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

API = 'https://api.github.com'
USERNAME = os.environ.get('GITHUB_USERNAME', 'cristinadianas')
TOKEN = os.environ.get('GITHUB_TOKEN', '')
NOW = datetime.now(timezone.utc)
CUTOFF = NOW - timedelta(days=90)
YEAR_CUTOFF = NOW - timedelta(days=365)
# Safety cap for large accounts. This is shown on the output when reached.
MAX_DETAIL_COMMITS = 800

EXTENSIONS = {
    '.py': 'Python', '.pyi': 'Python', '.ipynb': 'Jupyter Notebook',
    '.cpp': 'C++', '.cxx': 'C++', '.cc': 'C++', '.hpp': 'C++',
    '.hxx': 'C++', '.hh': 'C++', '.c': 'C', '.h': 'C/C++',
    '.java': 'Java', '.kt': 'Kotlin', '.kts': 'Kotlin',
    '.js': 'JavaScript', '.jsx': 'JavaScript', '.mjs': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript',
    '.html': 'HTML', '.htm': 'HTML', '.css': 'CSS', '.scss': 'SCSS',
    '.sql': 'SQL', '.sh': 'Shell', '.bash': 'Shell', '.zsh': 'Shell',
    '.rs': 'Rust', '.go': 'Go', '.rb': 'Ruby', '.php': 'PHP',
    '.cs': 'C#', '.swift': 'Swift', '.dart': 'Dart',
    '.r': 'R', '.jl': 'Julia', '.lua': 'Lua', '.ino': 'Arduino',
    '.tex': 'TeX', '.m': 'Objective-C', '.mm': 'Objective-C++',
    '.vue': 'Vue', '.svelte': 'Svelte', '.yml': 'YAML', '.yaml': 'YAML',
}
LANG_COLORS = {
    'Python': '#3572A5', 'Jupyter Notebook': '#DA5B0B',
    'C++': '#f34b7d', 'C': '#555555', 'C/C++': '#8993be',
    'Java': '#b07219', 'JavaScript': '#f1e05a', 'TypeScript': '#3178c6',
    'HTML': '#e34c26', 'CSS': '#563d7c', 'SCSS': '#c6538c',
    'SQL': '#e38c00', 'Shell': '#89e051', 'Rust': '#dea584',
    'Go': '#00ADD8', 'Kotlin': '#A97BFF', 'TeX': '#3D6117',
    'YAML': '#cb171e',
}


def api(path: str, params: dict | None = None):
    url = API + path
    if params:
        url += '?' + urlencode(params)
    headers = {
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'cristinadianas-profile-statistics',
    }
    if TOKEN:
        headers['Authorization'] = f'Bearer {TOKEN}'
    req = Request(url, headers=headers)
    for attempt in range(3):
        try:
            with urlopen(req, timeout=35) as response:
                return json.load(response)
        except HTTPError as err:
            if err.code == 409:  # GitHub returns 409 for repositories without commits
                return []
            if err.code in (429, 500, 502, 503, 504) and attempt < 2:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f'GitHub API HTTP {err.code} on {path}') from err
        except URLError as err:
            if attempt == 2:
                raise RuntimeError(f'GitHub API unavailable for {path}') from err
            time.sleep(2 ** attempt)
    raise RuntimeError(f'Unexpected failure for {path}')


def pages(path: str, params: dict):
    page = 1
    while True:
        batch = api(path, {**params, 'per_page': 100, 'page': page})
        if not isinstance(batch, list):
            raise RuntimeError(f'GitHub API did not return a list from {path}')
        yield from batch
        if len(batch) < 100:
            return
        page += 1


def date_time(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


def discover_repositories():
    """Include public repositories where the user contributed, not only owned repos."""
    repos = {}
    for repo in pages(f'/users/{quote(USERNAME)}/repos',
                      {'type': 'owner', 'sort': 'pushed', 'direction': 'desc'}):
        if repo.get('fork') or repo.get('archived') or repo.get('private'):
            continue
        pushed = repo.get('pushed_at')
        if pushed and date_time(pushed) < YEAR_CUTOFF:
            continue
        repos[repo['full_name']] = repo

    # GitHub's contributions collection also includes public repositories
    # owned by other people or organizations, where authored commits count.
    query = '''query($user: String!) {
      user(login: $user) {
        contributionsCollection {
          commitContributionsByRepository(maxRepositories: 100) {
            repository { nameWithOwner isPrivate isArchived isFork pushedAt }
          }
        }
      }
    }'''
    try:
        req = Request('https://api.github.com/graphql',
            data=json.dumps({'query': query, 'variables': {'user': USERNAME}}).encode(),
            headers={
                'Accept': 'application/vnd.github+json',
                'Authorization': f'Bearer {TOKEN}',
                'Content-Type': 'application/json',
                'User-Agent': 'cristinadianas-profile-statistics',
            }, method='POST')
        with urlopen(req, timeout=35) as response:
            result = json.load(response)
        if result.get('errors'):
            print('GraphQL repo discovery unavailable; using owned public repos', flush=True)
        else:
            entries = (result.get('data', {}).get('user', {})
                       .get('contributionsCollection', {})
                       .get('commitContributionsByRepository', []))
            for entry in entries:
                repo = entry.get('repository') or {}
                if repo.get('isPrivate') or repo.get('isArchived') or repo.get('isFork'):
                    continue
                name = repo.get('nameWithOwner')
                if name:
                    repos[name] = {'full_name': name, 'pushed_at': repo.get('pushedAt')}
    except (HTTPError, URLError, ValueError) as err:
        print('GraphQL repo discovery unavailable; using owned public repos:', err, flush=True)
    return list(repos.values())


def gather():
    changes_by_language = Counter()
    commit_days = Counter()
    commits_with_code = 0
    files_with_code = 0
    recent_commits_examined = 0
    limited = False
    repos_examined = 0
    for repo in discover_repositories():
        pushed = repo.get('pushed_at')
        if pushed and date_time(pushed) < YEAR_CUTOFF:
            continue
        repo_full = repo['full_name']
        repos_examined += 1
        path = f'/repos/{quote(repo_full, safe="/")}/commits'
        for commit in pages(path, {
            'author': USERNAME,
            'since': YEAR_CUTOFF.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }):
            # GitHub's commit API returns author dates for each authored commit.
            raw_date = (commit.get('commit', {}).get('author') or {}).get('date')
            if not raw_date:
                raw_date = (commit.get('commit', {}).get('committer') or {}).get('date')
            if not raw_date:
                continue
            day = date_time(raw_date)
            if day < YEAR_CUTOFF or day > NOW:
                continue
            commit_days[day.date().isoformat()] += 1
            if day < CUTOFF:
                continue
            if recent_commits_examined >= MAX_DETAIL_COMMITS:
                limited = True
                continue
            recent_commits_examined += 1
            sha = commit['sha']
            # Changed-file details may be paginated for large commits.
            detail_path = f'/repos/{quote(repo_full, safe="/")}/commits/{sha}'
            commit_has_code = False
            for file in pages_changed_files(detail_path):
                filename = file.get('filename', '')
                language = EXTENSIONS.get(Path(filename).suffix.lower())
                if language is None:
                    continue
                lines_changed = (file.get('additions') or 0) + (file.get('deletions') or 0)
                if lines_changed <= 0:
                    continue
                changes_by_language[language] += lines_changed
                files_with_code += 1
                commit_has_code = True
            if commit_has_code:
                commits_with_code += 1
    return {
        'languages': changes_by_language,
        'commit_days': commit_days,
        'commits_with_code': commits_with_code,
        'files_with_code': files_with_code,
        'recent_commits_examined': recent_commits_examined,
        'limited': limited,
        'repos_examined': repos_examined,
    }


def pages_changed_files(path: str):
    page = 1
    while True:
        response = api(path, {'per_page': 100, 'page': page})
        if not isinstance(response, dict):
            return
        batch = response.get('files', [])
        yield from batch
        if len(batch) < 100:
            break
        page += 1


def palette(dark: bool):
    if dark:
        return dict(bg='#0B1712', fg='#E7F9EF', subtle='#9FCBB1',
                    stroke='#254A37', track='#1D3529', accent='#6EE7B7')
    return dict(bg='#F7FCF9', fg='#16382A', subtle='#4B7462',
                stroke='#CCDFD3', track='#DFEBE2', accent='#047857')


def recent_svg(stats: dict, dark: bool) -> str:
    p = palette(dark)
    counts = stats['languages']
    top = counts.most_common(8)
    total = sum(counts.values())
    subtitle = (f"Last 90 days · {stats['commits_with_code']} code commits · "
                f"{stats['files_with_code']} files changed")
    if stats['limited']:
        subtitle += f" · most recent {MAX_DETAIL_COMMITS} commits sampled"
    height = 206 + 39 * ((len(top) + 1) // 2)
    height = max(height, 270)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="860" height="{height}" viewBox="0 0 860 {height}" role="img">',
        f'<rect width="860" height="{height}" rx="16" fill="{p["bg"]}"/>',
        f'<text x="28" y="42" font-family="Arial, sans-serif" font-size="24" font-weight="700" fill="{p["fg"]}">▾ Recently used languages</text>',
        f'<text x="28" y="72" font-family="Arial, sans-serif" font-size="15" fill="{p["accent"]}">{len(counts)} languages in public commits</text>',
        f'<text x="28" y="104" font-family="Arial, sans-serif" font-size="15" fill="{p["subtle"]}">{escape(subtitle)}</text>',
        f'<rect x="28" y="123" width="804" height="16" rx="6" fill="{p["track"]}"/>',
    ]
    if total:
        bar_x = 28.0
        for idx, (lang, changed) in enumerate(counts.most_common()):
            width = 804.0 * changed / total
            if width <= 0.01:
                continue
            color = LANG_COLORS.get(lang, ['#34D399','#6EE7B7','#86EFAC','#FBBF24'][idx % 4])
            parts.append(f'<rect x="{bar_x:.2f}" y="123" width="{width:.2f}" height="16" fill="{color}"/>')
            bar_x += width
        for i, (lang, changed) in enumerate(top):
            col = i % 2
            row = i // 2
            x = 29 + col * 414
            y = 178 + row * 39
            percent = changed / total * 100
            color = LANG_COLORS.get(lang, '#34D399')
            parts.extend([
                f'<circle cx="{x+7}" cy="{y-5}" r="6" fill="{color}"/>',
                f'<text x="{x+23}" y="{y}" font-family="Arial, sans-serif" font-size="18" fill="{p["fg"]}">{escape(lang)}</text>',
                f'<text x="{x+380}" y="{y}" font-family="Arial, sans-serif" font-size="16" text-anchor="end" fill="{p["subtle"]}">{percent:.1f}%</text>',
            ])
    else:
        parts.append(f'<text x="28" y="185" font-family="Arial, sans-serif" font-size="18" fill="{p["subtle"]}">No recent public code changes found on public default branches</text>')
    parts.append(f'<text x="28" y="{height-21}" font-family="Arial, sans-serif" font-size="14" fill="{p["subtle"]}">Share of changed lines (additions + deletions) · Excludes private and forked repositories</text>')
    parts.append('</svg>')
    return '\n'.join(parts)


def peak_svg(stats: dict, dark: bool) -> str:
    p = palette(dark)
    dates = stats['commit_days']
    max_count = max(dates.values(), default=0)
    peak_dates = sorted(d for d, count in dates.items() if count == max_count and max_count > 0)
    date_label = datetime.fromisoformat(peak_dates[0]).strftime('%b %d, %Y') if peak_dates else 'No public commits found'
    if len(peak_dates) > 1:
        date_label += f' (and {len(peak_dates)-1} other day(s))'
    return '\n'.join([
        '<svg xmlns="http://www.w3.org/2000/svg" width="860" height="96" viewBox="0 0 860 96" role="img">',
        f'<rect x="0.5" y="0.5" width="859" height="95" rx="15" fill="{p["bg"]}" stroke="{p["stroke"]}"/>',
        f'<text x="28" y="39" font-family="Arial, sans-serif" font-size="21" font-weight="700" fill="{p["fg"]}">Most commits in one day</text>',
        f'<text x="28" y="67" font-family="Arial, sans-serif" font-size="14" fill="{p["subtle"]}">Last 12 months · public public repositories · {escape(date_label)}</text>',
        f'<text x="828" y="59" text-anchor="end" font-family="Arial, sans-serif" font-size="42" font-weight="700" fill="{p["accent"]}">{max_count}</text>',
        '</svg>',
    ])


def main():
    if not TOKEN:
        raise SystemExit('Set GITHUB_TOKEN in the workflow environment')
    data = gather()
    target = Path('assets')
    target.mkdir(exist_ok=True)
    for theme, dark in [('dark', True), ('light', False)]:
        (target / f'recent-languages.{theme}.svg').write_text(
            recent_svg(data, dark), encoding='utf-8')
        (target / f'peak-commits.{theme}.svg').write_text(
            peak_svg(data, dark), encoding='utf-8')
    print('Generated recent languages from', data['commits_with_code'], 'public code commits')
    print('Recent language change lines:', data['languages'].most_common())
    print('Peak authored public commits/day:', max(data['commit_days'].values(), default=0))
    print('Repos checked:', data['repos_examined'])


if __name__ == '__main__':
    main()
