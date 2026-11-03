"""Home Assistant time platform entry point."""
from .utils.time import CJPTime, async_setup_entry

# the entities read the coordinator's data and change only the integration's own settings
PARALLEL_UPDATES = 0

__all__ = ["CJPTime", "async_setup_entry"]
