"""Worker tests. Importing this package installs the network guard.

This runs before any ``tests.*`` module, so ``python -m unittest discover -s
tests -t .`` and ``python -m unittest tests.<module>`` are guarded too. Under
pytest, ``worker/conftest.py`` has already installed it (install is idempotent).
"""

from tests import net_guard as _net_guard

_net_guard.install()
