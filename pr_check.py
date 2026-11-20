"""GitHub Actions entry point. Analyze Git blobs without executing project code."""
import html
import json
import os
from pathlib import Path
import sys
from urllib import error, request

from storage import InputError
from volumeguard import analyze_git, report


def api(url, token, method='GET', body=None):
    headers = {'Authorization': 'Bearer ' + token, 'Accept': 'application/vnd.github+json',
               'User-Agent': 'VolumeGuard', 'Content-Type': 'application/json'}
    req = request.Request(url, method=method, headers=headers,
                          data=json.dumps(body).encode() if body is not None else None)
    with request.urlopen(req, timeout=15) as response:
        return json.load(response)


def upsert(repo_url, number, token, body, marker):
    page = 1
    url = f'{repo_url}/issues/{number}/comments'
    while True:
        items = api(f'{url}?per_page=100&page={page}', token)
        for item in items:
            if item['user']['login'] == 'github-actions[bot]' and item['body'].startswith(marker):
                api(f"{repo_url}/issues/comments/{item['id']}", token, 'PATCH', {'body': body})
                return
        if len(items) < 100:
            break
        page += 1
    api(url, token, 'POST', {'body': body})


def main():
    import hashlib
    env = os.environ
    path = env['VG_FILE']
    repo = env['GITHUB_REPOSITORY']
    event = json.loads(Path(env['GITHUB_EVENT_PATH']).read_text(encoding='utf-8'))
    pr = event.get('pull_request')
    if not pr:
        raise InputError("Use this action on a pull_request event.")
    try:
        findings = analyze_git(env['VG_REPO'], pr['base']['sha'], env['GITHUB_SHA'], path,
                               env.get('VG_PROJECT') or None, repo.split('/')[-1])
        msg = report(findings)
        code = int(bool(findings))
    except (ValueError, OSError) as exc:
        msg = f'Analysis incomplete: {exc}'
        code = 2
    title = ['Clear', 'Review storage changes', 'Analysis incomplete'][code]
    if len(msg) > 8000:
        msg = msg[:7900] + '\n[Report truncated. Run the CLI for all findings.]'
    # HTML encoding keeps PR-controlled mount paths inside the preformatted report.
    body = f'### VolumeGuard · {title}\n\n<pre>{html.escape(msg)}</pre>\n'
    with open(env['GITHUB_STEP_SUMMARY'], 'a', encoding='utf-8') as summary:
        summary.write(body)
    print(f'VolumeGuard: {title}. See the job summary.')
    token = env.get('VG_TOKEN')
    same_repo = (pr['head'].get('repo') or {}).get('full_name') == repo
    if token and same_repo:
        marker = '<!-- volumeguard-' + hashlib.sha256(path.encode()).hexdigest()[:16] + ' -->'
        try:
            upsert(f"{env['GITHUB_API_URL']}/repos/{repo}", pr['number'], token, marker + '\n' + body, marker)
        except (error.URLError, TimeoutError, ValueError):
            print('Could not update the PR comment. The report is in the job summary.', file=sys.stderr)
    if code == 1 and env.get('VG_BLOCK', 'true').lower() == 'false':
        return 0
    return code


if __name__ == '__main__':
    sys.exit(main())
