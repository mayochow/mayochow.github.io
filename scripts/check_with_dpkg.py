#!/usr/bin/env python3
"""Independent validation with Debian's readers in Ubuntu CI."""
import hashlib
from pathlib import Path
import subprocess
import tempfile
from build_repo import parse_control

# Some third-party DEBs store control with mode 000. dpkg-deb --info/-f
# unpack control into a temporary directory before reading it. Use root only
# for those read-only commands on the disposable Ubuntu runner, as on-device
# dpkg normally does. Never install packages or execute maintainer scripts.
CONTROL_READER = ['sudo', '-n', 'dpkg-deb']

for source in Path('debs').rglob('*'):
    if source.is_file() and source.suffix.lower() == '.deb':
        subprocess.run(CONTROL_READER + ['--info', str(source)], check=True, stdout=subprocess.DEVNULL)
        # Read payload archive too; never execute maintainer scripts or install.
        subprocess.run(['dpkg-deb', '--contents', str(source)], check=True, stdout=subprocess.DEVNULL)

for index in Path('_site').rglob('Packages'):
    for block in index.read_text(encoding='utf-8').strip().split('\n\n'):
        if not block:
            continue
        fields = parse_control(block)
        package = index.parent / fields['filename']
        data = package.read_bytes()
        assert hashlib.sha256(data).hexdigest() == fields['sha256']
        assert len(data) == int(fields['size'])
        for field in ('Package', 'Version', 'Architecture'):
            actual = subprocess.check_output(CONTROL_READER + ['-f', str(package), field], text=True).strip()
            assert actual == fields[field.lower()], (package, field, actual)
        subprocess.run(['dpkg', '--validate-version', fields['version']], check=True)
print('All DEB identity fields, paths, sizes and SHA256 hashes match Debian tools.')

# Let APT itself consume the Release + flat Packages, with fully isolated state.
# trusted=yes is confined to these temporary local test source files.
for scheme, arch in [('rootful', 'iphoneos-arm'), ('rootless', 'iphoneos-arm64'), ('roothide', 'iphoneos-arm64e')]:
    with tempfile.TemporaryDirectory() as temp:
        state = Path(temp)
        (state / 'lists' / 'partial').mkdir(parents=True)
        (state / 'cache' / 'archives' / 'partial').mkdir(parents=True)
        (state / 'empty').mkdir()
        (state / 'status').touch()
        sources = state / 'sources.list'
        sources.write_text('deb [trusted=yes arch=' + arch + '] ' +
                           (Path('_site') / scheme).resolve().as_uri() + '/ ./\n')
        subprocess.run(['apt-get', 'update',
                        '-o', 'Dir::Etc::sourcelist=' + str(sources),
                        '-o', 'Dir::Etc::sourceparts=' + str(state / 'empty'),
                        '-o', 'Dir::State=' + str(state),
                        '-o', 'Dir::State::status=' + str(state / 'status'),
                        '-o', 'Dir::Cache=' + str(state / 'cache'),
                        '-o', 'APT::Architecture=' + arch,
                        '-o', 'APT::Architectures::=' + arch,
                        '-o', 'APT::Sandbox::User=root',
                        '-o', 'APT::Get::List-Cleanup=0',
                        '-o', 'APT::Update::Error-Mode=any',
                        '-o', 'Acquire::Languages=none'], check=True)
print('APT successfully loaded all three flat repositories.')
