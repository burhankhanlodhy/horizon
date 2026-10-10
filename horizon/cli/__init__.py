"""Horizon CLI - Command-line interface for memory and proxy management.

The subcommand submodules are imported eagerly below so they are bound as
attributes of `horizon.cli`. Click registration happens via side effects in
`main.py::_register_commands`, but that only binds them to the *main.py*
module. Tests that do `patch("horizon.cli.<sub>.<attr>")` resolve the target
by walking attributes on the package object, and that lookup fails when a
prior test has popped `horizon.cli` from `sys.modules` and re-imported it
through a path other than `main.py` (e.g. a test that replaces
`sys.modules["horizon.cli.main"]` with a fake to isolate one subcommand).
Doing `from . import ...` here means the submodule attribute binding
survives that kind of sys.modules mutation.
"""

from . import (  # noqa: F401
    account,
    audit,
    capture,
    copilot_auth,
    desktop,
    evals,
    forward,
    init,
    inspect,
    install,
    keys,
    learn,
    mcp,
    perf,
    proxy,
    recover,
    rollout,
    tools,
    vault,
    wrap,
)
from .main import main

try:
    from . import memory  # noqa: F401
except ImportError:
    pass

__all__ = ["main"]
