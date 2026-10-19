"""Per-connection deploy settings for the Kubernetes target (docs/kind.md, Real clusters).

A k8s connection without settings deploys like the local kind cluster: images go to
GC_KIND_REGISTRY (no auth), the Ingress is class nginx at <app>.<GC_KIND_APP_DOMAIN>, and verify
goes through GC_KIND_INGRESS_URL with a Host header. A real cluster (EKS, GKE, AKS, ...) cannot
pull from localhost:5001, so its owner sets these on the connection:

- registry: where images go, `host[:port][/path]` (ghcr.io/acme, <acct>.dkr.ecr.<region>.
  amazonaws.com, docker.io/acme). The image is `<registry>/<app>:<tag>`.
- username + token: optional push credentials. The token is Fernet-encrypted
  (app.security.crypto) inside the connection's config and never returned by the API.
- pull_secret: also create an imagePullSecret (kubernetes.io/dockerconfigjson) from the same
  credentials in gc-<app>, and reference it in the Deployment.
- ingress_class (default nginx), app_domain (host `<app>.<app_domain>`), tls (+ an optional
  cert-manager cluster issuer annotation).
- public_url_base: where verify sends requests (with the Host header), e.g. the ingress
  controller's load balancer before DNS exists. Default: the app's own URL.

Stored as `connection.config["deploy"]`.
"""

import base64
import ipaddress
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Self
from urllib.parse import urlparse

from pydantic import SecretStr

from app.db.models import Connection
from app.sandbox.base import PushTarget, RegistryLogin
from app.security.crypto import decrypt

CONFIG_KEY = "deploy"
DEFAULT_INGRESS_CLASS = "nginx"
CERT_MANAGER_ISSUER = "cert-manager.io/cluster-issuer"
# docker login / dockerconfigjson use this key for Docker Hub.
DOCKER_HUB = frozenset({"docker.io", "index.docker.io", "registry-1.docker.io"})
DOCKER_HUB_AUTH = "https://index.docker.io/v1/"

_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_DNS_NAME = re.compile(rf"^{_LABEL}(\.{_LABEL})*$")
_PATH_PART = re.compile(r"^[a-z0-9]+([._-][a-z0-9]+)*$")


class DeploySettingsError(ValueError):
    """Settings that cannot be used; the message says which field and why."""


def _dns_name(value: str, what: str, *, max_len: int = 253) -> str:
    value = value.strip().lower().rstrip(".")
    if not value or len(value) > max_len or not _DNS_NAME.match(value):
        raise DeploySettingsError(f"{what} must be a DNS name like example.com, not {value!r}.")
    return value


def _host_ok(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(_DNS_NAME.match(host)) and len(host) <= 253


def parse_registry(value: str) -> tuple[str, str]:
    """`host[:port][/path]` without a scheme, lowercased. Returns (registry, host)."""
    value = value.strip().rstrip("/")
    if "://" in value:
        raise DeploySettingsError(
            "The registry is a repository prefix without a scheme, like ghcr.io/acme."
        )
    if not value or len(value) > 255 or value != value.lower():
        raise DeploySettingsError(
            "The registry must be lowercase host[/path], like ghcr.io/acme or "
            "123456789012.dkr.ecr.us-east-1.amazonaws.com."
        )
    hostport, _, path = value.partition("/")
    host, _, port = hostport.partition(":")
    if not _host_ok(host) or (port and not (port.isdigit() and 0 < int(port) < 65536)):
        raise DeploySettingsError(f"The registry host {hostport!r} is not a valid host[:port].")
    if path and not all(_PATH_PART.match(p) for p in path.split("/")):
        raise DeploySettingsError(f"The registry path {path!r} is not a valid repository path.")
    return value, host


@dataclass(frozen=True)
class K8sDeploySettings:
    registry: str
    username: str | None = None
    # Fernet token of the registry password; decrypted only by login().
    password_encrypted: str | None = field(default=None, repr=False)
    pull_secret: bool = False
    ingress_class: str = DEFAULT_INGRESS_CLASS
    app_domain: str | None = None
    tls: bool = False
    tls_cluster_issuer: str | None = None
    public_url_base: str | None = None

    @classmethod
    def from_config(cls, config: dict[str, Any] | None) -> Self | None:
        raw = (config or {}).get(CONFIG_KEY)
        if not isinstance(raw, dict) or not raw.get("registry"):
            return None
        return cls(
            registry=str(raw["registry"]),
            username=raw.get("username") or None,
            password_encrypted=raw.get("password_encrypted") or None,
            pull_secret=bool(raw.get("pull_secret")),
            ingress_class=str(raw.get("ingress_class") or DEFAULT_INGRESS_CLASS),
            app_domain=raw.get("app_domain") or None,
            tls=bool(raw.get("tls")),
            tls_cluster_issuer=raw.get("tls_cluster_issuer") or None,
            public_url_base=raw.get("public_url_base") or None,
        )

    def to_config(self) -> dict[str, Any]:
        return {
            "registry": self.registry,
            "username": self.username,
            "password_encrypted": self.password_encrypted,
            "pull_secret": self.pull_secret,
            "ingress_class": self.ingress_class,
            "app_domain": self.app_domain,
            "tls": self.tls,
            "tls_cluster_issuer": self.tls_cluster_issuer,
            "public_url_base": self.public_url_base,
        }

    @property
    def registry_host(self) -> str:
        return self.registry.split("/", 1)[0]

    @property
    def has_credentials(self) -> bool:
        return bool(self.username and self.password_encrypted)

    @property
    def uses_pull_secret(self) -> bool:
        return self.pull_secret and self.has_credentials

    def image(self, repository: str, tag: str) -> str:
        return f"{self.registry}/{repository}:{tag}"

    def login(self) -> RegistryLogin | None:
        if not self.has_credentials:
            return None
        return RegistryLogin(
            registry=self.registry_host,
            username=str(self.username),
            password=SecretStr(decrypt(str(self.password_encrypted))),
        )

    def push_target(self, repository: str, tag: str) -> PushTarget:
        return PushTarget(image=self.image(repository, tag), login=self.login())

    def dockerconfigjson(self) -> str:
        """The imagePullSecret's `.dockerconfigjson` (decrypts the token)."""
        login = self.login()
        if login is None:
            raise DeploySettingsError("A pull secret needs a registry username and token.")
        password = login.password.get_secret_value()
        server = DOCKER_HUB_AUTH if login.registry in DOCKER_HUB else login.registry
        auth = base64.b64encode(f"{login.username}:{password}".encode()).decode()
        return json.dumps(
            {"auths": {server: {"username": login.username, "password": password, "auth": auth}}}
        )

    def public(self) -> dict[str, Any]:
        """What the API returns: everything but the token, which shows only as saved."""
        out = self.to_config()
        out.pop("password_encrypted")
        out["token_saved"] = bool(self.password_encrypted)
        return out


def deploy_settings(conn: Connection) -> K8sDeploySettings | None:
    return K8sDeploySettings.from_config(conn.config) if conn.kind == "k8s" else None


def is_local_kind(server: str | None) -> bool:
    """The local kind cluster: a loopback API server (host kubeconfig) or a kind node name
    (`<cluster>-control-plane` on the kind network). Unknown counts as local, as before."""
    if not server:
        return True
    host = (urlparse(server).hostname or "").lower()
    if host == "localhost" or host.endswith("-control-plane"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def build_settings(
    *,
    registry: str,
    username: str | None,
    token: str | None,
    previous: K8sDeploySettings | None,
    pull_secret: bool,
    ingress_class: str,
    app_domain: str | None,
    tls: bool,
    tls_cluster_issuer: str | None,
    public_url_base: str | None,
    allow_private: bool,
    is_private: Callable[[str], bool],
    encrypt: Callable[[str], str],
) -> K8sDeploySettings:
    """Validated settings from an owner's input. `token` None keeps the saved one (same
    username); no username clears the credentials."""
    registry, host = parse_registry(registry)
    if not allow_private and is_private(host):
        raise DeploySettingsError(
            f"The registry {host} is a private or internal address. The cluster and Groundcrew "
            "must both reach it over the internet."
        )
    user = (username or "").strip() or None
    if user is not None and (len(user) > 200 or any(c.isspace() for c in user)):
        raise DeploySettingsError("The registry username cannot contain spaces.")
    secret: str | None = None
    if user is not None:
        if token:
            secret = encrypt(token)
        elif previous is not None and previous.password_encrypted and previous.username == user:
            secret = previous.password_encrypted
        else:
            raise DeploySettingsError("Give the registry token (password) for this username.")
    elif token:
        raise DeploySettingsError("A registry token needs a username.")
    if pull_secret and secret is None:
        raise DeploySettingsError("A pull secret needs a registry username and token.")
    ingress = _dns_name(ingress_class or DEFAULT_INGRESS_CLASS, "The ingress class")
    domain = _dns_name(app_domain, "The app domain") if app_domain and app_domain.strip() else None
    if domain is not None and not allow_private and is_private(domain):
        raise DeploySettingsError(f"The app domain {domain} resolves to a private address.")
    if tls and domain is None:
        raise DeploySettingsError("TLS needs an app domain for the certificate.")
    issuer = (
        _dns_name(tls_cluster_issuer, "The cert-manager cluster issuer")
        if tls_cluster_issuer and tls_cluster_issuer.strip()
        else None
    )
    if issuer is not None and not tls:
        raise DeploySettingsError("A cert-manager cluster issuer needs TLS turned on.")
    base: str | None = None
    if public_url_base and public_url_base.strip():
        parsed = urlparse(public_url_base.strip())
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.path not in ("", "/")
            or parsed.query
            or parsed.username
        ):
            raise DeploySettingsError(
                "The public URL base must be an http(s) URL with no path, like "
                "https://lb.example.com."
            )
        if not allow_private and is_private(parsed.hostname):
            raise DeploySettingsError(
                f"The public URL base {parsed.hostname} is a private or internal address."
            )
        base = f"{parsed.scheme}://{parsed.netloc}".lower()
    return K8sDeploySettings(
        registry=registry,
        username=user,
        password_encrypted=secret,
        pull_secret=pull_secret,
        ingress_class=ingress,
        app_domain=domain,
        tls=tls,
        tls_cluster_issuer=issuer,
        public_url_base=base,
    )
