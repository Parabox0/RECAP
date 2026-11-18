"""RECAP: Recency-aware Context Assembly Protocol (reference implementation).

Typical use::

    from recap import Recap, AssembleRequest, load_config

    recap = Recap(load_config("recap.toml"))
    result = recap.assemble(AssembleRequest(
        profile_id="software", profile_version=1, model_id="example-model",
        context_window=200_000, base_input_tokens=3_000, reserved_output_tokens=4_096,
    ))
"""

from .config import (
    ConfigError,
    ModelEntry,
    ProfileManifest,
    RecapConfig,
    Settings,
    SourceConfig,
    load_config,
)
from .runtime import AssembleRequest, AssembleResult, Recap
from .store import Store

__version__ = "0.1.0"

__all__ = [
    "AssembleRequest",
    "AssembleResult",
    "ConfigError",
    "ModelEntry",
    "ProfileManifest",
    "Recap",
    "RecapConfig",
    "Settings",
    "SourceConfig",
    "Store",
    "load_config",
    "__version__",
]
