"""Adapters that keep legacy and UI concerns outside the V3 domain."""

from .v2_capabilities import LegacyResolverExecutor, sync_v2_capabilities
from .provider_model import ProviderLogicModel

__all__ = ["LegacyResolverExecutor", "ProviderLogicModel", "sync_v2_capabilities"]
