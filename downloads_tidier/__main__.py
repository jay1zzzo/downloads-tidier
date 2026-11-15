"""Lets you run the tool with:  python -m downloads_tidier"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
