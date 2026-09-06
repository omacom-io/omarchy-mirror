# Shared package pool: deploy and try it

This branch provides a Python publisher, an Arch container, and a Cloudflare Worker. Packages are uploaded once by hash. Immutable JSON manifests select the packages in each release; signed `.db` and `.files` databases are generated from cached metadata. Edge, RC, stable, and candidate databases share the package pool. The Worker also serves a searchable ring comparison website.

Start with the signed two-package demo below in a new R2 bucket. It proves uploads, dependency selection, pacman downloads, signatures, and website inventory without importing all of Arch. This is an initial deployment implementation: production buckets, DNS, client channel scripts, and the separate package builder have not been migrated.

## 1. Prepare the publisher on the new server

Use an x86_64 Linux server with Docker, Git, and jq. Ubuntu can host the publisher because pacman runs inside the container. Builds can run on the same physical server in their existing isolated build environment; submit their signed outputs to this publisher. Package serving runs in Cloudflare and does not depend on this server being online.

```sh
git clone --branch feat/shared-package-pool https://github.com/omacom/omarchy-mirror.git
sudo mv omarchy-mirror /opt/omarchy-mirror
cd /opt/omarchy-mirror
sudo docker build -t omarchy-mirror-publisher:preview .

sudo install -d -m 755 /etc/omarchy-mirror
sudo install -d -o 1000 -g 1000 -m 700 /var/lib/omarchy-mirror
sudo install -d -o 1000 -g 1000 -m 700 \
  /var/lib/omarchy-mirror/tmp /var/lib/omarchy-mirror/cache \
  /var/lib/omarchy-mirror/gnupg /var/lib/omarchy-mirror/incoming
sudo install -m 644 deploy/config.example.json /etc/omarchy-mirror/config.json
sudo install -m 600 deploy/credentials.env.example /etc/omarchy-mirror/credentials.env

# Convenience for the commands below, in a Bash shell:
set -euo pipefail
pool() { sudo /opt/omarchy-mirror/deploy/pool "$@"; }
```

The container runs as UID/GID 1000; select an appropriate host owner if 1000 is unsuitable. Set `MIRROR_PUBLISHER_UID` / `MIRROR_PUBLISHER_GID` consistently in the wrapper environment, including the systemd service, when using different IDs. The wrapper mounts only configuration and `/var/lib/omarchy-mirror`. Put incoming builds under that state directory. Package downloads use its `tmp/`, with up to four concurrent archives by default; no complete upstream tree is required. Full `.files` parsing needs substantially more RAM than the tiny demo: start with 8 GiB and measure a full import before setting limits.

## 2. Create an isolated R2 destination and deploy the Worker

On a machine with Node.js 24+ and access to your Cloudflare account:

```sh
cd /opt/omarchy-mirror/worker
npm ci
npx wrangler login
npx wrangler r2 bucket create omarchy-mirror-pool-preview
npm run check
npm test
npx wrangler deploy --dry-run
npx wrangler deploy
```

`worker/wrangler.jsonc` defaults to that preview bucket and the `preview` prefix. Use the deployed `workers.dev` URL as `base_url` in the publisher config. A custom domain can be added later. Keep the bucket private; the Worker exposes only package/database routes and public inventory, with no mutation endpoint or signing key.

Create R2 S3 credentials scoped to object read/write in this bucket, and put their access key and secret into `/etc/omarchy-mirror/credentials.env`. This is separate from Wrangler's deployment authentication. Set these values in `/etc/omarchy-mirror/config.json`:

| Setting | Value |
| --- | --- |
| `store` | `s3://omarchy-mirror-pool-preview/preview` |
| `endpoint` | Your account's R2 S3 endpoint, including any jurisdiction-specific hostname |
| `base_url` | The deployed Worker URL, with no additional path |
| `cache` | `/var/lib/omarchy-mirror/cache` |

The publisher bucket/prefix must match the Worker's `POOL` binding / `STORE_PREFIX`. Do not configure object expiration or point any legacy mirror upload/prune job at this destination. No R2 upload occurs until the commands below run.

## 3. Upload the tiny signed demo

Generate disposable signed packages and a local demonstration registry on the server:

```sh
sudo env MIRROR_ENTRYPOINT=python /opt/omarchy-mirror/deploy/pool \
  -m mirror.demo --output /var/lib/omarchy-mirror/demo
```

The output includes `signing_fingerprint`. For this demo, edit these three entries in the publisher config:

```json
{
  "keyrings": ["/var/lib/omarchy-mirror/demo/demo-keyring.gpg"],
  "sign_key": "THE_DEMO_SIGNING_FINGERPRINT",
  "gnupghome": "/var/lib/omarchy-mirror/demo/gnupg"
}
```

Merge those entries into the existing config, keeping its R2 settings. The generated `demo/mirror.json` describes the local store; it is not the R2 config. The demo key is unprotected disposable test material and must not become a production signing identity.

```sh
pool doctor --write-check
pool source import demo --layout flat --repo extra \
  --location /var/lib/omarchy-mirror/demo/stable-source --ring stable
pool source import demo --layout flat --repo extra \
  --location /var/lib/omarchy-mirror/demo/edge-source --ring edge

candidate=$(pool candidate create app-oob --base stable --from edge \
  --package extra/pool-demo-app --package extra/pool-demo-lib --quiet | jq -r .release)
published=$(pool candidate publish "$candidate" --quiet)
check_id=$(jq -r .check <<< "$published")
suffix=$(jq -r .suffix <<< "$published")
```

Now open the Worker URL: `pool-demo-app` and `pool-demo-lib` should show version `1-1` on stable and `2-1` on edge. Try omitting the library in a separate candidate: publication fails dependency resolution because app v2 requires lib v2. No package installation happens during publication.

Verify the R2 downloads with the actual pacman client, substituting the deployed URL:

```sh
sudo env MIRROR_ENTRYPOINT=python /opt/omarchy-mirror/deploy/pool \
  /opt/source/scripts/smoke-pacman.py --demo /var/lib/omarchy-mirror/demo \
  --url https://YOUR_WORKER_URL --suffix "$suffix"
```

The test uses isolated pacman state, requires trusted package and database signatures, downloads both v2 archives, and queries the file database. It installs nothing. After it passes, record that actual result and promote the demo:

```sh
test_id=$(pool candidate record-test "$candidate" \
  --note "Signed demo: both v2 downloads and file query passed; no runtime installation tested" \
  --quiet | jq -r .test)
pool candidate promote "$candidate" --to stable --check "$check_id" --test "$test_id" \
  --reason "Verified signed pool demonstration"
```

Reload the website; both rings should now show v2. Stable promotion references existing archives. Package history and retention are available with `pool package history pool-demo-app` and `pool gc --dry-run`.

## 4. Import real sources and keep edge current

Use a separate empty prefix/bucket and corresponding Worker configuration for real packages, rather than mixing the demo's `extra` repository into Arch. Configure a dedicated repository signing key and the trusted public keyrings for each imported source. The image contains `/usr/share/pacman/keyrings/archlinux.gpg`; supply Omarchy and ARM public keyrings separately when needed. A keyring is an explicit signer allowlist. Keep it current: newly revoked keys must be removed/revoked in the configured public keyring.

Provision the database signing private key in `/var/lib/omarchy-mirror/gnupg` (owner matching the container UID, directory mode 700), put its full fingerprint in `sign_key`, and export its public key to `/etc/omarchy-mirror/publisher.gpg`. Configure noninteractive signing access for the timer; do not put a passphrase or secret key in JSON or argv. Test signing with an explicit release build before enabling automation. Back up this key separately from R2.

First estimate the smallest complete repository, then upload it:

```sh
pool doctor --write-check
pool source import arch --location https://geo.mirror.pkgbuild.com \
  --repo core --ring edge --dry-run
pool source import arch --location https://geo.mirror.pkgbuild.com \
  --repo core --ring edge

# Complete the Arch selection before testing a real system:
pool source import arch --location https://geo.mirror.pkgbuild.com \
  --repo core --repo extra --repo multilib --ring edge
```

Imports require matching complete `.db` and `.files` sets and verify package signatures and checksums. Sources changing during acquisition fail without activation; retry after upstream settles. Successful uploads survive failure and are reused. Unchanged packages are not redownloaded while the configured trust keyring remains unchanged. A keyring change triggers reverification on the next import, which currently downloads those packages from the source again. Existing releases retain the verification evidence captured when imported.

HTTP imports download archives into temporary files and upload them directly to R2. An existing rsync stage also works:

```sh
pool source import arch --location /var/lib/omarchy-mirror/incoming/arch \
  --repo core --repo extra --repo multilib --ring edge
```

Keep a local source tree unchanged throughout import; coordinate any rsync writer with the import job. The new importer does not acquire the old shell scripts' stage lock. Upstream database signatures, when supplied, are verified; unsigned source databases remain a trust boundary. Captured dependency/file metadata comes from those source databases and is not independently rederived from each signed archive's `.PKGINFO`. Use sources you trust. Curated outputs are always signed by the configured repository signer.

Add the existing Omarchy repository to edge using its actual flat repository endpoint once confirmed:

```sh
pool source import omarchy --layout flat --repo omarchy \
  --location https://pkgs.omarchy.org/edge/x86_64 --ring edge
```

That endpoint must provide `omarchy.db`, `omarchy.files`, and all referenced signed packages; its producer is outside this checkout. If it cannot supply complete metadata, use the local build adapter:

```sh
release=$(pool package import /var/lib/omarchy-mirror/incoming/example-1-1-x86_64.pkg.tar.zst \
  --repo omarchy --source omarchy --base edge --quiet | jq -r .release)
pool release build "$release"
pool release show "$release" # .base is the expected edge ID
pool ring set edge --release "$release" --expect EXPECTED_EDGE_ID --reason "Import signed build"
```

Each archive needs an adjacent binary `.sig`. `repo-add` is used once for initial extraction; later database generation uses JSON. Local imports merge packages into the selected base repository; complete source imports replace the selected repositories and retain other repositories from the base. Source labels cannot change silently. Same public filename with different archive/signature content is rejected; rebuild with a new package version/release.

ARM imports use `--layout arm --arch aarch64 --repo core --repo extra --repo alarm --repo aur` and an ARM mirror root. They are subject to stricter matching `.db`/`.files` requirements than the old staging recovery mode. Metadata/dependency validation can run on x86_64; runtime testing still requires the target architecture.

After a complete successful manual Arch import, enable the independent hourly service:

```sh
sudo install -m 644 /opt/omarchy-mirror/deploy/edge.env.example /etc/omarchy-mirror/edge.env
sudo install -m 644 /opt/omarchy-mirror/deploy/omarchy-pool-edge.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now omarchy-pool-edge.timer
sudo journalctl -u omarchy-pool-edge.service
```

This timer imports only Arch into edge and preserves edge's Omarchy selection. Schedule the custom producer separately if desired. It does not schedule stable or RC changes. Systemd prevents this unit overlapping itself; another publisher changing edge concurrently causes a safe conflict/retry, while stable promotions preserve concurrent edge changes. The initial full import may be slow. Each no-op still reads source metadata and checks retained object availability. Full-scale R2 timing/costs have not been benchmarked.

## 5. Freeze stable and test an out-of-band update

Initialize stable/RC from a chosen complete edge release with `ring set --expect empty`. To preserve today's stable baseline, import its actual frozen source tree into stable instead. Do not substitute current edge for an existing older stable selection during migration.

```sh
pool ring list
pool ring set stable --release SELECTED_EDGE_ID --expect empty --reason "Initial stable baseline"
pool ring set rc --release SELECTED_EDGE_ID --expect empty --reason "Freeze RC"

pool candidate create hyprland-oob --base stable --from edge \
  --package extra/hyprland --package extra/DEPENDENCY_NAME
pool candidate publish CANDIDATE_ID
pool release config CANDIDATE_ID --output /var/lib/omarchy-mirror/hyprland-candidate.conf
```

Use the actual repository/package names and all required dependencies or related rebuilds. `candidate publish` checks every added/changed package with pacman and can include additional targets using `--check-package`. It resolves against an **empty installed database**, so this is neither an ABI/reverse-dependency audit nor a simulation of every installed stable package. It will not choose the correct package group for you. An incomplete group fails; create a new candidate with the revised explicit selection. Removal-only candidates need additional check targets. Promotion verifies the report's exact release, build, and changed-package coverage.

Exported configurations pin **all repositories present in the manifest**, including Omarchy if imported. They contain entries like:

```ini
[extra-hyprland-oob-RELEASE_PREFIX-BUILD_PREFIX]
Server = https://YOUR_WORKER_URL/extra/os/$arch
```

The database basename changes; the directory does not. Do not use an upstream mirrorlist containing `$repo` for renamed sections. Initial default precedence is core, extra, multilib, then other repositories; later imports preserve existing precedence and append new repositories. Set `--repo-order` on `source import` or `candidate create` to match the real installation, including every repository in the resulting release. The order is part of the immutable manifest and is used by both dependency checks and exported configs. Changing order requires a new release/candidate. The helper exports a file and never edits the host pacman configuration. Preserve relevant local options and audit Omarchy channel scripts that refer to literal repository names.

On a fresh stable test machine, verify and trust the repository signer's public fingerprint (`pacman-key --add` / `--lsign-key`), install the exported config at a test path, and run a full `pacman --config /path/to/candidate.conf -Syu`. Test the actual desktop/upgrade, including affected existing packages and hardware. Keep that same config for subsequent transactions. Record `pacman -Q` and the observed result:

```sh
pool candidate record-test CANDIDATE_ID --note "ACTUAL_ENVIRONMENT_AND_OBSERVED_RESULTS" \
  --inventory /var/lib/omarchy-mirror/test-inventory.txt
pool candidate promote CANDIDATE_ID --to stable --check CHECK_ID --test TEST_ID \
  --reason "Validated Hyprland out-of-band update"
```

The inventory must be copied back to the publisher's mounted state directory. Test records are operator reports; the CLI does not execute or independently certify manual tests. Stable must still match the candidate's frozen base. If stable moved, create and test a new candidate. Edge can continue moving throughout this process.

For full RC-to-stable releases, use the exact tested release ID with `ring set`, an expected stable ID, and a reason. `ring set` is the explicit operator override and does not enforce candidate test reports.

## Authority, recovery, and serving

All paths below are under the configured bucket prefix:

| Object | Role |
| --- | --- |
| `pool/<sha256>/<filename>` | Immutable original signed archive bytes |
| `signatures/<sha256>.sig` | Original package or generated database signature |
| `metadata/<sha256>.json` | Complete captured pacman records, file lists, checksum/signature/verification information |
| `releases/<sha256>.json` | Exact package/metadata selections and repository precedence, with base/source IDs |
| `builds/<sha256>.json` | Exact signed `.db`/`.files` outputs and website catalogue for a release |
| `ready/<release>.json` | Latest completed build for future publication; does not move a ring |
| `publications/<sha256>.json` | Immutable ring assignments, previous publication, reason and timestamp |
| `current.json` | Conditional-update pointer to the active publication: the authority for current rings |
| `routes/`, `filenames/`, `archive-signatures/` | Serving indexes and immutable candidate aliases |
| `checks/`, `tests/` | Dependency reports and operator test evidence |

R2 holds the authoritative registry; the local JSON cache is disposable. Back up the entire registry/pool plus signing keys separately. A lost publisher can resume from the R2 config and signing key. `release build ID --rebuild` recreates databases from stored metadata without reading archive bodies. Missing generated databases can be restored at the same deterministic object key. Rebuilt signatures/builds get new immutable candidate aliases; export the new configuration and rerun checks before promotion. Existing aliases remain pinned to their old build. Restore missing historical signatures from backup if those old URLs must remain usable. Rebuilding alone never changes an active ring; activate the rebuilt release explicitly when ready.

Normal publisher writes never overwrite immutable content with different bytes. Corrupt objects require a deliberate backup restore; `--rebuild` does not silently replace corrupt existing objects. Archive readiness uses size/SHA metadata from trusted storage HEAD responses; it is not a periodic full-byte R2 scrub. Keep backups and storage credentials protected.

`package history NAME` lists retained releases containing each version. `release diff ID --from OLD_ID` shows the selection changes. To roll a ring back, build the old release and use `ring set --expect CURRENT_ID --allow-downgrade`. Clients already running newer versions do **not** automatically downgrade with `-Syu`; returning a test machine to stable may require a reviewed `-Syuu` transaction or reinstall. Retaining an archive makes a version available, not necessarily compatible with arbitrary current dependencies. No automated deletion is implemented; `gc --dry-run` reports only. Budget for historical growth.

The Worker supports ordinary `core.db` URLs (default ring `edge`), explicit `core-stable.db` ring aliases, immutable candidate aliases, `.files`, signatures, HEAD, single Range/resume, and conditional requests. `HOST_RINGS` can map existing hostnames to stable/RC/edge later. Package requests redirect to one immutable pool URL; adjacent canonical `.sig` requests also work. Mutable ring/database/API routes use `no-store`; immutable candidate and archive routes are cacheable. A publication swap is atomic for the ring map, but separate HTTP requests for moving ring databases can straddle a promotion. Use immutable configurations for repeatable tests and retry ordinary client syncs after a transition.

The website is read-only and reloads the latest ring map; every catalogue in that view is pinned to its build. Integrate an existing website using:

```text
GET /api/v1/rings.json
GET /api/v1/builds/<build-id>/packages.json
GET /api/v1/rings/<arch>/<ring>/packages.json
```

Candidate inventories can be queried using their published build IDs, but candidates are not automatically added as website ring columns. No management UI, remote mutation API, automatic ABI/dependency selection, retention deletion, or production cutover is included in this version.

## Local development and checks

On an Arch development system with Python 3.11+, pacman/repo-add/vercmp, GnuPG, fakeroot, and Node.js 24+:

```sh
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m mirror.demo --output /tmp/pool-demo
cd worker
npm ci
npm run check
npm test
npx wrangler deploy --dry-run --outdir dist
node scripts/local-demo.mjs /tmp/pool-demo/store 8787
```

In another terminal, run `.venv/bin/python scripts/smoke-pacman.py --demo /tmp/pool-demo` and open `http://127.0.0.1:8787`. The seed script reads each object into memory and is intended only for the tiny demo. Unit/integration tests cover the real pacman tools, metadata-only regeneration, signatures, dependency failures, publication races, stale report rejection, and the S3 contract using Moto. Worker tests run in Cloudflare's local R2/runtime emulation. CI also builds the publisher image and runs the signed Worker download smoke test; live R2 still needs the deployment trial above.
