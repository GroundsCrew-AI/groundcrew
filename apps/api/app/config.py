"""Application settings loaded from environment variables and .env.

Secrets are optional at import time so the app (and /health) can start without them.
Code that needs a secret must check for it and fail with a clear error at call time.
"""

import ipaddress
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repo root .env (apps/api/app/config.py -> ../../../.env), then a local .env if present.
# In the Docker image the file is /app/app/config.py (no repo root); compose passes env vars.
_HERE = Path(__file__).resolve()
_REPO_ROOT_ENV = _HERE.parents[3] / ".env" if len(_HERE.parents) > 3 else Path(".env")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT_ENV, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        # An empty value in .env means "not set", so optional numbers can stay blank.
        env_ignore_empty=True,
    )

    # Where this instance runs. Unset means production, so dev-only tools (make kind-register)
    # refuse to run unless APP_ENV=dev is set explicitly. docker-compose.yml sets it for local dev.
    app_env: Literal["dev", "test", "staging", "production"] = "production"

    # Nebius Token Factory
    token_factory_base_url: str | None = None
    token_factory_api_key: SecretStr | None = None

    # Model IDs (exact IDs come from the Token Factory docs)
    model_nano: str | None = None
    model_super: str | None = None
    model_ultra: str | None = None
    # Nemotron 3.5 Lightning (30B MoE, 3B active, built for tool-call execution). Its routes
    # (tool_step, investigate_step, verify_recovery) switch on once PRICE_LIGHTNING_* are set.
    model_lightning: str | None = "nvidia/Nemotron-3_5-Lightning"
    # The only embedding model on Token Factory (probe, 2026-10-07). It returns 4096 dimensions;
    # embed() asks for EMBED_DIM, else truncates and re-normalises (it is a Matryoshka model).
    model_embed: str | None = "Qwen/Qwen3-Embedding-8B"
    # Reranker on Token Factory (POST /v1/rerank). Unset = rerank keeps the input order.
    model_rerank: str | None = None
    # Usable context per tier, in tokens: the largest prompt the tier reads reliably, not the
    # largest it accepts. A bigger prompt goes to the route's fallback tier. Probe (2026-10-07):
    # Nano accepted 65k tokens but answered wrong from about 20k, right at 6k; Lightning was
    # right at 60k.
    llm_context_nano: int = 8_000
    llm_context_super: int = 256_000
    llm_context_ultra: int = 256_000
    llm_context_lightning: int = 64_000

    # LLM calls
    llm_timeout_seconds: float = 120.0
    # Ultra with full reasoning and a 10k-token answer can take longer than the default.
    llm_timeout_seconds_ultra: float = 300.0
    # Tries per model for 429, 5xx, and connection errors (a timeout is retried once at most,
    # because a timed-out request may still be billed). Backoff: base * 2**n plus jitter.
    llm_max_attempts: int = 3
    llm_retry_base_seconds: float = 1.0
    # Max completion tokens for agent-loop steps on the generic task types (Echo Ops).
    # Pipeline tasks (Scout, Builder, Deployer) set their own limit in app/llm/router.py.
    llm_max_tokens: int = 4096
    # A call is not made if budgets leave less than this for the completion (reasoning included)
    llm_min_completion_tokens: int = 512
    # Use Super wherever Ultra would be used (Ultra credits are limited)
    dev_mode_no_ultra: bool = False

    # Prices in USD per million tokens. Defaults are from a third-party listing (Oct 2026),
    # UNVERIFIED: set the real ones from the Token Factory pricing page. A model with no price
    # is never called, so spend is always counted.
    price_nano_input_per_m: float | None = 0.06
    price_nano_output_per_m: float | None = 0.24
    price_super_input_per_m: float | None = 0.30
    price_super_output_per_m: float | None = 0.90
    price_ultra_input_per_m: float | None = 1.00
    price_ultra_output_per_m: float | None = 3.00
    # No public price seen for Lightning yet: it is refused, and its routes stay off, until set.
    price_lightning_input_per_m: float | None = None
    price_lightning_output_per_m: float | None = None
    # Embedding and rerank prices per million input tokens. No default: unset means not called.
    price_embed_per_m: float | None = None
    price_rerank_per_m: float | None = None

    # External services
    tavily_api_key: SecretStr | None = None
    database_url: SecretStr | None = None
    secrets_key: SecretStr | None = None
    # Signs ApprovedAction tokens (app/policy/actions.py), so a token issued by one worker is
    # valid in another and after a restart. Required outside APP_ENV=dev; dev derives one from
    # SECRETS_KEY when unset. Generate one: secrets.token_urlsafe(48) (see .env.example).
    approval_token_key: SecretStr | None = None
    # Dev only: lets a live test script approve its own deploy (POST /approvals/{id}/self-approve),
    # recorded as "script:<name> (dev)". Ignored unless APP_ENV=dev.
    live_script_self_approve: bool = False
    # Where `{"fixture": name}` deploy sources come from (dev only). Unset: demo/fixtures in the
    # checkout. The dev worker container mounts them at /app/demo-fixtures.
    deploy_fixtures_dir: str | None = None
    # Dev and CI only: the deploy pipeline runs on scripted model answers, a scripted sandbox,
    # and an in-memory cluster (app/pipeline/fake.py), for Playwright. Ignored unless APP_ENV=dev.
    pipeline_fake: bool = False

    # Repositories (Phase 4 before the GitHub App lands): local paths or public git URLs.
    # Optional token for private HTTPS clones; sent as an HTTP header through git's environment,
    # never in the URL or on the command line.
    git_clone_token: SecretStr | None = None
    git_clone_timeout_seconds: float = 120.0
    # A clone bigger than this (on disk) is refused and deleted.
    repo_max_bytes: int = 300_000_000
    # Where clones go (a temp dir per clone, always removed). Unset = the system temp dir.
    repo_workdir: Path | None = None

    # Vector size of incidents.embedding. Changing it needs a new migration.
    embed_dim: int = 1024

    # GitHub App
    github_app_id: str | None = None
    github_app_client_id: str | None = None
    github_app_client_secret: SecretStr | None = None
    github_app_private_key_path: Path | None = None
    github_webhook_secret: SecretStr | None = None

    # Connections and discovery (Phase 3)
    # Groundcrew's own AWS identity, the platform identity that assumes customer roles: an IAM
    # user whose only permission is sts:AssumeRole on arn:aws:iam::*:role/groundcrew-*. Unset =
    # the default credential chain without profiles or ~/.aws (a hosted deployment's task or
    # instance role). Customer sessions are cached in memory only, never stored.
    groundcrew_aws_access_key_id: SecretStr | None = None
    groundcrew_aws_secret_access_key: SecretStr | None = None
    # Its ARN: the principal customers trust in infra/aws-connect.yaml (prefilled in the
    # Quick-create link). Unset = whatever GetCallerIdentity says the platform identity is.
    groundcrew_platform_principal_arn: str | None = None
    # S3 URL of infra/aws-connect.yaml for the one-click CloudFormation Quick-create link
    # (CloudFormation only accepts S3 template URLs). `make publish-connect-template` uploads it.
    aws_connect_template_url: str | None = None
    # Regions discovery may call (comma separated). The demo account's org policy allows
    # workloads only in us-east-2; global services (IAM, STS) are fine.
    discovery_regions: Annotated[list[str], NoDecode] = ["us-east-2"]
    # Discovery stops adding resources for one connection after this many (and says so).
    max_resources_per_connection: int = 2000
    # Wall clock limit for one connection's discovery.
    discovery_timeout_seconds: float = 100.0
    github_api_url: str = "https://api.github.com"
    vercel_api_url: str = "https://api.vercel.com"

    @field_validator("discovery_regions", mode="before")
    @classmethod
    def _split_regions(cls, value: object) -> object:
        if isinstance(value, str):
            return [r.strip() for r in value.split(",") if r.strip()]
        return value

    # Deploy infrastructure (Phase 4). The contract: docs/phase4-infra.md. The names and
    # prefixes must match what infra/aws-connect.yaml (Tier=deploy) and infra/kind/rbac.yaml
    # allow, so the validators below refuse values the IAM policy or admission policy would deny.
    gc_aws_region: str = "us-east-2"
    gc_ecs_cluster: str = "groundcrew-deploys"
    # Unset = arn:aws:iam::<connection account>:role/groundcrew-task-execution
    # (app.connectors.deploy_infra.task_execution_role_arn).
    gc_task_execution_role_arn: str | None = None
    # Subnets for Fargate tasks (comma separated). Empty = the default VPC's default subnets,
    # with a public IP (app.connectors.deploy_infra.resolve_subnets).
    gc_subnet_ids: Annotated[list[str], NoDecode] = []
    gc_log_group_prefix: str = "/groundcrew/"
    gc_ecr_prefix: str = "groundcrew-"
    # Where images for the local kind cluster are pushed; nodes pull the same name through
    # the registry mirror that `make kind-up` configures.
    gc_kind_registry: str = "localhost:5001"
    gc_namespace_prefix: str = "gc-"
    # Targets (app/targets). Who may reach an ECS app's port: the security group's source CIDR
    # (0.0.0.0/0 for the demo; set your own /32 to keep an app private).
    gc_ingress_cidr: str = "0.0.0.0/0"
    # kind apps answer at http://<app>.<GC_KIND_APP_DOMAIN> through ingress-nginx. Verify sends
    # its requests to GC_KIND_INGRESS_URL with that Host header: 127.0.0.1 on this machine,
    # http://groundcrew-control-plane from Compose on the kind network (make dev KIND=1).
    gc_kind_app_domain: str = "localhost"
    gc_kind_ingress_url: str = "http://127.0.0.1"
    # How long verify waits for a rollout (k8s) or a stable service (ECS, usually 1 to 3 min).
    gc_verify_timeout_seconds: float = 300.0
    gc_log_retention_days: int = 7

    # Build sandbox (app/sandbox). `docker` talks to the daemon in DOCKER_HOST (the dind service
    # in local dev); `nebius` is TODO(nebius-sandbox). A smoke-test container publishes its port
    # on the daemon's host, which the worker reaches as SANDBOX_SMOKE_HOST (dind in Compose).
    # `isolated`: the platform host's rootless, gVisor build daemon (app/sandbox/isolated.py).
    sandbox_provider: Literal["docker", "isolated", "nebius"] = "docker"
    # Images the isolated sandbox runs per build: BuildKit and the smoke probe.
    sandbox_buildkit_image: str = "moby/buildkit:v0.25.1"
    sandbox_probe_image: str = "curlimages/curl:8.17.0"
    sandbox_smoke_host: str = "localhost"
    sandbox_build_timeout_seconds: float = 900.0
    sandbox_smoke_timeout_seconds: float = 60.0
    sandbox_push_timeout_seconds: float = 600.0

    @field_validator("gc_subnet_ids", mode="before")
    @classmethod
    def _split_subnets(cls, value: object) -> object:
        if isinstance(value, str):
            return [s.strip() for s in value.split(",") if s.strip()]
        return value

    @field_validator("gc_ecs_cluster", "gc_ecr_prefix")
    @classmethod
    def _groundcrew_name(cls, value: str) -> str:
        if not value.startswith("groundcrew-"):
            raise ValueError(
                "must start with groundcrew- (the deploy role only allows those names)"
            )
        return value

    @field_validator("gc_log_group_prefix")
    @classmethod
    def _log_prefix(cls, value: str) -> str:
        if not value.startswith("/groundcrew/"):
            raise ValueError(
                "must start with /groundcrew/ (the deploy role only allows that prefix)"
            )
        return value

    @field_validator("gc_namespace_prefix")
    @classmethod
    def _namespace_prefix(cls, value: str) -> str:
        if not value.startswith("gc-"):
            raise ValueError(
                "must start with gc- (the admission policy only allows gc-* namespaces)"
            )
        return value

    @field_validator("gc_ingress_cidr")
    @classmethod
    def _cidr(cls, value: str) -> str:
        ipaddress.ip_network(value, strict=True)
        return value

    @field_validator("gc_task_execution_role_arn")
    @classmethod
    def _execution_role(cls, value: str | None) -> str | None:
        if value and not value.endswith(":role/groundcrew-task-execution"):
            raise ValueError(
                "must be the groundcrew-task-execution role (the only role the deploy role "
                "may pass)"
            )
        return value

    # Spend caps in USD, counted in the llm_usage table. The total cap is the hard stop for the
    # whole hackathon (credits are about $30). Hosted demo: set LLM_DAILY_USD_CAP=0.50.
    llm_run_usd_cap: float = 0.40
    llm_daily_usd_cap: float = 2.00
    llm_total_usd_cap: float = 25.0
    # One workspace's spend per UTC day (a workspace owner can set a lower or higher cap in
    # its settings). Unset: LLM_DAILY_USD_CAP, so one workspace cannot use another's budget.
    llm_workspace_daily_usd_cap: float | None = None

    # Per-workspace quotas (app.quotas). Daily counts reset at 00:00 UTC. A platform operator
    # can set other limits for one workspace (workspaces.settings["quotas"], with
    # `python -m app.admin_cli quota`); workspace owners cannot raise them.
    quota_tavily_per_workspace_day: int = 10
    # Runs a person starts (runs, deploys, discovery, reported incidents) that are queued or
    # running at once; the incident watcher's investigations are not held back.
    quota_concurrent_runs_per_workspace: int = 2
    # Deploy builds started per day (each up to 5 attempts), and the most one attempt may take.
    quota_builds_per_workspace_day: int = 10
    quota_max_build_seconds: int = 900
    quota_connections_per_workspace: int = 10

    # Groundcrew agents (app/remote_agents). The address agents reach the API at (shown in the
    # install commands); how long a join token works; how far a signed request's clock may be
    # off; calls a minute per agent; and when an agent with no heartbeat counts as offline.
    agent_api_url: str = "http://localhost:8000"
    agent_join_token_minutes: int = 60
    agent_request_skew_seconds: int = 300
    agent_rate_limit_per_minute: int = 120
    agent_offline_after_seconds: int = 180
    # The Groundcrew registry for agent deploys (app/registry.py): the registry container on the
    # internal network, and the public host agents pull from (nginx passes /v2/ there). Unset:
    # agent deploys need the connection's own registry.
    registry_internal_url: str | None = None
    registry_public_host: str | None = None

    # Abuse limits in the API (nginx limits each client address in front of it). Per user: API
    # calls a minute, stream tokens a minute, and live streams open at once. Request bodies
    # larger than MAX_REQUEST_BYTES are refused (413).
    rate_limit_user_per_minute: int = 300
    rate_limit_stream_tokens_per_minute: int = 30
    max_open_streams_per_user: int = 10
    max_request_bytes: int = 1_048_576
    # New accounts: at most this many an hour in total and a day per client address. An
    # operator can pause sign-ups (`python -m app.admin_cli signups pause`, or Settings for
    # PLATFORM_ADMINS); people with a pending invite can still join.
    signups_per_hour: int = 30
    signups_per_ip_day: int = 5
    # Emails or GitHub logins (comma separated) who see the platform switches in Settings.
    platform_admins: Annotated[list[str], NoDecode] = []

    @field_validator("platform_admins", mode="before")
    @classmethod
    def _split_admins(cls, value: object) -> object:
        if isinstance(value, str):
            return [a.strip().lower().lstrip("@") for a in value.split(",") if a.strip()]
        return value

    # Token and call limits. Unset daily token budget or per-incident Ultra limit = no limit.
    daily_token_budget: int | None = None
    max_ultra_calls_per_incident: int | None = None
    max_ultra_calls_per_run: int = 3
    max_steps_per_run: int = 12
    max_tokens_per_run: int = 400_000

    # Kubernetes API servers on private or loopback addresses (unset: allowed only in
    # APP_ENV=dev, for the local kind cluster). Kubeconfigs never run exec plugins either way.
    k8s_allow_private_servers: bool | None = None

    # Incident detection (Phase 5). The watcher job checks linked resources every interval;
    # off by default so tests and fresh installs make no AWS calls on their own.
    incident_watch_enabled: bool = False
    incident_watch_interval_seconds: int = 60
    # Window Medic investigates when a report gives no `since`.
    incident_default_window_minutes: int = 60

    # Medic (Phase 5). Investigation wall clock, recovery checks after a fix, research cap,
    # and how close a past incident must be for its diagnosis and fix to be reused.
    medic_timeout_seconds: int = 300
    medic_verify_timeout_seconds: int = 1200
    # When only a fix PR was applied: a person merges it and the pipeline deploys it first.
    medic_pr_verify_timeout_seconds: int = 3600
    medic_verify_interval_seconds: int = 30
    # Before "No fix Medic can apply": wait this long for related signals to merge in.
    medic_no_fix_grace_seconds: int = 90
    tavily_max_calls_per_incident: int = 2
    medic_memory_threshold: float = 0.85

    # Worker and live updates
    worker_poll_interval_seconds: float = 1.0
    job_lock_timeout_seconds: int = 600
    # On SIGTERM, how long the current job may keep running before it is put back in the queue
    worker_shutdown_grace_seconds: float = 20.0
    # Touched every minute while the worker runs (the container's health check reads it).
    worker_heartbeat_file: Path | None = None
    sse_poll_interval_ms: int = 500

    # Auth. The web app signs a short-lived HS256 JWT with this secret for every API call
    # (and the API signs 60-second SSE stream tokens with it). At least 32 characters.
    internal_jwt_secret: SecretStr | None = None
    # The web app's origin. CORS is allowed for it only, for the direct SSE connection. Links in
    # emails (verify, reset password, invites) point here too.
    web_origin: str = "http://localhost:3000"

    # Accounts. Email and password sign-up next to GitHub and Google (the web app enables those
    # when their OAuth credentials are set). New passwords need PASSWORD_MIN_LENGTH characters.
    # Unset: on in dev and test, off in staging and production (GitHub and email links only);
    # set either to true to offer it on a hosted instance.
    password_auth_enabled: bool | None = None
    google_auth_enabled: bool | None = None
    password_min_length: int = 10
    # Password accounts sign in only after opening the link sent to their address.
    auth_require_verified_email: bool = True
    # After this many wrong passwords in a row the account waits AUTH_LOCKOUT_MINUTES.
    auth_lockout_attempts: int = 10
    auth_lockout_minutes: int = 15
    email_verification_hours: int = 48
    # Sign-in by an emailed link (single use, EMAIL_LINK_MINUTES): on wherever email can be
    # sent. A hosted instance needs SMTP_HOST while it is on.
    email_link_auth_enabled: bool = True
    email_link_minutes: int = 15
    password_reset_minutes: int = 60

    # Outgoing email (verification links, password resets, invites). Without SMTP_HOST, dev and
    # test write the message to the log instead; staging and production refuse to start while
    # email sign-up is on.
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    # "starttls" (port 587), "ssl" (port 465), or "none" (a local relay only).
    smtp_security: Literal["starttls", "ssl", "none"] = "starttls"
    email_from: str = "Groundcrew <no-reply@localhost>"

    # Logging
    log_level: str = "INFO"

    # Worker housekeeping: finished jobs are deleted after this many days, failed ones after
    # FAILED_JOB_RETENTION_DAYS (kept longer for debugging).
    job_retention_days: int = 7
    failed_job_retention_days: int = 30

    @property
    def build_timeout_seconds(self) -> float:
        """How long one sandbox build may run: the sandbox limit, or less by quota."""
        return min(self.sandbox_build_timeout_seconds, float(self.quota_max_build_seconds))

    @property
    def password_auth(self) -> bool:
        """Email and password accounts are offered (PASSWORD_AUTH_ENABLED, else dev and test)."""
        if self.password_auth_enabled is not None:
            return self.password_auth_enabled
        return self.app_env in ("dev", "test")

    @property
    def google_auth(self) -> bool:
        """Google sign-in is accepted (GOOGLE_AUTH_ENABLED, else dev and test)."""
        if self.google_auth_enabled is not None:
            return self.google_auth_enabled
        return self.app_env in ("dev", "test")

    def production_problems(self) -> list[str]:
        """What a staging or production instance is missing. Dev and test may run without
        these; a hosted instance must not (secrets would be unset or derived)."""
        if self.app_env not in ("staging", "production"):
            return []
        problems = []
        for name in ("database_url", "secrets_key", "internal_jwt_secret", "approval_token_key"):
            value = getattr(self, name)
            if value is None or not value.get_secret_value():
                problems.append(f"{name.upper()} is not set")
        secret = self.internal_jwt_secret
        if secret is not None and 0 < len(secret.get_secret_value()) < 32:
            problems.append("INTERNAL_JWT_SECRET is shorter than 32 characters")
        if "localhost" in self.web_origin or "127.0.0.1" in self.web_origin:
            problems.append("WEB_ORIGIN still points at localhost")
        if "localhost" in self.agent_api_url or "127.0.0.1" in self.agent_api_url:
            problems.append("AGENT_API_URL still points at localhost")
        # Dev-only switches must not be set at all on a hosted instance (each is also refused
        # at the place it is used).
        for name in ("live_script_self_approve", "pipeline_fake", "k8s_allow_private_servers"):
            if getattr(self, name):
                problems.append(f"{name.upper()} is a local development setting")
        if self.deploy_fixtures_dir:
            problems.append("DEPLOY_FIXTURES_DIR is a local development setting")
        if self.email_link_auth_enabled and not self.smtp_host:
            problems.append(
                "SMTP_HOST is not set, so email sign-in links cannot be sent "
                "(set it, or EMAIL_LINK_AUTH_ENABLED=false)"
            )
        if self.password_auth and not self.smtp_host:
            problems.append(
                "SMTP_HOST is not set, so email sign-up cannot send verification links "
                "(set it, or PASSWORD_AUTH_ENABLED=false)"
            )
        return problems

    def require_production_ready(self) -> None:
        problems = self.production_problems()
        if problems:
            raise RuntimeError(
                f"APP_ENV={self.app_env} cannot start: "
                + "; ".join(problems)
                + ". See docs/self-hosting.md."
            )


@lru_cache
def get_settings() -> Settings:
    return Settings()
