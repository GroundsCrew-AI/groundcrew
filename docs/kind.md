# Local Kubernetes with kind

A local cluster for Groundcrew's Kubernetes deploys: kind cluster `groundcrew`, a registry on
`localhost:5001`, ingress-nginx on `127.0.0.1:80/443` (apps at `http://<name>.localhost`),
and the `groundcrew-deployer` ServiceAccount Groundcrew deploys with. Tested on Ubuntu 26.04
with Docker 29.

## Quick start

```bash
make kind-up            # cluster, registry, ingress, deployer RBAC (about 3 minutes the first time)
make kind-kubeconfig    # .kind/groundcrew-deployer*.kubeconfig
make kind-check         # proves what the deployer can and cannot do
make dev KIND=1         # the dev stack, with api, worker, and dind on the kind network
make dind-check         # from the worker: build in dind, push to kind-registry:5000, run in kind
```

Then add a Kubernetes connection in Groundcrew with `.kind/groundcrew-deployer.kubeconfig`.
`make kind-down` deletes the cluster, the registry, and the kubeconfigs.
`METRICS=1 make kind-up` also installs metrics-server (for `kubectl top`).

## Register kind as a Kubernetes connection

Groundcrew in Docker Compose reaches the cluster only on the `kind` network, so start (or
restart) the stack with `KIND=1` first:

```bash
make kind-up && make kind-kubeconfig
make dev KIND=1                              # or: make down && make dev KIND=1
```

Then either:

- **Connections page:** Kubernetes, **Upload kubeconfig**, choose
  `.kind/groundcrew-deployer.kubeconfig` (not the `.host` one), **Verify and connect**. The API
  lists namespaces with it before saving; a 401, 403, or unreachable server is shown and nothing
  is stored.
- **Command line:** `make kind-register LOGIN=<your GitHub login>`. It posts the same file to
  `POST /connections/k8s` in the workspace you own (sign in to the web app once first). It signs
  a 5-minute API token with `INTERNAL_JWT_SECRET` from `.env`, like the web proxy, so it is for
  your own dev stack only: it refuses to run unless `.env` (or the environment) has
  `APP_ENV=dev` and the API URL is on this machine (`localhost`, `127.0.0.1`, `::1`). `APP_ENV`
  defaults to `production`; never set `dev` on a hosted instance. Already registered? It reuses
  the connection.

`make kind-register LOGIN=<login> DISCOVER=1` also runs discovery for the connection and checks
that the cluster's `gc-*` namespaces (and every other non-`kube-*` namespace) are on the Map and
that no Secret was read. The same check runs as a test:
`RUN_LIVE_TESTS=1 KIND_LOGIN=<login> uv run --project apps/api pytest apps/api/tests/test_kind_live.py`.

In code, the k8s target (`app/targets/k8s.py`) gets the kubeconfig through
`CredentialsProvider.kubeconfig(connection_id)`: a 0600 file in a 0700 temp directory, deleted
when the provider is closed.

Discovery reads namespaces (not `kube-*`), Deployments, StatefulSets, Services, Ingresses, and
Pods (counted per workload; only their metadata is kept), and links Ingress -> Service ->
Deployment (`k8s_ingress`, `k8s_selector`). It never lists Secrets.

## Install on Ubuntu

**Docker** (kind runs the cluster in a container):

```bash
sudo apt-get update && sudo apt-get install -y docker.io
sudo usermod -aG docker "$USER"    # then log out and back in
docker info                        # must work without sudo
```

**kind and kubectl:** nothing to do. `make kind-up` downloads the pinned versions
(kind v0.33.0, kubectl v1.37.0) into `.tools/bin` (git-ignored), checks their SHA-256, and
uses them; nothing is installed system-wide. To use them in your shell:

```bash
export PATH="$PWD/.tools/bin:$PATH"
kubectl --context kind-groundcrew get nodes
```

To install them system-wide instead (the scripts use a kind or kubectl on `PATH` when its
version matches):

```bash
curl -Lo kind https://github.com/kubernetes-sigs/kind/releases/download/v0.33.0/kind-linux-amd64
curl -LO https://dl.k8s.io/release/v1.37.0/bin/linux/amd64/kubectl
sudo install -m 0755 kind kubectl /usr/local/bin/
```

Only linux/amd64 has pinned checksums; on arm64 install both tools yourself first.

## What `make kind-up` sets up

| Piece | Details |
|---|---|
| Cluster | `infra/kind/cluster.yaml`: one node, Kubernetes v1.37.0 pinned by digest, API server on `127.0.0.1`, kubectl context `kind-groundcrew` |
| Registry | Container `kind-registry` (`registry:3.0.0`), `localhost:5001` on this machine, `kind-registry:5000` on the `kind` Docker network. Nodes pull `localhost:5001/<image>` and `kind-registry:5000/<image>` through containerd mirrors, so the name you push is the name a pod spec uses: `localhost:5001` from this machine, `kind-registry:5000` from the `dind` build daemon. `GC_KIND_REGISTRY=localhost:5001` on the host; `make dev KIND=1` sets `kind-registry:5000` for the worker |
| Ingress | ingress-nginx v1.15.1 (kind manifest, checksummed), IngressClass `nginx`, ports 80 / 443 on `127.0.0.1`. `*.localhost` resolves to 127.0.0.1, so an Ingress host `shop.localhost` is `http://shop.localhost` with no `/etc/hosts` change |
| Deployer | `infra/kind/rbac.yaml` and `infra/kind/admission-policy.yaml` (below) |

Running it again is safe: it skips what exists and re-applies the manifests.

ingress-nginx was retired upstream in March 2026 (v1.15.1 is its last release). That is fine
for a local cluster on 127.0.0.1; do not use it to expose a shared cluster.

## The groundcrew-deployer ServiceAccount

Lives in `groundcrew-system`. It is what Groundcrew uses, never cluster-admin.

| | Allowed | How |
|---|---|---|
| Everywhere | Read (no Secrets, no RBAC objects) | ClusterRoleBinding to the built-in `view` role |
| Namespaces | Create and delete, only named `gc-*`, created with label `project=groundcrew` | RBAC allows create / delete; the admission policy checks the name and label |
| RoleBindings | Create, only in `gc-*`, only to `gc-namespace-admin` | RBAC: `create` on rolebindings plus `bind` with `resourceNames: [gc-namespace-admin]`; the admission policy checks the namespace and `roleRef` |
| Inside `gc-*` | Deployments, ReplicaSets, Services, Ingresses, ConfigMaps, Secrets; read and delete Pods; read pod logs and events | ClusterRole `gc-namespace-admin`, granted per namespace by the RoleBinding above |
| `groundcrew-system` | Nothing: not its ServiceAccount, its token Secret, or any binding | No RBAC there (`view` hides Secrets), and the admission policy refuses every write outside `gc-*` |
| Cluster-scoped objects other than `gc-*` namespaces | Nothing | Admission policy |

Kubernetes RBAC cannot limit a verb to names matching a pattern, so the namespace and
RoleBinding limits are a `ValidatingAdmissionPolicy` (`groundcrew-deployer-scope`) that
matches only this ServiceAccount's requests. **It needs Kubernetes 1.30 or later**
(ValidatingAdmissionPolicy is GA from 1.30). The kind node is v1.37. On a customer cluster
older than 1.30 the policy does not exist, and the ServiceAccount could create any namespace
and RoleBindings to `gc-namespace-admin` anywhere: use 1.30+ for this protection.

Deploy flow for the k8s target (`docs/phase4-infra.md`):

1. Create namespace `gc-<app>` with label `project=groundcrew` (and `groundcrew/deploy=<run_id>`).
2. Create RoleBinding `groundcrew-deployer` in it, `roleRef` ClusterRole `gc-namespace-admin`,
   subject the ServiceAccount.
3. Create the Deployment, Service, and Ingress there.

### The kubeconfig

`make kind-kubeconfig` writes two files from the ServiceAccount's token Secret
(`groundcrew-deployer-token`), both mode 600 and git-ignored:

- `.kind/groundcrew-deployer.kubeconfig`: server `https://groundcrew-control-plane:6443`, for
  Groundcrew running in Docker Compose (`make dev KIND=1` joins api and worker to the `kind`
  network). Register this one as the Kubernetes connection.
- `.kind/groundcrew-deployer.host.kubeconfig`: server `https://127.0.0.1:<port>`, for kubectl
  on this machine.

The token does not expire. To revoke it, delete the Secret
(`kubectl --context kind-groundcrew -n groundcrew-system delete secret groundcrew-deployer-token`),
then `make kind-up` and `make kind-kubeconfig` issue a new one.

### Proving it: `make kind-check`

Runs as the ServiceAccount and stops on the first surprise:

- `kubectl auth can-i` for RBAC: read everywhere, no Secrets outside `gc-*`, no writes to its
  own ServiceAccount, token, or bindings, no `bind` on any role but `gc-namespace-admin`.
- Server-side dry runs for the admission policy (`can-i` only checks RBAC; admission runs only on
  a real or dry-run write): RoleBindings in `groundcrew-system` or `default`, deleting
  `groundcrew-system`, a namespace not named `gc-*`, or one without `project=groundcrew` are all
  denied.
- The real flow: create `gc-groundcrew-check`, refuse RoleBindings to `view` and `cluster-admin`
  there, bind `gc-namespace-admin`, check the namespace permissions, delete it.

As a test: `RUN_LIVE_TESTS=1 uv run --project apps/api pytest apps/api/tests/test_kind_live.py`.
The offline checks of the same manifests run in CI (`tests/test_kind_manifests.py`).

## Troubleshooting

**`port 80 on this machine is already in use`.** Something listens on 80, 443, or 5001
(`sudo ss -ltnp 'sport = :80'`). Stop it (often `apache2` or `nginx`:
`sudo systemctl stop apache2`), or use another registry port with
`GC_KIND_REGISTRY_PORT=5002 make kind-up` and set `GC_KIND_REGISTRY=localhost:5002`.

**`Docker is not running, or this user cannot use it`.** `sudo systemctl start docker`; if
`docker info` needs sudo, add yourself to the `docker` group and log in again.

**The cluster does not start, or pods crash with `too many open files`.** Raise the inotify
limits:

```bash
echo -e 'fs.inotify.max_user_watches=524288\nfs.inotify.max_user_instances=512' | \
  sudo tee /etc/sysctl.d/99-kind.conf && sudo sysctl --system
```

**`ErrImagePull` for `localhost:5001/...`.** The image was not pushed
(`curl -s localhost:5001/v2/_catalog`), or the registry is not on the kind network:
`docker network connect kind kind-registry`, or run `make kind-up` again (it fixes both).

**`ErrImagePull` for `kind-registry:5000/...`, or `http: server gave HTTP response to HTTPS
client`.** The cluster was created before the `kind-registry:5000` mirror existed: run
`make kind-up` again (it rewrites both mirrors on the running node, no restart needed).

**The worker gets `x509: certificate is valid for ..., not dind`, or `cannot connect to the
Docker daemon`.** Start the stack with `make dev KIND=1` (it starts `dind` before the worker).
If the certificates are broken, `docker compose down` and `docker volume rm
groundcrew_dind-certs-ca groundcrew_dind-certs-issued groundcrew_dind-certs-client`; dind
makes new ones on start.

**`http://<name>.localhost` returns 404 from nginx.** The Ingress needs `ingressClassName: nginx`
and host `<name>.localhost`; ingress-nginx takes a few seconds to pick up a new Ingress.
`curl -H 'Host: <name>.localhost' http://127.0.0.1/` works where `*.localhost` does not resolve.

**"Cannot resolve the cluster address https://groundcrew-control-plane:6443".** Groundcrew is
not on the kind network: restart it with `make down && make dev KIND=1` (after `make kind-up`),
and register `.kind/groundcrew-deployer.kubeconfig` (not the `.host` one).

**"rejected the kubeconfig's credentials (401)".** The token Secret was deleted or the cluster was
recreated: `make kind-kubeconfig`, then remove the connection and register it again.

**`ValidatingAdmissionPolicy 'groundcrew-deployer-scope' ... denied request`.** Working as
intended: the deployer tried to write outside `gc-*`, create a namespace without
`project=groundcrew`, or bind a role other than `gc-namespace-admin`. The message says which.

**`no kind cluster groundcrew`.** Run `make kind-up`. `kind get clusters` lists clusters.
