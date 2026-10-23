# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Adapter registry and the ``AI_MODEL`` parser.

``AI_MODEL`` is one or more comma-separated ``provider:model`` entries, tried in order (fallback). Adapters come from
the built-ins and from the ``bahmni_ai.providers`` entry-point group, so a provider can live in another package and be
added without touching this one.
"""

from importlib.metadata import entry_points

from pydantic_ai.models import Model
from pydantic_ai.models.fallback import FallbackModel

from bahmni_ai.logging import get_logger
from bahmni_ai.providers.base import ProviderAdapter, ProviderError, ProviderSettings
from bahmni_ai.providers.builtin import BUILTIN_ADAPTERS
from bahmni_ai.providers.retry import RetryingModel

ENTRY_POINT_GROUP = "bahmni_ai.providers"

log = get_logger("bahmni_ai.providers")
_adapters: dict[str, ProviderAdapter] = {}
_loaded = False


def register(adapter: ProviderAdapter, *, replace: bool = False) -> None:
    name = adapter.name
    if not name or name != name.lower() or ":" in name or "," in name:
        raise ProviderError(f"invalid provider name '{name}' (lowercase, no ':' or ',')")
    if name in _adapters and not replace:
        raise ProviderError(f"provider '{name}' is already registered")
    _adapters[name] = adapter


def _discover() -> list[ProviderAdapter]:
    found: list[ProviderAdapter] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            found.append(obj() if callable(obj) and not isinstance(obj, ProviderAdapter) else obj)
        except Exception as exc:  # a broken plugin must not stop the built-in providers
            log.error("provider_plugin_failed", entry_point=ep.name, error=exc.__class__.__name__)
    return found


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    for adapter in BUILTIN_ADAPTERS:
        _adapters.setdefault(adapter.name, adapter)
    for adapter in _discover():
        try:
            register(adapter, replace=True)  # a plugin may deliberately override a built-in
        except ProviderError as exc:
            log.error("provider_plugin_rejected", error=str(exc))


def registered_names() -> list[str]:
    _ensure_loaded()
    return sorted(_adapters)


def get_adapter(name: str) -> ProviderAdapter:
    _ensure_loaded()
    try:
        return _adapters[name]
    except KeyError:
        raise ProviderError(f"unknown provider '{name}' (known: {', '.join(sorted(_adapters))})") from None


def _build_one(spec: str, settings: ProviderSettings) -> Model:
    name, _, model_name = spec.strip().partition(":")
    adapter = get_adapter(name.strip().lower())
    model = adapter.build(model_name.strip(), settings)
    return RetryingModel(
        model,
        max_retries=settings.max_retries,
        base_delay=settings.retry_base_delay_seconds,
        max_delay=settings.retry_max_delay_seconds,
    )


def resolve_model(spec: str, settings: ProviderSettings | None = None) -> Model:
    """``"openai:gpt-4o"`` -> model. ``"anthropic:x, openai:y"`` -> the first, then the next if it fails."""
    settings = settings or ProviderSettings()
    entries = [s for s in spec.split(",") if s.strip()]
    if not entries:
        raise ProviderError("AI_MODEL is empty")
    models = [_build_one(s, settings) for s in entries]
    return models[0] if len(models) == 1 else FallbackModel(*models)
