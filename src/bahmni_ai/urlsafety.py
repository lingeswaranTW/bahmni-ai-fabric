# This Source Code Form is subject to the terms of the Mozilla Public License, v. 2.0.
# If a copy of the MPL was not distributed with this file, You can obtain one at
# https://www.bahmni.org/license/mplv2hd.

"""Shared rule for where credentials may be sent."""

from urllib.parse import urlsplit

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "host.docker.internal"})


def is_private_or_tls(url: str) -> bool:
    """https always; plain http only where traffic cannot leave the machine or the compose network
    (localhost, host.docker.internal, or a single-label container name)."""
    parts = urlsplit(url)
    if parts.scheme == "https":
        return True
    host = (parts.hostname or "").lower()
    return host in _LOCAL_HOSTS or ("." not in host and bool(host))
