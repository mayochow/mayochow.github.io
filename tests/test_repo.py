import bz2
import gzip
import hashlib
import io
import json
import lzma
from pathlib import Path
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from build_repo import build, parse_control, read_deb
from verify_repo import verify


def deb(arch='iphoneos-arm64', payload='var/jb/Library/MobileSubstrate/DynamicLibraries/test.dylib', package='local.test', version='1.0', compression='xz', control_override=None):
    def archive(name, data):
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode='w:' + compression) as tf:
            if name is not None:
                entry = tarfile.TarInfo('./' + name)
                entry.size = len(data)
                tf.addfile(entry, io.BytesIO(data))
        return stream.getvalue()
    control = f'Package: {package}\nVersion: {version}\nArchitecture: {arch}\nMaintainer: Test\nDescription: first line\n second line\nDepends: firmware (>= 14.0)\nSection: Tweaks\n'
    if control_override is not None:
        control = control_override
    members = [('debian-binary', b'2.0\n'), ('control.tar.' + compression, archive('control', control.encode())),
               ('data.tar.' + compression, archive(payload, b'test bytes'))]
    result = b'!<arch>\n'
    for name, data in members:
        header = f'{name + "/":<16}{0:<12}{0:<6}{0:<6}{"100644":<8}{len(data):<10}`\n'
        result += header.encode() + data + (b'\n' if len(data) % 2 else b'')
    return result


class RepoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'debs').mkdir()
        self.config({})

    def config(self, assignments):
        (self.root / 'repo-config.json').write_text(json.dumps({'all_assignments': assignments}))

    def run_build(self):
        build(self.root, self.root / '_site', 'https://mayochow.github.io/zyy.github.io/')
        return self.root / '_site'

    def put(self, name, data):
        (self.root / 'debs' / name).write_bytes(data)

    def test_realistic_three_schemes_and_all_hashes(self):
        for arch, name, payload in [('iphoneos-arm', 'rootful', 'Library/MobileSubstrate/test'),
                                   ('iphoneos-arm64', 'rootless', 'var/jb/usr/lib/test'),
                                   ('iphoneos-arm64e', 'roothide', 'usr/lib/test')]:
            self.put(name + '.deb', deb(arch, payload))
        site = self.run_build()
        for folder in [site, site / 'rootful', site / 'rootless', site / 'roothide']:
            # Sileo RepoManager requires the components key even for flat repos.
            release_fields = dict(line.split(':', 1) for line in (folder / 'Release').read_text().splitlines()
                                  if line and not line.startswith((' ', '\t')))
            for required in ('Architectures', 'Components'):
                self.assertTrue(release_fields[required].strip())
            raw = (folder / 'Packages').read_bytes()
            self.assertEqual(gzip.decompress((folder / 'Packages.gz').read_bytes()), raw)
            self.assertEqual(bz2.decompress((folder / 'Packages.bz2').read_bytes()), raw)
            self.assertEqual(lzma.decompress((folder / 'Packages.xz').read_bytes()), raw)
            for block in raw.decode().strip().split('\n\n'):
                f = parse_control(block)
                data = (folder / f['filename']).read_bytes()
                self.assertEqual(f['sha256'], hashlib.sha256(data).hexdigest())
                self.assertIn('\n second line', f['description'])
                self.assertEqual(f['depends'], 'firmware (>= 14.0)')
            for line in (folder / 'Release').read_text().split('SHA256:\n')[1].split('SHA512:')[0].splitlines():
                digest, size, filename = line.split()
                data = (folder / filename).read_bytes()
                self.assertEqual(digest, hashlib.sha256(data).hexdigest())
                self.assertEqual(int(size), len(data))
        manifest = json.loads((site / 'manifest.json').read_text())
        for name, f in manifest.items():
            self.assertEqual(f['sha256'], hashlib.sha256((site / name).read_bytes()).hexdigest())

    def test_all_requires_review(self):
        self.put('all.deb', deb('all', 'usr/share/test'))
        with self.assertRaisesRegex(ValueError, 'needs reviewed'):
            self.run_build()
        self.assertFalse((self.root / '_site').exists())

    def test_reviewed_all(self):
        data = deb('all', 'usr/share/test')
        self.put('all.deb', data)
        self.config({hashlib.sha256(data).hexdigest(): ['rootful', 'roothide']})
        site = self.run_build()
        self.assertEqual((site / 'rootless' / 'Packages').read_bytes(), b'')
        self.assertEqual((site / 'Packages').read_text().count('Package:'), 1)

    def test_unknown_arch_not_guessed_from_filename(self):
        self.put('rootless_roothide.deb', deb('arm64', 'usr/share/test'))
        with self.assertRaisesRegex(ValueError, 'unsupported Architecture'):
            self.run_build()

    def test_conflicting_payload(self):
        self.put('wrong.deb', deb('iphoneos-arm', 'var/jb/usr/lib/test'))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.run_build()

    def test_rootless_wrong_tweak_location(self):
        self.put('wrong.deb', deb('iphoneos-arm64', 'Library/MobileSubstrate/test'))
        with self.assertRaisesRegex(ValueError, 'conflicts'):
            self.run_build()

    def test_duplicate_identity_rejected(self):
        self.put('one.deb', deb())
        self.put('two.deb', deb(payload='var/jb/other'))
        with self.assertRaisesRegex(ValueError, 'duplicate Package'):
            self.run_build()

    def test_different_versions_preserved(self):
        self.put('one.deb', deb(version='1.0'))
        self.put('two.deb', deb(version='1.1~beta1'))
        self.assertEqual((self.run_build() / 'Packages').read_text().count('Package:'), 2)

    def test_broken_deb_fails(self):
        self.put('broken.deb', b'not a deb')
        with self.assertRaisesRegex(ValueError, 'not a DEB'):
            self.run_build()

    def test_control_duplicate_field_rejected(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            parse_control('Package: local.test\nPackage: local.other')

    def test_gzip_and_bzip2_input(self):
        for compression in ('gz', 'bz2'):
            fields, paths = read_deb(deb(compression=compression))
            self.assertEqual(fields['architecture'], 'iphoneos-arm64')
            self.assertTrue(paths)

    def test_empty_repo(self):
        site = self.run_build()
        self.assertEqual((site / 'Packages').read_bytes(), b'')

    def test_existing_output_never_deleted(self):
        (self.root / '_site').mkdir()
        (self.root / '_site' / 'keep').touch()
        with self.assertRaisesRegex(ValueError, 'already exists'):
            self.run_build()
        self.assertTrue((self.root / '_site' / 'keep').exists())

    def test_http_verifier_fetches_all_artifacts(self):
        self.put('one.deb', deb())
        site = self.run_build()
        manifest = json.loads((site / 'manifest.json').read_text())
        def fetch(req, **kwargs):
            return io.BytesIO((site / req.full_url.removeprefix('https://example.test/')).read_bytes())
        with patch('verify_repo.urlopen', side_effect=fetch) as request:
            verify('https://example.test/', manifest)
            self.assertEqual(request.call_count, len(manifest) + 1)

    def test_http_verifier_rejects_corrupt_deb(self):
        self.put('one.deb', deb())
        site = self.run_build()
        manifest = json.loads((site / 'manifest.json').read_text())
        def fetch(req, **kwargs):
            path = req.full_url.removeprefix('https://example.test/')
            return io.BytesIO(b'corrupted' if path.endswith('.deb') else (site / path).read_bytes())
        with patch('verify_repo.urlopen', side_effect=fetch):
            with self.assertRaisesRegex(ValueError, 'hash mismatch'):
                verify('https://example.test/', manifest)

    def test_included_real_deb(self):
        included = Path(__file__).resolve().parents[1] / 'debs'
        for path in included.rglob('*.deb'):
            fields, paths = read_deb(path.read_bytes())
            self.assertIn(fields['architecture'], ('iphoneos-arm', 'iphoneos-arm64', 'iphoneos-arm64e', 'all'))

    def test_metadata_only_package_preserves_identity_and_archive(self):
        # Empty data archives are valid for meta/script-only packages. Reading
        # both tar archives still detects corrupt or missing DEB members.
        original = deb(payload=None)
        self.put('meta.deb', original)
        site = self.run_build()
        fields = parse_control((site / 'Packages').read_text().strip())
        self.assertEqual(fields['package'], 'local.test')
        self.assertEqual(fields['depends'], 'firmware (>= 14.0)')
        self.assertEqual((site / fields['filename']).read_bytes(), original)

    def test_display_override_preserves_deb_identity_and_bytes(self):
        original = deb()
        self.put('one.deb', original)
        (self.root / 'display-overrides.json').write_text(json.dumps({'local.test': {
            'name': '我的插件', 'description': '简单说明\n第二行\n\n更多说明'}}), encoding='utf-8')
        site = self.run_build()
        for folder in (site, site / 'rootless'):
            fields = parse_control((folder / 'Packages').read_text(encoding='utf-8').strip())
            self.assertEqual(fields['name'], '我的插件')
            self.assertEqual(fields['description'], '简单说明\n 第二行\n .\n 更多说明')
            self.assertEqual(fields['package'], 'local.test')
            self.assertEqual(fields['version'], '1.0')
            self.assertEqual(fields['architecture'], 'iphoneos-arm64')
            self.assertEqual(fields['depends'], 'firmware (>= 14.0)')
            self.assertEqual((folder / fields['filename']).read_bytes(), original)

    def test_display_override_rejects_identity_changes(self):
        (self.root / 'display-overrides.json').write_text(json.dumps({'local.test': {'architecture': 'all'}}))
        with self.assertRaisesRegex(ValueError, 'only name and description'):
            self.run_build()

    def test_display_override_rejects_name_field_injection(self):
        (self.root / 'display-overrides.json').write_text(json.dumps({'local.test': {'name': 'Test\nArchitecture: all'}}))
        with self.assertRaisesRegex(ValueError, 'single line'):
            self.run_build()

    def test_blank_descriptive_fields_get_index_defaults_only(self):
        original = deb(control_override='Package: local.test\nName: My Test\nVersion: 1.0\nArchitecture: iphoneos-arm64\nDescription: \nMaintainer: \n')
        self.put('blank.deb', original)
        site = self.run_build()
        fields = parse_control((site / 'Packages').read_text().strip())
        self.assertEqual(fields['description'], 'My Test')
        self.assertEqual(fields['maintainer'], 'Unknown')
        self.assertEqual((site / fields['filename']).read_bytes(), original)

    def test_missing_identity_still_rejected_with_metadata_defaults(self):
        with self.assertRaisesRegex(ValueError, 'missing architecture'):
            read_deb(deb(control_override='Package: local.test\nVersion: 1.0\n'))


if __name__ == '__main__':
    unittest.main()
