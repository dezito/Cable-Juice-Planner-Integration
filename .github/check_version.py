"""Fail when a release version is one Home Assistant refuses to load.

Home Assistant blocks a custom integration whose manifest version is not
CalVer, SemVer, SimpleVer, BuildVer or PEP 440. Used by the release workflows.
"""

import sys

from awesomeversion import AwesomeVersion, AwesomeVersionStrategy

ALLOWED = (
    AwesomeVersionStrategy.CALVER,
    AwesomeVersionStrategy.SEMVER,
    AwesomeVersionStrategy.SIMPLEVER,
    AwesomeVersionStrategy.BUILDVER,
    AwesomeVersionStrategy.PEP440,
)

version = AwesomeVersion(sys.argv[1])
if version.strategy not in ALLOWED:
    print(
        f"::error::Version {version} is not valid for Home Assistant. "
        "Use 1.2.3, 1.2.3.4, 1.2.3-beta.1 (three numbers only) or 1.2.3.4b1."
    )
    sys.exit(1)
print(f"Version {version} ({version.strategy.value})")
