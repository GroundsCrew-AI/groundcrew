# infra/platform/host: setting up the platform host

Everything on the host besides the Terraform resources, as versioned scripts. `install.sh` is
idempotent: it sets up a fresh host and brings an existing one up to date.

```bash
infra/platform/host/push.sh            # from a laptop: copy this directory, run install.sh
infra/platform/host/push.sh 40-nginx   # one step
```

| Step | What it does |
| --- | --- |
| `10-packages` | Docker (main daemon: journald logs, live-restore, no-new-privileges), gVisor `runsc`, nginx, certbot; journald keeps 30 days, at most 2 GB |
| `20-build-daemon` | User `gcbuild` (uid and gid 1500) with a rootless Docker daemon: gVisor (`runsc --ignore-cgroups`) for every container, public DNS, data on its own 30 GB XFS filesystem (`/var/lib/gcbuild`), limits on its systemd slice (3.5 GB memory, 4096 tasks, 2 CPUs) |
| `30-firewall` | `gc-firewall` at boot: the build user reaches the internet but not the metadata service, loopback, private ranges, or this host; only the API and worker containers reach the metadata service |
| `40-nginx` | nginx as the only public entry (bootstrap site until the first certificate), rate limits, fail2ban jail, certbot renewal hook and weekly dry run |
| `50-app` | `/opt/groundcrew` (Compose file, `gc-*` commands), `/etc/groundcrew` secrets (generated where possible), the build sweeper |

Commands on the host (`/opt/groundcrew/bin`, on the PATH for login shells):

| Command | |
| --- | --- |
| `gc-secrets init / set NAME / set-file github-app.pem / check` | secrets in `/etc/groundcrew` (0600, root) |
| `gc-certs issue --email E --agree-tos / status / dry-run` | Let's Encrypt for groundkrew.com, www, api |
| `gc-status / restart / logs SERVICE` | services, health, disk; rolling restart |
| `gc-isolation-check` | live proof that builds cannot reach internal services or the metadata service |
| `gc-release deploy SHA < source.tar / rollback [SHA] / list` | build on the host, migrate, roll out with health checks, roll back |

## Deploying

From your machine, for a commit you want live (the server needs no repository access):

```bash
GC_HOST=<server> GC_SSH_KEY=~/.ssh/<key> GC_CONFIG_FILE=<settings.env> infra/platform/bin/deploy <git-ref>
GC_HOST=<server> GC_SSH_KEY=~/.ssh/<key> infra/platform/bin/rollback          # previous release
GC_HOST=<server> GC_SSH_KEY=~/.ssh/<key> infra/platform/bin/rollback --list   # the last 3
```

`deploy` sends `git archive` of the commit over SSH; `gc-release` builds `groundcrew/api:<sha>` and
`groundcrew/web:<sha>` on the host's main Docker daemon (the web image takes the API's public URL from
`web.env` at build time), runs the migrations, which must be additive, then replaces worker, api and
web one at a time, each until its health check passes, and finally checks `/ready` and `/api/health`
over HTTPS through nginx. Any failure puts the previous release back the same way, without
migrations. `GC_CONFIG_FILE` (optional) becomes `/etc/groundcrew/config.env`: the non-secret
settings (models, SMTP host, regions). Secrets stay in `gc-secrets`.
| `gc-firewall apply / show` | the rules above |

How builds are isolated, and what the spike on this host found: docs/phase6-plan.md, section 6.
