"""Pre-commit wrapper: run pytest, treat "no tests collected" (exit 5) as success.

This lets the pytest hook stay active even before the test suite is populated.
"""

from __future__ import annotations

import subprocess
import sys


def main() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *sys.argv[1:]],
        check=False,
    )
    # pytest exit codes: 0 = ok, 5 = no tests collected
    if result.returncode == 5:
        return 0
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
