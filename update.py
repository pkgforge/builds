#!/usr/bin/env python3
"""Resolve the latest upstream version of each package and rewrite its pin.

Every package pins its source one of two ways: a git ``commit`` at a release
tag, or a released ``url`` with its ``sha256``. This walks each definition,
asks the upstream forge for its newest version, and rewrites only the pinned
lines when there is a newer one.

The diff for a bump is a handful of lines a human can actually read, which is
the point. A changed hash on an otherwise unchanged artifact means upstream
replaced the bytes in place, and that should be visible before it is merged.

Usage:
  update.py [package ...]   # no args = every package
"""
import calendar
import hashlib
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from pathlib import Path

API = "https://api.github.com"


def api(path: str):
    """GET a GitHub API path as JSON, or None on 404.

    A token lifts the anonymous rate limit and is passed when present; the
    calls are public reads, so it is optional for a local run.
    """
    req = urllib.request.Request(f"{API}/{path}")
    req.add_header("Accept", "application/vnd.github+json")
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        sys.exit(f"github api {path}: {e}")


def semver(name: str):
    """Parse a tag name into a comparable tuple, or None if it is not one.

    Tags that carry a suffix (prereleases, unrelated markers like ``guts_``)
    parse to None so they never win a version comparison.
    """
    parts = name.lstrip("v").split(".")
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def latest(owner: str, repo: str):
    """Newest upstream version as ``(tag, version, epoch)``.

    A published release is preferred: it excludes prereleases and drafts, so
    the latest release is a deliberate one and its date is known. Repos that
    only tag fall back to the highest semver tag, with no date to derive.
    """
    rel = api(f"repos/{owner}/{repo}/releases/latest")
    if rel:
        tag = rel["tag_name"]
        published = rel.get("published_at")
        epoch = (calendar.timegm(
            time.strptime(published, "%Y-%m-%dT%H:%M:%SZ"))
            if published else None)
        return tag, tag.lstrip("v"), epoch

    tags = api(f"repos/{owner}/{repo}/tags?per_page=100") or []
    best = None
    for t in tags:
        v = semver(t["name"])
        if v and (best is None or v > best[0]):
            best = (v, t["name"])
    if not best:
        sys.exit(f"no release or semver tag for {owner}/{repo}")
    return best[1], best[1].lstrip("v"), None


def fetch_sha256(url: str) -> str:
    with urllib.request.urlopen(url, timeout=300) as r:
        return hashlib.sha256(r.read()).hexdigest()


def slug(cfg: dict) -> tuple[str, str]:
    """The ``owner/repo`` to query for a package.

    A git source names its own repo; a repackaged release does not, so its
    homepage stands in.
    """
    src = cfg["source"]
    url = src["git"] if "git" in src else cfg["pkg"]["homepage"][0]
    owner, repo = url.rstrip("/").removeprefix(
        "https://github.com/").split("/")[:2]
    return owner, repo.removesuffix(".git")


def update(pkg_dir: Path) -> bool:
    """Rewrite a package's pin to the latest upstream version.

    Returns True when the file changed. The version string is dotted, so it
    cannot collide with a hex hash, commit or epoch; replacing it across the
    whole file updates the version line and every URL that embeds it at once.
    """
    path = pkg_dir / "build.toml"
    cfg = tomllib.loads(path.read_text())
    src = cfg["source"]
    cur = src["version"]

    owner, repo = slug(cfg)
    tag, ver, epoch = latest(owner, repo)
    if semver(ver) is None or semver(cur) is None or semver(ver) <= semver(cur):
        print(f"{pkg_dir.name}: {cur} is current")
        return False

    if cur not in path.read_text():
        sys.exit(f"{pkg_dir.name}: version {cur} not found in build.toml")
    text = path.read_text().replace(cur, ver)

    if "git" in src:
        commit = api(f"repos/{owner}/{repo}/commits/{tag}")
        if not commit:
            sys.exit(f"{pkg_dir.name}: tag {tag} has no commit")
        text = text.replace(src["commit"], commit["sha"])
    else:
        new_url = src["url"].replace(cur, ver)
        text = text.replace(src["sha256"], fetch_sha256(new_url))
        for e in cfg.get("extra") or []:
            text = text.replace(e["sha256"], fetch_sha256(e["url"].replace(cur, ver)))
        if epoch is not None and "epoch" in src:
            text = text.replace(str(src["epoch"]), str(epoch))

    path.write_text(text)
    print(f"{pkg_dir.name}: {cur} -> {ver}")
    return True


def main() -> None:
    only = set(sys.argv[1:])
    dirs = sorted(p.parent for p in Path("packages").glob("*/build.toml"))
    if only:
        dirs = [d for d in dirs if d.name in only]
    changed = [d.name for d in dirs if update(d)]
    if changed:
        print(f"\nupdated: {', '.join(changed)}")


if __name__ == "__main__":
    main()
