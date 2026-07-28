# pkgforge/builds

Builds the handful of packages that cannot be pinned straight from an upstream
release, and publishes them as ordinary GitHub releases.

[soarpkgs](https://github.com/pkgforge/soarpkgs) then pins those releases the
same way it pins anyone else's: a URL and a hash, reviewed in a commit. From
its point of view this repository is just another upstream, which is the whole
idea. soarpkgs stays declarative and nothing in it executes.

## Why a package ends up here

Only when pinning upstream directly is impossible. Every definition records its
reason, because the alternative is always preferable when it exists:

| package | reason |
|---|---|
| `eza` | upstream ships gnu-linked binaries only; soar needs static musl |
| `b3sum` | upstream ships x86_64 only |
| `bingrep` | upstream publishes no releases, only tags |
| `amdgpu_top` | needs `onelf` lib bundling into cli and gui variants |

## Reproducibility

A built artifact has no external referent: nobody else publishes those bytes,
so the hash attests to our build rather than to something a third party can
check. Reproducibility is what converts that back into something verifiable.
If an independent rebuild produces the same hash, the builder stops being a
single point of failure.

So every build fixes the things that otherwise vary between runs:

- **source pinned by commit**, never by tag, and verified after fetch
- **toolchain pinned by image digest**, never by tag
- **`SOURCE_DATE_EPOCH`** taken from the source commit, not the clock
- **fixed build path** (`/build`), since absolute paths leak into debug info
- **`LC_ALL=C`, `TZ=UTC`**
- **normalised archive**: fixed mtime, uid/gid 0, sorted entries, and gzip's
  own header timestamp pinned to 0

`eza` currently reproduces byte-for-byte across clean builds.

### The remaining gap

`deps` are installed with `apk add` at build time and are **not
version-pinned**, so an Alpine package update can change the result. Closing
this means building and pinning our own base image. Until then, reproducibility
holds within a window rather than indefinitely.

## Usage

```sh
python3 build.py eza --host x86_64-linux
```

Produces `dist/<name>-<version>-<host>.tar.gz` and prints its sha256, which is
the value that gets pinned in soarpkgs.

Requires `podman` (or `--runtime docker`) and `git`. Everything else lives in
the pinned image.

## Adding a package

Create `packages/<name>/build.toml`:

```toml
[pkg]
name        = "eza"
description = "A modern replacement for ls"
license     = ["MIT"]
reason      = "upstream ships gnu-linked binaries only"

[source]
git     = "https://github.com/eza-community/eza"
commit  = "98442ab17c2c3738701b62a7e060b1431ae2d6ea"
version = "0.23.5"

[build]
image = "docker.io/library/rust@sha256:b4b54b1..."
hosts = ["x86_64-linux", "aarch64-linux"]
deps  = ["zlib-dev", "musl-dev"]

[build.target]
x86_64-linux  = "x86_64-unknown-linux-musl"
aarch64-linux = "aarch64-unknown-linux-musl"

[build.env]
RUSTFLAGS = "-C target-feature=+crt-static"

[build.script]
run = """
cargo build --release --locked --target "$TARGET"
"""

[artifact]
"target/${target}/release/eza" = "eza"
"LICENSE.txt"                  = "LICENSE"
```

Note that `deps`, `image` and `hosts` must appear **before** any `[build.x]`
sub-table: TOML assigns bare keys to whichever table precedes them, so a `deps`
line after `[build.target]` silently becomes `build.target.deps`.
