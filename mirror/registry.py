"""Release authority, database generation, and compare-and-swap promotion."""

import copy
from concurrent.futures import ThreadPoolExecutor
import json

from .common import Conflict, Error, Missing, digest, encode, name, now, repository_order, sha
from .metadata import assemble


class Registry:
    def __init__(self, store, signing):
        self.store, self.signing = store, signing

    def current(self):
        body, token = self.store.versioned("current.json")
        if body is None:
            return {"schema_version": 1, "rings": {}, "previous": None}, None, token
        pointer = json.loads(body)
        identifier = sha(pointer["publication"])
        return self.store.verified_json("publications", identifier), identifier, token

    def ring(self, ring, arch):
        return self.current()[0]["rings"].get(f"{name(arch)}/{name(ring)}")

    def resolve(self, reference, arch):
        if len(reference) == 64 and all(c in "0123456789abcdef" for c in reference):
            self.release(reference, arch)
            return reference
        selected = self.ring(reference, arch)
        if not selected:
            raise Missing(f"ring {arch}/{reference}")
        return selected["release"]

    def release(self, identifier, arch=None):
        release = self.store.verified_json("releases", sha(identifier))
        if release.get("schema_version") != 1 or (arch and release["arch"] != arch):
            raise Error("Unsupported release schema or incompatible architecture.")
        return release

    def metadata(self, reference):
        record = self.store.verified_json("metadata", sha(reference["metadata"]))
        if record.get("schema_version") != 1 or record["sha256"] != reference["archive"]:
            raise Error("Manifest and package metadata disagree.")
        return record

    def save(self, release):
        release = copy.deepcopy(release)
        # Preserve explicit precedence when a later import adds a repository.
        order = [r for r in release.get("repo_order", []) if r in release["repositories"]]
        order += [r for r in repository_order(release["repositories"]) if r not in order]
        release["repo_order"] = repository_order(release["repositories"], order)
        return self.store.content_json("releases", release)

    def records(self, release):
        for repo, selected in sorted(release["repositories"].items()):
            for pkgname, reference in sorted(selected["packages"].items()):
                metadata = self.metadata(reference)
                if metadata["name"] != pkgname or metadata["arch"] not in (release["arch"], "any"):
                    raise Error("Manifest package identity mismatch.")
                yield repo, metadata

    def check_artifacts(self, release):
        def verify(item):
            _, metadata = item
            for key, expected_size, checksum in (
                (metadata["archive_key"], metadata["size"], metadata["sha256"]),
                (metadata["signature_key"], None, metadata["signature_sha256"]),
            ):
                info = self.store.head(key)
                if not info or info["sha256"] != checksum or (expected_size is not None and info["size"] != expected_size):
                    raise Error(f"Missing or damaged release artifact: {key}", hint="Reimport this source before publication.")
            return 1
        # HEAD checks do not transfer archives. Parallelize the network roundtrips
        # for a full Arch selection; metadata JSON is cached independently.
        with ThreadPoolExecutor(max_workers=16) as executor:
            return sum(executor.map(verify, self.records(release)))

    def build(self, identifier, rebuild=False):
        release = self.release(identifier)
        ready_key = f"ready/{identifier}.json"
        old, token = self.store.versioned(ready_key)
        if old and not rebuild:
            build_id = json.loads(old)["build"]
            self.check_build(build_id)
            return build_id
        self.check_artifacts(release)
        repositories, catalogue = {}, []
        for repo, selected in sorted(release["repositories"].items()):
            records = [self.metadata(ref) for ref in selected["packages"].values()]
            outputs = {}
            for kind in ("db", "files"):
                body = assemble(records, kind)
                key = f"databases/{digest(body)}.{kind}"
                self.store.put(key, body, "application/gzip")
                signature = self.signing.sign(body)
                sigkey = f"signatures/{digest(signature)}.sig"
                self.store.put(sigkey, signature)
                outputs[kind], outputs[f"{kind}.sig"] = key, sigkey
            repositories[repo] = outputs
            for record in records:
                catalogue.append({"repo": repo, "source": selected["source"], **{
                    key: record[key] for key in ("name", "version", "arch", "filename", "sha256", "size")},
                    "signers": record["verification"]["signers"],
                    "metadata": selected["packages"][record["name"]]["metadata"]})
        catalogue_id = self.store.content_json("catalogues", {
            "schema_version": 1, "release": identifier, "arch": release["arch"],
            "packages": sorted(catalogue, key=lambda p: (p["name"], p["repo"]))})
        build_id = self.store.content_json("builds", {"schema_version": 1, "release": identifier,
            "repositories": repositories, "catalogue": f"catalogues/{catalogue_id}.json",
            "signing_key": self.signing.sign_key, "created_at": now()})
        self.store.cas(ready_key, encode({"build": build_id}), token)
        return build_id

    def check_build(self, identifier):
        build = self.store.verified_json("builds", sha(identifier))
        for outputs in build["repositories"].values():
            for key in outputs.values():
                info = self.store.head(key)
                expected = key.rsplit("/", 1)[-1].split(".", 1)[0]
                if not info or info["sha256"] != sha(expected):
                    raise Error(f"Missing or damaged generated object: {key}", hint="Run release build --rebuild to regenerate missing metadata; restore corrupt immutable objects from backup.")
        catalogue = build["catalogue"]
        info = self.store.head(catalogue)
        if not info or info["sha256"] != sha(catalogue.rsplit("/", 1)[-1][:-5]):
            raise Error("Missing or damaged release catalogue; rebuild missing metadata or restore a backup.")
        return build

    def activate(self, ring, identifier, expected, reason, allow_downgrade=False):
        name(ring)
        release = self.release(identifier)
        ready = self.store.json(f"ready/{identifier}.json")
        build = self.check_build(ready["build"])
        self.check_artifacts(release)
        if build["release"] != identifier:
            raise Error("Build/release mismatch.")
        if expected:
            diff = self.diff(expected, identifier)
            downgrades = [p for p in diff if p.get("direction") == "downgrade"]
            if downgrades and not allow_downgrade:
                raise Error("Promotion would downgrade packages.", hint="Inspect release diff, then pass --allow-downgrade for an intentional rollback.")
        arch, ring_key = release["arch"], f"{release['arch']}/{ring}"
        # Routes are harmless until the ring appears in the activated publication.
        for repo in build["repositories"]:
            self.store.put(f"routes/{arch}/{repo}/{repo}-{ring}.json", encode({
                "kind": "ring", "ring": ring, "arch": arch, "repo": repo}), "application/json")
        for _ in range(10):
            publication, previous_id, token = self.current()
            prior = publication["rings"].get(ring_key)
            if prior and prior["release"] == identifier and prior["build"] == ready["build"]:
                return {"ring": ring_key, "release": identifier, "publication": previous_id, "changed": False}
            if (prior["release"] if prior else None) != expected:
                raise Conflict(f"{ring_key} changed since the selected base; promotion was not activated.")
            rings = copy.deepcopy(publication["rings"])
            rings[ring_key] = {"release": identifier, "build": ready["build"], "updated_at": now(), "reason": reason}
            next_publication = {"schema_version": 1, "previous": previous_id, "rings": rings,
                                "created_at": now(), "change": {"ring": ring_key, "from": expected, "to": identifier, "reason": reason}}
            pub_id = self.store.content_json("publications", next_publication)
            try:
                self.store.cas("current.json", encode({"schema_version": 1, "publication": pub_id}), token)
                return {"ring": ring_key, "release": identifier, "publication": pub_id, "changed": True}
            except Conflict:
                continue  # Preserve newer edge/other-ring changes on retry.
        raise Conflict("Publication remained busy after ten attempts.")

    def diff(self, left_id, right_id):
        from .validation import vercmp
        left, right = self.release(left_id), self.release(right_id)
        if left["arch"] != right["arch"]:
            raise Error("Cannot compare releases for different architectures.")
        def index(release):
            return {(repo, m["name"]): m for repo, m in self.records(release)}
        old, new = index(left), index(right)
        result = []
        for repo, pkg in sorted(set(old) | set(new)):
            before, after = old.get((repo, pkg)), new.get((repo, pkg))
            if before and after and before["sha256"] == after["sha256"]:
                continue
            direction = "add" if not before else "remove" if not after else (
                "downgrade" if vercmp(after["version"], before["version"]) < 0 else "update")
            result.append({"repo": repo, "name": pkg, "from": before["version"] if before else None,
                           "to": after["version"] if after else None, "direction": direction})
        return result

    def candidate(self, label, base_id, source_id, additions, removals=(), repo_order=None):
        name(label)
        if len(label) > 40:
            raise Error("Candidate names are limited to 40 characters.")
        base, source = self.release(base_id), self.release(source_id)
        if base["arch"] != source["arch"]:
            raise Error("Candidate base and source architectures differ.")
        candidate = copy.deepcopy(base)
        candidate.update(kind="candidate", label=label, base=base_id, source_release=source_id, created_at=now())
        for spec in additions:
            matches = [(r, selected) for r, selected in source["repositories"].items()
                       if spec in selected["packages"] or ("/" in spec and spec.split("/", 1)[0] == r and spec.split("/", 1)[1] in selected["packages"])]
            if len(matches) != 1:
                raise Error(f"Package selection is missing or ambiguous: {spec}", hint="Use --package REPO/NAME from the source release.")
            repo, selected = matches[0]
            pkg = spec.split("/", 1)[-1]
            if repo not in candidate["repositories"]:
                candidate["repositories"][repo] = {"source": selected["source"], "packages": {}}
            if candidate["repositories"][repo]["source"] != selected["source"]:
                raise Error(f"Repository source changed for {repo}; import and review it explicitly.")
            candidate["repositories"][repo]["packages"][pkg] = selected["packages"][pkg]
        for spec in removals:
            if "/" not in spec:
                raise Error("Removal requires REPO/NAME.")
            repo, pkg = spec.split("/", 1)
            if repo not in candidate["repositories"] or pkg not in candidate["repositories"][repo]["packages"]:
                raise Missing(spec)
            del candidate["repositories"][repo]["packages"][pkg]
        if not additions and not removals:
            raise Error("A candidate needs --package or --remove selections.")
        if repo_order is not None:
            candidate["repo_order"] = repository_order(candidate["repositories"], repo_order)
        return self.save(candidate)

    def expose_candidate(self, identifier):
        release = self.release(identifier)
        build = self.store.json(f"ready/{identifier}.json")["build"]
        # A rebuild may use a new database signature. Existing client URLs remain
        # pinned; export a fresh alias for that build of the same package selection.
        suffix = f"{release.get('label', 'candidate')}-{identifier[:16]}-{build[:16]}"
        for repo in release["repositories"]:
            self.store.put(f"routes/{release['arch']}/{repo}/{repo}-{suffix}.json", encode({
                "kind": "build", "build": build, "repo": repo, "arch": release["arch"]}), "application/json")
        return suffix

    def test_record(self, identifier, note, inventory=None):
        self.release(identifier)
        return self.store.content_json("tests", {"schema_version": 1, "release": identifier,
            "recorded_at": now(), "note": note, "inventory": inventory, "kind": "operator_report"})
