import sys

from . import run

raise SystemExit(run(sys.argv[1] if len(sys.argv) > 1 else None))
