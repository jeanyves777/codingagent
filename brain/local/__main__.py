import os
import sys

from .cli import main

try:
    raise SystemExit(main())
except BrokenPipeError:  # output piped into a command that stopped reading (e.g. | Select-Object -First 2)
    os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    raise SystemExit(0)
