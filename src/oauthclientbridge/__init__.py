"""Build identity from the installed distribution, independent of the checkout."""

from importlib import metadata

__all__ = ["__dirty__", "__revision__", "__version__"]

_metadata = metadata.metadata("oauthclientbridge")
__version__ = _metadata["Version"]
__revision__ = _metadata.get("Vcs-Revision", "unknown")
__dirty__ = _metadata.get("Vcs-Dirty", "unknown")
