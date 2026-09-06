"""Command parsing/output; release behavior lives in the library modules."""

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
from urllib.parse import urlsplit

from . import __version__
from .common import Conflict, Error, Missing, digest, encode, name, now, repository_order
from .ingest import Source, ingest
from .registry import Registry
from .signing import Signing
from .store import LocalStore, S3Store
from .validation import check


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise Error(message, "usage", "Run this command with --help.", 2)


def parser():
    root = Parser(description="Immutable pacman package pools, candidates, and release rings.",
                  epilog="Config: file < MIRROR_* environment < flags. Exit: 0 success, 1 validation, 2 usage, 3 missing, 4 conflict, 6 transport, 130 interrupted.")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", help="JSON config (env: MIRROR_CONFIG)")
    root.add_argument("--store", help="Local directory or s3://bucket/prefix (env: MIRROR_STORE)")
    root.add_argument("--endpoint", help="R2 S3 endpoint (env: MIRROR_ENDPOINT)")
    root.add_argument("--keyring", action="append", help="Trusted GPG keyring; repeatable")
    root.add_argument("--sign-key", help="Database signing fingerprint (env: MIRROR_SIGN_KEY)")
    root.add_argument("--gnupghome", help="Signing key home (env: MIRROR_GNUPGHOME)")
    output = root.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help="Versioned JSON output (automatic when piped)")
    output.add_argument("--quiet", action="store_true", help="Raw JSON data without the envelope")
    commands = root.add_subparsers(dest="command", required=True)
    def group(noun):
        group = commands.add_parser(noun)
        return group.add_subparsers(dest="action", required=True)
    config = group("config")
    config.add_parser("show", help="Show effective non-secret configuration")
    setup = config.add_parser("setup", help="Write an example configuration")
    setup.add_argument("--output", required=True)
    setup.add_argument("--base-url", default="http://127.0.0.1:8787")
    doctor = commands.add_parser("doctor", help="Check tools, keyrings, and storage")
    doctor.add_argument("--write-check", action="store_true", help="Exercise conditional writes under diagnostics/; never changes rings")
    source = group("source")
    imp = source.add_parser("import", help="Import complete upstream repositories from HTTP or a local rsync stage")
    imp.add_argument("name")
    imp.add_argument("--location", required=True)
    imp.add_argument("--repo", action="append", required=True)
    imp.add_argument("--arch", default="x86_64")
    imp.add_argument("--layout", choices=("arch", "arm", "flat"), default="arch")
    imp.add_argument("--base", help="Release/ring whose unselected repositories are retained")
    imp.add_argument("--repo-order", help="Freeze comma-separated repository precedence for the complete result")
    imp.add_argument("--ring", help="Build and activate this ring after a complete import")
    imp.add_argument("--workers", type=int, default=4)
    imp.add_argument("--reason", default="Upstream import")
    imp.add_argument("--allow-downgrade", action="store_true")
    imp.add_argument("--dry-run", action="store_true", help="Read metadata and estimate missing bytes without writing storage")
    release = group("release")
    release.add_parser("list", help="List retained immutable releases")
    for verb in ("show", "build", "check", "config"):
        cmd = release.add_parser(verb)
        cmd.add_argument("id")
        if verb == "build":
            cmd.add_argument("--rebuild", action="store_true")
        if verb == "check":
            cmd.add_argument("--package", action="append", required=True)
        if verb == "config":
            cmd.add_argument("--base-url")
            cmd.add_argument("--output", help="Write pacman configuration to a new file")
            cmd.add_argument("--repo-order", help="Assert the release's frozen comma-separated precedence")
    diff = release.add_parser("diff")
    diff.add_argument("id")
    diff.add_argument("--from", dest="from_id", required=True)
    rings = group("ring")
    rings.add_parser("list")
    set_ring = rings.add_parser("set", help="Activate a built release with an expected base")
    set_ring.add_argument("name")
    set_ring.add_argument("--release", required=True)
    set_ring.add_argument("--expect", required=True, help="Current release ID, or 'empty' to initialize")
    set_ring.add_argument("--reason", required=True)
    set_ring.add_argument("--allow-downgrade", action="store_true")
    candidate = group("candidate")
    create = candidate.add_parser("create", help="Freeze a base plus explicit changes from a source")
    create.add_argument("name")
    create.add_argument("--base", default="stable")
    create.add_argument("--from", dest="source", default="edge")
    create.add_argument("--arch", default="x86_64")
    create.add_argument("--package", action="append", default=[])
    create.add_argument("--remove", action="append", default=[])
    create.add_argument("--repo-order", help="Override the base's frozen precedence; include every repository")
    publish = candidate.add_parser("publish", help="Build, resolve changed packages with pacman, and expose an immutable candidate")
    publish.add_argument("id")
    publish.add_argument("--check-package", action="append", default=[])
    test = candidate.add_parser("record-test", help="Attach an operator's manual test report to an exact candidate")
    test.add_argument("id")
    test.add_argument("--note", required=True)
    test.add_argument("--inventory", help="Text file from pacman -Q on the test installation")
    promote = candidate.add_parser("promote")
    promote.add_argument("id")
    promote.add_argument("--to", default="stable")
    promote.add_argument("--test", required=True, help="Test report ID for this exact revision")
    promote.add_argument("--check", required=True, help="Dependency-check report ID for this revision")
    promote.add_argument("--reason", required=True)
    promote.add_argument("--allow-downgrade", action="store_true")
    packages = group("package")
    history = packages.add_parser("history")
    history.add_argument("name")
    history.add_argument("--arch", default="x86_64")
    add = packages.add_parser("import", help="Import signed local builds using repo-add for initial metadata extraction")
    add.add_argument("file", nargs="+")
    add.add_argument("--repo", required=True)
    add.add_argument("--source", default="omarchy")
    add.add_argument("--arch", default="x86_64")
    add.add_argument("--base", help="Merge additions into this release/ring")
    gc = commands.add_parser("gc", help="Report orphaned archives; this version never deletes stored history")
    gc.add_argument("--dry-run", action="store_true", required=True)
    skill = group("skill")
    skill.add_parser("show")
    install = skill.add_parser("install")
    install.add_argument("--output", default=str(Path.home() / ".agents/skills/omarchy-mirror/SKILL.md"))
    return root


def configuration(args):
    path = Path(args.config or os.environ.get("MIRROR_CONFIG", Path.home() / ".config/omarchy-mirror/config.json"))
    cfg = {}
    if path.exists():
        cfg = json.loads(path.read_text())
    elif args.config or os.environ.get("MIRROR_CONFIG"):
        raise Missing(str(path))
    cfg.setdefault("store", str(Path.home() / ".local/state/omarchy-mirror/store"))
    cfg.setdefault("keyrings", [])
    for key in ("store", "endpoint", "sign_key", "gnupghome", "base_url", "region"):
        if os.environ.get("MIRROR_" + key.upper()):
            cfg[key] = os.environ["MIRROR_" + key.upper()]
        if getattr(args, key, None):
            cfg[key] = getattr(args, key)
    if args.keyring:
        cfg["keyrings"] = args.keyring
    cfg["config_path"] = str(path)
    return cfg


def write_new(path, content):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise Error(f"File already exists: {target}", hint="Choose a new output path or move the existing file.") from exc
    with os.fdopen(fd, "w") as output:
        output.write(content)
    return str(target)


def client_config(registry, identifier, url, order=None):
    if not url:
        raise Error("No serving URL configured.", hint="Set base_url in config or pass --base-url.")
    parsed = urlsplit(url)
    if parsed.scheme not in ("http", "https") or parsed.query or parsed.fragment or parsed.username or any(c.isspace() for c in url):
        raise Error("Invalid serving URL.")
    release = registry.release(identifier)
    repos = repository_order(release["repositories"], release.get("repo_order"))
    if order is not None and repository_order(release["repositories"], order) != repos:
        raise Error("Exported precedence must match the release.", hint="Set --repo-order when importing or creating a new candidate, then validate it.")
    suffix = registry.expose_candidate(identifier)
    lines = [f"# Immutable Omarchy mirror release: {identifier}", "# Repository signing key must be trusted before use.",
             "# Review repository precedence and preserve any required local pacman options.", "[options]",
             f"Architecture = {release['arch']}", "SigLevel = Required DatabaseRequired", "LocalFileSigLevel = Required", ""]
    for repo in repos:
        lines += [f"[{repo}-{suffix}]", f"Server = {url.rstrip('/')}/{repo}/os/$arch", ""]
    return "\n".join(lines)


def dispatch(args, cfg, registry, progress):
    store = registry.store
    command, action = args.command, getattr(args, "action", None)
    if command == "config":
        if action == "show":
            return {k: cfg.get(k) for k in ("config_path", "store", "endpoint", "region", "keyrings", "sign_key", "gnupghome", "base_url")}
        sample = {k: cfg.get(k) for k in ("store", "endpoint", "keyrings", "sign_key", "gnupghome")}
        sample["base_url"] = args.base_url
        return {"path": write_new(args.output, json.dumps(sample, indent=2) + "\n")}
    if command == "skill":
        content = Path(__file__).with_name("SKILL.md").read_text()
        return {"content": content} if action == "show" else {"path": write_new(args.output, content)}
    if command == "doctor":
        tools = {tool: shutil.which(tool) for tool in ("gpg", "gpgv", "pacman", "repo-add", "vercmp")}
        publication, identifier, _ = registry.current()
        result = {"tools": tools, "store": cfg["store"], "publication": identifier,
                  "rings": len(publication["rings"]), "trusted_keyrings": registry.signing.trust_id(),
                  "sign_key_configured": bool(registry.signing.sign_key)}
        if args.write_check:
            key = f"diagnostics/{digest(os.urandom(32))}.json"
            store.cas(key, b'{"step":1}\n', None)
            body, token = store.versioned(key)
            if body != b'{"step":1}\n':
                raise Error("Storage read-after-write check failed.")
            store.cas(key, b'{"step":2}\n', token)
            try:
                store.cas(key, b'{"step":3}\n', token)
            except Conflict:
                result["conditional_writes"] = "passed"
            else:
                raise Error("Storage accepted a stale conditional write; do not publish here.")
        return result
    if command == "source":
        if not 1 <= args.workers <= 32:
            raise Error("--workers must be between 1 and 32.")
        prior = registry.ring(args.ring, args.arch) if args.ring else None
        expected = prior["release"] if prior else None
        base = registry.resolve(args.base, args.arch) if args.base else expected
        if args.ring and base != expected:
            raise Error("An import targeting a ring must use that ring's current base.")
        identifier = ingest(registry, Source(args.location), args.name, args.repo, args.arch,
                            args.layout, base, args.workers, progress, args.dry_run, args.repo_order)
        if args.dry_run:
            return identifier
        result = {"release": identifier, "repositories": args.repo}
        if args.ring:
            registry.build(identifier)
            result.update(registry.activate(args.ring, identifier, expected, args.reason, args.allow_downgrade))
        return result
    if command == "release":
        if action == "list":
            return [{"id": key.split("/")[-1][:-5], **{k: store.json(key).get(k) for k in ("arch", "kind", "label", "created_at")}}
                    for key in store.keys("releases/")]
        if action == "show":
            return registry.release(args.id)
        if action == "build":
            return {"release": args.id, "build": registry.build(args.id, args.rebuild)}
        if action == "check":
            return check(registry, args.id, args.package)
        if action == "diff":
            return registry.diff(args.from_id, args.id)
        if action == "config":
            content = client_config(registry, args.id, args.base_url or cfg.get("base_url"), args.repo_order)
            return {"path": write_new(args.output, content), "release": args.id} if args.output else {"release": args.id, "configuration": content}
    if command == "ring":
        if action == "list":
            publication, identifier, _ = registry.current()
            return {"publication": identifier, "rings": publication["rings"]}
        return registry.activate(args.name, args.release, None if args.expect == "empty" else args.expect, args.reason, args.allow_downgrade)
    if command == "candidate":
        if action == "create":
            base, source = registry.resolve(args.base, args.arch), registry.resolve(args.source, args.arch)
            identifier = registry.candidate(args.name, base, source, args.package, args.remove, args.repo_order)
            return {"release": identifier, "base": base, "source": source, "changes": registry.diff(base, identifier)}
        release = registry.release(args.id)
        if release["kind"] != "candidate":
            raise Error("This command requires a candidate release.")
        if action == "publish":
            build = registry.build(args.id)
            changed = [f"{d['repo']}/{d['name']}" for d in registry.diff(release["base"], args.id) if d["to"]]
            targets = sorted(set(changed + args.check_package))
            report = check(registry, args.id, targets)
            return {"release": args.id, "build": build, "check": report["id"],
                    "suffix": registry.expose_candidate(args.id), "validation": report}
        if action == "record-test":
            inventory = Path(args.inventory).read_text() if args.inventory else None
            return {"release": args.id, "test": registry.test_record(args.id, args.note, inventory)}
        if action == "promote":
            records = {}
            for category, identifier in (("tests", args.test), ("checks", args.check)):
                records[category] = store.verified_json(category, identifier)
                if records[category]["release"] != args.id:
                    raise Error(f"{category} record belongs to another candidate.")
            changed = {f"{d['repo']}/{d['name']}" for d in registry.diff(release["base"], args.id) if d["to"]}
            if not changed.issubset(records["checks"]["targets"]):
                raise Error("Dependency report does not cover every changed package.", hint="Run candidate publish again and use its check ID.")
            if records["checks"]["build"] != store.json(f"ready/{args.id}.json")["build"]:
                raise Error("Candidate databases were rebuilt after this check.", hint="Run candidate publish again and test the newly exported configuration.")
            return registry.activate(args.to, args.id, release["base"], args.reason, args.allow_downgrade)
    if command == "package":
        if action == "history":
            result = []
            for key in store.keys("releases/"):
                release = store.json(key)
                if release["arch"] != args.arch:
                    continue
                for repo, selected in release["repositories"].items():
                    if args.name in selected["packages"]:
                        metadata = registry.metadata(selected["packages"][args.name])
                        result.append({"release": key.split("/")[-1][:-5], "repo": repo, "version": metadata["version"], "sha256": metadata["sha256"]})
            return result
        from .signing import run
        base = registry.resolve(args.base, args.arch) if args.base else None
        name(args.repo)
        with tempfile.TemporaryDirectory(prefix="mirror-build-import-") as temp:
            paths = []
            for filename in args.file:
                source = Path(filename).resolve()
                target = Path(temp) / source.name
                if target.exists():
                    raise Error(f"Duplicate package filename: {source.name}")
                shutil.copyfile(source, target)
                shutil.copyfile(str(source) + ".sig", str(target) + ".sig")
                paths.append(str(target))
            run(["repo-add", "--include-sigs", str(Path(temp) / f"{args.repo}.db.tar.gz"), *paths])
            identifier = ingest(registry, Source(temp), args.source, [args.repo], args.arch, "flat", None, 1, progress)
        if base:
            prior, imported = registry.release(base), registry.release(identifier)
            existing = prior["repositories"].get(args.repo, {"source": args.source, "packages": {}})
            if existing["source"] != args.source:
                raise Error("Package source does not match the base repository.")
            existing["packages"].update(imported["repositories"][args.repo]["packages"])
            prior["repositories"][args.repo] = existing
            prior.update(kind="import", base=base, label=args.source, created_at=now())
            prior.pop("source_release", None)
            identifier = registry.save(prior)
        return {"release": identifier}
    if command == "gc":
        referenced = set()
        for key in store.keys("releases/"):
            for _, metadata in registry.records(store.json(key)):
                referenced.add(metadata["archive_key"])
        orphans = [key for key in store.keys("pool/") if key not in referenced]
        return {"mode": "report_only", "retention": "all releases and candidates retained",
                "referenced_archives": len(referenced), "unreferenced_archives": orphans,
                "deleted": 0, "note": "In-progress imports may own unreferenced objects; this version never deletes them."}
    raise Error("Unsupported command.")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # Output flags are convenient anywhere in the command tree.
    modes = [v for v in argv if v in ("--json", "--quiet")]
    argv = modes + [v for v in argv if v not in ("--json", "--quiet")]
    machine = bool(modes) or not sys.stdout.isatty()
    try:
        args = parser().parse_args(argv)
        cfg = configuration(args)
        store = S3Store(cfg["store"], cfg.get("endpoint"), cfg.get("region", "auto"),
                        cache=cfg.get("cache", str(Path.home() / ".cache/omarchy-mirror/metadata"))) if cfg["store"].startswith("s3://") else LocalStore(cfg["store"])
        registry = Registry(store, Signing(cfg["keyrings"], cfg.get("sign_key"), cfg.get("gnupghome")))
        progress = (lambda _: None) if machine else (lambda message: print(message, file=sys.stderr, flush=True))
        data = dispatch(args, cfg, registry, progress)
        output = data if args.quiet else {"schema_version": 1, "ok": True, "data": data}
        print(json.dumps(output, indent=None if machine else 2))
    except KeyboardInterrupt:
        print("Interrupted; current ring assignments are unchanged unless activation already completed.", file=sys.stderr)
        raise SystemExit(130)
    except Exception as exc:
        if isinstance(exc, Error):
            error = exc
        else:
            # SDK exceptions may contain endpoint details; never print request headers.
            error = Error(f"{type(exc).__name__}: {str(exc)[:700]}", "runtime", "Check doctor, configuration, and storage credentials; retry the operation.")
        body = {"schema_version": 1, "ok": False, "error": str(error), "code": error.code, "hint": error.hint}
        print(json.dumps(body), file=sys.stderr)
        raise SystemExit(error.exit_code)


if __name__ == "__main__":
    main()
