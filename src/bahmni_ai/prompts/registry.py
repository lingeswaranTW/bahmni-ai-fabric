# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Prompt-source registry: built-ins plus the ``bahmni_ai.prompt_sources`` entry-point group (same pattern as
providers/registry.py), so a source can live in another package."""

from importlib.metadata import entry_points

from bahmni_ai.logging import get_logger
from bahmni_ai.prompts.base import PromptSourceAdapter, PromptSourceError
from bahmni_ai.prompts.openlit import OpenLITAdapter

ENTRY_POINT_GROUP = "bahmni_ai.prompt_sources"

log = get_logger("bahmni_ai.prompts")
_adapters: dict[str, PromptSourceAdapter] = {}
_loaded = False


def register(adapter: PromptSourceAdapter, *, replace: bool = False) -> None:
    name = adapter.name
    if not name or name != name.lower():
        raise PromptSourceError(f"invalid prompt source name '{name}' (lowercase)")
    if name in _adapters and not replace:
        raise PromptSourceError(f"prompt source '{name}' is already registered")
    _adapters[name] = adapter


def _discover() -> list[PromptSourceAdapter]:
    found: list[PromptSourceAdapter] = []
    for ep in entry_points(group=ENTRY_POINT_GROUP):
        try:
            obj = ep.load()
            found.append(obj() if callable(obj) and not isinstance(obj, PromptSourceAdapter) else obj)
        except Exception as exc:  # a broken plugin must not stop the built-in sources
            log.error("prompt_source_plugin_failed", entry_point=ep.name, error=exc.__class__.__name__)
    return found


def _ensure_loaded() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    _adapters.setdefault("openlit", OpenLITAdapter())
    for adapter in _discover():
        try:
            register(adapter, replace=True)
        except PromptSourceError as exc:
            log.error("prompt_source_plugin_rejected", error=str(exc))


def registered_names() -> list[str]:
    _ensure_loaded()
    return sorted(_adapters)


def get_adapter(name: str) -> PromptSourceAdapter:
    _ensure_loaded()
    try:
        return _adapters[name.strip().lower()]
    except KeyError:
        raise PromptSourceError(f"unknown prompt source '{name}' (known: {', '.join(sorted(_adapters))})") from None
