#!/usr/bin/env python3
"""Download signed demo packages through the Worker using isolated pacman state."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen


def run(command, **kwargs):
    result = subprocess.run(command, capture_output=True, **kwargs)
    if result.returncode:
        raise RuntimeError(result.stdout.decode(errors="replace") + result.stderr.decode(errors="replace"))
    return result.stdout.decode()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", required=True)
    parser.add_argument("--url", default="http://127.0.0.1:8787")
    parser.add_argument("--suffix", help="Use the candidate suffix published to a separate R2 store")
    parser.add_argument("--wait", type=int, default=0, help="Seconds to wait for the local Worker to become ready")
    args = parser.parse_args()
    demo = Path(args.demo).resolve()
    info = json.loads((demo / "result.json").read_text())
    suffix = args.suffix or info["suffix"]
    if not suffix or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789_.-" for c in suffix):
        raise ValueError("Invalid candidate suffix")
    deadline = time.monotonic() + args.wait
    while True:
        try:
            with urlopen(args.url.rstrip("/") + "/api/v1/rings.json", timeout=2) as response:
                response.read()
            break
        except (URLError, TimeoutError):
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.25)
    with tempfile.TemporaryDirectory(prefix="mirror-pacman-http-") as temporary:
        root = Path(temporary)
        for directory in ("db", "cache", "gnupg"):
            (root / directory).mkdir(mode=0o700)
        gpg = ["gpg", "--no-options", "--homedir", str(root / "gnupg"), "--batch",
               "--no-default-keyring", "--keyring", str(root / "gnupg/pubring.gpg")]
        run(gpg + ["--import", info["keyring"]])
        run(gpg + ["--import-ownertrust"], input=(info["signing_fingerprint"] + ":6:\n").encode())
        config = root / "pacman.conf"
        config.write_text("[options]\nArchitecture = x86_64\nSigLevel = Required DatabaseRequired\n" +
                          f"[extra-{suffix}]\nServer = {args.url.rstrip('/')}/extra/os/$arch\n")
        command = ["pacman", "--config", str(config), "--dbpath", str(root / "db"),
                   "--cachedir", str(root / "cache"), "--gpgdir", str(root / "gnupg"),
                   "--logfile", str(root / "pacman.log"), "--noconfirm"]
        prefix = []
        if os.geteuid() != 0:
            if not shutil.which("fakeroot"):
                raise RuntimeError("Install fakeroot or run this inside the supplied container; no host packages are installed.")
            prefix = ["fakeroot"]
        try:
            output = run(prefix + command + ["-Syw", "pool-demo-app"])
            packages = sorted(p.name for p in (root / "cache").glob("*.pkg.tar.gz"))
            if packages != ["pool-demo-app-2-1-x86_64.pkg.tar.gz", "pool-demo-lib-2-1-x86_64.pkg.tar.gz"]:
                raise RuntimeError(f"Unexpected downloaded selection: {packages}")
            run(prefix + command + ["-Fy"])
            files = run(command + ["-Fl", "pool-demo-app"])
            if "usr/share/pool-demo-app/version" not in files:
                raise RuntimeError("Candidate file database did not contain the expected file")
            print(json.dumps({"ok": True, "packages": packages, "signatures": "package and database signatures required",
                              "file_query": "passed", "installed": False, "database": f"extra-{suffix}"}, indent=2))
        finally:
            subprocess.run(["gpgconf", "--homedir", str(root / "gnupg"), "--kill", "gpg-agent"], capture_output=True)


if __name__ == "__main__":
    main()
