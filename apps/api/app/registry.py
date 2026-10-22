"""The Groundcrew registry: where agent deploys keep their images unless the agent connection
names the customer's own registry (ECR, Docker Hub, GHCR, ...; app/targets/agent.py).

A plain registry container runs on the platform's internal network (REGISTRY_INTERNAL_URL).
Nothing pushes to it over the internet: the worker reads the tested image out of the build
service (`docker save`, an OCI image layout) and uploads its blobs and manifest directly
(`push_saved_image`). The build service itself cannot reach the registry (its firewall refuses
this host), so build isolation stays as it was.

Agents pull through the public API host (REGISTRY_PUBLIC_HOST, e.g. api.groundkrew.com):
nginx passes `/v2/` to the registry for GET and HEAD only, after asking the API
(`check_access`, GET /internal/registry/auth) whether the request's credentials allow it. The
credentials are a short-lived pull token for one workspace's namespace
(`w<workspace id hex>`), signed with a key derived from APPROVAL_TOKEN_KEY: nothing is stored,
and a token for one workspace cannot read another's images.
"""

import base64
import binascii
import hashlib
import hmac
import io
import json
import re
import subprocess
import tarfile
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from typing import IO, Any

import httpx

from app.config import Settings

TOKEN_PREFIX = "gcr_"  # noqa: S105 - a prefix, not a secret
USERNAME = "groundcrew"
PULL_TTL = timedelta(minutes=30)
_CHUNK = 4 * 1024 * 1024
# Manifests and image configs are small JSON blobs; kept in memory to PUT the manifests.
_SMALL = 4 * 1024 * 1024
_V2_PATH = re.compile(
    r"^/v2/(?:(?P<ns>w[0-9a-f]{32})/[a-z0-9._/-]+/(?:manifests|blobs|tags)/\S*)?$"
)
MANIFEST_TYPES = (
    "application/vnd.oci.image.manifest.v1+json",
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
)


class RegistryError(RuntimeError):
    """The image could not be stored or the registry is not set up."""


def namespace(workspace_id: uuid.UUID) -> str:
    return f"w{workspace_id.hex}"


def repository(workspace_id: uuid.UUID, app: str) -> str:
    return f"{namespace(workspace_id)}/{app}"


def enabled(settings: Settings) -> bool:
    return bool(settings.registry_internal_url and settings.registry_public_host)


def _key(settings: Settings) -> bytes:
    from app.remote_agents.commands import _material

    return hmac.new(_material(settings), b"groundcrew registry tokens v1", hashlib.sha256).digest()


def pull_token(settings: Settings, workspace_id: uuid.UUID, now: float | None = None) -> str:
    """A token that may pull this workspace's images for PULL_TTL."""
    body = json.dumps(
        {
            "ns": namespace(workspace_id),
            "exp": int((now or time.time()) + PULL_TTL.total_seconds()),
        },
        separators=(",", ":"),
    ).encode()
    mac = hmac.new(_key(settings), body, hashlib.sha256).digest()
    return (
        TOKEN_PREFIX
        + base64.urlsafe_b64encode(body).decode().rstrip("=")
        + "."
        + base64.urlsafe_b64encode(mac).decode().rstrip("=")
    )


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def token_namespace(settings: Settings, token: str, now: float | None = None) -> str | None:
    """The namespace a valid, unexpired token may pull from, else None."""
    if not token.startswith(TOKEN_PREFIX) or "." not in token:
        return None
    body_b64, _, mac_b64 = token.removeprefix(TOKEN_PREFIX).partition(".")
    try:
        body, mac = _unb64(body_b64), _unb64(mac_b64)
        claims = json.loads(body)
    except (binascii.Error, ValueError):
        return None
    if not hmac.compare_digest(mac, hmac.new(_key(settings), body, hashlib.sha256).digest()):
        return None
    if not isinstance(claims, dict) or int(claims.get("exp", 0)) < (now or time.time()):
        return None
    ns = claims.get("ns")
    return ns if isinstance(ns, str) else None


def check_access(
    settings: Settings, authorization: str | None, uri: str, method: str, now: float | None = None
) -> bool:
    """nginx's question for one registry request: may these credentials do this? Only pulls
    (GET, HEAD) of the token's own namespace, plus the /v2/ version check."""
    if method.upper() not in ("GET", "HEAD"):
        return False
    scheme, _, encoded = (authorization or "").partition(" ")
    if scheme.lower() != "basic":
        return False
    try:
        user, _, password = base64.b64decode(encoded).decode().partition(":")
    except (binascii.Error, UnicodeDecodeError):
        return False
    ns = token_namespace(settings, password, now) if user == USERNAME else None
    if ns is None:
        return False
    path = uri.split("?", 1)[0]
    match = _V2_PATH.match(path)
    if match is None:
        return False
    return match.group("ns") in (None, ns)


def public_image(settings: Settings, workspace_id: uuid.UUID, app: str, tag: str) -> str:
    """What the agent pulls: `<REGISTRY_PUBLIC_HOST>/w<id>/<app>:<tag>`."""
    return f"{settings.registry_public_host}/{repository(workspace_id, app)}:{tag}"


# --- upload ------------------------------------------------------------------------------


def _chunks(f: IO[bytes]) -> Iterator[bytes]:
    while chunk := f.read(_CHUNK):
        yield chunk


def _upload_blob(client: httpx.Client, repo: str, digest: str, f: IO[bytes]) -> None:
    if client.head(f"/v2/{repo}/blobs/{digest}").status_code == 200:
        return
    started = client.post(f"/v2/{repo}/blobs/uploads/")
    if started.status_code != 202:
        raise RegistryError(f"the registry refused an upload ({started.status_code})")
    location = started.headers["Location"]
    sep = "&" if "?" in location else "?"
    done = client.put(f"{location}{sep}digest={digest}", content=_chunks(f))
    if done.status_code != 201:
        raise RegistryError(f"the registry refused blob {digest[:19]} ({done.status_code})")


def push_layout(stream: IO[bytes], registry_url: str, repo: str, tag: str) -> str:
    """Upload an OCI image layout tar stream (what `docker save` writes since Docker 25) to
    `repo:tag` and return the manifest digest. Blobs are streamed as they come; the manifests
    are put last, the image manifests before an index that names them."""
    small: dict[str, bytes] = {}
    index: dict[str, Any] | None = None
    with httpx.Client(base_url=registry_url, timeout=httpx.Timeout(60, read=900)) as client:
        with tarfile.open(fileobj=stream, mode="r|") as tar:
            for member in tar:
                if not member.isfile():
                    continue
                f = tar.extractfile(member)
                if f is None:
                    continue
                if member.name == "index.json":
                    index = json.load(f)
                    continue
                if not member.name.startswith("blobs/sha256/"):
                    continue
                digest = "sha256:" + member.name.rsplit("/", 1)[-1]
                if member.size <= _SMALL:
                    data = f.read()
                    small[digest] = data
                    _upload_blob(client, repo, digest, io.BytesIO(data))
                else:
                    _upload_blob(client, repo, digest, f)
        if not index or not index.get("manifests"):
            raise RegistryError(
                "the build service saved the image in the old format (Docker 25 or newer saves "
                "an OCI layout)"
            )
        digest, media_type = _saved_manifest(index, small)
        _put_manifest(client, repo, tag, small[digest], media_type)
    return digest


def _saved_manifest(index: dict[str, Any], small: dict[str, bytes]) -> tuple[str, str]:
    """The image manifest that was saved. A multi-platform image's index lists every platform,
    but `docker save` writes only this host's (and attestation entries): the platform image
    whose manifest is in the saved blobs is pushed under the tag, not the index."""
    top = index["manifests"][0]
    digest, media_type = str(top["digest"]), str(top.get("mediaType") or MANIFEST_TYPES[0])
    for _ in range(3):  # an index may point at another index
        if digest not in small:
            raise RegistryError("the image's manifest is missing from the saved image")
        if media_type not in (MANIFEST_TYPES[1], MANIFEST_TYPES[3]):
            return digest, media_type
        children = json.loads(small[digest]).get("manifests", [])
        saved = [
            c
            for c in children
            if str(c.get("digest")) in small
            and (c.get("platform") or {}).get("os", "linux") != "unknown"
        ]
        if not saved:
            raise RegistryError("no platform image of this index was saved")
        digest = str(saved[0]["digest"])
        media_type = str(saved[0].get("mediaType") or MANIFEST_TYPES[0])
    raise RegistryError("the saved image's indexes nest too deep")


def _put_manifest(client: httpx.Client, repo: str, ref: str, body: bytes, media_type: str) -> None:
    r = client.put(
        f"/v2/{repo}/manifests/{ref}", content=body, headers={"Content-Type": media_type}
    )
    if r.status_code != 201:
        raise RegistryError(f"the registry refused the manifest ({r.status_code}: {r.text[:200]})")


def push_saved_image(
    docker_save: list[str], env: dict[str, str], registry_url: str, repo: str, tag: str
) -> str:
    """Blocking: run `docker save` (`docker_save` is the full command) and upload its output."""
    proc = subprocess.Popen(  # noqa: S603 - a fixed docker command built by the sandbox
        docker_save, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    )
    if proc.stdout is None:  # pragma: no cover - stdout=PIPE always gives one
        raise RegistryError("docker save gave no output")
    try:
        digest = push_layout(proc.stdout, registry_url, repo, tag)
    except (tarfile.TarError, httpx.HTTPError) as exc:
        proc.kill()
        raise RegistryError(f"uploading the image failed: {exc}") from None
    finally:
        err = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        code = proc.wait()
    if code != 0:
        raise RegistryError(f"docker save failed: {err.strip()[-300:]}")
    return digest
