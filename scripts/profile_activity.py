#!/usr/bin/env python3
"""Generate both language panels and add peak commits to the calendar.

This script intentionally uses GitHub's **repository commit API**, not the
limited account events stream. Data scope: public, non-fork repositories,
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
from xml.dom import Node, minidom

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


LANG_COLORS = {
    'Python': '#3572A5', 'Jupyter Notebook': '#DA5B0B',
    'C++': '#f34b7d', 'C': '#555555', 'C/C++': '#8993be',
    'Java': '#b07219', 'JavaScript': '#f1e05a', 'TypeScript': '#3178c6',
    'HTML': '#e34c26', 'CSS': '#563d7c', 'SCSS': '#c6538c',
    'SQL': '#e38c00', 'Shell': '#89e051', 'Rust': '#dea584',
    'Go': '#00ADD8', 'Kotlin': '#A97BFF', 'TeX': '#3D6117',
    'YAML': '#cb171e', 'CSV': '#237346', 'JSON': '#8b949e',
    'EJS': '#a91e50', 'SVG': '#ff9900',
}


def elements(node):
    return [child for child in node.childNodes if child.nodeType == Node.ELEMENT_NODE]


def set_text(node, value):
    while node.firstChild:
        node.removeChild(node.firstChild)
    node.appendChild(node.ownerDocument.createTextNode(value))


def text_content(node):
    return ''.join(child.data if child.nodeType == Node.TEXT_NODE else text_content(child)
                   for child in node.childNodes).strip()


def most_used_data(source: str):
    """Read the language rows and summary from the Metrics action output."""
    root = minidom.parseString(source).documentElement
    headers = root.getElementsByTagName('h2')
    if not headers:
        raise ValueError('Metrics output is missing its language section')
    summaries = root.getElementsByTagName('small')
    if not summaries:
        raise ValueError('Metrics output is missing its language analysis summary')
    subtitle = text_content(summaries[0])
    rows = []
    for item in root.getElementsByTagName('div'):
        if item.getAttribute('class') != 'field language details':
            continue
        name, details = elements(item)
        language = text_content(name)
        if language == 'Other':
            continue
        lines, size, percentage = [text_content(field) for field in elements(details)]
        share = float(percentage.removesuffix('%')) / 100
        color = name.getElementsByTagName('path')[0].getAttribute('fill')
        rows.append((language, share, (lines, size, percentage), color))
    return text_content(headers[0]), subtitle, sorted(rows, key=lambda row: row[1], reverse=True)[:8]


def language_section(heading, count, subtitle, rows, top, bar_id):
    bar_y = top + 98
    width = 798
    section = [
        f'<path d="M25 {top+13}h17v12H31l-5 4v-4h-1z m6 4-2 2 2 2 m4-4 2 2-2 2" class="icon"/>',
        f'<text x="51" y="{top+26}" class="green count">{escape(count)}</text>',
        f'<text x="423" y="{top+60}" text-anchor="middle" class="green">{heading}</text>',
        f'<text x="423" y="{top+82}" text-anchor="middle" class="detail">{escape(subtitle)}</text>',
        f'<clipPath id="{bar_id}"><rect x="24" y="{bar_y}" width="{width}" height="9" rx="4.5"/></clipPath>',
        f'<g clip-path="url(#{bar_id})"><rect x="24" y="{bar_y}" width="{width}" height="9" class="track"/>',
    ]
    x = 24
    for _, share, _, color in rows:
        segment = width * share
        section.append(f'<rect x="{x:.3f}" y="{bar_y}" width="{segment:.3f}" height="9" fill="{escape(color)}"/>')
        x += segment
    section.append('</g>')
    for index, (language, _, details, color) in enumerate(rows):
        x = 30 + (index % 2) * 405
        y = top + 132 + (index // 2) * 24
        section.append(f'<circle cx="{x}" cy="{y-5}" r="5.5" fill="{escape(color)}"/>')
        section.append(f'<text x="{x+17}" y="{y}" class="language">{escape(language)}</text>')
        for offset, detail in zip((377, 306, 224), reversed(details)):
            section.append(f'<text x="{x+offset}" y="{y}" class="detail" text-anchor="end">{escape(detail)}</text>')
    return section, top + 149 + max(0, (len(rows) - 1) // 2) * 24


def languages_svg(most_used, recently_used, dark: bool) -> str:
    most, end = language_section('Most used languages', *most_used, 15, 'most-used-bar')
    recent, height = language_section('Recently used languages', *recently_used, end + 9, 'recent-bar')
    if dark:
        bg, border, fg, secondary, green, track = '#0d1117', '#3d444d', '#f0f6fc', '#9198a1', '#3fb950', '#30363d'
    else:
        bg, border, fg, secondary, green, track = '#ffffff', '#d1d9e0', '#1f2328', '#59636e', '#1a7f37', '#d1d9e0'
    return '\n'.join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="846" height="{height}" viewBox="0 0 846 {height}" role="img">',
        '<title>Most used and recently used languages</title>',
        '<style>',
        'text{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif}',
        f'.green{{fill:{green};font-size:17px;font-weight:600}}.count{{font-size:18px}}',
        f'.detail{{fill:{secondary};font-size:13px}}.language{{fill:{fg};font-size:16px}}',
        f'.icon{{fill:none;stroke:{secondary};stroke-width:1.4;stroke-linejoin:round}}.track{{fill:{track}}}',
        '</style>',
        f'<rect x="0.5" y="0.5" width="845" height="{height-1}" rx="12" fill="{bg}" stroke="{border}"/>',
        *most,
        f'<path d="M24 {end}h798" stroke="{border}"/>',
        *recent,
        '</svg>',
        '',
    ])


def with_peak_commits(source: str, count: int, date_label: str) -> str:
    """Add a native fourth stat and leave the isometric calendar intact."""
    root = minidom.parseString(source).documentElement
    for group in elements(root):
        cards = elements(group)
        labels = [next((text.firstChild.data for text in elements(card)
                        if text.getAttribute('class') == 't-label'), '') for card in cards]
        if (labels[:2] == ['Current streak', 'Longest streak']
                and len(labels) in (3, 4)
                and labels[2] in ('Contributions (past 12 months)', 'Contributions')
                and labels[3:] in ([], ['Most commits in one day'])):
            break
    else:
        raise ValueError('The contribution calendar no longer contains the expected stat cards')

    if len(cards) == 3:
        peak = cards[-1].cloneNode(deep=True)
        group.appendChild(peak)
        cards.append(peak)
    else:
        peak = cards[-1]
    _, _, contribution_label, _, *contribution_captions = elements(cards[2])
    set_text(contribution_label, 'Contributions')
    for caption in contribution_captions:
        text = text_content(caption)
        if ' / DAY · PEAK ' in text:
            set_text(caption, text.replace(' / DAY · PEAK ', '/D · PEAK ') + ' TOTAL')
    for node in elements(root):
        if node.tagName == 'text' and text_content(node).startswith('REFRESHED '):
            root.removeChild(node)
    _, icon, label, value, *captions = elements(peak)
    set_text(label, 'Most commits in one day')
    set_text(value, str(count))
    if not captions:
        caption = root.ownerDocument.createElementNS(root.namespaceURI, 'text')
        caption.setAttribute('class', 't-mono')
        caption.setAttribute('x', value.getAttribute('x'))
        caption.setAttribute('y', '140')
        peak.appendChild(caption)
    else:
        caption = captions[0]
    set_text(caption, date_label)
    for child in elements(icon):
        icon.removeChild(child)
    commit_icon = root.ownerDocument.createElementNS(root.namespaceURI, 'path')
    commit_icon.setAttribute('d', 'M2 12h5m10 0h5m-5 0a5 5 0 1 1-10 0 5 5 0 0 1 10 0z')
    icon.appendChild(commit_icon)

    for index, card in enumerate(cards):
        rect, *content = elements(card)
        old_x = float(rect.getAttribute('x'))
        x = 24.5 + index * 202
        rect.setAttribute('x', f'{x:g}')
        rect.setAttribute('width', '191')
        for child in content:
            child.setAttribute('x', f'{float(child.getAttribute("x")) + x - old_x:g}')

    summary = root.getAttribute('aria-label').split(' Most commits in one day:')[0]
    summary += f' Most commits in one day: {count} ({date_label}).'
    root.setAttribute('aria-label', summary)
    set_text(root.getElementsByTagName('desc')[0], summary)
    return root.toxml() + '\n'


def main():
    if not TOKEN:
        raise SystemExit('Set GITHUB_TOKEN in the workflow environment')
    data = gather()
    target = Path('assets')
    languages = data['languages']
    total = languages.total()
    rows = [
        (name, count / total, (f'{count:,} lines', f'{count / total:.2%}'), LANG_COLORS.get(name, '#8b949e'))
        for name, count in languages.most_common(8)
    ]
    subtitle = (f"Last 90 days · {data['commits_with_code']} public code commits · "
                f"{data['files_with_code']} files changed · share of changed lines")
    if not rows:
        subtitle = 'No public code changes on default branches in the last 90 days'
    elif data['limited']:
        subtitle += f' · capped at {MAX_DETAIL_COMMITS} commits'
    if len(languages) > len(rows):
        subtitle += ' · top 8 shown'
    most_used = most_used_data((target / 'metrics-languages.svg').read_text(encoding='utf-8'))
    recently_used = (f'{len(languages)} Languages', subtitle, rows)

    dates = data['commit_days']
    peak_count = max(dates.values(), default=0)
    peak_dates = sorted(date for date, count in dates.items() if count == peak_count)
    date_label = 'NO PUBLIC COMMITS'
    if peak_dates:
        date_label = datetime.fromisoformat(peak_dates[0]).strftime('%b %d, %Y').upper()
        if len(peak_dates) > 1:
            date_label += f' +{len(peak_dates) - 1} MORE'
    outputs = {}
    for theme in ('dark', 'light'):
        path = target / f'contributions.{theme}.svg'
        outputs[path] = with_peak_commits(path.read_text(encoding='utf-8'), peak_count, date_label)
        outputs[target / f'languages.{theme}.svg'] = languages_svg(most_used, recently_used, theme == 'dark')
    for path, svg in outputs.items():
        path.write_text(svg, encoding='utf-8')
    print('Generated recent languages from', data['commits_with_code'], 'public code commits')
    print('Recent language change lines:', data['languages'].most_common())
    print('Peak authored public commits/day:', peak_count)
    print('Repos checked:', data['repos_examined'])


if __name__ == '__main__':
    main()
