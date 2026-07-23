"""Job source adapters.

Importing the modules below populates the registry. Keep those imports here so that
`import funnel.adapters` is enough to register every adapter.
"""

from funnel.adapters import (  # noqa: F401  (imported for registration)
    adzuna,
    arbeitnow,
    ats,
    gmail,
    remoteok,
    remotive,
    teletype,
    themuse,
    weworkremotely,
)
from funnel.adapters.base import BaseAdapter, get_adapter, register, registry

__all__ = ["BaseAdapter", "get_adapter", "register", "registry"]
