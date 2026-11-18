#!/usr/bin/env python3
"""Render themed language panels and contribution statistics.

This script intentionally uses GitHub's **repository commit API**, not the
limited account events stream. Data scope: public, non-fork repositories,
default-branch history. Language shares are from changed lines (additions plus
deletions), not claimed proficiency or the full repository's code composition.

No third-party Python dependencies are required.
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
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


def contribution_collection():
    """Fetch the same trailing calendar used by the contribution card action."""
    query = """query($user: String!) {
      user(login: $user) {
        contributionsCollection {
          contributionCalendar {
            totalContributions
            weeks { contributionDays { date contributionCount } }
          }
          commitContributionsByRepository(maxRepositories: 100) {
            repository { nameWithOwner isPrivate isArchived isFork pushedAt }
          }
        }
      }
    }"""
    request = Request(
        API + '/graphql',
        data=json.dumps({'query': query, 'variables': {'user': USERNAME}}).encode(),
        headers={
            'Accept': 'application/vnd.github+json',
            'Authorization': f'Bearer {TOKEN}',
            'Content-Type': 'application/json',
            'User-Agent': 'cristinadianas-profile-statistics',
        },
        method='POST',
    )
    with urlopen(request, timeout=35) as response:
        result = json.load(response)
    user = (result.get('data') or {}).get('user')
    if result.get('errors') or not user:
        raise RuntimeError(f'GitHub could not fetch the contribution calendar for {USERNAME}')
    return user['contributionsCollection']


def discover_repositories(contributed):
    """Include public repositories where the user contributed, not only owned repos."""
    repos = {}
    for repo in pages(f'/users/{quote(USERNAME)}/repos',
                      {'type': 'owner', 'sort': 'pushed', 'direction': 'desc'}):
        if not (repo.get('fork') or repo.get('archived') or repo.get('private')):
            repos[repo['full_name']] = repo
    for entry in contributed:
        repo = entry['repository']
        if not (repo['isPrivate'] or repo['isArchived'] or repo['isFork']):
            name = repo['nameWithOwner']
            repos[name] = {'full_name': name, 'pushed_at': repo['pushedAt']}
    return list(repos.values())


def gather(contributed_repositories):
    changes_by_language = Counter()
    commits_with_code = 0
    files_with_code = 0
    recent_commits_examined = 0
    limited = False
    repos_examined = 0
    for repo in discover_repositories(contributed_repositories):
        pushed = repo.get('pushed_at')
        if pushed and date_time(pushed) < CUTOFF:
            continue
        repo_full = repo['full_name']
        repos_examined += 1
        path = f'/repos/{quote(repo_full, safe="/")}/commits'
        for commit in pages(path, {
            'author': USERNAME,
            'since': CUTOFF.strftime('%Y-%m-%dT%H:%M:%SZ'),
        }):
            # GitHub's commit API returns author dates for each authored commit.
            raw_date = (commit.get('commit', {}).get('author') or {}).get('date')
            if not raw_date:
                raw_date = (commit.get('commit', {}).get('committer') or {}).get('date')
            if not raw_date:
                continue
            day = date_time(raw_date)
            if day < CUTOFF or day > NOW:
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


# Segments and dots use the same swatches, with separate contrast for light and dark.
LANGUAGE_COLORS = {
    'light': ('#08705c', '#2853a6', '#6b3ca7', '#38a98e', '#568bd8', '#a374d0', '#719ab1', '#8981c5'),
    'dark': ('#45ba9f', '#6b9df7', '#b084ed', '#1a856f', '#3c65be', '#854cc4', '#85b8cc', '#8682d4'),
}
CALENDAR_RAMPS = {
    'light': (
        ('#9be9a8', '#40c463', '#30a14e', '#216e39'),
        ('#a8ddcd', '#4da893', '#5283cc', '#7544ac'),
    ),
    'dark': (
        ('#0e4429', '#006d32', '#26a641', '#39d353'),
        ('#124b43', '#258571', '#5588dc', '#a679e3'),
    ),
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
        rows.append((language, share, (lines, size, percentage)))
    return text_content(headers[0]), subtitle, sorted(rows, key=lambda row: row[1], reverse=True)[:8]


def language_section(heading, count, subtitle, rows, colors, top, bar_id):
    column_size = (len(rows) + 1) // 2
    bar_y = top + 98
    width = 798
    section = [
        f'<path d="M25 {top+13}h17v12H31l-5 4v-4h-1z m6 4-2 2 2 2 m4-4 2 2-2 2" class="icon"/>',
        f'<text x="51" y="{top+26}" class="count">{escape(count)}</text>',
        f'<text x="423" y="{top+60}" text-anchor="middle" class="section-title">{heading}</text>',
        f'<text x="423" y="{top+82}" text-anchor="middle" class="detail">{escape(subtitle)}</text>',
        f'<clipPath id="{bar_id}"><rect x="24" y="{bar_y}" width="{width}" height="9" rx="4.5"/></clipPath>',
        f'<g clip-path="url(#{bar_id})"><rect x="24" y="{bar_y}" width="{width}" height="9" class="track"/>',
    ]
    x = 24
    for language, share, _ in rows:
        color = colors[language]
        segment = width * share
        section.append(f'<rect x="{x:.3f}" y="{bar_y}" width="{segment:.3f}" height="9" fill="{escape(color)}"/>')
        x += segment
    section.append('</g>')
    for index, (language, _, details) in enumerate(rows):
        column, row = divmod(index, column_size)
        color = colors[language]
        x = 30 + column * 405
        y = top + 132 + row * 24
        section.append(f'<circle cx="{x}" cy="{y-5}" r="5.5" fill="{escape(color)}"/>')
        section.append(f'<text x="{x+17}" y="{y}" class="language">{escape(language)}</text>')
        for offset, detail in zip((377, 306, 224), reversed(details)):
            section.append(f'<text x="{x+offset}" y="{y}" class="detail" text-anchor="end">{escape(detail)}</text>')
    return section, top + 149 + max(0, column_size - 1) * 24


def languages_svg(most_used, recently_used, dark: bool) -> str:
    palette = LANGUAGE_COLORS['dark' if dark else 'light']
    most_colors = dict(zip((row[0] for row in most_used[2]), palette))
    recent_colors = {row[0]: most_colors[row[0]] for row in recently_used[2] if row[0] in most_colors}
    available = iter(color for color in palette if color not in recent_colors.values())
    for language, _, _ in recently_used[2]:
        if language not in recent_colors:
            recent_colors[language] = next(available)

    most, end = language_section('Most used languages', *most_used, most_colors, 15, 'most-used-bar')
    recent, height = language_section('Recently used languages', *recently_used, recent_colors, end + 9, 'recent-bar')
    if dark:
        bg, border, fg, secondary, track = '#0d1117', '#3d444d', '#f0f6fc', '#a1a9b4', '#30363d'
        green, blue, purple = '#60cfb2', '#92b9ff', '#c7a4ff'
    else:
        bg, border, fg, secondary, track = '#ffffff', '#d1d9e0', '#1f2328', '#59636e', '#e1e6ed'
        green, blue, purple = '#08705c', '#2853a6', '#6b3ca7'
    return '\n'.join([
        f'<svg xmlns="http://www.w3.org/2000/svg" width="846" height="{height}" viewBox="0 0 846 {height}" role="img">',
        '<title>Most used and recently used languages</title>',
        '<style>',
        'text{font-family:-apple-system,BlinkMacSystemFont,Segoe UI,Arial,sans-serif}',
        f'.section-title{{fill:{green};font-size:17px;font-weight:600}}',
        f'.recent .section-title{{fill:{purple}}}.count{{fill:{blue};font-size:18px;font-weight:600}}',
        f'.detail{{fill:{secondary};font-size:13px}}.language{{fill:{fg};font-size:16px}}',
        f'.icon{{fill:none;stroke:{secondary};stroke-width:1.4;stroke-linejoin:round}}.track{{fill:{track}}}',
        '</style>',
        f'<rect x="0.5" y="0.5" width="845" height="{height-1}" rx="12" fill="{bg}" stroke="{border}"/>',
        *most,
        f'<path d="M24 {end}h798" stroke="{border}"/>',
        '<g class="recent">',
        *recent,
        '</g>',
        '</svg>',
        '',
    ])


def contribution_stats(calendar):
    days = sorted(
        (date.fromisoformat(day['date']), day['contributionCount'])
        for week in calendar['weeks']
        for day in week['contributionDays']
    )
    runs = []
    for day, count in days:
        if not count:
            continue
        if runs and day - runs[-1][1] == timedelta(days=1):
            runs[-1] = (runs[-1][0], day)
        else:
            runs.append((day, day))

    peak_day, peak_count = max(days, key=lambda day: day[1], default=(None, 0))
    longest = max(runs, key=lambda run: run[1] - run[0], default=None)
    current = None
    if runs and days:
        last_day, count = days[-1]
        anchor = last_day if count else last_day - timedelta(days=1)
        if runs[-1][1] == anchor:
            current = runs[-1]
    total = calendar['totalContributions']
    average = total / len(days) if days else 0
    return current, longest, peak_day, peak_count, total, average


def streak_display(run, empty_caption):
    if run is None:
        return 0, 'days', empty_caption
    start, end = run
    length = (end - start).days + 1
    if start == end:
        caption = end.strftime('%b %d, %Y')
    elif start.year == end.year:
        caption = f"{start:%b %d} – {end:%b %d, %Y}"
    else:
        caption = f"{start:%b %d ’%y} – {end:%b %d ’%y}"
    return length, 'day' if length == 1 else 'days', caption.upper()


def shade(color, factor):
    channels = (int(color[i:i+2], 16) for i in (1, 3, 5))
    return '#' + ''.join(f'{round(channel * factor):02x}' for channel in channels)


def recolor_calendar(root, theme):
    original, themed = CALENDAR_RAMPS[theme]
    colors = dict(zip(original, themed, strict=True))
    # The stock renderer emits each active day as top, left, and right polygons.
    for group in elements(root):
        if group.getAttribute('class') != 'iso':
            continue
        polygons = elements(group)
        index = 0
        while index < len(polygons):
            fill = polygons[index].getAttribute('fill')
            if fill in colors:
                color = colors[fill]
                polygons[index].setAttribute('fill', color)
                polygons[index + 1].setAttribute('fill', shade(color, 0.84))
                polygons[index + 2].setAttribute('fill', shade(color, 0.69))
                index += 3
            else:
                index += 1
    # Recolor legend swatches and the shared header, icon, and glow accents.
    accent = ('#3fb950', '#92b9ff') if theme == 'dark' else ('#1a7f37', '#2853a6')
    colors[accent[0]] = accent[1]
    svg = root.toxml()
    for old, new in colors.items():
        svg = svg.replace(old, new)
    return svg + '\n'


def contribution_svg(source: str, stats, theme: str) -> str:
    """Restyle the upstream 3D calendar and use its trailing window for all four tiles."""
    root = minidom.parseString(source).documentElement
    for group in elements(root):
        cards = {}
        for tile in elements(group):
            label = next((child for child in elements(tile) if child.getAttribute('class') == 't-label'), None)
            if label is not None:
                cards[text_content(label)] = tile
        if 'Current streak' in cards and 'Longest streak' in cards:
            break
    else:
        raise ValueError('The contribution calendar is missing its streak tiles')

    total_card = next((cards[name] for name in ('Total contributions', 'Contributions', 'Contributions (past 12 months)')
                       if name in cards), None)
    if total_card is None:
        raise ValueError('The contribution calendar is missing its total tile')
    peak_card = cards.get('Most contributions in a day') or cards.get('Most commits in one day')
    if peak_card is None:
        peak_card = total_card.cloneNode(deep=True)

    current, longest, peak_day, peak_count, total, average = stats
    current_display = streak_display(current, 'NO ACTIVE STREAK')
    longest_display = streak_display(longest, 'NO CONTRIBUTIONS YET')
    peak_caption = peak_day.strftime('%b %d, %Y').upper() if peak_count else 'NO CONTRIBUTIONS YET'
    tiles = [
        (cards['Current streak'], 'Current streak', *current_display),
        (cards['Longest streak'], 'Longest streak', *longest_display),
        (peak_card, 'Most contributions in a day', peak_count, '', peak_caption),
        (total_card, 'Total contributions', total, '', f'AVG {average:.1f} / DAY'),
    ]
    doc = root.ownerDocument
    for index, (tile, name, count, unit, detail) in enumerate(tiles):
        rect, icon, label, value, *captions = elements(tile)
        set_text(label, name)
        set_text(value, f'{count:,}')
        if unit:
            suffix = doc.createElementNS(root.namespaceURI, 'tspan')
            suffix.setAttribute('class', 't-unit')
            suffix.setAttribute('dx', '6')
            set_text(suffix, unit)
            value.appendChild(suffix)
        if captions:
            caption = captions[0]
        else:
            caption = doc.createElementNS(root.namespaceURI, 'text')
            caption.setAttribute('class', 't-mono')
            caption.setAttribute('x', value.getAttribute('x'))
            caption.setAttribute('y', '140')
            tile.appendChild(caption)
        set_text(caption, detail)
        if tile is peak_card:
            label.setAttribute('style', 'font-size: 11px')
            for child in elements(icon):
                icon.removeChild(child)
            peak_icon = doc.createElementNS(root.namespaceURI, 'path')
            peak_icon.setAttribute('d', 'M4 19h16M7 15v-3m5 3V8m5 7V4')
            icon.appendChild(peak_icon)

        old_x = float(rect.getAttribute('x'))
        x = 24.5 + index * 202
        rect.setAttribute('x', f'{x:g}')
        rect.setAttribute('width', '191')
        for child in (icon, label, value, caption):
            child.setAttribute('x', f'{float(child.getAttribute("x")) + x - old_x:g}')
        group.appendChild(tile)

    for node in elements(root):
        if node.tagName != 'text':
            continue
        text = text_content(node)
        if text.startswith('REFRESHED '):
            root.removeChild(node)
        elif 'STREAKS OVER ALL YEARS' in text:
            set_text(node, text.replace(' · STREAKS OVER ALL YEARS', ''))

    summary = (f'Contribution activity for {USERNAME} over the past 12 months. '
               f'Current streak: {current_display[0]} {current_display[1]}. '
               f'Longest streak: {longest_display[0]} {longest_display[1]}. '
               f'Most contributions in a day: {peak_count}. Total contributions: {total}.')
    root.setAttribute('aria-label', summary)
    set_text(root.getElementsByTagName('desc')[0], summary)
    return recolor_calendar(root, theme)


def main():
    if not TOKEN:
        raise SystemExit('Set GITHUB_TOKEN in the workflow environment')
    collection = contribution_collection()
    stats = contribution_stats(collection['contributionCalendar'])
    data = gather(collection['commitContributionsByRepository'])
    target = Path('assets')
    languages = data['languages']
    total = languages.total()
    rows = [
        (name, count / total, (f'{count:,} lines', f'{count / total:.2%}'))
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

    outputs = {}
    for theme in ('dark', 'light'):
        path = target / f'contributions.{theme}.svg'
        outputs[path] = contribution_svg(path.read_text(encoding='utf-8'), stats, theme)
        outputs[target / f'languages.{theme}.svg'] = languages_svg(most_used, recently_used, theme == 'dark')
    for path, svg in outputs.items():
        path.write_text(svg, encoding='utf-8')
    print('Generated recent languages from', data['commits_with_code'], 'public code commits')
    print('Recent language change lines:', data['languages'].most_common())
    print('Repos checked:', data['repos_examined'])


if __name__ == '__main__':
    main()
