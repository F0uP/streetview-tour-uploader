"""Keep an apt repository with the newest packages, so `apt upgrade` brings new versions.

The release workflow keeps it in the gh-pages branch (folder apt/), which GitHub Pages
serves; see "Linux" in the README for how users add it. This script only lays out the
files; signing the Release file is left to gpg (InRelease, Release.gpg), in the workflow.

    python packaging/linux/apt_repo.py <repository folder> <package.deb>... [--keep 3]

  <folder>/pool/main/<package>_<version>_all.deb        the newest --keep versions
  <folder>/dists/stable/main/binary-<arch>/Packages(.gz)
  <folder>/dists/stable/Release

Pure Python (no dpkg tools needed), so it runs and is tested anywhere.
"""

import argparse
import gzip
import hashlib
import io
import lzma
import re
import shutil
import tarfile
import time
from pathlib import Path

ARCHITECTURES = ['amd64', 'arm64']  # the package is "all": listed for both
SUITE = 'stable'
COMPONENT = 'main'


def deb_control(path):
    """The control file of a .deb (an ar archive holding control.tar.*)."""
    data = Path(path).read_bytes()
    if not data.startswith(b'!<arch>\n'):
        raise ValueError(f'{path}: not a .deb file')
    pos = 8
    while pos + 60 <= len(data):
        header = data[pos:pos + 60]
        name = header[:16].decode().strip().rstrip('/')
        size = int(header[48:58].decode().strip())
        body = data[pos + 60:pos + 60 + size]
        pos += 60 + size + (size % 2)
        if not name.startswith('control.tar'):
            continue
        if name.endswith('.xz'):
            body = lzma.decompress(body)
        elif name.endswith('.gz'):
            body = gzip.decompress(body)
        elif name != 'control.tar':
            raise ValueError(f'{path}: {name} is not supported (build with xz or gzip)')
        with tarfile.open(fileobj=io.BytesIO(body)) as tar:
            member = next(m for m in tar.getmembers() if m.name.lstrip('./') == 'control')
            return tar.extractfile(member).read().decode('utf-8')
    raise ValueError(f'{path}: no control file')


def fields(control):
    return dict(re.findall(r'^([\w-]+):[ \t]*(.*)$', control, re.M))


def _order(c):
    if c == '~':
        return -1
    if c.isalpha():
        return ord(c)
    return ord(c) + 256


def _compare_part(a, b):
    """dpkg's comparison of an upstream version or revision."""
    while a or b:
        ia = len(re.match(r'\D*', a).group())
        ib = len(re.match(r'\D*', b).group())
        na, nb = a[:ia], b[:ib]
        for k in range(max(len(na), len(nb))):
            ca = _order(na[k]) if k < len(na) else 0
            cb = _order(nb[k]) if k < len(nb) else 0
            if ca != cb:
                return -1 if ca < cb else 1
        a, b = a[ia:], b[ib:]
        da, db = re.match(r'\d*', a).group(), re.match(r'\d*', b).group()
        if int(da or 0) != int(db or 0):
            return -1 if int(da or 0) < int(db or 0) else 1
        a, b = a[len(da):], b[len(db):]
    return 0


def compare_versions(v1, v2):
    """-1, 0 or 1, as dpkg --compare-versions orders them."""
    def split(v):
        epoch, _, rest = v.rpartition(':') if ':' in v else ('0', '', v)
        upstream, _, revision = rest.rpartition('-') if '-' in rest else (rest, '', '')
        return int(epoch or 0), upstream, revision
    e1, u1, r1 = split(v1)
    e2, u2, r2 = split(v2)
    if e1 != e2:
        return -1 if e1 < e2 else 1
    return _compare_part(u1, u2) or _compare_part(r1, r2)


def _hashes(data):
    return {'MD5Sum': hashlib.md5(data).hexdigest(), 'SHA1': hashlib.sha1(data).hexdigest(),
            'SHA256': hashlib.sha256(data).hexdigest()}


def update(repo, debs, keep=3):
    repo = Path(repo)
    pool = repo / 'pool' / COMPONENT
    pool.mkdir(parents=True, exist_ok=True)
    for deb in debs:
        shutil.copy2(deb, pool / Path(deb).name)

    # Per package, the newest `keep` versions stay.
    packages = {}
    for deb in pool.glob('*.deb'):
        info = fields(deb_control(deb))
        packages.setdefault(info['Package'], []).append((info['Version'], deb, info))
    stanzas = []
    for name in sorted(packages):
        versions = packages[name]
        for i in range(len(versions)):  # newest first (a few versions: a simple sort will do)
            for j in range(len(versions) - 1 - i):
                if compare_versions(versions[j][0], versions[j + 1][0]) < 0:
                    versions[j], versions[j + 1] = versions[j + 1], versions[j]
        for _, deb, _ in versions[keep:]:
            deb.unlink()
        for _, deb, _ in versions[:keep]:
            data = deb.read_bytes()
            control = deb_control(deb).strip('\n')
            h = _hashes(data)
            stanzas.append(f"{control}\nFilename: pool/{COMPONENT}/{deb.name}\nSize: {len(data)}\n"
                           f"MD5sum: {h['MD5Sum']}\nSHA1: {h['SHA1']}\nSHA256: {h['SHA256']}\n")
    index = '\n'.join(stanzas).encode('utf-8')

    dists = repo / 'dists' / SUITE
    if dists.exists():
        shutil.rmtree(dists)
    listed = []
    for arch in ARCHITECTURES:
        folder = dists / COMPONENT / f'binary-{arch}'
        folder.mkdir(parents=True)
        (folder / 'Packages').write_bytes(index)
        (folder / 'Packages.gz').write_bytes(gzip.compress(index, mtime=0))
        listed += [f'{COMPONENT}/binary-{arch}/Packages', f'{COMPONENT}/binary-{arch}/Packages.gz']

    release = [
        'Origin: Streetview Tour Editor',
        'Label: Streetview Tour Editor',
        f'Suite: {SUITE}',
        f'Codename: {SUITE}',
        f'Date: {time.strftime("%a, %d %b %Y %H:%M:%S UTC", time.gmtime())}',
        f'Architectures: {" ".join(ARCHITECTURES)}',
        f'Components: {COMPONENT}',
        'Description: Streetview Tour Editor - https://github.com/F0uP/streetview-tour-uploader',
    ]
    for algo in ('MD5Sum', 'SHA1', 'SHA256'):
        release.append(f'{algo}:')
        for rel in listed:
            data = (dists / rel).read_bytes()
            release.append(f' {_hashes(data)[algo]} {len(data):>16} {rel}')
    (dists / 'Release').write_text('\n'.join(release) + '\n', encoding='utf-8')
    return [d.name for d in sorted(pool.glob('*.deb'))]


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('repo', help='the repository folder (created if missing)')
    parser.add_argument('debs', nargs='*', help='packages to add')
    parser.add_argument('--keep', type=int, default=3, help='versions kept per package (default 3)')
    args = parser.parse_args()
    for name in update(args.repo, args.debs, args.keep):
        print(f'  {name}')


if __name__ == '__main__':
    main()
