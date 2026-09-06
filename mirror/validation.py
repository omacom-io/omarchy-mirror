"""Ask pacman to resolve transactions in isolated database/configuration paths."""

from pathlib import Path
import subprocess
import tempfile

from .common import Error, now, repository_order


def vercmp(left, right):
    try:
        result = subprocess.run(["vercmp", left, right], capture_output=True, text=True, check=True)
        return int(result.stdout.strip())
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise Error("Arch vercmp is required for version comparison.", hint="Run the publisher in the supplied Arch container.") from exc


def check(registry, identifier, targets):
    release = registry.release(identifier)
    build = registry.store.json(f"ready/{identifier}.json")["build"]
    outputs = registry.check_build(build)
    if not targets:
        raise Error("Supply at least one package to validate.", hint="Use release check ID --package hyprland.")
    valid_targets = {f"{repo}/{pkg}" for repo, values in release["repositories"].items() for pkg in values["packages"]}
    for target in targets:
        if "/" in target:
            valid = target in valid_targets
        else:
            valid = any(t.split("/", 1)[1] == target for t in valid_targets)
        if not valid:
            raise Error(f"Unknown transaction target: {target}")
    with tempfile.TemporaryDirectory(prefix="mirror-pacman-") as temp:
        root = Path(temp)
        for directory in ("db/sync", "db/local", "cache", "gnupg"):
            (root / directory).mkdir(parents=True)
        # Only locally generated metadata is queried. No packages are fetched or
        # installed; gpgv verifies imports and the publisher signs public metadata.
        config = ["[options]", f"Architecture = {release['arch']}", "SigLevel = Never"]
        order = repository_order(release["repositories"], release.get("repo_order"))
        for repo in order:
            metadata = outputs["repositories"][repo]
            (root / "db/sync" / f"{repo}.db").write_bytes(registry.store.read(metadata["db"]))
            config += [f"[{repo}]", "Server = https://invalid.example/mirror-validation"]
        (root / "pacman.conf").write_text("\n".join(config) + "\n")
        command = ["pacman", "--config", str(root / "pacman.conf"), "--dbpath", str(root / "db"),
                   "--cachedir", str(root / "cache"), "--gpgdir", str(root / "gnupg"),
                   "--logfile", str(root / "pacman.log"), "--noconfirm", "-Sp", "--print-format", "%n %v", "--", *targets]
        try:
            result = subprocess.run(command, capture_output=True, text=True)
        except FileNotFoundError as exc:
            raise Error("pacman is required for dependency validation.", hint="Run the publisher in the supplied Arch container.") from exc
        if result.returncode:
            raise Error("Candidate dependency resolution failed: " + result.stderr[-2000:],
                        code="dependencies", hint="Add the required package group from the pinned source and create a new candidate.")
        report = {"schema_version": 1, "release": identifier, "build": build, "checked_at": now(),
                  "targets": targets, "transaction": result.stdout.splitlines(), "warnings": result.stderr.strip(),
                  "repo_order": order,
                  "scope": "pacman dependency resolution with an empty installed database; manual stable upgrade/runtime testing is required"}
        report["id"] = registry.store.content_json("checks", report)
        return report
