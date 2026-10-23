# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Model provider adapters: the only place that knows how a provider name becomes a model (docs/PROVIDERS.md)."""

from bahmni_ai.providers.base import ProviderAdapter, ProviderCapabilities, ProviderError, ProviderSettings
from bahmni_ai.providers.registry import get_adapter, register, registered_names, resolve_model

__all__ = [
    "ProviderAdapter",
    "ProviderCapabilities",
    "ProviderError",
    "ProviderSettings",
    "get_adapter",
    "register",
    "registered_names",
    "resolve_model",
]
