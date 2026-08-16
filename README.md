# pkgforge/builds

Builds the handful of packages that cannot be pinned straight from an upstream
release, and publishes them as ordinary GitHub releases.

[soarpkgs](https://github.com/pkgforge/soarpkgs) then pins those releases the
same way it pins anyone else's: a URL and a hash, reviewed in a commit. From
its point of view this repository is just another upstream, which is the whole
idea. soarpkgs stays declarative and nothing in it executes.

## Why a package ends up here

Only when pinning upstream directly is impossible. Every definition carries a
`reason` field saying which case it is, because the alternative is always
preferable when it exists:

```toml
reason = "upstream ships gnu-linked binaries only; soar needs static musl"
```

The recurring ones are upstream shipping glibc-linked binaries, shipping only
some architectures, or publishing no releases at all. `grep reason
packages/*/build.toml` is the current list; it is not repeated here, because a
list in a README is a list that goes stale.

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

A scheduled job rebuilds published packages and compares against the bytes in
the release, so a definition that stops reproducing surfaces here rather than
with whoever tries to verify it later. It runs on its own rather than on the
publish path: rebuilding twice in one job, minutes apart on a single runner,
only ever caught timestamps and paths, while doubling the work and the number
of ways a release could fail to happen.

### The remaining gap

`deps` are installed with `apk`/`apt` at build time and are **not
version-pinned**, so a distribution package update can change the result.
Closing this means building and pinning our own base image. Until then,
reproducibility holds within a window rather than indefinitely.

## What gets checked before publishing

Compiling is not evidence that the result works, least of all when building for
an architecture the builder cannot run. Every staged binary is therefore
checked:

- it is an ELF for the architecture it claims to be
- it has no `PT_INTERP`, so it is statically linked and needs no libc on the
  host
- it runs, under `qemu-user` when the build host is a different architecture

The last one is opt-in per package, since not every binary has a harmless flag
to invoke:

```toml
[verify]
run = ["--version"]
```

A binary that is dynamically linked, built for the wrong machine, or unable to
start fails the build rather than reaching a release.

## Versions, and who follows whom

For a package served partly from here, soarpkgs takes its version from *our*
release rather than from upstream:

```toml
[update]
strategy   = "github-releases"
repo       = "pkgforge/builds"
tag-prefix = "nushell-"
```

Otherwise the two repositories race. soarpkgs resolves upstream's newest
version, writes a URL pointing at a release here that does not exist yet, and
its update fails until we catch up. Following our tag means the version it
pins is one both sides have published, and the update workflow cannot outrun
the build.

The cost is that these packages reach soarpkgs only once built here, so this
repository updates daily and ahead of soarpkgs' own run. `[pkg] src` keeps
pointing at the real upstream, so provenance does not move with the version.

## Architectures

`hosts` lists what a package is built for, and a package is built only for the
architectures upstream does not already serve. Where upstream publishes a
static musl binary, soarpkgs pins that directly and this repository stays out
of it.

x86_64 and aarch64 build natively on their own runners. riscv64 has no runner
and no official Rust image, so it cross-compiles through
[cargo-zigbuild](https://github.com/rust-cross/cargo-zigbuild): zig supplies a
C cross toolchain for every target, which plain `rustup target add` does not,
and which any crate carrying C needs. Go cross-compiles on its own with
`CGO_ENABLED=0`.

## Source kinds

A package either compiles from a git commit or repackages an upstream release
artifact. Both are pinned:

```toml
[source]                      # compile from source
git     = "https://github.com/eza-community/eza"
commit  = "98442ab..."
version = "0.23.5"
```

```toml
[source]                      # repackage a published binary
url     = "https://github.com/.../amdgpu_top-0.11.5-....tar.gz"
sha256  = "5efd0b98..."
version = "0.11.5"
epoch   = 1735689600          # no commit to take a date from
```

Build tools and any side files the artifact does not ship are pinned the same
way, because they are build inputs like everything else:

```toml
[[tool]]
name   = "onelf"
url    = "https://github.com/QaidVoid/onelf/releases/download/0.3.0/onelf-x86_64-linux"
sha256 = "94127fc7..."

[[extra]]
url    = "https://raw.githubusercontent.com/.../LICENSE"
sha256 = "a31bd088..."
to     = "LICENSE"
```

`[[extra]]` is only for the repackaging case. A package built from a git commit
already has its licence in the source tree, and the commit pin covers it.

The hash on a licence is about determinism rather than security: the file ships
inside the archive, so unpinned bytes would change the artifact's hash and
break the rebuild check for a reason nobody could see in the recipe.

## Usage

```sh
python3 build.py eza --host x86_64-linux
```

Produces `dist/<name>-<version>-<host>.tar.gz` and prints its sha256, which is
the value that gets pinned in soarpkgs.

Requires `podman` (or `--runtime docker`), `git`, and Python 3.11+. Everything
else lives in the pinned image.

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

[verify]
run = ["--version"]

[artifact]
"target/${target}/release/eza" = "eza"
"LICENSE.txt"                  = "LICENSE"
```

`deps` defaults to `apk`; set `deps_via = "apt"` for a Debian-based image.

Note that `deps`, `image` and `hosts` must appear **before** any `[build.x]`
sub-table: TOML assigns bare keys to whichever table precedes them, so a `deps`
line after `[build.target]` silently becomes `build.target.deps`.
