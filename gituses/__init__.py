"""gituses - unlimited self-hosted git.

A dependency-free git smart-HTTP server and repository manager.

There are no artificial limits on the number of repositories, the number of
pushes, the size of a push, or the number of concurrent clients.  The only
bounds are the ones your own disk, CPU and network impose.
"""

from .repo import GitusesError, Store, validate_name

__all__ = ["GitusesError", "Store", "validate_name", "__version__"]

__version__ = "1.0.0"
