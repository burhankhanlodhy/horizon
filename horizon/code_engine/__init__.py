"""ContextShrink Code engine: the local proxy that compresses only while licensed.

``horizon code-engine run`` serves the ordinary proxy on loopback with the
``max`` savings profile and the license gate on. The license module needs
``cryptography``; when it is missing the engine still starts and serves
pass-through, because Claude Code is pointed at the engine's port.
"""
