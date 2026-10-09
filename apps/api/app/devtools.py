"""Guards for developer tools that must never run against a hosted Groundcrew.

`make kind-register` signs API tokens with INTERNAL_JWT_SECRET, which on a hosted instance would
let it act as any user. `require_dev` refuses unless this instance is explicitly a dev one
(APP_ENV=dev) and the API it talks to is on this machine.
"""

import ipaddress
from urllib.parse import urlparse

from app.config import Settings


class NotDevError(RuntimeError):
    """A dev-only tool was asked to run outside local development."""


def _loopback(host: str) -> bool:
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def require_dev(settings: Settings, api_url: str, *, tool: str) -> None:
    if settings.app_env != "dev":
        raise NotDevError(
            f"{tool} only runs in local development, and APP_ENV is {settings.app_env!r}. "
            "Set APP_ENV=dev in .env on your own machine; never on a hosted instance."
        )
    host = urlparse(api_url).hostname or ""
    if not _loopback(host):
        raise NotDevError(
            f"{tool} only talks to an API on this machine, not {api_url}. It signs tokens with "
            "INTERNAL_JWT_SECRET, so it must never point at a hosted Groundcrew."
        )
