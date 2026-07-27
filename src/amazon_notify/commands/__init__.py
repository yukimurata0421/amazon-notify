"""CLI command handler modules.

Each sub-module isolates one CLI action (polling, streaming, watch, reauth, …).
Thin wrappers like ``verify.py`` and ``scenario.py`` exist so that the CLI
layer (``cli.py``) never imports business-logic modules directly; this keeps
the DI seam consistent and makes every command individually replaceable in
tests without monkey-patching imports.
"""
