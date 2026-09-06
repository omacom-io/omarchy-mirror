"""Explicit public keyrings for imports; isolated access to the signing key."""

from pathlib import Path
import subprocess
import tempfile
from threading import Lock

from .common import Error, digest, encode, file_digest


def run(args, **kwargs):
    try:
        result = subprocess.run(args, capture_output=True, **kwargs)
    except FileNotFoundError as exc:
        raise Error(f"Required executable missing: {args[0]}", hint="Install gnupg and the Arch publishing tools; run doctor.") from exc
    if result.returncode:
        detail = result.stderr.decode(errors="replace")[-1500:]
        raise Error(f"{args[0]} failed: {detail}", code="verification", hint="Check the package/signing key and configured keyrings.")
    return result.stdout


class Signing:
    def __init__(self, keyrings, sign_key=None, gnupghome=None):
        self.keyrings = [str(Path(p).resolve()) for p in keyrings]
        self.sign_key, self.gnupghome = sign_key, gnupghome
        self._trust_lock, self._trust_stamp, self._trust_digest = Lock(), None, None

    def trust_id(self):
        if not self.keyrings:
            raise Error("No trusted package keyring configured.", hint="Set keyrings in the config or pass --keyring /path/to/trusted.gpg.")
        with self._trust_lock:
            stamp = []
            for path in self.keyrings:
                stat = Path(path).stat()
                stamp.append((path, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino))
            if stamp != self._trust_stamp:
                self._trust_digest = digest(encode([file_digest(p) for p in self.keyrings]))
                self._trust_stamp = stamp
            return self._trust_digest

    def verify(self, archive, signature):
        trust = self.trust_id()
        with tempfile.TemporaryDirectory(prefix="mirror-verify-") as temp:
            sig = Path(temp) / "package.sig"
            sig.write_bytes(signature)
            args = ["gpgv", "--homedir", temp, "--status-fd", "1"]
            for keyring in self.keyrings:
                args += ["--keyring", keyring]
            status = run(args + [str(sig), str(archive)]).decode()
        if self.trust_id() != trust:
            raise Error("Trusted keyrings changed during signature verification; retry with stable keyrings.")
        signers = [line.split()[2] for line in status.splitlines() if line.startswith("[GNUPG:] VALIDSIG ")]
        if not signers:
            raise Error("No valid signature reported.")
        return signers

    def sign(self, body):
        if not self.sign_key:
            raise Error("No repository signing key configured.", hint="Set sign_key (fingerprint) and gnupghome in the config.")
        args = ["gpg", "--no-options", "--batch", "--yes"]
        if self.gnupghome:
            args += ["--homedir", str(self.gnupghome)]
        args += ["--local-user", self.sign_key, "--detach-sign", "--output", "-"]
        return run(args, input=body)
