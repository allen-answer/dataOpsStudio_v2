# Desktop packaging

DataOpsStudio maintains one Tauri v2 shell in `desktop/`, one cross-platform runtime contract in
`bundle-scripts/`, and platform builders in `tools/package/`. The builders package source, the built
Vue application, Python 3.12, uv, PostgreSQL 16 and hash-locked wheels; they never package instance
state or credentials.

The checked-in `Cargo.lock` and `uv.lock` are part of the reproducibility contract. Python, uv and
PostgreSQL archives downloaded by either builder have explicit SHA-256 pins in the builder source.
Python wheels are selected from `uv.lock` and downloaded/installed with `--require-hashes`.

Generated runtimes, wheels, staging trees, `desktop/bundle/`, Rust `target/`, `.deb` files and `.zip`
files belong only below the ignored output directories. Do not add them to git.

## Windows 10 x64

Build on Windows 10 x64 (or a compatible newer Windows host) with:

- Python 3.12 and uv;
- Node.js/npm matching the frontend lockfile;
- stable Rust with the MSVC x86_64 toolchain and Visual Studio C++ build tools;
- network access to the SHA-256-pinned upstream runtime archives and the locked Python package
  index during the build.

From the repository root:

```powershell
uv run python tools/package/build_win10_bundle.py --gui
```

The builder records the full Git commit automatically. When building from an exported source
archive without `.git`, pass the trusted full object id with `--build-commit <40-or-64-hex>`.

The default GUI command builds the Vue frontend and the shared Tauri shell, assembles the offline
runtime, and writes these ignored artifacts:

```text
dist-bundle/dataops-studio-<version>-win10-x64-gui.zip
dist-bundle/dataops-studio-<version>-win10-x64-gui.MANIFEST.txt
```

The frontend is always rebuilt with `npm ci` and `npm run build`; release identity must never point
at an ignored or stale `frontend/dist`. The existing script-only offline and online packages remain
available by omitting `--gui` and by using `--mode online`, respectively. A GUI online package can
be built with `--mode online --gui`.

The target needs the Microsoft WebView2 runtime. On first launch `DataOpsStudio.exe` shows the admin
password form, injects `DATAOPS_ADMIN_PASSWORD` only into `start.ps1`, and polls
`http://127.0.0.1:8020/healthz` for at most 900 seconds. Closing the window asks `stop.ps1` to stop
gracefully, waits at most 45 seconds, then kills the owned process tree if needed.

The verified Windows GUI baseline is exactly 198,142,107 bytes (reported as 189 MiB). The builder
fails if the offline GUI zip grows beyond that value. Package size may decrease, not increase.

## Linux Mint 21.3 x86_64

Build on Linux Mint 21.3 x86_64, or another x86_64 host with glibc no newer than 2.35. Building on a
newer glibc host is rejected so the resulting binary does not silently lose Mint 21.3 compatibility.
Install the normal Tauri v2 Linux prerequisites, including a C/C++ toolchain, pkg-config,
webkit2gtk-4.1 development files, OpenSSL development files, AppIndicator/Ayatana development
files, librsvg development files, Rust/Cargo, the Tauri v2 Cargo CLI, Node.js/npm, Python 3.12 and
uv. PostgreSQL is compiled from the pinned official source archive, so `make`, `tar` and `bzip2`
must also be available.

From the repository root:

```bash
uv run python tools/package/build_mint_bundle.py
```

The builder records the full Git commit automatically. When building from an exported source
archive without `.git`, pass the trusted full object id with `--build-commit <40-or-64-hex>`.

The builder creates ignored cache/staging data below `dist-mint/` and `desktop/bundle/`, invokes
`cargo tauri build --bundles deb`, and writes:

```text
dist-mint/dataops-studio-<version>-mint21.3-amd64-offline.deb
dist-mint/dataops-studio-<version>-mint21.3-amd64-offline.deb.MANIFEST.txt
```

After Tauri creates the Debian archive, the builder deterministically recompresses its control and
data members with gzip level 9 and zero timestamps. Installed bytes are unchanged; the stable,
maximum compression keeps rebuilds comparable against the no-growth package-size gate.

The frontend is always rebuilt from the current source and lockfile. The `.deb` keeps immutable
resources under the application install directory and stores the generated virtual environment and
instance home under `${XDG_DATA_HOME:-$HOME/.local/share}/dataops-studio/` by default.

The verified Mint baseline is exactly 180,636,606 bytes (reported as 172.27 MiB). The builder fails
if the `.deb` grows beyond that value. Package size may decrease, not increase.

## Shared runtime contract

Both script families default to API port `8020`, PostgreSQL port `15432`, and admin user `admin`.
Both honor `DATAOPS_HOME`, force `UV_OFFLINE`, `UV_NO_SYNC` and `UV_FROZEN`, and are idempotent.
Before creating the virtual environment, the Mint installer calculates the UTF-8 byte length of
`<DATAOPS_HOME>/data/pg-socket/.s.PGSQL.15432` and rejects paths longer than 107 bytes. Windows
PostgreSQL uses loopback TCP only; its launcher omits the Unix-only `-k` option.

Both builders validate the package version, full Git object id and platform image version, then
inject the three non-secret `DATAOPS_BUILD_*` exports into the existing shared startup script.
The checked-in marker never ships in a built package. A dirty Git worktree, a commit mismatch, an
unknown/unsafe value, or an archive build without explicit `--build-commit` fails before assembly
instead of producing a package that renders `VUNKNOWN` or misstates its source.

The desktop shell and scripts must preserve the following behaviors together:

- readiness is the `/healthz` HTTP status, not merely a listening process;
- first-run readiness may take up to 900 seconds;
- the first-run password exists only in the child process environment;
- window close performs graceful stop, bounded wait and platform-specific force-kill fallback.

## Signed payload updates (stage 1)

Stage 1 provides a platform-neutral, offline-verifiable update seam for the high-frequency
application payload. It intentionally does **not** download from a production endpoint and does not
embed a release public key or private key. Online discovery, the Tauri import/check-update UI and
health-probe orchestration are later stages after the owner chooses the endpoint and trust-key
distribution policy.

Every newly built full desktop package contains two non-secret files:

```text
runtime/update-compatibility.json  exact runtime/non-payload compatibility baseline
runtime/build-identity.json        base full-package version, commit and image identity
```

The compatibility baseline binds the platform, Python/uv/PostgreSQL artifacts, frozen dependency
set, Tauri source, bundle scripts, migrations, package tooling and the stable updater. A payload
release is allowed only when its freshly exported requirements and all non-payload fingerprints
equal that base document. A wheel/dependency, PostgreSQL, Python/uv, Tauri, migration, startup
contract or updater change fails closed with an instruction to ship a full package.

The payload builder has a two-step release interface so private signing material never enters the
repository or the builder process. First prepare `app/`, a freshly built `frontend/dist/`, and the
canonical manifest using the exact compatibility document from the intended base full package:

```bash
uv run python tools/package/build_payload_update.py prepare \
  --platform <win10-x64-or-mint21.3-x86_64> \
  --base-compatibility <base-package>/runtime/update-compatibility.json
```

The release signer signs the emitted `manifest.json` bytes outside the repository and writes only
the base64 detached Ed25519 signature to a file. Finalization attaches that signature and its
owner-assigned key id; it never accepts a private key:

```bash
uv run python tools/package/build_payload_update.py finalize \
  --work-dir <prepared-work-dir> \
  --key-id <owner-assigned-key-id> \
  --signature-file <base64-detached-signature-file> \
  --output <release>.dupdate
```

The trust store is owner-provisioned JSON; public keys are raw 32-byte Ed25519 keys encoded as
strict base64. The repository intentionally contains no default release key:

```json
{
  "schema_version": 1,
  "keys": [
    {
      "key_id": "<owner-assigned-key-id>",
      "algorithm": "ed25519",
      "public_key": "<base64-raw-ed25519-public-key>"
    }
  ]
}
```

On an installed package, `python -m updater` is the stable interface used by scripts and, in the
next stage, by Tauri. `install` requires an owner-provisioned trust-store path, verifies the manifest
signature before trusting hashes, verifies every file while extracting, then atomically selects the
new payload under `DATAOPS_STATE_ROOT/updates/versions/`. The caller starts the selected version and
reports the real health result with `complete --result healthy|failed`; `failed` automatically
restores the previous pointer, while `healthy` retains one explicit manual rollback. `home/`, the PG
data directory, bootstrap secrets, the venv and the read-only base runtime are never moved.

This pointer design is also the Linux `.deb` path: `/usr/lib` remains immutable and updates live in
the normal user's state directory. The selected payload receives the signed release version/commit
through `DATAOPS_BUILD_*`, so `/api/version` continues to identify the code actually running.

The current offline acceptance sequence is deliberately explicit:

Run these commands with the full package root as the current directory so its stable `updater/`
module is the one imported:

```text
<venv-python> -m updater install --bundle-root <bundle> --state-root <state> \
  --package <release>.dupdate --trust-store <trusted-update-keys.json>
<start the application and probe /healthz>
<venv-python> -m updater complete --bundle-root <bundle> --state-root <state> \
  --result healthy
```

Use `--result failed` instead of `healthy` when startup or health verification fails; that command
atomically restores the previous pointer. After a healthy completion, `python -m updater rollback`
consumes the one retained manual rollback.

Stage 1 is independently testable through the builder and updater CLIs, but it is not yet a
one-click in-app feature. Do not publish a payload until the owner has supplied the trust-store/key
rotation policy and the release coordinator has exercised stop → install → start → health →
complete plus forced-failure rollback on each target OS.

## Verification expectations

For a release candidate, record the builder command, full build commit, generated manifest,
SHA-256, exact byte size, `/api/version` response, first-run `/healthz` result, GUI launch result,
and graceful-close residue check on the target OS.
Static checks or a cross-platform Rust compile do not replace that target smoke test. If a platform
build or smoke test was not run, report it explicitly as unverified.
