import copy
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
from unittest.mock import patch

from mirror.cli import client_config
from mirror.common import Conflict, Error, encode
from mirror.demo import package, repository, signing_key
from mirror.ingest import Source, ingest
from mirror.metadata import assemble, fields, packages, render, unpack
from mirror.registry import Registry
from mirror.store import LocalStore, S3Store
from mirror.validation import check


class StoreTest(unittest.TestCase):
    def test_immutable_objects_and_stale_activation(self):
        with tempfile.TemporaryDirectory() as temp:
            store = LocalStore(temp)
            store.put("test/data", b"one")
            self.assertFalse(store.put("test/data", b"one"))
            with self.assertRaises(Conflict):
                store.put("test/data", b"two")
            store.cas("current.json", b"one", None)
            _, token = store.versioned("current.json")
            store.cas("current.json", b"two", token)
            with self.assertRaises(Conflict):
                store.cas("current.json", b"lost update", token)
            self.assertEqual(store.read("current.json"), b"two")
            with self.assertRaises(Error):
                store.read("../outside")


@unittest.skipUnless(all(shutil.which(tool) for tool in ("gpg", "gpgv", "repo-add", "pacman", "vercmp")), "Requires Arch tools")
class PoolTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = tempfile.TemporaryDirectory(prefix="mirror-fixture-")
        cls.root = Path(cls.fixture.name)
        cls.signing = signing_key(cls.root)
        cls.stable_dir, cls.edge_dir = cls.root / "stable", cls.root / "edge"
        repository(cls.stable_dir, cls.signing, "1-1")
        repository(cls.edge_dir, cls.signing, "2-1")

    @classmethod
    def tearDownClass(cls):
        subprocess.run(["gpgconf", "--homedir", cls.signing.gnupghome, "--kill", "gpg-agent"], capture_output=True)
        cls.fixture.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="mirror-test-")
        self.addCleanup(self.temp.cleanup)
        self.store = LocalStore(Path(self.temp.name) / "store")
        self.registry = Registry(self.store, self.signing)

    def import_repo(self, directory, base=None):
        return ingest(self.registry, Source(str(directory)), "demo", ["extra"], "x86_64", "flat", base)

    def initial(self):
        stable, edge = self.import_repo(self.stable_dir), self.import_repo(self.edge_dir)
        for ring, identifier in (("stable", stable), ("edge", edge)):
            self.registry.build(identifier)
            self.registry.activate(ring, identifier, None, "initial")
        return stable, edge

    def test_metadata_roundtrip_matches_repo_add_including_file_queries(self):
        identifier = self.import_repo(self.stable_dir)
        release = self.registry.release(identifier)
        records = [meta for _, meta in self.registry.records(release)]
        for kind in ("db", "files"):
            generated = assemble(records, kind)
            original = (self.stable_dir / f"extra.{kind}").read_bytes()
            self.assertEqual(unpack(generated), unpack(original))
            self.assertEqual(generated, assemble(records, kind))
        self.registry.build(identifier)
        check_result = check(self.registry, identifier, ["extra/pool-demo-app"])
        self.assertIn("pool-demo-lib 1-1", check_result["transaction"])
        # Exercise pacman's file database reader, not just our own parser.
        sandbox = Path(self.temp.name) / "pacman"
        (sandbox / "sync").mkdir(parents=True)
        (sandbox / "sync/extra.files").write_bytes(assemble(records, "files"))
        config = sandbox / "pacman.conf"
        config.write_text("[options]\nArchitecture = x86_64\nSigLevel = Never\n[extra]\n")
        result = subprocess.run(["pacman", "--config", str(config), "--dbpath", str(sandbox), "-Fl", "extra/pool-demo-app"], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usr/share/pool-demo-app/version", result.stdout)

    def test_rebuild_uses_metadata_without_archive_reads(self):
        identifier = self.import_repo(self.stable_dir)
        original_read = self.store.read
        def no_archives(key):
            if key.startswith("pool/"):
                self.fail("Archive body read during regeneration")
            return original_read(key)
        from mirror.common import file_digest as actual_digest
        def no_package_hash(path):
            if "/pool/" in str(path):
                self.fail("Archive rehashed during regeneration")
            return actual_digest(path)
        with patch.object(self.store, "read", side_effect=no_archives), patch("mirror.store.file_digest", side_effect=no_package_hash):
            self.registry.build(identifier)

    def test_noop_import_reuses_artifacts_without_download(self):
        identifier = self.import_repo(self.stable_dir)
        source = Source(str(self.stable_dir))
        original_copy = source.copy
        def no_package(relative, *args, **kwargs):
            if ".pkg.tar." in relative:
                self.fail("Downloaded a previously verified package")
            return original_copy(relative, *args, **kwargs)
        with patch.object(source, "copy", side_effect=no_package), patch("mirror.ingest.now", return_value="2027-01-01T00:00:00+00:00"):
            repeated = ingest(self.registry, source, "demo", ["extra"], "x86_64", "flat", identifier)
        self.assertEqual(identifier, repeated)
        self.assertEqual(len(list(self.store.keys("pool/"))), 2)

    def test_dry_run_does_not_create_store_or_download_packages(self):
        source = Source(str(self.stable_dir))
        original = source.copy
        def no_package(relative, *args, **kwargs):
            self.assertNotIn(".pkg.tar.", relative)
            return original(relative, *args, **kwargs)
        with patch.object(source, "copy", side_effect=no_package):
            plan = ingest(self.registry, source, "demo", ["extra"], "x86_64", "flat", dry_run=True)
        self.assertEqual(plan["repositories"][0]["missing_archives"], 2)
        self.assertFalse(self.store.root.exists())

    def test_detached_signatures_and_independent_source_merge(self):
        stable = self.import_repo(self.stable_dir)
        source = Path(self.temp.name) / "omarchy"
        paths = repository(source, self.signing, "1-1", "omarchy")
        # repo-add without --include-sigs leaves only adjacent package signatures.
        for file in source.glob("omarchy.*"):
            file.unlink()
        subprocess.run(["repo-add", str(source / "omarchy.db.tar.gz"), *map(str, paths)], check=True, capture_output=True)
        identifier = ingest(self.registry, Source(str(source)), "omarchy", ["omarchy"], "x86_64", "flat", stable)
        selected = self.registry.release(identifier)["repositories"]
        self.assertEqual(selected["extra"], self.registry.release(stable)["repositories"]["extra"])
        for ref in selected["omarchy"]["packages"].values():
            metadata = self.registry.metadata(ref)
            self.assertTrue(metadata["db"]["desc"]["PGPSIG"])
            adjacent = self.store.json(f"archive-signatures/{metadata['sha256']}/{metadata['filename']}.json")
            self.assertEqual(adjacent["signature_key"], metadata["signature_key"])

    def test_rebuild_restores_missing_databases_and_keeps_old_alias(self):
        identifier = self.import_repo(self.stable_dir)
        first = self.registry.build(identifier)
        suffix = self.registry.expose_candidate(identifier)
        old_route = self.store.read(f"routes/x86_64/extra/extra-{suffix}.json")
        build = self.registry.check_build(first)
        db = build["repositories"]["extra"]["db"]
        self.store.path(db).unlink()
        with self.assertRaisesRegex(Error, "Missing or damaged"):
            self.registry.check_build(first)
        with patch("mirror.registry.now", return_value="2027-01-01T00:00:00+00:00"):
            second = self.registry.build(identifier, rebuild=True)
        self.assertNotEqual(first, second)
        self.registry.check_build(first)
        self.assertNotEqual(self.registry.expose_candidate(identifier), suffix)
        self.assertEqual(self.store.read(f"routes/x86_64/extra/extra-{suffix}.json"), old_route)

    def test_repository_precedence_is_frozen_for_validation_and_export(self):
        stable = self.import_repo(self.stable_dir)
        source = Path(self.temp.name) / "omarchy"
        repository(source, self.signing, "2-1", "omarchy")
        edge = ingest(self.registry, Source(str(source)), "omarchy", ["omarchy"], "x86_64", "flat", stable,
                      repo_order="omarchy,extra")
        self.assertEqual(self.registry.release(edge)["repo_order"], ["omarchy", "extra"])
        candidate = self.registry.candidate("precedence", stable, edge,
            ["omarchy/pool-demo-app", "omarchy/pool-demo-lib"], repo_order="omarchy,extra")
        self.registry.build(candidate)
        report = check(self.registry, candidate, ["pool-demo-app"])
        self.assertIn("pool-demo-app 2-1", report["transaction"])
        self.assertEqual(report["repo_order"], ["omarchy", "extra"])
        exported = client_config(self.registry, candidate, "https://example.invalid")
        self.assertLess(exported.index("[omarchy-"), exported.index("[extra-"))
        with self.assertRaisesRegex(Error, "precedence must match"):
            client_config(self.registry, candidate, "https://example.invalid", "extra,omarchy")

    def test_candidate_dependency_group_and_promotion(self):
        stable, edge = self.initial()
        incomplete = self.registry.candidate("incomplete", stable, edge, ["pool-demo-app"])
        self.registry.build(incomplete)
        with self.assertRaisesRegex(Error, "dependency resolution failed"):
            check(self.registry, incomplete, ["extra/pool-demo-app"])
        candidate = self.registry.candidate("app-oob", stable, edge, ["pool-demo-app", "pool-demo-lib"])
        self.registry.build(candidate)
        report = check(self.registry, candidate, ["extra/pool-demo-app"])
        self.assertIn("pool-demo-lib 2-1", report["transaction"])
        suffix = self.registry.expose_candidate(candidate)
        config = client_config(self.registry, candidate, "https://example.invalid")
        self.assertIn(f"[extra-{suffix}]", config)
        self.assertIn("Server = https://example.invalid/extra/os/$arch", config)
        self.assertNotIn("$repo", config)
        self.registry.activate("stable", candidate, stable, "Tested fixture")
        self.assertEqual(self.registry.ring("stable", "x86_64")["release"], candidate)
        self.assertEqual(len(list(self.store.keys("pool/"))), 4)

    def test_edge_race_preserves_both_promotions(self):
        stable, edge = self.initial()
        candidate = self.registry.candidate("app-oob", stable, edge, ["pool-demo-app", "pool-demo-lib"])
        self.registry.build(candidate)
        next_edge_doc = self.registry.release(edge)
        next_edge_doc["label"] = "next-edge"
        next_edge = self.registry.save(next_edge_doc)
        self.registry.build(next_edge)
        original = self.store.cas
        raced = False
        def race(key, body, expected):
            nonlocal raced
            if key == "current.json" and not raced:
                raced = True
                self.registry.activate("edge", next_edge, edge, "Concurrent edge update")
            return original(key, body, expected)
        with patch.object(self.store, "cas", side_effect=race):
            self.registry.activate("stable", candidate, stable, "Candidate")
        self.assertEqual(self.registry.ring("edge", "x86_64")["release"], next_edge)
        self.assertEqual(self.registry.ring("stable", "x86_64")["release"], candidate)

    def test_changed_stable_and_downgrade_are_rejected(self):
        stable, edge = self.initial()
        candidate = self.registry.candidate("app-oob", stable, edge, ["pool-demo-app", "pool-demo-lib"])
        self.registry.build(candidate)
        self.registry.activate("stable", edge, stable, "Intervening release")
        with self.assertRaises(Conflict):
            self.registry.activate("stable", candidate, stable, "Stale candidate")
        with self.assertRaisesRegex(Error, "downgrade"):
            self.registry.activate("stable", stable, edge, "Rollback")
        self.registry.activate("stable", stable, edge, "Explicit rollback", allow_downgrade=True)

    def test_upstream_race_and_failure_leave_current_untouched(self):
        stable, _ = self.initial()
        current = self.store.read("current.json")
        source = Source(str(self.edge_dir))
        original = source.read
        calls = 0
        def changed(path, **kwargs):
            nonlocal calls
            if path == "extra.db":
                calls += 1
                if calls > 1:
                    return b"changed"
            return original(path, **kwargs)
        with patch.object(source, "read", side_effect=changed), self.assertRaisesRegex(Error, "Upstream changed"):
            ingest(self.registry, source, "demo", ["extra"], "x86_64", "flat", stable)
        self.assertEqual(self.store.read("current.json"), current)
        with patch.object(self.signing, "sign", side_effect=Error("Signing unavailable")), self.assertRaises(Error):
            self.registry.build(stable, rebuild=True)
        self.assertEqual(self.store.read("current.json"), current)

    def test_wrong_signature_and_checksum_fail(self):
        damaged = Path(self.temp.name) / "damaged"
        shutil.copytree(self.stable_dir, damaged)
        pkg = next(damaged.glob("pool-demo-app*.pkg.tar.gz"))
        pkg.write_bytes(b"corruption")
        with self.assertRaisesRegex(Error, "mismatch"):
            self.import_repo(damaged)
        self.assertIsNone(self.store.versioned("current.json")[0])
        with patch.object(self.signing, "verify", side_effect=Error("Invalid signature")), self.assertRaisesRegex(Error, "Invalid signature"):
            self.import_repo(self.stable_dir)

    def test_http_import_and_embedded_signatures(self):
        class Quiet(SimpleHTTPRequestHandler):
            def log_message(self, *args): pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(self.stable_dir)))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            identifier = ingest(self.registry, Source(f"http://127.0.0.1:{server.server_port}"), "demo", ["extra"], "x86_64", "flat")
            self.assertEqual(len(self.registry.release(identifier)["repositories"]["extra"]["packages"]), 2)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_record_parser_rejects_bad_metadata_and_preserves_unknown_fields(self):
        with self.assertRaises(Error): fields(b"%NAME%\na\n\n%NAME%\nb\n")
        with self.assertRaises(Error): fields(b"\0\0")
        original = {"NAME": ["fixture"], "FUTURE_FIELD": ["first", "second"]}
        self.assertEqual(fields(render(original)), original)
        with self.assertRaises(Error):
            packages((self.stable_dir / "extra.db").read_bytes(), (self.edge_dir / "extra.files").read_bytes(), "x86_64")

    def test_cli_json_contract_and_test_revision_enforcement(self):
        stable, edge = self.initial()
        cfg = Path(self.temp.name) / "config.json"
        cfg.write_text(json.dumps({"store": str(self.store.root), "keyrings": self.signing.keyrings,
                                  "sign_key": self.signing.sign_key, "gnupghome": self.signing.gnupghome}))
        command = [sys.executable, "-m", "mirror.cli", "--config", str(cfg)]
        result = subprocess.run(command + ["ring", "list", "--json"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["ok"])
        self.assertEqual(result.stderr, "")
        bad = subprocess.run(command + ["source", "import", "demo", "--json"], text=True, capture_output=True)
        self.assertEqual(bad.returncode, 2)
        self.assertEqual(bad.stdout, "")
        self.assertEqual(json.loads(bad.stderr)["code"], "usage")
        candidate = self.registry.candidate("app-oob", stable, edge, ["pool-demo-app", "pool-demo-lib"])
        self.registry.build(candidate)
        checked = check(self.registry, candidate, ["extra/pool-demo-app", "extra/pool-demo-lib"])["id"]
        tested = self.registry.test_record(candidate, "Tested candidate fixture")
        wrong_test = self.registry.test_record(edge, "Wrong revision")
        wrong_check = check(self.registry, stable, ["extra/pool-demo-app"])["id"]
        for test_id, check_id in ((wrong_test, checked), (tested, wrong_check)):
            result = subprocess.run(command + ["candidate", "promote", candidate, "--test", test_id,
                "--check", check_id, "--reason", "Test"], text=True, capture_output=True)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn("another candidate", json.loads(result.stderr)["error"])
            self.assertEqual(self.registry.ring("stable", "x86_64")["release"], stable)
        result = subprocess.run(command + ["candidate", "promote", candidate, "--test", tested,
            "--check", checked, "--reason", "Test"], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.registry.ring("stable", "x86_64")["release"], candidate)

    def test_s3_full_import_build_and_candidate(self):
        try:
            import boto3
            from moto import mock_aws
        except ImportError:
            self.skipTest("Install .[test] for S3 tests")
        with mock_aws():
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="mirror-flow-test")
            self.store = S3Store("s3://mirror-flow-test/preview", client=client, cache=str(Path(self.temp.name) / "cache"))
            self.registry = Registry(self.store, self.signing)
            self.test_candidate_dependency_group_and_promotion()


class S3Test(unittest.TestCase):
    def test_s3_immutable_upload_and_conditional_publication(self):
        try:
            import boto3
            from moto import mock_aws
        except ImportError:
            self.skipTest("Install .[test] for S3 contract tests")
        with mock_aws(), tempfile.TemporaryDirectory() as temp:
            client = boto3.client("s3", region_name="us-east-1")
            client.create_bucket(Bucket="mirror-test")
            store = S3Store("s3://mirror-test/preview", client=client)
            store.put("metadata/test.json", b"hello")
            self.assertFalse(store.put("metadata/test.json", b"hello"))
            with self.assertRaises(Conflict): store.put("metadata/test.json", b"different")
            store.cas("current.json", b"one", None)
            _, token = store.versioned("current.json")
            store.cas("current.json", b"two", token)
            with self.assertRaises(Conflict): store.cas("current.json", b"three", token)
            self.assertEqual(store.read("current.json"), b"two")
            path = Path(temp) / "package"
            path.write_bytes(b"package data")
            from mirror.common import file_digest
            self.assertTrue(store.put_file("pool/artifact", path, file_digest(path)))
            self.assertFalse(store.put_file("pool/artifact", path, file_digest(path)))
            self.assertEqual(list(store.keys("pool/")), ["pool/artifact"])


if __name__ == "__main__":
    unittest.main()
