#!/usr/bin/env python3
"""Compare the storage a Compose deployment would mount, without starting it."""
import argparse
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys

from storage import InputError, LIMIT, compare, snapshot


def default_project(name):
    return re.sub('[^a-z0-9_-]', '', name.lower()).lstrip('_-')


def read_file(path):
    with path.open('rb') as stream:
        return stream.read(LIMIT + 1)


def git_blob(repo, revision, path):
    if not re.fullmatch(r'[a-fA-F0-9]{40}|[a-fA-F0-9]{64}', revision):
        raise InputError("Pass a full Git commit SHA.")
    def git(*args):
        p = subprocess.run(['git', '--no-replace-objects', '--literal-pathspecs', '-C', str(repo),
                            *args], capture_output=True)
        if p.returncode:
            raise InputError("Git object unavailable. Check the checkout and fetch depth.")
        return p.stdout
    if git('cat-file', '-t', revision).strip() != b'commit':
        raise InputError("Expected a Git commit.")
    tree = git('ls-tree', '-z', revision, '--', path)
    if not tree:
        return None
    mode, kind, oid = tree.split(b'\t', 1)[0].split()
    if kind != b'blob' or mode not in (b'100644', b'100755'):
        raise InputError("Compose path must point to a regular file.")
    if int(git('cat-file', '-s', oid)) > LIMIT:
        raise InputError("Compose input exceeds 1 MiB.")
    return git('cat-file', 'blob', oid)


def analyze_files(before, after, override=None):
    states = []
    for file in (before, after):
        path = Path(file).resolve()
        states.append(snapshot(read_file(path), path.parent.as_posix(),
                               default_project(path.parent.name), override))
    return compare(*states)


def analyze_git(repo, before, after, filename, override=None, repo_name=None):
    path = PurePosixPath(filename)
    if not filename or path.is_absolute() or '..' in path.parts or path.as_posix() != filename or filename == '.':
        raise InputError("Use a repository-relative Compose file path.")
    data = [git_blob(repo, rev, filename) for rev in (before, after)]
    if data == [None, None]:
        raise InputError("Compose file is absent in both revisions. Check compose-file.")
    fallback = default_project(path.parent.name or repo_name or Path(repo).resolve().name)
    return compare(*(snapshot(blob, str(path.parent), fallback, override) for blob in data))


def report(findings, fmt='text'):
    if fmt == 'json':
        return json.dumps(dict(status='review' if findings else 'clear', findings=findings),
                          indent=2, ensure_ascii=True)
    if not findings:
        return "VolumeGuard: no change to an existing declared storage source."
    lines = [f"VolumeGuard: {len(findings)} storage change(s) to review."]
    for f in findings:
        lines += ['', f"{f['service']} at {f['target']} — {f['reason']}",
                  f"  Before: {f['before']}", f"  After:  {f['after'] or '(no mount at this target)'}"]
    lines += ['', 'Verify the intended mount or migration before deploying.',
              'This is a configuration comparison. Existing data may still be on the previous volume.']
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('before', type=Path)
    parser.add_argument('after', type=Path)
    parser.add_argument('--project-name', help='Deployment project name, equivalent to Compose -p')
    parser.add_argument('--format', choices=['text', 'json'], default='text')
    args = parser.parse_args()
    try:
        findings = analyze_files(args.before, args.after, args.project_name)
    except (InputError, OSError, ValueError) as exc:
        if args.format == 'json':
            print(json.dumps(dict(status='error', error=str(exc))))
        else:
            print(f'VolumeGuard: analysis incomplete. {exc}', file=sys.stderr)
        return 2
    print(report(findings, args.format))
    return int(bool(findings))


if __name__ == '__main__':
    sys.exit(main())
