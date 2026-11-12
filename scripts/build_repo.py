#!/usr/bin/env python3
"""Build a small flat APT repository without installing/executing any DEB."""
import argparse
import bz2
import datetime
import gzip
import hashlib
import html
import io
import json
import lzma
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile

ARCHES = {"iphoneos-arm": "rootful", "iphoneos-arm64": "rootless",
          "iphoneos-arm64e": "roothide"}
GROUPS = tuple(ARCHES.values())
GENERATED = {"filename", "size", "md5sum", "sha1", "sha256", "sha512"}


def parse_control(text):
    fields, key = {}, None
    for line in text.replace("\r\n", "\n").splitlines():
        if not line:
            raise ValueError("unexpected blank line in control stanza")
        if line[0] in " \t":
            if key is None:
                raise ValueError("orphan continuation")
            fields[key] += "\n" + line
            continue
        key, sep, value = line.partition(":")
        key = key.lower()
        if not sep or not re.fullmatch(r"[a-z0-9][a-z0-9-]*", key) or key in fields:
            raise ValueError("invalid/duplicate control field")
        fields[key] = value.strip()
    for name in ("package", "version", "architecture", "description", "maintainer"):
        if not fields.get(name):
            raise ValueError("missing " + name)
    if not re.fullmatch(r"[a-z0-9][a-z0-9+.-]+", fields["package"]):
        raise ValueError("invalid Package")
    if not re.fullmatch(r"[0-9][A-Za-z0-9.+:~\-]*", fields["version"]):
        raise ValueError("invalid Version")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", fields["architecture"]):
        raise ValueError("invalid Architecture")
    return fields


def ar_members(data):
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("not a DEB ar archive")
    result, pos = {}, 8
    while pos < len(data):
        header = data[pos:pos + 60]
        if len(header) != 60 or header[58:] != b"`\n":
            raise ValueError("invalid ar header")
        name = header[:16].decode("ascii").strip().rstrip("/")
        size = int(header[48:58])
        pos += 60
        if size < 0 or pos + size > len(data) or name in result:
            raise ValueError("invalid ar member")
        result[name] = data[pos:pos + size]
        pos += size + size % 2
    if result.get("debian-binary") != b"2.0\n":
        raise ValueError("unsupported debian-binary version")
    return result


def open_tar(members, prefix):
    names = [n for n in members if n == prefix + ".tar" or n.startswith(prefix + ".tar.")]
    if len(names) != 1:
        raise ValueError("expected exactly one " + prefix + " archive")
    name = names[0]
    data = members[name]
    if name.endswith(".zst"):
        # Ubuntu Actions installs zstd. No archive paths are extracted.
        data = subprocess.run(["zstd", "-d", "-q", "-c"], input=data,
                              stdout=subprocess.PIPE, check=True).stdout
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:*")


def read_deb(data):
    members = ar_members(data)
    with open_tar(members, "control") as archive:
        entries = [m for m in archive if m.name.removeprefix("./") == "control"]
        if len(entries) != 1 or not entries[0].isfile() or entries[0].size > 1024 * 1024:
            raise ValueError("missing/oversized/invalid control file")
        control = parse_control(archive.extractfile(entries[0]).read().decode("utf-8").rstrip("\n"))
    paths = []
    with open_tar(members, "data") as archive:
        for member in archive:
            name = member.name.removeprefix("./")
            p = PurePosixPath(name)
            if p.is_absolute() or ".." in p.parts:
                raise ValueError("unsafe payload path: " + name)
            if not member.isdir():
                paths.append(name)
    return control, paths


def classify(fields, paths, digest, config):
    arch = fields["architecture"]
    if arch in ARCHES:
        targets = [ARCHES[arch]]
        if digest in config:
            raise ValueError("manual assignments only allowed for Architecture: all")
    elif arch == "all":
        targets = config.get(digest)
        if not isinstance(targets, list) or not targets or len(set(targets)) != len(targets) or any(t not in GROUPS for t in targets):
            raise ValueError("Architecture: all needs reviewed SHA256 assignment in repo-config.json: " + digest)
    else:
        raise ValueError("unsupported Architecture: " + arch)
    varjb = any(p.startswith("var/jb/") for p in paths)
    root_tweak = any(p.startswith(("Library/MobileSubstrate/", "usr/lib/TweakInject/")) for p in paths)
    if varjb and any(t != "rootless" for t in targets):
        raise ValueError("/var/jb payload conflicts with selected package scheme")
    if root_tweak and "rootless" in targets:
        raise ValueError("rootful tweak path conflicts with rootless metadata")
    return targets


def stanza(fields, filename, data, group=None):
    f = {k: v for k, v in fields.items() if k not in GENERATED}
    if group:
        if "section" in f:
            f["x-original-section"] = f["section"]
        f["section"] = group
    f.update(filename=filename, size=str(len(data)),
             md5sum=hashlib.md5(data).hexdigest(), sha256=hashlib.sha256(data).hexdigest(),
             sha512=hashlib.sha512(data).hexdigest())
    labels = {"md5sum": "MD5sum", "sha256": "SHA256", "sha512": "SHA512"}
    return "\n".join(labels.get(k, "-".join(w.capitalize() for w in k.split("-"))) + ": " + v
                     for k, v in f.items()) + "\n\n"


def write_indexes(folder, stanzas, architectures, label, base_url):
    folder.mkdir(parents=True, exist_ok=True)
    data = "".join(stanzas).encode("utf-8")
    blobs = {"Packages": data, "Packages.gz": gzip.compress(data, mtime=0),
             "Packages.bz2": bz2.compress(data), "Packages.xz": lzma.compress(data)}
    for name, content in blobs.items():
        (folder / name).write_bytes(content)
    lines = ["Origin: Mayo Personal Repo", "Label: " + label,
             "Suite: stable", "Codename: personal", "Version: 1.0", "Components: main",
             "Date: " + datetime.datetime.now(datetime.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000"),
             "Architectures: " + " ".join(sorted(set(architectures))),
             "Description: Personal jailbreak packages", "Support: " + base_url]
    for title, algo in (("MD5Sum", "md5"), ("SHA256", "sha256"), ("SHA512", "sha512")):
        lines.append(title + ":")
        lines.extend(" " + hashlib.new(algo, b).hexdigest() + " " + str(len(b)) + " " + n
                     for n, b in blobs.items())
    (folder / "Release").write_text("\n".join(lines) + "\n", encoding="utf-8")


def build(source, output, base_url):
    source, output = Path(source).resolve(), Path(output).resolve()
    if output.exists():
        raise ValueError("output already exists; choose a fresh output directory")
    if not base_url.startswith("https://") or any(c in base_url for c in "\n\r<>\""):
        raise ValueError("base URL must be HTTPS")
    base_url = base_url.rstrip("/") + "/"
    config = json.loads((source / "repo-config.json").read_text(encoding="utf-8"))
    assignments = config.get("all_assignments", {})
    if not isinstance(assignments, dict):
        raise ValueError("all_assignments must be an object")
    records, identities, digests = [], set(), set()
    for path in sorted((source / "debs").rglob("*")):
        if path.is_symlink():
            raise ValueError("symlinks are not allowed under debs/")
        if not path.is_file() or path.suffix.lower() != ".deb":
            continue
        try:
            data = path.read_bytes()
            digest = hashlib.sha256(data).hexdigest()
            fields, paths = read_deb(data)
            targets = classify(fields, paths, digest, assignments)
            identity = tuple(fields[k] for k in ("package", "version", "architecture"))
            if identity in identities:
                raise ValueError("duplicate Package/Version/Architecture; remove duplicate or bump Version")
            identities.add(identity)
            digests.add(digest)
            records.append((path.relative_to(source).as_posix(), data, digest, fields, targets))
        except Exception as exc:
            raise ValueError(str(path.relative_to(source)) + ": " + str(exc)) from exc
    stale = set(assignments) - digests
    if stale:
        raise ValueError("remove stale SHA256 assignments: " + ", ".join(sorted(stale)))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temp:
        site = Path(temp) / "site"
        site.mkdir()
        combined, grouped, report = [], {g: [] for g in GROUPS}, []
        for filename, data, digest, fields, targets in records:
            # One immutable copy per package, shared by the combined index.
            first = targets[0]
            for group in targets:
                pool = site / group / "debs"
                pool.mkdir(parents=True, exist_ok=True)
                (pool / (digest + ".deb")).write_bytes(data)
                grouped[group].append(stanza(fields, "debs/" + digest + ".deb", data))
            combined.append(stanza(fields, first + "/debs/" + digest + ".deb", data,
                                   first if len(targets) == 1 else "universal-reviewed"))
            report.append(dict(source=filename, package=fields["package"], version=fields["version"],
                               architecture=fields["architecture"], sha256=digest, groups=targets))
        write_indexes(site, combined, list(ARCHES) + (["all"] if any(r["architecture"] == "all" for r in report) else []),
                      "Mayo Personal Repo", base_url)
        for arch, group in ARCHES.items():
            write_indexes(site / group, grouped[group], [arch, "all"],
                          "Mayo " + group, base_url + group + "/")
        (site / ".nojekyll").touch()
        (site / "classification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        links = "".join('<li><a href="' + g + '/">' + g + '</a> · ' + str(len(grouped[g])) + ' packages</li>' for g in GROUPS)
        page = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>Mayo 自用源</title><style>body{font:18px system-ui;max-width:780px;margin:60px auto;padding:20px;line-height:1.8}code{overflow-wrap:anywhere}</style><h1>Mayo 自用源</h1><p>在 Sileo 添加：<br><code>' + html.escape(base_url) + '</code></p><p>主源自动按 DEB 架构过滤，并以环境作为分类。也可只添加对应子源：</p><ul>' + links + '</ul><p><a href="Packages">Packages</a> · <a href="Release">Release</a> · <a href="classification.json">分类记录</a></p><p>自动分类不会转换插件；能否运行还取决于设备、系统版本和依赖。</p></html>'
        (site / "index.html").write_text(page, encoding="utf-8")
        for g in GROUPS:
            (site / g / "index.html").write_text('<!doctype html><meta charset="utf-8"><title>' + g + '</title><h1>' + g + '</h1><p>在 Sileo 添加：' + html.escape(base_url + g + '/') + '</p><a href="Packages">Packages</a> · <a href="Release">Release</a>', encoding="utf-8")
        manifest = {p.relative_to(site).as_posix(): dict(size=p.stat().st_size, sha256=hashlib.sha256(p.read_bytes()).hexdigest())
                    for p in sorted(site.rglob("*")) if p.is_file() and p.name != ".nojekyll"}
        (site / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        os.replace(site, output)
    print(json.dumps(report, ensure_ascii=True, indent=2))
    print("Built", len(records), "packages for", base_url)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", default=".")
    parser.add_argument("--output", default="_site")
    parser.add_argument("--base-url", required=True)
    args = parser.parse_args()
    build(args.source, args.output, args.base_url)
