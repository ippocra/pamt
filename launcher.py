"""PyInstaller entry point.

PAmt is a package (pamt/), so it must be launched through a small top-level
script that imports the package's main(). Building app.py directly would
break its relative imports (from . import __version__).
"""

import sys

from pamt.app import main

if __name__ == "__main__":
    sys.exit(main())
