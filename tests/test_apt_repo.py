"""The apt repository the release workflow keeps on GitHub Pages (packaging/linux/apt_repo.py)."""
import gzip
import hashlib
import io
import lzma
import sys
import tarfile

from conftest import REPO

sys.path.insert(0, str(REPO / 'packaging' / 'linux'))
import apt_repo  # noqa: E402


def ar_member(name, data):
    header = f'{name:<16}{0:<12}{0:<6}{0:<6}{100644:<8}{len(data):<10}`\n'.encode()
    return header + data + (b'\n' if len(data) % 2 else b'')


def deb(path, version):
    control = f'Package: streetview-tour-editor\nVersion: {version}\nArchitecture: all\nDescription: test\n'.encode()
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode='w') as tar:
        info = tarfile.TarInfo('./control')
        info.size = len(control)
        tar.addfile(info, io.BytesIO(control))
    path.write_bytes(b'!<arch>\n' + ar_member('debian-binary', b'2.0\n')
                     + ar_member('control.tar.xz', lzma.compress(tar_buf.getvalue()))
                     + ar_member('data.tar.xz', lzma.compress(b'')))
    return path


def test_versions_sort_like_dpkg():
    assert apt_repo.compare_versions('1.10.0', '1.9.3') == 1
    assert apt_repo.compare_versions('2.3.0~rc1', '2.3.0') == -1
    assert apt_repo.compare_versions('2.3.0', '2.3.0') == 0
    assert apt_repo.compare_versions('1:0.1', '9.9') == 1


def test_repository_keeps_the_newest(tmp_path):
    debs = [deb(tmp_path / f'streetview-tour-editor_{v}_all.deb', v) for v in ('1.9.0', '1.10.0~rc1', '1.10.0', '1.10.1')]  # rc1 sorts before 1.10.0
    repo = tmp_path / 'apt'
    kept = apt_repo.update(repo, debs, keep=2)
    assert kept == ['streetview-tour-editor_1.10.0_all.deb', 'streetview-tour-editor_1.10.1_all.deb']

    packages = (repo / 'dists/stable/main/binary-amd64/Packages').read_text()
    assert packages.index('Version: 1.10.1') < packages.index('Version: 1.10.0')  # newest first
    assert 'Filename: pool/main/streetview-tour-editor_1.10.1_all.deb' in packages
    sha = hashlib.sha256((repo / 'pool/main/streetview-tour-editor_1.10.1_all.deb').read_bytes()).hexdigest()
    assert f'SHA256: {sha}' in packages
    assert gzip.decompress((repo / 'dists/stable/main/binary-arm64/Packages.gz').read_bytes()).decode() == packages

    release = (repo / 'dists/stable/Release').read_text()
    assert 'Suite: stable' in release and 'Architectures: amd64 arm64' in release
    digest = hashlib.sha256(packages.encode()).hexdigest()
    assert f' {digest} ' in release and release.count('main/binary-amd64/Packages') == 6  # 3 hashes x (Packages, .gz)

    # the next release adds to what is there
    apt_repo.update(repo, [deb(tmp_path / 'streetview-tour-editor_2.0.0_all.deb', '2.0.0')], keep=2)
    assert sorted(p.name for p in (repo / 'pool/main').iterdir()) == ['streetview-tour-editor_1.10.1_all.deb', 'streetview-tour-editor_2.0.0_all.deb']
