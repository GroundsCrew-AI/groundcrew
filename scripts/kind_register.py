"""Register the local kind cluster as a Kubernetes connection, through the API (dev only).

    make kind-register LOGIN=<your GitHub login> [DISCOVER=1]

Posts .kind/groundcrew-deployer.kubeconfig to POST /connections/k8s in the workspace that
LOGIN owns, exactly as the Connections page does: the API lists namespaces with it before
saving and stores it Fernet-encrypted. Already registered (same server)? It is reused.

With --discover it also runs discovery for that connection, waits for it, and checks that
every gc-* namespace in the cluster (read with the host kubeconfig) is on the Map, and that no
Secret was read. Exits non-zero otherwise.

It signs a 5-minute API token for LOGIN with INTERNAL_JWT_SECRET from .env, like the web
app's proxy, so it refuses to run unless APP_ENV=dev and the API is on this machine
(app.devtools.require_dev). Needs `make dev KIND=1` (the API must reach
https://groundcrew-control-plane:6443) and the local Postgres (DB_URL).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from pathlib import Path
from typing import Any, NoReturn

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "apps" / "api"))

import httpx  # noqa: E402
import jwt  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.connectors.kubernetes import LiveKubeReader  # noqa: E402
from app.db.models import User, Workspace  # noqa: E402
from app.db.session import get_session_factory  # noqa: E402
from app.devtools import NotDevError, require_dev  # noqa: E402
from app.security.auth import ALGORITHM, API_AUDIENCE, WEB_ISSUER  # noqa: E402

KUBECONFIG = REPO_ROOT / ".kind" / "groundcrew-deployer.kubeconfig"
HOST_KUBECONFIG = REPO_ROOT / ".kind" / "groundcrew-deployer.host.kubeconfig"
NAME = "kind groundcrew"


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


async def owner(login: str) -> tuple[User, Workspace]:
    async with get_session_factory()() as session:
        user = await session.scalar(select(User).where(User.github_login == login))
        if user is None or user.github_id is None:
            fail(f"No user {login!r} has signed in to this Groundcrew yet. Sign in once first.")
        workspace = await session.scalar(
            select(Workspace).where(Workspace.owner_id == user.id).order_by(Workspace.created_at)
        )
        if workspace is None:
            fail(f"{login} owns no workspace.")
        return user, workspace


def token(user: User) -> str:
    secret = get_settings().internal_jwt_secret
    if not secret:
        fail("INTERNAL_JWT_SECRET is not set in .env.")
    now = int(time.time())
    claims = {
        "iss": WEB_ISSUER,
        "aud": API_AUDIENCE,
        "sub": str(user.github_id),
        "login": user.github_login or "",
        "name": user.name,
        "email": user.email,
        "iat": now,
        "exp": now + 300,
    }
    return jwt.encode(claims, secret.get_secret_value(), algorithm=ALGORITHM)


def register(api: httpx.Client, workspace: Workspace, kubeconfig: str) -> dict[str, Any]:
    server = next(
        line.split("server:", 1)[1].strip()
        for line in kubeconfig.splitlines()
        if line.strip().startswith("server:")
    )
    conns = api.get("/connections", params={"workspace_id": str(workspace.id)})
    conns.raise_for_status()
    for conn in conns.json():
        if conn["kind"] == "k8s" and conn.get("server") == server:
            print(f"Already registered: {conn['name']} ({conn['id']}), {server}")
            return dict(conn)
    r = api.post(
        "/connections/k8s",
        json={"workspace_id": str(workspace.id), "name": NAME, "kubeconfig": kubeconfig},
    )
    if r.status_code != 200:
        fail(f"The API refused the kubeconfig ({r.status_code}): {r.json().get('detail')}")
    conn = dict(r.json())
    print(f"Registered {conn['name']} ({conn['id']}): {conn['namespaces']} namespaces, {server}")
    return conn


def discover(api: httpx.Client, workspace: Workspace, conn: dict[str, Any]) -> None:
    r = api.post(f"/connections/{conn['id']}/discover")
    if r.status_code != 202:
        fail(f"Could not start discovery ({r.status_code}): {r.text}")
    run_id = r.json()["id"]
    print(f"Discovery run {run_id}", end="", flush=True)
    deadline = time.monotonic() + 180
    while True:
        run = api.get(f"/runs/{run_id}").json()
        if run["status"] in ("succeeded", "failed", "stopped"):
            break
        if time.monotonic() > deadline:
            fail("discovery did not finish in 3 minutes")
        print(".", end="", flush=True)
        time.sleep(2)
    print(f" {run['status']}")
    if run["status"] != "succeeded":
        fail(f"discovery {run['status']}: {run.get('error')}")

    graph = api.get(f"/workspaces/{workspace.id}/graph", params={"provider": "k8s"}).json()
    mine = [n for n in graph["nodes"] if n.get("connection_id") == conn["id"]]
    on_map = sorted(n["name"] for n in mine if n["type"] == "k8s:namespace")
    print(f"On the Map: {len(mine)} Kubernetes resources, namespaces {', '.join(on_map)}")

    reader = LiveKubeReader(HOST_KUBECONFIG.read_text())
    try:
        # Active namespaces only: one being deleted may still be on the Map from this run.
        items = asyncio.run(reader.list("namespaces"))
    finally:
        reader.close()
    in_cluster = sorted(
        str(n["metadata"]["name"])
        for n in items
        if (n.get("status") or {}).get("phase", "Active") == "Active"
    )
    terminating = {str(n["metadata"]["name"]) for n in items} - set(in_cluster)
    on_map = [n for n in on_map if n not in terminating]
    gc_cluster = sorted(n for n in in_cluster if n.startswith("gc-"))
    gc_map = sorted(n for n in on_map if n.startswith("gc-"))
    print(f"gc-* namespaces in the cluster: {gc_cluster or 'none'}; on the Map: {gc_map or 'none'}")
    if gc_cluster != gc_map:
        fail("the Map's gc-* namespaces do not match the cluster's")
    expected = sorted(n for n in in_cluster if not n.startswith("kube-"))
    if on_map != expected:
        fail(f"namespaces on the Map {on_map} differ from the cluster's {expected}")
    if any("secret" in n["type"] for n in mine):
        fail("a Secret is on the Map")
    print("Live check passed.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--login", required=True, help="GitHub login of the workspace owner")
    parser.add_argument("--api", default=os.environ.get("API_URL", "http://localhost:8000"))
    parser.add_argument("--discover", action="store_true", help="run discovery and check the Map")
    args = parser.parse_args()
    os.chdir(REPO_ROOT)  # Settings reads .env from here
    # Before anything else: it signs API tokens, so it must never run against a hosted instance.
    try:
        require_dev(get_settings(), args.api, tool="make kind-register")
    except NotDevError as exc:
        fail(str(exc))
    if not KUBECONFIG.exists():
        fail("No .kind/groundcrew-deployer.kubeconfig: run make kind-up and make kind-kubeconfig.")
    user, workspace = asyncio.run(owner(args.login))
    print(f"Workspace: {workspace.name} ({workspace.id})")
    headers = {"Authorization": f"Bearer {token(user)}"}
    with httpx.Client(base_url=args.api, headers=headers, timeout=60) as api:
        try:
            api.get("/health").raise_for_status()
        except httpx.HTTPError:
            fail(f"The API is not reachable at {args.api}: start it with make dev KIND=1.")
        conn = register(api, workspace, KUBECONFIG.read_text())
        if args.discover:
            discover(api, workspace, conn)


if __name__ == "__main__":
    main()
