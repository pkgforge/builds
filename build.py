#!/usr/bin/env python3
"""Run a build definition and produce a publishable artifact.

Builds happen inside a container pinned by digest, from a source pinned by
commit, with the clock and paths fixed. The point is not just to produce a
binary but to produce the *same* binary on a second run, so that a third party
can check our work rather than take it on faith.

Usage:
  build.py <package> [--host x86_64-linux] [--out dist] [--runtime podman]
"""
import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import gzip
import tarfile
import tomllib
from pathlib import Path

# Build inside a fixed path: absolute paths leak into debug info and some
# binaries embed them, which breaks reproducibility across machines.
WORKDIR = "/build"


def run(cmd, **kw):
    proc = subprocess.run(cmd, **kw)
    if proc.returncode != 0:
        sys.exit(f"failed: {' '.join(str(c) for c in cmd)}")
    return proc


def source_epoch(repo: Path) -> str:
    """Commit timestamp, used as SOURCE_DATE_EPOCH.

    Deriving it from the source rather than from the clock is what stops two
    builds of the same commit differing only by when they ran.
    """
    out = subprocess.run(
        ["git", "-C", str(repo), "show", "-s", "--format=%ct", "HEAD"],
        capture_output=True, text=True, check=True,
    )
    return out.stdout.strip()


def fetch_url(url: str, sha256: str, dest: Path) -> None:
    """Download and verify. A build input is pinned the same way a package is."""
    import urllib.request
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(url, timeout=300) as r, open(dest, "wb") as fh:
        shutil.copyfileobj(r, fh)
    got = hashlib.sha256(dest.read_bytes()).hexdigest()
    if got != sha256:
        sys.exit(f"hash mismatch for {url}\n  want {sha256}\n  got  {got}")


def remove_tree(path: Path, runtime: str, image: str) -> None:
    """Remove a work directory left by a previous build.

    Deps are installed at build time, so the container runs as root. Under a
    rootful runtime that leaves files the caller cannot delete, and the
    removal has to happen as root inside the image.
    """
    if not path.exists():
        return
    try:
        shutil.rmtree(path)
    except PermissionError:
        run([
            runtime, "run", "--rm",
            "-v", f"{path.parent.resolve()}:/w:z",
            "-w", "/w",
            image,
            "rm", "-rf", path.name,
        ])


def fetch_source(spec: dict, dest: Path) -> None:
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    # A source can be a git commit or a pinned release artifact; the second
    # is for packages that repackage an upstream binary rather than compile.
    if "url" in spec:
        archive = dest / "source-archive"
        fetch_url(spec["url"], spec["sha256"], archive)
        with tarfile.open(archive) as tf:
            tf.extractall(dest, filter="data")
        archive.unlink()
        return

    run(["git", "init", "-q", str(dest)])
    run(["git", "-C", str(dest), "remote", "add", "origin", spec["git"]])
    # Fetch just the pinned commit rather than cloning history.
    run(["git", "-C", str(dest), "fetch", "-q", "--depth", "1", "origin", spec["commit"]])
    run(["git", "-C", str(dest), "checkout", "-q", "FETCH_HEAD"])

    got = subprocess.run(
        ["git", "-C", str(dest), "rev-parse", "HEAD"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if got != spec["commit"]:
        sys.exit(f"source commit mismatch: wanted {spec['commit']}, got {got}")


def build(cfg: dict, pkg_dir: Path, host: str, out_dir: Path, runtime: str) -> Path:
    name = cfg["pkg"]["name"]
    src = cfg["source"]
    b = cfg["build"]
    target = b["target"][host]

    work = pkg_dir / ".work"
    repo = work / "src"
    remove_tree(repo, runtime, b["image"])
    fetch_source(src, repo)
    # A git source dates itself from its commit. A pinned artifact has no
    # commit, so the definition states the epoch explicitly.
    epoch = source_epoch(repo) if "git" in src else str(src.get("epoch", 0))

    # Tools are build inputs too, and are pinned by hash like everything else.
    tools = work / "tools"
    if cfg.get("tool"):
        if tools.exists():
            shutil.rmtree(tools)
        tools.mkdir(parents=True)
        for t in cfg["tool"]:
            path = tools / t["name"]
            fetch_url(t["url"], t["sha256"], path)
            path.chmod(0o755)

    env = [
        "-e", f"TARGET={target}",
        "-e", f"SOURCE_DATE_EPOCH={epoch}",
        # Normalise anything that would otherwise vary per machine.
        "-e", "LC_ALL=C",
        "-e", "TZ=UTC",
        "-e", f"CARGO_HOME={WORKDIR}/.cargo",
    ]
    for k, v in (b.get("env") or {}).items():
        env += ["-e", f"{k}={v}"]

    deps = b.get("deps") or []
    installer = b.get("deps_via", "apk")
    if not deps:
        prelude = ""
    elif installer == "apt":
        prelude = ("export DEBIAN_FRONTEND=noninteractive\n"
                   "apt-get update -qq\n"
                   f"apt-get install -y --no-install-recommends {' '.join(deps)}\n")
    else:
        prelude = f"apk add --no-cache {' '.join(deps)}\n"
    script = prelude + b["script"]["run"]
    print(f"building {name} {src['version']} for {host} ({target})")
    mounts = ["-v", f"{repo.resolve()}:{WORKDIR}:z"]
    if cfg.get("tool"):
        mounts += ["-v", f"{tools.resolve()}:/tools:z"]
        env += ["-e", "PATH=/tools:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"]

    run([
        runtime, "run", "--rm",
        *mounts,
        "-w", WORKDIR,
        *env,
        b["image"],
        "sh", "-euc", script,
    ])

    # Pinned side files that the upstream artifact does not ship, typically
    # a licence. Fetched outside the container, verified like any input.
    for e in cfg.get("extra") or []:
        fetch_url(e["url"], e["sha256"], repo / e["to"])

    # Collect declared artifacts under their published names.
    stage = work / "stage"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    published = dict(cfg["artifact"])
    for e in cfg.get("extra") or []:
        published[e["to"]] = e["to"]
    for frm, to in published.items():
        path = repo / frm.replace("${target}", target)
        if not path.is_file():
            sys.exit(f"artifact not produced: {frm} -> {path}")
        shutil.copy2(path, stage / to)
        (stage / to).chmod(0o755 if to == name else 0o644)

    out_dir.mkdir(parents=True, exist_ok=True)
    archive = out_dir / f"{name}-{src['version']}-{host}.tar.gz"
    # Fixed mtime, owner and ordering, or the tarball differs between runs even
    # when its contents do not. gzip stores its own timestamp in the header, so
    # it has to be pinned separately: without mtime=0 two identical tars still
    # compress to different bytes.
    with open(archive, "wb") as raw:
        with gzip.GzipFile(fileobj=raw, mode="wb", compresslevel=9, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w", format=tarfile.GNU_FORMAT) as tf:
                for path in sorted(stage.iterdir()):
                    info = tf.gettarinfo(path, arcname=path.name)
                    info.mtime = int(epoch)
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    with open(path, "rb") as fh:
                        tf.addfile(info, fh)
    return archive


def digests(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("package")
    ap.add_argument("--host", default="x86_64-linux")
    ap.add_argument("--out", default="dist")
    ap.add_argument("--runtime", default="podman")
    args = ap.parse_args()

    pkg_dir = Path("packages") / args.package
    cfg = tomllib.loads((pkg_dir / "build.toml").read_text())
    if args.host not in cfg["build"]["hosts"]:
        sys.exit(f"{args.package} does not build for {args.host}")

    archive = build(cfg, pkg_dir, args.host, Path(args.out), args.runtime)
    sha, size = digests(archive)
    print(f"\n{archive}")
    print(f"  sha256 {sha}")
    print(f"  bytes  {size}")
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as fh:
            fh.write(f"archive={archive}\nsha256={sha}\nsize={size}\n")


if __name__ == "__main__":
    main()
