"""Install the pinned requirements of the installed Home Assistant.

Used by the CI canary job, which installs the newest ``homeassistant`` with
``--no-deps`` over a ``pytest-homeassistant-custom-component`` release built
for an older one. ``pip check`` then lists what the newer Home Assistant needs
(new packages, changed pins); this installs exactly those, and nothing that
would downgrade Home Assistant itself.
"""

from __future__ import annotations

import re
import subprocess
import sys


def main() -> int:
    """Install whatever ``pip check`` says Home Assistant is missing."""
    check = subprocess.run(
        [sys.executable, "-m", "pip", "check"],
        capture_output=True,
        text=True,
        check=False,
    )
    wanted: list[str] = []
    for line in check.stdout.splitlines():
        if not line.startswith("homeassistant "):
            continue
        match = re.search(r"has requirement ([^,]+), but", line) or re.search(
            r"requires ([^,]+), which is not installed", line
        )
        if match:
            wanted.append(match.group(1).strip())
    if not wanted:
        print("Home Assistant's requirements are already satisfied")
        return 0
    print("Installing:", " ".join(wanted))
    return subprocess.run(
        [sys.executable, "-m", "pip", "install", *wanted], check=False
    ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
