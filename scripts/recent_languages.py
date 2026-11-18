"""Render a GitHub profile SVG of languages used in authored public commits.

Tracks languages of files touched by commits in owned public repositories over
90 days; a commit touching N different languages contributes once to each.
This is intentionally NOT the repo-size-based 'most-commit-language' widget.
Uses only the GitHub REST API and Python's standard library.
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

TOKEN = os.environ['GITHUB_TOKEN']
USERNAME = os.environ.get('GITHUB_USERNAME', 'cristinadianas')
CUTOFF = datetime.now(timezone.utc) - timedelta(days=90)
SINCE = CUTOFF.strftime('%Y-%m-%dT%H:%M:%SZ')
API = 'https://api.github.com'

LANGUAGES = {
    '.py': 'Python', '.pyi': 'Python', '.ipynb': 'Jupyter Notebook',
    '.cpp': 'C++', '.cxx': 'C++', '.cc': 'C++', '.hpp': 'C++',
    '.hxx': 'C++', '.hh': 'C++', '.c': 'C', '.h': 'C/C++',
    '.java': 'Java', '.kt': 'Kotlin', '.kts': 'Kotlin',
    '.js': 'JavaScript', '.jsx': 'JavaScript',
    '.ts': 'TypeScript', '.tsx': 'TypeScript',
    '.html': 'HTML', '.htm': 'HTML', '.css': 'CSS', '.scss': 'SCSS',
    '.sql': 'SQL', '.sh': 'Shell', '.bash': 'Shell', '.zsh': 'Shell',
    '.rs': 'Rust', '.go': 'Go', '.rb': 'Ruby', '.php': 'PHP',
    '.cs': 'C#', '.swift': 'Swift', '.dart': 'Dart',
    '.r': 'R', '.jl': 'Julia', '.lua': 'Lua',
    '.ino': 'Arduino', '.tex': 'TeX', '.m': 'Objective-C',
    '.mm': 'Objective-C++', '.vue': 'Vue', '.svelte': 'Svelte',
}
COLORS = {
    'Python': '#3776AB', 'C++': '#f34b7d', 'C': '#858585',
    'C/C++': '#8993be', 'Jupyter Notebook': '#DA5B0B',
    'Java': '#b07219', 'JavaScript': '#f1e05a', 'TypeScript': '#3178c6',
    'HTML': '#e34c26', 'CSS': '#563d7c', 'SQL': '#e38c00',
    'Shell': '#89e051', 'Rust': '#dea584', 'Go': '#00ADD8',
}


def api(path: str, params: dict | None = None):
    url = API + path
    if params:
        url += '?' + urlencode(params)
    req = Request(url, headers={
        'Authorization': f'Bearer {TOKEN}',
        'Accept': 'application/vnd.github+json',
        'X-GitHub-Api-Version': '2022-11-28',
        'User-Agent': 'profile-recent-languages-card',
    })
    try:
        with urlopen(req, timeout=30) as response:
            return json.load(response)
    except HTTPError as error:
        if error.code == 409:  # Empty repositories.
            return []
        raise RuntimeError(f'GitHub API failed ({error.code}): {path}') from error


def pages(path: str, params: dict):
    page = 1
    while True:
        data = api(path, {**params, 'per_page': 100, 'page': page})
        if not isinstance(data, list):
            raise RuntimeError(f'Expected a GitHub list from {path}')
        yield from data
        if len(data) < 100:
            return
        page += 1


def get_languages():
    counts = Counter()
    commit_total = 0
    repos = pages(f'/users/{quote(USERNAME)}/repos',
                  {'type': 'owner', 'sort': 'pushed', 'direction': 'desc'})
    for repo in repos:
        if repo.get('fork') or repo.get('archived'):
            continue
        pushed_at = repo.get('pushed_at')
        if pushed_at and datetime.fromisoformat(pushed_at.replace('Z', '+00:00')) < CUTOFF:
            continue
        name = repo['full_name']
        commits = pages(f'/repos/{name}/commits', {'author': USERNAME, 'since': SINCE})
        for commit in commits:
            sha = commit['sha']
            # GitHub paginates file lists for commits with > 100 changed files.
            touched = set()
            # Commit detail is a JSON object, not a list. Read the first page,
            # and only paginate the changed-file list when full.
            detail = api(f'/repos/{name}/commits/{sha}', {'per_page': 100, 'page': 1})
            changed_files = list(detail.get('files', []))
            if len(changed_files) == 100:
                page = 2
                while True:
                    next_detail = api(f'/repos/{name}/commits/{sha}',
                                      {'per_page': 100, 'page': page})
                    batch = next_detail.get('files', [])
                    changed_files.extend(batch)
                    if len(batch) < 100:
                        break
                    page += 1
            for changed in changed_files:
                extension = Path(changed.get('filename', '')).suffix.lower()
                language = LANGUAGES.get(extension)
                if language:
                    touched.add(language)
            if touched:
                counts.update(touched)
                commit_total += 1
    return counts, commit_total


def make_svg(counts: Counter, commit_total: int, dark: bool):
    bg = '#10172A' if dark else '#FFFFFF'
    border = '#2A3551' if dark else '#DDE4EF'
    fg = '#EEF2FF' if dark else '#182138'
    muted = '#A0AEC6' if dark else '#58657C'
    track = '#293453' if dark else '#E8EDF6'
    leaders = counts.most_common(6)
    maximum = leaders[0][1] if leaders else 1
    parts = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="380" viewBox="0 0 640 380" role="img">',
        f'<rect x="1" y="1" width="638" height="378" rx="18" fill="{bg}" stroke="{border}"/>',
        f'<text x="30" y="43" font-size="23" font-weight="700" fill="{fg}" font-family="Segoe UI,Arial,sans-serif">Recent coding languages</text>',
        f'<text x="30" y="70" font-size="16" fill="{muted}" font-family="Segoe UI,Arial,sans-serif">Last 90 days · {commit_total} public commits with code changes</text>',
    ]
    if not leaders:
        parts.append(f'<text x="30" y="170" font-size="20" fill="{muted}" font-family="Segoe UI,Arial,sans-serif">No recent public code commits found</text>')
    else:
        for index, (language, count) in enumerate(leaders):
            y = 102 + index * 42
            color = COLORS.get(language, '#8B5CF6')
            label = escape(language)
            parts.extend([
                f'<text x="30" y="{y+5}" fill="{fg}" font-size="17" font-family="Segoe UI,Arial,sans-serif">{label}</text>',
                f'<rect x="225" y="{y-12}" width="320" height="16" rx="7" fill="{track}"/>',
                f'<rect x="225" y="{y-12}" width="{max(4, int(320*count/maximum))}" height="16" rx="7" fill="{color}"/>',
                f'<text x="557" y="{y+2}" fill="{fg}" font-size="16" font-family="Segoe UI,Arial,sans-serif">{count}</text>',
            ])
    parts.extend([
        f'<text x="30" y="359" fill="{muted}" font-size="14" font-family="Segoe UI,Arial,sans-serif">Count = commits touching files of each language</text>',
        '</svg>',
    ])
    return '\n'.join(parts)


def main():
    counts, commit_total = get_languages()
    dest = Path('assets')
    dest.mkdir(parents=True, exist_ok=True)
    for theme, dark in [('dark', True), ('light', False)]:
        (dest / f'recent-languages.{theme}.svg').write_text(
            make_svg(counts, commit_total, dark), encoding='utf-8')
    print(f'Generated recent-language cards for {commit_total} commits: {counts}')


if __name__ == '__main__':
    main()
