---
title: Building the shared package pool from the current mirror
description: Concrete changes to ingestion, publication, ring promotion, serving, and testing in omarchy-mirror.
---

This document records the original implementation assessment. The initial implementation is now on `feat/shared-package-pool`; use the [deployment quickstart](shared-package-pool-quickstart.md) for actual commands, implemented behavior, and remaining limits. It adds independent components and leaves legacy staging/upload scripts intact. Direct HTTP ingestion replaces the need for a full rsync stage, while local stages remain supported. Automatic retention deletion and the production package-builder/client migration remain future work.

The hourly upstream pull can continue. The substantial change is to replace directory-based publication and deletion with a package importer and a release publisher. Keep rsync, rclone, the existing mirror host, R2, and systemd scheduling initially. Add durable manifests, named candidates, a publisher that can generate databases, and a Worker that serves database aliases from those manifests.

This assessment is based on checkout `9f0bf383c2425f141f6d3676e6b65318751db927`. No production buckets, active services, client installations, or Omarchy package-build infrastructure were inspected. It refines the [shared-pool design proposal](https://a.mosaic.heyoodle.com/a/repo-omarchy-mirror/shared-package-pool-proposal/); the changes below are proposed work, not implemented functionality.

**The hourly flow becomes import, snapshot, activate.** Today, `omarchy-mirror-sync` calls staging and upload. Staging rsyncs the upstream tree with deletion. Upload copies package files into a ring bucket, overwrites databases/freshness, and prunes files missing from the stage. There are no enforced failure stops between every shell phase, and no release manifest or activation transaction.

The replacement flow is:

```text
upstream Arch repositories
  -> one local incoming tree, refreshed by rsync
  -> validate and import newly observed packages into the shared R2 pool
  -> save an immutable snapshot of the complete repository selections
  -> publish metadata and update edge's assignment

saved snapshot -> frozen RC
frozen RC -> stable promotion
stable snapshot + selected edge artifacts -> test candidate -> stable promotion
```

Each run must lock the incoming tree against another writer, capture `.db` and `.files`, and validate the selected package set. Verify new package hashes and signatures, detect upstream changes during acquisition, and retry incomplete snapshots. Reuse the approach already exercised by ARM staging. Stable upstream bytes at the end of a fetch are useful evidence, but do not by themselves prove dependency coherence across repositories; also check the resulting repository set before activation.

Import archives by immutable identity and upload only missing objects. Record every selected artifact in the snapshot, including objects already present in the pool. Preserve the original upstream database bytes/signatures when the complete upstream selection is unchanged; there is no need to regenerate all of Arch's metadata every hour. A no-op selection can reuse the existing release while recording a separate ingestion-check time. A failed or incomplete run leaves edge's current assignment unchanged and retains completed verified transfers for retry.

The rsync `--delete-delay` behavior can remain for the disposable incoming tree. It must never govern deletion from the retained pool. Before another refresh is allowed to remove a local archive, any release that has been activated must already have its referenced archive safely stored in R2. Local working space can be one upstream tree plus a bounded cache of older archives needed for candidate builds, rather than complete local edge/RC/stable copies. Retain backups independently from this cache.

**Changes in this repository.** These are proposed responsibilities and paths, not a commitment to a final module layout.

| Current component | Required change |
| --- | --- |
| `bin/omarchy-mirror-stage` | Keep rsync as the initial transfer mechanism, but make errors fatal, coordinate locking with import, and treat `lastsync` as an ingestion observation rather than permission to publish. |
| `bin/omarchy-mirror-sync` | Orchestrate stage, validated import, immutable snapshot creation, and edge activation. Stop on a failed phase and expose structured status. |
| `bin/omarchy-mirror-upload` | Replace broad ring-directory copies with additive artifact uploads and immutable metadata uploads. Remove the trailing `rclone sync` deletion step from the new pool workflow. Use a separate publisher operation to activate a complete release. |
| `bin/omarchy-mirror-sync-rc` | Freeze a selected complete edge release into RC. Do not run another upstream sync as part of freezing it. |
| `bin/omarchy-mirror-sync-stable` and `bin/omarchy-mirror-sync-stable-from-rc` | Replace direct upstream refresh/copy behavior with promotion of an exact retained release or candidate. The current RC-to-stable wrapper copies RC then fetches upstream, which must stop. |
| Stable stage/upload services and timers | Replace staging with a scheduled snapshot selection and uploading with promotion of that selected snapshot. Packages are already in the pool. |
| `bin/omarchy-mirror-stage-arm` | Extract reusable parsing, download verification, locking, and status logic without changing its existing strict/degraded contract. Add a source adapter later. |
| `bin/setup` | Provision the publisher runtime, persistent state, signing access, and new systemd units on the documented Ubuntu host. |
| CI and tests | Extend the existing Python/Arch test setup to ingestion, publication, regeneration, promotion, retention, and actual candidate consumption. Add Worker route tests. |

The hourly service can keep its current cadence. The current stable schedule stages upstream on the first and uploads late in the month. To preserve that behavior, record an immutable release at the first scheduled event and promote that same recorded ID at the second. Do not resolve a potentially changed `rc` pointer blindly at promotion time. The repo contains a manual RC wrapper, but does not establish a separate deployed RC timer; verify production scheduling during rollout.

**New code and infrastructure.** Python is a practical fit for the host-side importer, manifests, CLI, and publisher because the repo already has a tested Python ingestion path. Organize it as reusable modules behind a `bin/omarchy-mirror` entry point instead of extending a set of loosely chained shell scripts indefinitely. Keep the existing wrappers during migration where they provide stable service entry points. A small TypeScript Worker can handle HTTP routing. Neither choice requires a new web application for the first release.

The ARM parser currently keeps filename, compressed size, SHA-256, and an embedded signature. General ingestion additionally needs package name, epoch/version/release, source, repository, architecture, dependencies, providers, conflicts, replacements, package base, and file-list records or access to the full archive. Support signatures obtained from either `%PGPSIG%` or the detached upstream `.sig`, according to what each source publishes. Verify authenticity using the appropriate keyring; the current ARM script preserves signature bytes but does not establish authenticity.

The R2 schema needs immutable artifact objects, signature objects, complete package-metadata JSON records, release manifests, generated databases, candidate revisions, test records, and publications containing ring assignments. Each release references both archive and metadata hashes; share the full metadata across releases instead of duplicating large file lists in every manifest. Add a current publication pointer and schema versions. Enforce filename-collision checks in each public repository namespace. An optional local SQLite index can accelerate queries and availability checks; the durable manifests/publications remain sufficient to restore authority if that index is lost.

The publisher needs a controlled Arch environment on the Ubuntu host for `repo-add --include-sigs` as a reference/recovery tool, pacman/libalpm validation, and database signing. Capture complete repository field/value records and file lists at ingestion, preserving additional fields, source provenance, and schema/tool versions. A custom database assembler can then generate candidates from metadata JSON without downloading package archives. Keep a local metadata cache, reuse unchanged repository outputs, and recover incomplete metadata from verified archives when necessary. Large `.files` generation and recovery reads are host work, not request-time Worker work.

Metadata assembly becomes a defined implementation step: compare generated `.db` and `.files` records against the standard tools, exercise pacman package/file queries and signatures, and verify that a complete rebuild succeeds with an empty local package cache and package-download access disabled. A separate recovery check should rebuild records from retained archives. Measure metadata fetch, assembly, compression, and signing times against the `repo-add` baseline; avoiding archive transfers/decompression should help, but performance is not yet measured. The existing ARM parser reads only `desc` summaries and cannot supply this complete metadata store unchanged.

The database-signing key and relevant public-key trust rollout need configuration. Keep package archives and their original signatures unchanged. Client keyrings must trust the key signing curated databases before those databases become visible under signature-checking policies. The serving Worker does not need the private signing key.

**Continuous edge updates must not interfere with candidates.** A candidate records both its stable base release and its source edge release at creation. New hourly edge snapshots have no effect on those IDs or on the package set being tested.

When promoting the candidate, check that stable still selects the expected base. Read the latest global publication and retain its current edge/RC assignments while replacing stable. Serialize that short activation step and use a conditional pointer update to prevent lost writes. If edge changes concurrently, retry the commit against the latest publication; do not force a candidate rebase. If stable has changed, show the diff and require a new candidate revision. Long downloads, database builds, and manual tests should not hold the activation lock.

Automated edge ingestion, scheduled RC/stable changes, manual candidate promotion, and retention must all use this same publisher protocol. Otherwise an hourly writer could overwrite a manual promotion even though each operation works in isolation. In-progress imports and candidates must also register retention protection before collection can race with them.

**Serve database names as ring selectors.** At a fixed directory such as `/extra/os/x86_64/`, expose:

```text
extra-edge.db
extra-rc.db
extra-stable.db
extra-hyprland-oob-c123.db
<original-package-filename>
```

Publish matching `.files`, database signatures, and supported compressed aliases. The Worker maps each registered database alias to its selected immutable metadata object, while package and package-signature requests resolve through the retained artifact index into the common pool. Register aliases explicitly rather than guessing the repository/candidate split from hyphenated names. Mutable ring aliases should initially bypass caching; candidate revision aliases and canonical package URLs are immutable and can be cached. Check HEAD, Range/resume, conditional requests, redirects if used, and signature fetches.

Existing hostnames and ordinary `extra.db`/`core.db` URLs can remain compatibility aliases. Pacman repository section names determine the requested database name, so candidate configuration must use explicit base repository directories: a mirrorlist containing `$repo` would also change the directory when a section becomes `[extra-hyprland-oob-c123]`.

The client helper should initially print/export the complete candidate pacman configuration, so a tester can inspect and apply it on the fresh machine. A later `candidate install` operation can apply it with configuration backup and a visible package transaction. Check existing Omarchy channel-switch/update scripts and any package commands that explicitly qualify repositories, because renamed sections can affect those consumers. Those client scripts are not in this checkout. Pin all managed candidate repositories to one revision; moving stable aliases still have the usual multi-request mirror transition limits.

**The Omarchy package source is a separate integration.** This checkout shows upstream Arch mirroring, T2 mirroring, and local ARM staging; it does not contain the producer of `pkgs.omarchy.org`. A complete candidate must freeze the applicable Omarchy repository alongside core/extra/multilib, including its precedence and signer policy. Leaving `[omarchy]` on moving edge would make the test environment change outside the candidate.

Initially, an importer can ingest that repository's databases and signed packages as another source. Connecting the build pipeline to submit artifacts and completed source snapshots directly is a later optimization. Inspect the actual package producer, retention behavior, and client configuration before deciding the adapter. Do not assume Arch and Omarchy snapshots share an upstream transaction. Record the exact combination selected for the candidate and validate it together.

**A useful first release.** Build the smallest workflow that proves the operating model:

1. Import the current stable and edge selections and their package union into a separate pool/registry.
2. Create a named candidate by selecting an explicit Hyprland package group from a pinned edge snapshot.
3. Check dependency transactions with pacman's own semantics and show unresolved choices or affected packages. Let the operator adjust the explicit group.
4. Build and serve immutable candidate databases; export the matching pacman configuration.
5. Test an actual fresh stable installation, record the installed package inventory and manual results, and promote the exact tested revision to a test stable alias.
6. Export ring versions and candidate diffs as JSON for the website.

Automatic selection of every required ABI rebuild and a management web UI can follow. Use pacman/libalpm for version/provider/dependency semantics from the start rather than building an approximate replacement resolver. Passing a dependency transaction does not establish that Hyprland works on the tested hardware; preserve the manual test evidence and environment.

**Rollout and verification.** First inventory the live edge, RC, and stable databases and referenced packages, retaining the old-ring artifacts before existing pruning can remove them. Measure hash overlap and initial storage requirements, and preserve the imported ring manifests as rollback anchors. Bootstrap one ring at a time from a stable input snapshot; do not mix a changing bucket listing with independently changing metadata.

Run the new importer and publisher in parallel against a separate destination. The old scripts may continue operating only on their existing buckets, with the new pool outside their pruning scope. Demonstrate that repeated edge imports reuse existing objects and leave stable/candidates unchanged. During cutover, retarget or disable the old writer for each migrated destination before it can overwrite the new publication. Do not retire old buckets until serving compatibility and recovery checks pass.

Required checks include: ordinary and suffixed database names consumed by pacman; complete `.db`/`.files` regeneration from manifests and archives; valid package/database signatures with isolated trusted keys; interrupted imports/builds leaving assignments unchanged; an edge update racing with a stable promotion without losing either; rejection when stable's base changed; retained historical downloads; and collection preserving every current, pinned, or in-progress reference. Extend the existing ARM fixture server and Arch-container CI rather than replacing them. Add the new module and Worker paths to CI triggers, which currently cover only ARM staging and tests.

The work is a substantial replacement of the publication and retention layer, plus a small serving component and CLI. The existing transfer machinery and host remain useful. The largest implementation uncertainties are obtaining a coherent set of upstream metadata during rolling updates, integrating the separate Omarchy package source, validating package groups with pacman, and coordinating activation/retention correctly. Database aliasing and website JSON are comparatively small pieces. A fresh-install Hyprland candidate that remains unchanged through several hourly edge imports is the right first demonstration before migrating stable.
