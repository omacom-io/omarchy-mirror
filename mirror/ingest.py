"""Capture complete source repositories, then save an immutable selection."""

import copy
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

from .common import Error, digest, encode, file_digest, name, now, repository_order, safe_path
from .metadata import attach_signature, embedded_signature, packages


class Source:
    def __init__(self, location, timeout=60, retries=3):
        parsed = urlsplit(location)
        self.remote = parsed.scheme in ("https", "http")
        if parsed.scheme and not self.remote:
            raise Error("Source must be an HTTP(S) URL or a local directory.")
        if self.remote and (parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise Error("Source URLs cannot contain credentials, queries, or fragments.")
        self.location, self.timeout, self.retries = location.rstrip("/"), timeout, retries

    def copy(self, relative, output, maximum=None, optional=False):
        safe_path(relative)
        for attempt in range(self.retries):
            try:
                if self.remote:
                    request = Request(self.location + "/" + quote(relative, safe="/"), headers={
                        "User-Agent": "omarchy-mirror/0.1", "Cache-Control": "no-cache"})
                    source = urlopen(request, timeout=self.timeout)
                else:
                    source = (Path(self.location) / relative).open("rb")
                with source, output.open("wb") as dest:
                    total = 0
                    while chunk := source.read(1024 * 1024):
                        total += len(chunk)
                        if maximum is not None and total > maximum:
                            raise Error(f"Source object exceeds size limit: {relative}")
                        dest.write(chunk)
                return True
            except (FileNotFoundError, HTTPError) as exc:
                if isinstance(exc, HTTPError):
                    exc.close()
                if optional and (isinstance(exc, FileNotFoundError) or exc.code == 404):
                    return False
                if isinstance(exc, HTTPError) and exc.code in (408, 429, 500, 502, 503, 504) and attempt + 1 < self.retries:
                    time.sleep(min(2**attempt, 4))
                    continue
                raise Error(f"Source file unavailable: {relative}", code="source", hint="Retry after upstream finishes syncing; check the source layout.") from exc
            except (URLError, TimeoutError, ConnectionError, OSError) as exc:
                if attempt + 1 == self.retries:
                    raise Error(f"Source transfer failed: {relative}", code="network", exit_code=6,
                                hint="Check source connectivity and retry; completed artifacts are retained.") from exc
                time.sleep(min(2**attempt, 4))

    def read(self, relative, maximum=256 * 1024 * 1024, optional=False):
        with tempfile.TemporaryDirectory(prefix="mirror-source-") as temp:
            target = Path(temp) / "download"
            return target.read_bytes() if self.copy(relative, target, maximum, optional) else None


def repo_path(repo, arch, layout):
    if layout == "arch":
        return f"{repo}/os/{arch}"
    if layout == "arm":
        return f"{arch}/{repo}"
    if layout == "flat":
        return ""
    raise Error("Unsupported source layout.")


def ingest(registry, source, label, repos, arch, layout="arch", base_id=None, workers=4, progress=lambda _: None, dry_run=False, repo_order=None):
    name(label), name(arch)
    if not repos or len(set(repos)) != len(repos) or (layout == "flat" and len(repos) != 1):
        raise Error("Select distinct repositories; flat layout supports one repository per import.")
    for repo in repos:
        name(repo)
    base = registry.release(base_id, arch) if base_id else None
    all_repos = set(repos) | (set(base["repositories"]) if base else set())
    if repo_order is not None:
        repo_order = repository_order(all_repos, repo_order)
    elif base:
        repo_order = base.get("repo_order", repository_order(base["repositories"]))
        repo_order = repo_order + [r for r in repository_order(all_repos) if r not in repo_order]
    else:
        repo_order = repository_order(all_repos)
    trust = registry.signing.trust_id()
    snapshots, imported, plan = {}, {}, []
    for repo in repos:
        if base and repo in base["repositories"] and base["repositories"][repo]["source"] != label:
            raise Error(f"{repo} belongs to another source in the base release.", hint="Use its existing source label or import into a separate release.")
        directory = repo_path(repo, arch, layout)
        prefix = directory + "/" if directory else ""
        progress(f"Read {label}: {repo}/{arch}")
        db = source.read(f"{prefix}{repo}.db")
        files = source.read(f"{prefix}{repo}.files")
        dbsig = source.read(f"{prefix}{repo}.db.sig", maximum=16384, optional=True)
        filessig = source.read(f"{prefix}{repo}.files.sig", maximum=16384, optional=True)
        for kind, body, signature in (("db", db, dbsig), ("files", files, filessig)):
            if signature:
                with tempfile.TemporaryDirectory(prefix="mirror-db-verify-") as temp:
                    path = Path(temp) / kind
                    path.write_bytes(body)
                    registry.signing.verify(path, signature)
        records = packages(db, files, arch)
        snapshots[repo] = {"prefix": prefix, "db": db, "files": files, "db.sig": dbsig, "files.sig": filessig}
        imported[repo] = {"source": label, "packages": {}, "upstream": {"db": digest(db), "files": digest(files)}}
        jobs = list(records.values())
        if dry_run:
            missing = [r for r in jobs if not registry.store.head(f"pool/{r['sha256']}/{r['filename']}")]
            plan.append({"repo": repo, "packages": len(jobs), "archive_bytes": sum(r["size"] for r in jobs),
                         "missing_archives": len(missing), "missing_bytes": sum(r["size"] for r in missing)})
            continue

        def import_package(record):
            filename = record["filename"]
            signature = embedded_signature(record)
            if signature is None:
                signature = source.read(prefix + filename + ".sig", maximum=16384)
            attach_signature(record, signature)
            # An immutable artifact is verified at ingestion. A changed trust
            # keyring invalidates reuse; unchanged hourly imports need no archives.
            verification_key = "verification/" + digest(encode({"archive": record["sha256"],
                "signature": record["signature_sha256"], "trust": trust})) + ".json"
            existing = registry.store.head(record["archive_key"])
            proof, _ = registry.store.versioned(verification_key)
            if existing and proof:
                import json
                if existing != {"size": record["size"], "sha256": record["sha256"]}:
                    raise Error(f"Stored archive differs from upstream: {filename}")
                verification = json.loads(proof)
            else:
                progress(f"Verify {filename}")
                with tempfile.TemporaryDirectory(prefix="mirror-package-") as temp:
                    path = Path(temp) / filename
                    source.copy(prefix + filename, path, maximum=record["size"])
                    if path.stat().st_size != record["size"] or file_digest(path) != record["sha256"]:
                        raise Error(f"Package checksum/size mismatch: {filename}", hint="Retry against a complete upstream snapshot.")
                    signers = registry.signing.verify(path, signature)
                    if registry.signing.trust_id() != trust:
                        raise Error("Trusted keyrings changed during import; retry with stable keyrings.")
                    registry.store.put_file(record["archive_key"], path, record["sha256"])
                verification = {"signers": signers, "keyrings_sha256": trust, "verified_day": now()[:10]}
                registry.store.put(verification_key, encode(verification), "application/json")
            registry.store.put(record["signature_key"], signature)
            registry.store.put(f"archive-signatures/{record['sha256']}/{filename}.json", encode({
                "signature_key": record["signature_key"]}), "application/json")
            record["verification"] = verification
            metadata_id = registry.store.content_json("metadata", record)
            registry.store.put(f"filenames/{arch}/{repo}/{filename}.json", encode({
                "archive_key": record["archive_key"], "signature_key": record["signature_key"],
                "sha256": record["sha256"], "size": record["size"]}), "application/json")
            return record["name"], {"archive": record["sha256"], "metadata": metadata_id}

        # Bound queued work too: a failed source must not start thousands of jobs.
        with ThreadPoolExecutor(max_workers=workers) as executor:
            pending, remaining = set(), iter(jobs)
            try:
                while True:
                    while len(pending) < workers * 2:
                        item = next(remaining, None)
                        if item is None:
                            break
                        pending.add(executor.submit(import_package, item))
                    if not pending:
                        break
                    finished, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in finished:
                        pkg, reference = future.result()
                        imported[repo]["packages"][pkg] = reference
            except BaseException:
                for future in pending:
                    future.cancel()
                raise
    if dry_run:
        return {"dry_run": True, "repositories": plan,
                "note": "Metadata checked; package availability/authenticity not verified and no objects or rings written."}
    if registry.signing.trust_id() != trust:
        raise Error("Trusted keyrings changed during import; retry with stable keyrings.")
    # Never expose a moving/incomplete upstream acquisition.
    for repo, snapshot in snapshots.items():
        for suffix in ("db", "files", "db.sig", "files.sig"):
            actual = source.read(snapshot["prefix"] + repo + "." + suffix, optional=suffix.endswith("sig"))
            if actual != snapshot[suffix]:
                raise Error(f"Upstream changed during import: {repo}.{suffix}", hint="Retry; verified package uploads will be reused.")
        for kind in ("db", "files"):
            body = snapshot[kind]
            registry.store.put(f"upstream/{digest(body)}.{kind}", body, "application/gzip")
    selected = copy.deepcopy(base["repositories"]) if base else {}
    selected.update(imported)
    if base and selected == base["repositories"] and repo_order == base.get("repo_order"):
        return base_id
    return registry.save({"schema_version": 1, "kind": "import", "label": label, "arch": arch,
                          "base": base_id, "created_at": now(), "repositories": selected, "repo_order": repo_order})
