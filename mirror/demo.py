"""Create a signed, harmless two-package laboratory without installing anything."""

import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile

from .cli import client_config
from .ingest import Source, ingest
from .registry import Registry
from .signing import Signing, run
from .store import LocalStore
from .validation import check


def signing_key(root):
    home = root / "gnupg"
    home.mkdir(mode=0o700)
    run(["gpg", "--homedir", str(home), "--batch", "--pinentry-mode", "loopback", "--passphrase", "",
         "--quick-generate-key", "Omarchy Pool Demo <pool-demo@example.invalid>", "ed25519", "sign", "0"])
    listing = run(["gpg", "--homedir", str(home), "--with-colons", "--list-secret-keys"]).decode()
    fingerprint = next(line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:"))
    public = root / "demo-keyring.gpg"
    public.write_bytes(run(["gpg", "--homedir", str(home), "--export", fingerprint]))
    return Signing([str(public)], fingerprint, str(home))


def package(directory, pkgname, version, signing, depends=()):
    directory.mkdir(parents=True, exist_ok=True)
    filename = directory / f"{pkgname}-{version}-x86_64.pkg.tar.gz"
    info = f"pkgname = {pkgname}\npkgbase = {pkgname}\npkgver = {version}\npkgdesc = Signed mirror fixture\nurl = https://example.invalid\nbuilddate = 1700000000\npackager = Mirror Demo\nsize = 32\narch = x86_64\nlicense = MIT\n"
    info += "".join(f"depend = {dep}\n" for dep in depends)
    with tarfile.open(filename, "w:gz") as archive:
        for path, content in ((".PKGINFO", info.encode()), (f"usr/share/{pkgname}/version", version.encode())):
            member = tarfile.TarInfo(path)
            member.size, member.mode, member.mtime = len(content), 0o644, 1700000000
            archive.addfile(member, io.BytesIO(content))
    Path(str(filename) + ".sig").write_bytes(signing.sign(filename.read_bytes()))
    return filename


def repository(directory, signing, version, repo="extra"):
    lib = package(directory, "pool-demo-lib", version, signing)
    app = package(directory, "pool-demo-app", version, signing, [f"pool-demo-lib>={version}"])
    run(["repo-add", "--include-sigs", str(directory / f"{repo}.db.tar.gz"), str(lib), str(app)])
    return [lib, app]


def create(root, base_url="http://127.0.0.1:8787"):
    root = Path(root).resolve()
    if root.exists() and any(root.iterdir()):
        raise ValueError("Demo output directory must be empty.")
    root.mkdir(parents=True, exist_ok=True)
    signing = signing_key(root)
    registry = Registry(LocalStore(root / "store"), signing)
    stable_dir, edge_dir = root / "stable-source", root / "edge-source"
    repository(stable_dir, signing, "1-1")
    repository(edge_dir, signing, "2-1")
    stable = ingest(registry, Source(str(stable_dir)), "demo", ["extra"], "x86_64", "flat")
    edge = ingest(registry, Source(str(edge_dir)), "demo", ["extra"], "x86_64", "flat")
    for ring, identifier in (("stable", stable), ("edge", edge)):
        registry.build(identifier)
        registry.activate(ring, identifier, None, "Initialize signed demo")
    candidate = registry.candidate("app-oob", stable, edge, ["pool-demo-app", "pool-demo-lib"])
    registry.build(candidate)
    report = check(registry, candidate, ["extra/pool-demo-app", "extra/pool-demo-lib"])
    suffix = registry.expose_candidate(candidate)
    configuration = client_config(registry, candidate, base_url)
    (root / "candidate.conf").write_text(configuration)
    (root / "mirror.json").write_text(json.dumps({"store": str(root / "store"), "keyrings": signing.keyrings,
        "sign_key": signing.sign_key, "gnupghome": signing.gnupghome, "base_url": base_url}, indent=2) + "\n")
    result = {"stable": stable, "edge": edge, "candidate": candidate, "check": report["id"],
              "suffix": suffix, "store": str(root / "store"), "config": str(root / "mirror.json"),
              "pacman_config": str(root / "candidate.conf"), "keyring": signing.keyrings[0],
              "signing_fingerprint": signing.sign_key}
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8787")
    args = parser.parse_args()
    print(json.dumps(create(args.output, args.base_url), indent=2))


if __name__ == "__main__":
    main()
