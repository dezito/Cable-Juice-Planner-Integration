"""Suggest the next release tag from the existing release tags.

Usage: next_version.py <beta|stable> <patch|minor|major> <tag> [<tag> ...]

Used by the Draft release workflow when no tag is typed. Only tags like v1.0.9
and v1.0.9-beta.4 count; any other tag is ignored.

* The latest version is a beta (v1.0.9-beta.4): `beta` gives v1.0.9-beta.5 and
  `stable` gives v1.0.9. The bump is not used.
* The latest version is stable (v1.0.8): the bump raises it and `beta` starts a
  new beta line (v1.0.9-beta.1), `stable` gives v1.0.9.
"""

import re
import sys

TAG_RE = re.compile(r"^v(\d+)\.(\d+)\.(\d+)(?:-beta\.(\d+))?$")


def parse(tag):
    """Return (major, minor, patch, beta) with beta None for a stable tag."""
    match = TAG_RE.match(tag)
    if not match:
        return None
    major, minor, patch, beta = match.groups()
    return int(major), int(minor), int(patch), None if beta is None else int(beta)


def sort_key(version):
    major, minor, patch, beta = version
    # A stable release is newer than every beta of the same number.
    return major, minor, patch, beta is None, beta or 0


def fmt(major, minor, patch, beta=None):
    tag = f"v{major}.{minor}.{patch}"
    return tag if beta is None else f"{tag}-beta.{beta}"


def next_tag(tags, release_type, bump):
    if release_type not in ("beta", "stable"):
        raise ValueError(f"release type must be beta or stable, got '{release_type}'")
    if bump not in ("patch", "minor", "major"):
        raise ValueError(f"bump must be patch, minor or major, got '{bump}'")
    versions = [v for v in map(parse, tags) if v is not None]
    if not versions:
        raise ValueError("no existing vX.Y.Z tag to count from; type the tag by hand")
    major, minor, patch, beta = max(versions, key=sort_key)
    if beta is not None:
        if release_type == "stable":
            return fmt(major, minor, patch)
        return fmt(major, minor, patch, beta + 1)
    if bump == "major":
        major, minor, patch = major + 1, 0, 0
    elif bump == "minor":
        minor, patch = minor + 1, 0
    else:
        patch += 1
    return fmt(major, minor, patch, 1 if release_type == "beta" else None)


def main(argv):
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        print(next_tag(argv[3:], argv[1], argv[2]))
    except ValueError as err:
        print(f"::error::{err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
