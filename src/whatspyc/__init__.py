import os

__version__ = "0.0.1"

# `websockets` truncates DEBUG-level frame dumps to MAX_LOG_SIZE chars
# (default 75) with `...` in the middle. That clip-marker hides the
# actual JSON payload, which is exactly what a user enabling DEBUG
# wants to see. The library reads this env var at class-definition
# time, so it has to be set before any submodule import that pulls
# in `websockets` — `__init__.py` is the earliest point. `setdefault`
# preserves an explicit user override.
os.environ.setdefault("WEBSOCKETS_MAX_LOG_SIZE", str(2**24))
