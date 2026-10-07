"""Run `horizon proxy` with the proxy's own debug messages enabled (sys.argv passed through)."""
import logging
import sys

import horizon.proxy.server  # noqa: F401  (sets up logging at INFO)
from horizon.cli.main import main

for name in ("horizon.proxy", "horizon.transforms.read_maturation"):
    logging.getLogger(name).setLevel(logging.DEBUG)
sys.exit(main(args=sys.argv[1:], prog_name="horizon"))
