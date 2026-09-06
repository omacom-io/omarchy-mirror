---
title: One package pool, independently managed release rings
description: Design options for Omarchy mirror storage, selective releases, rollback history, and a public package catalogue.
---

The proposed direction is sound: store each distinct package archive once, and let each ring's repository databases select which versions users receive. I recommend a shared R2 pool, immutable release manifests and repository databases, and a small Cloudflare Worker that preserves existing mirror URLs. Use a CLI to prepare named candidates and one publisher to activate them. The authoritative published ring map and release manifests live in R2; generate the website's package catalogue from those records. Git can review and back up proposed changes without becoming a second authority for what is live.

Pacman's `.db` remains authoritative for what the client can install. On the publishing side, a versioned manifest should define what goes into that database. The same manifest generates the website data and records the history that a current `.db` cannot provide.

This proposal covers mirrored Arch packages and future Omarchy builds. Packages need not all be `.pkg.tar.zst`: preserve upstream `.xz` and other supported formats unchanged, including their signatures. Recompressing an archive changes its bytes and invalidates its existing signature.

**What the current code establishes.** This is a review of checkout `9f0bf383c2425f141f6d3676e6b65318751db927`, dated September 5, 2026. Production buckets, deployed services, and client configuration were not inspected.

| Current behavior | Consequence for the design |
| --- | --- |
| Edge, RC, and stable use separate local trees and R2 destinations. | Identical package bytes can be stored and uploaded once per ring. Actual savings need a bucket inventory. |
| Staging uses rsync with deletion; uploading copies packages, publishes metadata, then prunes against the local tree. | Old versions disappear according to directory membership. Retention needs an explicit policy based on references from releases. |
| `omarchy-mirror-sync-stable-from-rc` copies RC to stable, then calls a stable sync that fetches upstream again. | The result can contain packages that were never in the selected RC. Promotion should identify an immutable release. |
| The shell sync/upload scripts do not enforce failure stops between every phase. Metadata and its signatures are separate uploads. | The new publisher needs validation gates and an explicit activation step. Upload ordering alone is insufficient. |
| ARM staging already reads `.db` package names, sizes, hashes, and embedded signatures, and distinguishes incomplete/degraded runs. | Reuse the manifest-driven ingestion approach. It does not yet verify signature authenticity or provide a transactional release publisher. |

Sources: [ring wrappers](https://github.com/omacom/omarchy-mirror/blob/9f0bf383c2425f141f6d3676e6b65318751db927/bin/omarchy-mirror-sync-rc), [stable promotion](https://github.com/omacom/omarchy-mirror/blob/9f0bf383c2425f141f6d3676e6b65318751db927/bin/omarchy-mirror-sync-stable-from-rc), [stable upstream sync](https://github.com/omacom/omarchy-mirror/blob/9f0bf383c2425f141f6d3676e6b65318751db927/bin/omarchy-mirror-sync-stable), [upload phases](https://github.com/omacom/omarchy-mirror/blob/9f0bf383c2425f141f6d3676e6b65318751db927/bin/omarchy-mirror-upload), and [ARM ingestion](https://github.com/omacom/omarchy-mirror/blob/9f0bf383c2425f141f6d3676e6b65318751db927/bin/omarchy-mirror-stage-arm).

**Separate package identity, release membership, and activation.** A package version can be in several rings simultaneously. Promoting it adds a reference in the target ring; it need not remove the source ring's reference. Removing it from a ring does not delete its archive or uninstall it from users' machines.

| Record | Contents and purpose |
| --- | --- |
| Artifact | Archive SHA-256, filename, size, package name, epoch/version/release, package architecture, origin, signature digest, signer and verification result. Preserve dependency, provides, conflicts, replaces, and package-base metadata, plus available build provenance. |
| Release | Immutable ID, parent/base release, source snapshots, and an exact mapping from repository + package name to artifact. Include hashes of `.db`, `.files`, their signatures, and the resolved manifest. |
| Ring assignment | For each ecosystem and target architecture, select the current edge, RC, and stable release IDs. Record generation/revision to reject concurrent stale changes. |
| Change event | Who changed which selections, old/new IDs, time, reason, validation evidence, and successful activation. |
| Retention record | Which artifacts and releases are pinned, retention deadlines, verification status, and whether a rollback has been tested. |

Keep Arch Linux, Arch Linux ARM, Omarchy builds, and T2 origins distinguishable. Deduplicate identical bytes by hash while preserving origin and trust policy. An `any` package can share bytes across target architectures when it is actually the same artifact. Never infer interchangeability from a matching name/version alone.

One possible object layout is:

```text
pool/sha256/<archive-hash>/<original-filename>
signatures/sha256/<signature-hash>
metadata/sha256/<metadata-hash>.json
releases/<release-id>/manifest.json
releases/<release-id>/<repo>/os/<arch>/<repo>.db
releases/<release-id>/<repo>/os/<arch>/<repo>.db.sig
releases/<release-id>/<repo>/os/<arch>/<repo>.files
releases/<release-id>/<repo>/os/<arch>/<repo>.files.sig
publications/<publication-id>/rings.json
publications/<publication-id>/packages.json
current.json
```

ARM release metadata retains ARM's `$arch/$repo` URL layout. Compatibility aliases such as `.db.tar.gz` and `.files.tar.gz` resolve to the corresponding immutable metadata. `current.json` selects one publication containing the ring assignments and website data references. Unchanged repositories can reuse prior immutable metadata objects rather than rebuilding every database.

The client interface can also select rings by database name at a fixed server directory: `extra-edge.db`, `extra-rc.db`, `extra-stable.db`, and `extra-hyprland-oob-c123.db`, with matching `.files` and signature aliases. For example, `[extra-hyprland-oob-c123]` paired with `Server = https://mirror.omarchy.org/extra/os/$arch` selects the candidate database while package requests use the same directory and original filenames. Configure the base directory explicitly: a mirrorlist containing `$repo` would expand the renamed section into a different directory too. The Worker maps registered database aliases to the appropriate release objects and package filenames to the shared pool. Existing conventional repository names and ring hostnames can remain compatibility aliases. Candidate revision names stay immutable; ordinary stable/RC/edge database names follow their ring assignments.

Maintain an index from the public repository namespace, target architecture, and original filename to the artifact and selected signature. Include retained historical filenames so clients using an older database can still download them. If two different archives would occupy the same public filename, stop ingestion into that namespace and resolve the collision; never silently overwrite. Omarchy rebuilds should have distinct package releases. The hash pool can hold both conflicting archives while publication remains blocked.

**Three serving options.** Standard `repo-add` records a package basename in `%FILENAME%`. With a normal `Server`, pacman expects the database and package URLs under that server location. A common storage pool therefore needs either server-side mapping or a separate client package server.

| Option | How it works | Benefits | Costs and limitations |
| --- | --- | --- | --- |
| Filesystem pool with ring links | Each ring has its own databases and symlinks or hardlinks into one filesystem pool; an HTTP server follows those links. | Straightforward on an ordinary mirror host; unchanged pacman configuration. | R2 objects do not provide filesystem hardlink semantics. Uploading each linked ring tree materializes package copies again. Useful for local storage or if serving from a filesystem origin. |
| Shared R2 pool with a Worker — recommended | Existing ring hostnames resolve metadata to the selected release and package filenames to shared immutable objects. | Removes ring duplication in R2, keeps client mirror URLs, supports historical downloads, and centralizes catalogue access. | Adds routing, an artifact lookup, and cache behavior to operate and test. |
| Separate package server using pacman's `CacheServer` | Ring `Server` locations supply databases; `CacheServer` points to the shared package pool. | Native pacman separation of metadata and package downloads; can use a simple filename-addressed pool without a Worker. | Requires compatible clients and configuration rollout. Cache servers are tried first and normal servers are fallback; a metadata-only normal server cannot rescue a pool outage. Filename collisions still need a policy. |

The [pacman configuration manual](https://archlinux.org/pacman/pacman.conf.5.html) explicitly says cache servers “will not be used for database files.” This is a credible alternative if changing all clients is acceptable. Inventory supported pacman versions first.

For the Worker option, stream the archive from R2 or redirect to its canonical immutable package URL. A canonical URL/cache key allows downloads from different rings to share cached bytes. Implement and verify GET, HEAD, Range/resume, conditional requests, content lengths, signatures, and redirects. Do not load an entire package into Worker memory. Package lookup must work for retained historical releases, rather than checking only the current ring's membership.

**Where authority lives.** The authoritative answer to “what is on stable?” is the release selected by the current published ring map in R2. Resolve `current.json` to an immutable publication, find stable's release ID for the relevant ecosystem/architecture, and read that release's complete package manifest. The publication and manifest are durable records, with hashes and backups. They define live state; a draft candidate or Git commit does not change a ring.

The publisher alone activates a publication, using an expected previous generation and serialized writes. CLI and future web actions submit operations to that publisher. Record the previous publication and the actor/reason in each new publication before activation, so the durable state already contains its audit trail if the process crashes immediately after the pointer switch. The Worker reads this state to serve clients. A SQLite or D1 search index is a disposable projection of it, rather than an independent set of editable ring-membership rows.

The records have distinct roles:

| Record | Authority |
| --- | --- |
| Draft candidate or proposed Git change | What an operator wants to test or publish; no effect on a live ring. |
| Immutable candidate/release manifest | The exact repository membership of that snapshot, including archive and signature identities. |
| Activated publication's ring map | Which snapshot each ring currently serves. |
| Generated `.db`, `.files`, website JSON, and search tables | Outputs derived from the manifests and selected publication. |

**Save complete package metadata at ingestion.** Persist one immutable, schema-versioned JSON metadata record for each captured artifact/metadata revision in R2, alongside the signed archive. Release manifests reference the archive hash and the metadata hash. Different rings can reference the same record without repeating all dependency and file-list data in every ring manifest. A correction or newly captured metadata field produces a new record; existing releases retain their original references.

The record must contain the complete ordered field/value lists needed for the package's repository entries: name, version including epoch/package release, filename, architecture, sizes, checksums, package base, description, URLs, licenses, groups, build details, dependencies, optional/build/check dependencies where present, providers, conflicts, replacements, signatures, and the complete file list. Preserve supported additional fields and the distinction between `.db` and `.files` records, rather than reducing everything to the fields needed by the website. Include schema version, source metadata hashes, and extraction tool/version. Keep verification evidence tied to the archive and selected signature; cached metadata is not a substitute for signature verification or artifact retention.

For upstream packages, capture validated matching records from `.db` and `.files`. For local builds, extract and validate the records while the signed archive is already local. When upstream records are incomplete or disagree, obtain complete metadata from the verified archive or leave the record ineligible for generation; do not publish a curated `.files` database with a guessed or missing file list. Preserve original upstream metadata as provenance.

**Rebuilding the databases.** The normal publisher reads the release manifest and its referenced metadata JSON, writes the selected package entries into standard pacman database archives, compresses them, signs them, and publishes the results. It reads exactly the selected records; it never adds all packages in the pool or chooses versions by filename. Package archives need not be downloaded for this operation once their complete metadata is captured and their availability is established.

Retain the signed archives as the recovery source. If a metadata record is missing or needs repair, fetch the archive and use `repo-add --include-sigs` or equivalent extraction to recover complete package/file records. Standard `repo-add` still requires local archives. A manifest plus its full archives and detached signatures remains sufficient to reconstruct the package selection and `.files` contents even if every cached record is lost. A names-and-versions list or ordinary `.db` alone is insufficient to reconstruct the file lists.

This requires our own compatible database assembler. Validate its output against `repo-add` at the parsed record/file-list level and exercise it through pacman, including file queries and signature checks. Define deterministic ordering and archive timestamps/compression settings so repeated assembly is predictable. Reuse unchanged repository metadata and cache JSON records locally to avoid thousands of repeated R2 fetches. The expected speedup comes from avoiding package downloads, hashing, and decompression on each rebuild; reading file lists, compressing databases, and signing still take work. Benchmark actual releases before claiming a speedup figure.

Regeneration means the same package selection and file metadata, not necessarily identical compressed database bytes. Tool versions, tar timestamps, compression, and new signatures can change bytes. Preserve original generated databases for exact historical recovery. If regeneration produces different bytes, write a new immutable build/release record with fresh hashes and database signatures; do not overwrite objects under a historical identity. Package signatures remain valid because the package archives are unchanged. Recovery also requires access to an appropriate trusted database-signing key.

**Manage releases with explicit selections.** Start with a CLI and one serialized publisher running on the existing mirror machine. The publisher records immutable resolved releases and publications in R2. Git may hold proposed changes and an audit mirror, and a small SQLite catalogue may speed artifact searches; neither should be required to determine which packages a live ring contains. Package bytes stay out of Git.

A useful authoring format is a pinned base release plus explicit overrides/removals. Resolve that into a complete manifest before publication. Never leave a base as the moving name `edge`, and never store a ring as only a patch that depends on future upstream state. The published stable view still contains the complete repositories clients need.

Editing `.db` archives directly is the smallest operational starting point, but it makes reasons, review, dependency checks, and history harder to manage. Conversely, a database-backed service with an admin UI would support several operators and richer automation, but is more system than this repository currently needs. CLI + durable manifests + one publisher is a reasonable middle ground; add a web interface when it improves the operating workflow.

Illustrative commands, not existing functionality:

```text
mirror ingest arch-linux --arch x86_64
mirror diff stable rc
mirror promote --from <rc-release-id> --to stable --package chromium --plan
mirror publish --plan <validated-plan-id>
mirror rollback stable --to <retained-release-id> --plan
mirror history chromium --arch x86_64
mirror gc --dry-run
```

A plan should show package additions, changes and removals, required dependency changes, relevant reverse dependencies, signature status, package bytes already present/missing, and the target ring generation it expects. Publication rejects a plan if that target ring has changed. An hourly edge update does not invalidate a stable candidate: its source edge release is already frozen. At activation, read the latest global publication, check the target ring, preserve the latest assignments of all other rings, and conditionally replace the publication pointer. A competing write requires a fresh read and comparison; only a change to the target ring requires rebasing the candidate. Use pacman's version comparison, including epochs and package releases, rather than lexical or generic semantic version ordering.

**Test Hyprland as a named candidate.** The operational unit is a candidate: a frozen stable release with an explicit set of changes selected from a frozen edge release. It is a temporary, complete repository view with a name and an immutable revision ID. Creating it adds metadata and package references, without copying packages or changing stable/RC/edge.

For example, creating `hyprland-oob` resolves stable to release S42 and edge to E108. Adding Hyprland selects its exact artifact from E108. Dependency analysis uses S42 plus the proposed additions: it leaves already-satisfied stable dependencies alone and proposes additional changes needed from E108, accounting for version constraints, providers, conflicts, replacements, and related package groups. It must surface unresolved provider choices and affected reverse dependencies. The operator can add the known dependencies explicitly and inspect the entire change list before freezing it. Runtime/ABI testing remains necessary even if dependency metadata resolves.

Proposed CLI flow; these commands are not implemented:

```text
# Prepare a candidate; stable and edge resolve to fixed release IDs here.
mirror candidate create hyprland-oob --base stable --from edge
mirror candidate add hyprland-oob hyprland
mirror candidate plan hyprland-oob
# Add the selected dependency packages, then publish a frozen revision.
mirror candidate publish hyprland-oob
# Output: immutable candidate c123 and its repository URL.

# On the fresh stable test installation:
sudo mirror candidate install c123

# After testing the actual Hyprland session and affected functionality:
mirror candidate record-test c123 --note "Fresh stable install; session checks passed"
mirror candidate promote c123 --to stable
```

`candidate install` should configure all managed repositories to that candidate's immutable URLs and perform a normal pacman upgrade transaction on the test machine. It should show the exact planned package changes, retain a copy of the prior mirror configuration, and record the resulting installed versions and any additional repositories used. It must not enable the moving edge repositories. Candidate pinning remains visible on the test machine until explicitly changed. Returning to stable is a separate operation and may require a downgrade plan or rebuilding the disposable test installation.

On a fresh installation at S42, the managed-package changes should be the selected Hyprland group. If the installation predates S42, its upgrade also includes changes required to reach that base; the CLI must show those rather than attributing every change to the candidate. Checking that a clean installation boots and runs the new session establishes evidence for that tested environment. Record specific checks such as session startup, rendering, input, outputs, screen sharing, and relevant extensions as applicable to the intended release.

`record-test` records the operator's report and machine/package inventory against immutable revision c123; it does not claim to run or independently certify the manual tests. Any candidate change creates a new revision and does not inherit a passing test record automatically. A single successful test installation is useful evidence, with its hardware and configuration limits preserved in the record.

Promotion selects c123's exact package manifest for stable. It does not look up edge again, recalculate dependencies, or import additional newer versions. If stable is still S42, this can reuse the already-built and tested repository snapshot. If stable has advanced, publication stops with the intervening diff: rebase the candidate onto the new stable release, create a new revision, and retest the affected combination. Do not silently replace newer stable work with an old candidate base. Candidate revisions remain pinned through testing and promotion so collection cannot remove their artifacts.

**Which component does what.** The CLI, Worker, and web UI are different interfaces and execution roles, not competing authorities.

| Component | Responsibility |
| --- | --- |
| CLI | Create candidates, select package groups, show diffs, configure a test installation, record results, request promotion, and query history. Provide structured output for automation. |
| Publisher on the mirror machine | Validate exact manifests, assemble `.db`/`.files` from saved metadata, validate with Arch tooling, sign databases, upload immutable outputs, and activate a ring with concurrency checks. This is where bumping actually happens. |
| Cloudflare Worker | Serve ring and candidate metadata and route package requests to the pool. It can expose authenticated management endpoints later, but database generation/signing stays in the publisher. |
| Website | Initially display ring versions, candidate changes, test notes, history, and retained artifacts from published data. A later admin UI can invoke the same candidate/promotion operations as the CLI. |
| R2 release registry | Preserve artifacts, manifests, publications, test records, and the active pointer. It is the durable authority shared by all of the above. |

The existing mirror host is documented as Ubuntu. Run the publisher's `repo-add`, pacman checks, and signing workflow in a controlled Arch environment on that host, with the signing key available only to the publisher. Do not assume these tools already exist on the production host, and do not put the signing key in the serving Worker. A first CLI can invoke the publisher through authenticated SSH; an HTTP API or queue is an optional later transport, using the same operation semantics.

**Off-cycle Chromium releases.** Construct `stable-new` from the currently published stable release, replace Chromium with an exact candidate artifact, and examine the resulting repository set. Test dependency resolution, shared-library compatibility, affected reverse dependencies, and install/upgrade/runtime behavior in a stable environment. Metadata dependency resolution alone cannot establish ABI compatibility.

If the upstream build is compatible, publish that one-package change. If it needs a bounded compatible package group, validate and publish the group together. If it requires a broad library transition, rebuild/backport Chromium against stable's libraries with a distinct package release and the Omarchy signing key, or accelerate the coherent RC release. A new index cannot solve binary incompatibility.

Arch's [maintenance guidance](https://wiki.archlinux.org/title/System_maintenance#Partial_upgrades_are_unsupported) explains why selectively mixing packages from different rolling-release states can break other packages using the same libraries. Curating stable this way makes Omarchy responsible for validating that curated combination. Clients should continue upgrading normally against their complete ring repositories.

Reconcile a stable hotfix into the next RC promotion. A full RC replacement must detect if it would remove a stable hotfix or lower a package version, and require an explicit resolution. Otherwise an older RC can silently undo the off-cycle work.

Avoid making every hotfix a separate client-side repository. Pacman gives earlier repositories precedence for duplicate names regardless of version; an old overlay entry can keep shadowing a later main-repository version. Publisher-side overrides compiled into the normal databases are easier to retire correctly.

**Build, validate, then activate.** Ingestion stores package bytes and signatures once, checks hashes and authenticity using the appropriate trusted keyring, and captures upstream metadata as provenance. A missing or invalid signature should quarantine the artifact from signed-ring publication. Signature preservation alone is not verification.

For unchanged upstream repository snapshots, retain the original database bytes and valid signatures. For a curated snapshot, assemble both databases from the exact manifest and saved complete metadata. During development, `repo-add --include-sigs` and `repo-remove` against private working databases provide a reference implementation and recovery route using local archives. Do not use their package-deleting `--remove` option on the pool. Publish matching `.files` metadata from the same selection.

Modifying a database invalidates any signature on that database. Sign curated databases with an Omarchy repository key, preserve upstream signatures on unmodified package archives, and distribute trust in the repository key to clients before enabling those signatures. Even an optional database-signature policy can reject a present signature from an untrusted key. Inspect actual client `SigLevel` settings during migration.

The activation sequence should be:

1. Resolve the full release and validate it; obtain a publisher lock or equivalent serialized authority.
2. Upload missing packages and signatures, then verify every manifest reference is readable and matches the recorded artifact.
3. Upload immutable databases, their signatures, manifests, and the website export. Validate with pacman through the intended serving path.
4. Switch the single publication pointer only if its expected prior generation still matches; record the successful activation.
5. Apply retention separately after the release is available. A failed build/upload never advances the pointer or freshness marker.

R2 provides [strongly consistent object operations](https://developers.cloudflare.com/r2/reference/consistency/), but separate objects are not a multi-object transaction. Cached custom-domain responses can remain stale, including cached 404s. Read the mutable pointer through a strongly consistent path, bypass caching for mutable metadata aliases initially, and cache immutable package/release URLs aggressively. Serialize publishers so a last-writer-wins pointer update cannot lose another promotion.

There is a remaining client boundary: switching one server pointer does not pin pacman's several requests. A client can request `core.db` before a switch and `extra.db` or a detached database signature after it. Retaining files prevents old package URLs from disappearing, but cannot make mixed database generations coherent. For a strict release guarantee, the Omarchy update flow should resolve one release ID and use its immutable URLs for the whole operation, including all repository databases and signatures. Keep current ring aliases for compatibility; document their ordinary mirror transition behavior and verify failure/retry handling. This distinction should be part of the implementation decision, not hidden behind the word “atomic.”

**Rollback and retention.** Retaining an old archive makes a rollback possible to investigate. It does not establish that the old package still works with today's libraries, configuration, or application data.

| Website/CLI status | Meaning |
| --- | --- |
| Artifact retained | Package and signature exist, with a last verification time. |
| Release restorable | All artifacts, metadata, and trust requirements for the old release are available. |
| Rollback tested | A specified transition was tested on a specified environment/date. State any application-data limitations. |

Restoring a ring pointer changes what the repository offers. Normal `pacman -Syu` does not automatically downgrade installed newer packages. Client rollback needs an explicit, validated downgrade plan: a coordinated transaction for selected packages or a deliberately chosen full-release downgrade. Pacman's doubled sysupgrade option enables downgrades, but it is not an automatic recovery policy. Packages removed from repositories may remain installed, and application migrations or user data may also need separate recovery.

Garbage collection should retain everything referenced by current rings, retained release snapshots, manually pinned packages, and in-progress publications. Retain recently retired artifacts through a defined client-cache/download grace period. Coordinate collection with the publisher and use a mark/recheck/delete process so a concurrent promotion cannot acquire a package being deleted.

A starting policy could retain six stable releases, a bounded window of RC releases, and recent edge snapshots, with explicit long-term pins. Measure unique artifact churn before selecting durations. Preserving every hourly edge release indefinitely also preserves every artifact referenced by them; keeping just a fixed number of old package versions can conversely break an older pinned release.

Storage becomes the size of the union of retained artifacts plus metadata. The theoretical saving from collapsing three identical current rings is about two thirds of their package storage, before history. Real savings depend on overlap, local working storage, and retained historical versions. A single logical pool still needs backups for its manifests, catalogue history, and irreplaceable Omarchy builds.

**Publish the package catalogue with each release.** Generate a versioned JSON export from the same resolved manifests and activation record used by pacman. Do not infer ring membership from objects present in the pool or from the latest upstream database. A package can exist in storage while being in no current ring.

The first website view can be a searchable comparison table with repository and architecture filters:

| Package | Architecture | Edge | RC | Stable | Retained versions |
| --- | --- | --- | --- | --- | --- |
| chromium | x86_64 | C | B | B — off-cycle release | A, B, C |
| example-library | x86_64 | L3 | L2 | L1 | L1, L2, L3 |

These are symbolic examples, not a live inventory. A package page should show exact versions and hashes, origin, current ring membership, promotion dates/reasons, source/build links when known, signature verification, download links, and rollback status. Mark missing upstream provenance as unknown rather than inventing it. Compare published version values using pacman's ordering.

Suggested read-only endpoints are `/api/v1/rings.json`, `/api/v1/packages/<ecosystem>/<arch>.json`, and `/api/v1/packages/<ecosystem>/<arch>/<name>.json`. For larger inventories, shard by repository or package. Include schema version, publication ID, the ring release IDs, published time, upstream snapshot time, and latest ingestion-check time; they answer different freshness questions.

The website should resolve one publication ID and load data pinned to it. Display the “as of” time so browser/CDN caching is visible. A static website can consume this export without an admin service or live relational database. Retention changes also produce a catalogue publication so historical availability stays accurate. An admin UI can later call the same plan/publish operations.

**Migration and evidence.** Begin by inventorying the existing three rings from their databases and bucket objects, hashing the package union, calculating overlap, and identifying filename conflicts and signature gaps. Import those exact current rings as initial manifests; preserve them as rollback anchors. Reuse the ARM ingestion principles while treating known degraded ARM metadata as unsuitable for activation.

Build the shared pool and publisher alongside the existing destinations. Compare every imported ring's package selection against its current `.db`, then validate the Worker or `CacheServer` route with isolated clients. Exercise large/resumed downloads, signatures, concurrent publication, stale databases, cross-repository transitions, and collection races. Trial edge, then RC and an off-cycle release, before routing stable. Retire duplicate buckets only after the agreed retention window and successful recovery checks.

A local experiment with pacman 7.1.0 and two real signed ARM archives demonstrated that stable and RC databases can select different sets from one package directory, that both can reference the same package, and that `repo-remove` can remove RC membership while leaving both archives intact. Pacman listed both repositories and produced the expected ring-relative package URLs. Archive SHA-256 and embedded signature bytes matched the original files.

The experiment also found that the installed `repo-add` defaults to omitting embedded signatures unless `--include-sigs` is supplied, despite the general manual text describing automatic inclusion. Use the explicit flag and inspect generated records. The experiment used isolated configuration/database paths and signature checking disabled for metadata queries; it did not install packages, verify cryptographic authenticity, exercise HTTP/R2, or change the mirror scripts. Its scope is proof of the selection model, not production validation. See the [repo-add manual](https://archlinux.org/pacman/repo-add.8.html) and [pacman upgrade semantics](https://archlinux.org/pacman/pacman.8.html).

The recommended first implementation is the R2 pool and Worker compatibility layer, a CLI for named candidates, one publisher on the mirror machine, and a static website export. Published manifests and the activated ring map in R2 are authoritative. Keep the monthly RC/stable cadence, adding tested package-group promotions when needed. Pin candidate installations to immutable release URLs from the start; decide separately when the ordinary Omarchy updater should use the same mechanism for strict consistency throughout each update.

The [implementation assessment](https://a.mosaic.heyoodle.com/a/repo-omarchy-mirror/shared-package-pool-implementation/) maps this design onto the existing scripts, hourly ingestion, timers, client configuration, and a staged rollout.
