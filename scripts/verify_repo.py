#!/usr/bin/env python3
"""Verify published bytes against the exact deployment artifact (no credentials)."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import time
from urllib.request import Request, urlopen


def verify(base, manifest):
    def fetch(path):
        req = Request(base.rstrip('/') + '/' + path, headers={'Cache-Control': 'no-cache', 'User-Agent': 'personal-apt-check'})
        with urlopen(req, timeout=30) as response:
            return response.read()
    deployed = json.loads(fetch('manifest.json'))
    if deployed != manifest:
        raise ValueError('Published manifest does not match this build yet')
    for name, expected in manifest.items():
        p = PurePosixPath(name)
        if p.is_absolute() or '..' in p.parts or any(c in name for c in '?#\\'):
            raise ValueError('Unsafe manifest path')
        data = fetch(name)
        if len(data) != expected['size'] or hashlib.sha256(data).hexdigest() != expected['sha256']:
            raise ValueError('Size/hash mismatch: ' + name)
    print('Verified', len(manifest), 'published files including every DEB:', base)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', required=True)
    parser.add_argument('--site', default='_site')
    parser.add_argument('--attempts', type=int, default=1)
    args = parser.parse_args()
    manifest = json.loads((Path(args.site) / 'manifest.json').read_text(encoding='utf-8'))
    for attempt in range(args.attempts):
        try:
            verify(args.base_url, manifest)
            break
        except Exception as exc:
            if attempt + 1 == args.attempts:
                raise
            print('Waiting for Pages:', str(exc))
            time.sleep(5)
