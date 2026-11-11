"""Entry script for PyInstaller (a package's __main__.py cannot be frozen directly)."""

import sys

from vf_agent.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
