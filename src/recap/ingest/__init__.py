"""Off-path ingestion: fetch allowlisted sources, normalise, validate, store."""

from .adapters import ADAPTERS, Adapter, ParseError, ParseResult, ReviewCandidate, register_adapter
from .fetch import FetchError, FetchResult, OriginPolicy, SafeFetcher
from .poller import Poller, PollOutcome

__all__ = [
    "ADAPTERS",
    "Adapter",
    "FetchError",
    "FetchResult",
    "OriginPolicy",
    "ParseError",
    "ParseResult",
    "PollOutcome",
    "Poller",
    "ReviewCandidate",
    "SafeFetcher",
    "register_adapter",
]
