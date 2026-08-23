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

The default GUI command builds the Vue frontend and the shared Tauri shell, assembles the offline
runtime, and writes these ignored artifacts:

```text
dist-bundle/dataops-studio-<version>-win10-x64-gui.zip
dist-bundle/dataops-studio-<version>-win10-x64-gui.MANIFEST.txt
```

Use `--skip-frontend-build` only when a verified `frontend/dist/index.html` already exists. The
existing script-only offline and online packages remain available by omitting `--gui` and by using
`--mode online`, respectively. A GUI online package can be built with `--mode online --gui`.

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

The builder creates ignored cache/staging data below `dist-mint/` and `desktop/bundle/`, invokes
`cargo tauri build --bundles deb`, and writes:

```text
dist-mint/dataops-studio-<version>-mint21.3-amd64-offline.deb
dist-mint/dataops-studio-<version>-mint21.3-amd64-offline.deb.MANIFEST.txt
```

Use `--skip-frontend-build` only with an already verified frontend build. The `.deb` keeps immutable
resources under the application install directory and stores the generated virtual environment and
instance home under `${XDG_DATA_HOME:-$HOME/.local/share}/dataops-studio/` by default.

The verified Mint baseline is exactly 180,636,606 bytes (reported as 172.27 MiB). The builder fails
if the `.deb` grows beyond that value. Package size may decrease, not increase.

## Shared runtime contract

Both script families default to API port `8020`, PostgreSQL port `15432`, and admin user `admin`.
Both honor `DATAOPS_HOME`, force `UV_OFFLINE`, `UV_NO_SYNC` and `UV_FROZEN`, and are idempotent.
Before creating the virtual environment, both installers calculate the UTF-8 byte length of
`<DATAOPS_HOME>/data/pg-socket/.s.PGSQL.15432` and reject paths longer than 107 bytes.

The desktop shell and scripts must preserve the following behaviors together:

- readiness is the `/healthz` HTTP status, not merely a listening process;
- first-run readiness may take up to 900 seconds;
- the first-run password exists only in the child process environment;
- window close performs graceful stop, bounded wait and platform-specific force-kill fallback.

## Verification expectations

For a release candidate, record the builder command, generated manifest, SHA-256, exact byte size,
first-run `/healthz` result, GUI launch result, and graceful-close residue check on the target OS.
Static checks or a cross-platform Rust compile do not replace that target smoke test. If a platform
build or smoke test was not run, report it explicitly as unverified.
