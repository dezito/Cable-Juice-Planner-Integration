"""Home Assistant datetime platform entry point."""
from .utils.datetime import CJPDateTime, async_setup_entry

# the entities read the coordinator's data and change only the integration's own settings
PARALLEL_UPDATES = 0

__all__ = ["CJPDateTime", "async_setup_entry"]
