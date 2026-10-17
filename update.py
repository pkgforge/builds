#!/usr/bin/env python3
"""Resolve the latest upstream version of each package and rewrite its pin.

Every package pins its source one of two ways: a git ``commit`` at a release
tag, or a released ``url`` with its ``sha256``. This walks each definition,
asks the upstream forge for its newest version, and rewrites only the pinned
lines when there is a newer one. A package that cannot be resolved is
reported and skipped; it never stops the others.

The diff for a bump is a handful of lines a human can actually read, which is
the point. A changed hash on an otherwise unchanged artifact means upstream
replaced the bytes in place, and that should be visible before it is merged.

Usage:
  update.py [package ...]   # no args = every package
"""
import hashlib
import json
import os
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API = "https://api.github.com"
CODEBERG_API = "https://codeberg.org/api/v1"


class UpdateError(Exception):
    """One package could not be resolved or pinned; never fatal to the run."""


def api(path: str, base: str = API):
    """GET a JSON API path, or None on 404.

    A token lifts the anonymous rate limit on GitHub, and is sent nowhere
    else: forges reject foreign tokens outright, so forwarding it to Codeberg
    turns an anonymous read into a 401. Transient failures (unreachable host,
    429, 5xx) are retried with a short backoff, since Codeberg in particular
    drops connections; anything else raises UpdateError.
    """
    last = None
    for attempt in range(1, 4):
        req = urllib.request.Request(f"{base}/{path}")
        req.add_header("Accept", "application/vnd.github+json")
        if base == API:
            token = os.environ.get("GITHUB_TOKEN")
            if token:
                req.add_header("Authorization", f"Bearer {token}")
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code < 500 and e.code != 429:
                raise UpdateError(f"{base}/{path}: {e}") from e
            last = e
        except urllib.error.URLError as e:
            last = e
        if attempt < 3:
            time.sleep(2 * attempt)
    raise UpdateError(f"{base}/{path}: {last}") from last


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


def latest(base: str, owner: str, repo: str):
    """Newest upstream version as ``(tag, version, epoch)``.

    A published release is preferred: it excludes prereleases and drafts, so
    the latest release is a deliberate one and its date is known. Repos that
    only tag fall back to the highest semver tag, with no date to derive.
    """
    rel = api(f"repos/{owner}/{repo}/releases/latest", base)
    if rel:
        tag = rel["tag_name"]
        published = rel.get("published_at")
        # GitHub stamps UTC with a literal Z; Forgejo carries a UTC offset.
        # fromisoformat takes both apart into a comparable instant.
        epoch = (int(datetime.fromisoformat(
            published.replace("Z", "+00:00")).timestamp())
            if published else None)
        return tag, tag.lstrip("v"), epoch

    tags = api(f"repos/{owner}/{repo}/tags?per_page=100", base) or []
    best = None
    for t in tags:
        v = semver(t["name"])
        if v and (best is None or v > best[0]):
            best = (v, t["name"])
    if not best:
        raise UpdateError(f"no release or semver tag for {owner}/{repo}")
    return best[1], best[1].lstrip("v"), None


def fetch_sha256(url: str) -> str:
    """Hash a released artifact, retrying transient failures.

    The Content-Length header, when the forge sends one, is checked so a
    truncated transfer never lands in build.toml as a pin.
    """
    last = None
    for attempt in range(1, 4):
        try:
            with urllib.request.urlopen(url, timeout=300) as r:
                data = r.read()
                want = r.headers.get("Content-Length")
            if want and int(want) != len(data):
                raise UpdateError(
                    f"{url}: truncated transfer ({len(data)} of {want} bytes)")
            return hashlib.sha256(data).hexdigest()
        except urllib.error.HTTPError as e:
            if e.code < 500 and e.code != 429:
                raise UpdateError(f"{url}: {e}") from e
            last = e
        except urllib.error.URLError as e:
            last = e
        if attempt < 3:
            time.sleep(2 * attempt)
    raise UpdateError(f"{url}: {last}") from last


def slug(cfg: dict) -> tuple[str, str, str]:
    """The API base and ``owner/repo`` to query for a package.

    A git source names its own repo; a repackaged release does not, so its
    homepage stands in. Codeberg runs Forgejo, whose API mirrors the GitHub
    release shape under a different base URL.
    """
    src = cfg["source"]
    url = (src["git"] if "git" in src else cfg["pkg"]["homepage"][0]).rstrip("/")
    for host, base in (
        ("https://github.com/", API),
        ("https://codeberg.org/", CODEBERG_API),
    ):
        if url.startswith(host):
            owner, repo = url[len(host):].split("/")[:2]
            return base, owner, repo.removesuffix(".git")
    raise UpdateError(f"unsupported forge: {url}")


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

    base, owner, repo = slug(cfg)
    tag, ver, epoch = latest(base, owner, repo)
    if semver(ver) is None or semver(cur) is None or semver(ver) <= semver(cur):
        print(f"{pkg_dir.name}: {cur} is current")
        return False

    if cur not in path.read_text():
        raise UpdateError(f"{pkg_dir.name}: version {cur} not found in build.toml")
    text = path.read_text().replace(cur, ver)

    if "git" in src:
        commit = api(f"repos/{owner}/{repo}/commits/{tag}", base)
        if not commit:
            raise UpdateError(f"{pkg_dir.name}: tag {tag} has no commit")
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


def report(broken: list[tuple[str, str]]) -> None:
    """Hand failures to the calling workflow, which decides what they mean.

    Under GITHUB_OUTPUT they land in step outputs the workflow can put on the
    pull request; a local run has already printed everything to stderr.
    """
    out = os.environ.get("GITHUB_OUTPUT")
    if not out:
        return
    lines = "".join(f"- **{name}**: {msg}\n" for name, msg in broken)
    with open(out, "a") as fh:
        fh.write("failed=true\n")
        fh.write(f"failures<<UPDATE_FAILURES_EOF\n{lines}UPDATE_FAILURES_EOF\n")


def main() -> None:
    only = set(sys.argv[1:])
    dirs = sorted(p.parent for p in Path("packages").glob("*/build.toml"))
    if only:
        dirs = [d for d in dirs if d.name in only]
    changed, broken = [], []
    for d in dirs:
        try:
            if update(d):
                changed.append(d.name)
        except UpdateError as e:
            broken.append((d.name, str(e)))
            print(f"{d.name}: {e}", file=sys.stderr)
    if changed:
        print(f"\nupdated: {', '.join(changed)}")
    if broken:
        print(f"\nfailed: {', '.join(name for name, _ in broken)}", file=sys.stderr)
        report(broken)


if __name__ == "__main__":
    main()
