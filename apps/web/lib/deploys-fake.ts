/**
 * Preview responses for the Deploy page while POST /deploys and GET /deploys answer 501.
 *
 * TODO(M6): delete this file and its imports once the pipeline is wired up. Nothing here is
 * used when the API answers: lib/deploys.ts falls back to it only on a 501, and the page
 * shows a "Preview" banner whenever it does.
 *
 * Every value is typed with the real M1 schema (lib/api/schema.d.ts), and the scripted run
 * emits the same reducer actions as the SSE stream, so the page is built against the real
 * contract. The script is legacy-flask-app with one automatic build fix (docs/demo.md).
 */
import type { Approval, Decision, RunDetail, Schemas, Step } from "@/lib/api/client";
import type { TimelineAction } from "@/lib/timeline";

type DeployCreate = Schemas["DeployCreate"];
type DeploySummary = Schemas["DeploySummary"];
type DeployDetail = Schemas["DeployDetail"];
type DeployPlan = Schemas["DeployPlan"];
type TargetKind = DeployPlan["target"];
type EventData = NonNullable<Step["data"]>;

export type PreviewLabels = { repo: string; ref: string | null; targetName: string; targetKind: TargetKind };

type Beat = { delay: number; run: () => void };

type FakeDeploy = {
  detail: DeployDetail;
  run: RunDetail;
  steps: Step[];
  approvals: Record<string, Approval>;
  beats: Beat[];
  next: number;
  paused: boolean;
  listeners: Set<(action: TimelineAction) => void>;
  timer: ReturnType<typeof setTimeout> | null;
};

const deploys = new Map<string, FakeDeploy>();
const byRun = new Map<string, string>();

const APP = "legacy-flask-app";
const COMMIT = "3f2a9c1b7d4e5f60718293a4b5c6d7e8f9a0b1c2";
const NAIVE = `FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["gunicorn", "-b", "0.0.0.0:8000", "app:app"]
`;
const FIX_DIFF = `--- Dockerfile (attempt 1)
+++ Dockerfile (attempt 2)
@@ -1,4 +1,6 @@
 FROM python:3.12-slim
+RUN apt-get update && apt-get install -y --no-install-recommends gcc libc6-dev libpq-dev \\
+    && rm -rf /var/lib/apt/lists/*
 WORKDIR /app
 COPY requirements.txt .
 RUN pip install --no-cache-dir -r requirements.txt
`;
const FAILED_LOG = `#8 [4/5] RUN pip install --no-cache-dir -r requirements.txt
#8 9.13   Error: pg_config executable not found.
#8 9.13
#8 9.13   pg_config is required to build psycopg2 from source.  Please add the directory
#8 9.13   containing pg_config to the $PATH or specify the full executable path with the
#8 9.13   option:
#8 ERROR: process "/bin/sh -c pip install --no-cache-dir -r requirements.txt" did not complete successfully: exit code: 1
`;

type Resource = DeployPlan["resources"][number];

function res(kind: string, name: string, detail: string | null = null): Resource {
  return { kind, name, action: "create", detail };
}

function omit<T extends object, K extends keyof T>(value: T, ...keys: K[]): Omit<T, K> {
  const copy = { ...value };
  for (const key of keys) delete copy[key];
  return copy;
}

function now(): string {
  return new Date().toISOString();
}

function step(d: FakeDeploy, kind: string, agent: string, extra: Partial<Step> = {}): Step {
  return {
    id: crypto.randomUUID(),
    seq: d.steps.length + 1,
    agent,
    kind,
    model_tier: null,
    model_id: null,
    tool_name: null,
    tool_policy: null,
    approval_id: null,
    input_summary: null,
    output_summary: null,
    prompt_tokens: null,
    completion_tokens: null,
    latency_ms: null,
    cost_usd: null,
    data: null,
    created_at: now(),
    ...extra,
  };
}

function emit(d: FakeDeploy, action: TimelineAction): void {
  for (const listener of d.listeners) listener(action);
}

function push(d: FakeDeploy, s: Step): Step {
  d.steps.push(s);
  emit(d, { type: "step", step: s });
  return s;
}

function event(d: FakeDeploy, agent: string, data: EventData, extra: Partial<Step> = {}): Step {
  if (data.type === "stage_started") d.detail = { ...d.detail, stage: data.stage };
  if (data.type === "build_attempt") d.detail = { ...d.detail, build_attempts: data.attempt };
  return push(d, step(d, data.type ?? "step", agent, { data, ...extra }));
}

function setRun(d: FakeDeploy, patch: Partial<RunDetail>): void {
  d.run = { ...d.run, ...patch };
  const status = omit(d.run, "steps", "approvals");
  emit(d, { type: "status", status: { ...status, result: d.run.result ?? null, error: d.run.error ?? null } });
}

function llm(
  d: FakeDeploy,
  agent: string,
  tier: "nano" | "super",
  summary: string,
  tokens: [number, number],
  cost: number,
): Step {
  const s = push(
    d,
    step(d, "llm", agent, {
      model_tier: tier,
      model_id: tier === "nano" ? "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B" : "nvidia/nemotron-3-super-120b-a12b",
      reasoning_mode: tier === "nano" ? "low" : "on",
      prompt_tokens: tokens[0],
      completion_tokens: tokens[1],
      latency_ms: 1800 + tokens[1],
      cost_usd: cost,
      output_summary: summary,
    }),
  );
  setRun(d, {
    prompt_tokens: d.run.prompt_tokens + tokens[0],
    completion_tokens: d.run.completion_tokens + tokens[1],
    total_tokens: d.run.total_tokens + tokens[0] + tokens[1],
    cost_usd: Number(((d.run.cost_usd ?? 0) + cost).toFixed(6)),
  });
  return s;
}

function stage(d: FakeDeploy, name: Schemas["StageStarted"]["stage"], agent: Schemas["StageStarted"]["agent"], summary: string, ms: number): Beat[] {
  return [
    { delay: 300, run: () => event(d, agent, { type: "stage_started", stage: name, agent }) },
    {
      delay: ms,
      run: () =>
        event(d, agent, { type: "stage_finished", stage: name, agent, status: "succeeded", duration_ms: ms, summary }),
    },
  ];
}

function plan(d: FakeDeploy, request: DeployCreate, labels: PreviewLabels): DeployPlan {
  const k8s = labels.targetKind === "k8s";
  const image = k8s
    ? `kind-registry:5000/${APP}:${COMMIT.slice(0, 12)}`
    : `881050425883.dkr.ecr.us-east-2.amazonaws.com/groundcrew-${APP}:${COMMIT.slice(0, 12)}`;
  const given = new Map((request.env ?? []).map((v) => [v.name, v]));
  const names = new Set(["DATABASE_URL", ...given.keys()]);
  const env = [...names].sort().map((name) => {
    const v = given.get(name);
    const secret = v?.secret ?? name === "DATABASE_URL";
    const provided = Boolean(v && v.value);
    return { name, secret, required: name === "DATABASE_URL", provided, value: !secret && provided ? v!.value : null };
  });
  const missing = env.filter((v) => v.required && !v.provided).map((v) => v.name);
  const allow = request.options?.allow_missing_env ?? false;
  const masked = env.map((v) => `${v.name}=${v.secret ? "********" : (v.value ?? "")}`);
  const rendered = k8s
    ? [
        `apiVersion: v1\nkind: Namespace\nmetadata:\n  name: gc-${APP}\n  labels: {project: groundcrew}`,
        `apiVersion: v1\nkind: Secret\nmetadata: {name: ${APP}-env, namespace: gc-${APP}}\nstringData:\n${env
          .filter((v) => v.secret && v.provided)
          .map((v) => `  ${v.name}: "********"`)
          .join("\n") || "  {}"}`,
        `apiVersion: apps/v1\nkind: Deployment\nmetadata: {name: ${APP}, namespace: gc-${APP}}\nspec:\n  replicas: 1\n  template:\n    spec:\n      containers:\n        - name: ${APP}\n          image: ${image}\n          ports: [{containerPort: 8000}]\n          resources: {requests: {cpu: 250m, memory: 512Mi}}\n          readinessProbe: {httpGet: {path: /health, port: 8000}}\n          env: [${masked.join(", ")}]`,
        `apiVersion: v1\nkind: Service\nmetadata: {name: ${APP}, namespace: gc-${APP}}\nspec: {ports: [{port: 80, targetPort: 8000}]}`,
        `apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: ${APP}, namespace: gc-${APP}}\nspec: {rules: [{host: ${APP}.localhost}]}`,
      ].join("\n---\n")
    : JSON.stringify(
        {
          taskDefinition: {
            family: `groundcrew-${APP}`,
            cpu: "256",
            memory: "512",
            containerDefinitions: [
              {
                name: APP,
                image,
                portMappings: [{ containerPort: 8000 }],
                environment: env.filter((v) => !v.secret && v.provided).map((v) => ({ name: v.name, value: v.value })),
                secrets: env
                  .filter((v) => v.secret && v.provided)
                  .map((v) => ({ name: v.name, valueFrom: `groundcrew/${APP}/${v.name}` })),
              },
            ],
          },
          service: { cluster: "groundcrew-deploys", serviceName: APP, desiredCount: 1, assignPublicIp: "ENABLED" },
        },
        null,
        2,
      );
  return {
    type: "deploy_plan",
    target: labels.targetKind,
    target_connection_id: request.target_connection_id,
    app: APP,
    image,
    port: 8000,
    health_path: "/health",
    replicas: 1,
    cpu: k8s ? "250m" : "256",
    memory: k8s ? "512Mi" : "512",
    env,
    resources: k8s
      ? [
          res("Namespace", `gc-${APP}`),
          res("RoleBinding", "groundcrew-deployer"),
          ...(env.some((v) => v.secret && v.provided) ? [res("Secret", `${APP}-env`)] : []),
          res("Deployment", APP, "1 replica, 250m / 512Mi"),
          res("Service", APP),
          res("Ingress", `${APP}.localhost`),
        ]
      : [
          res("ecr:Repository", `groundcrew-${APP}`),
          res("logs:LogGroup", `/groundcrew/${APP}`),
          res("ec2:SecurityGroup", `groundcrew-${APP}`, "tcp/8000 from 0.0.0.0/0"),
          ...env
            .filter((v) => v.secret && v.provided)
            .map((v) => (res("secretsmanager:Secret", `groundcrew/${APP}/${v.name}`))),
          res("ecs:TaskDefinition", `groundcrew-${APP}`, "0.25 vCPU / 0.5 GB"),
          res("ecs:Service", APP, "groundcrew-deploys, public IP"),
        ],
    preflight: [
      { name: "image", status: "pass", detail: `${image} pushed`, overridden: false },
      {
        name: "env",
        status: missing.length ? "fail" : "pass",
        detail: missing.length ? `Missing required: ${missing.join(", ")}` : "All required env vars set",
        overridden: missing.length > 0 && allow,
      },
      { name: "port", status: "pass", detail: "8000 from the start command", overridden: false },
      { name: "name_collision", status: "pass", detail: `No existing ${k8s ? `gc-${APP}` : APP}`, overridden: false },
    ],
    allow_missing_env: allow,
    expected_url: k8s ? `http://${APP}.localhost` : null,
    rendered,
    rendered_format: k8s ? "yaml" : "json",
    rollback_to_revision: null,
  };
}

function script(d: FakeDeploy, request: DeployCreate, labels: PreviewLabels): Beat[] {
  const deployPlan = plan(d, request, labels);
  const approvalId = crypto.randomUUID();
  const k8s = labels.targetKind === "k8s";
  const url = k8s ? `http://${APP}.localhost` : "http://203.0.113.24:8000";
  return [
    { delay: 400, run: () => setRun(d, { status: "running", started_at: now() }) },
    { delay: 300, run: () => event(d, "scout", { type: "stage_started", stage: "scout", agent: "scout" }) },
    { delay: 900, run: () => void llm(d, "scout", "nano", "Flask app, gunicorn on port 8000, health at /health", [1840, 212], 0.00016) },
    {
      delay: 300,
      run: () =>
        event(d, "scout", {
          type: "stage_finished",
          stage: "scout",
          agent: "scout",
          status: "succeeded",
          duration_ms: 1500,
          summary: "Python 3.12, Flask, gunicorn on 8000, health /health",
        }),
    },
    { delay: 300, run: () => event(d, "builder", { type: "stage_started", stage: "build", agent: "builder" }) },
    { delay: 1100, run: () => void llm(d, "builder", "super", "Wrote a Dockerfile: python:3.12-slim, pip install, gunicorn", [2210, 486], 0.0011) },
    {
      delay: 1600,
      run: () =>
        event(d, "builder", {
          type: "build_attempt",
          attempt: 1,
          max_attempts: 5,
          dockerfile_source: "generated",
          dockerfile_diff: NAIVE,
          rationale: "Slim Python image, dependencies from requirements.txt, gunicorn as in the Procfile.",
          ok: false,
          duration_ms: 77600,
          failing_step: "RUN pip install --no-cache-dir -r requirements.txt",
          error_summary: "psycopg2 builds from source and the image has no pg_config (Postgres headers).",
          log_tail: FAILED_LOG,
          image_ref: null,
          model_call: {
            task: "write_dockerfile",
            model_tier: "super",
            model_id: "nvidia/nemotron-3-super-120b-a12b",
            reasoning_mode: "low",
            escalated: false,
            prompt_tokens: 2210,
            completion_tokens: 486,
            cost_usd: 0.0011,
            step_seq: d.steps.length,
          },
        }),
    },
    { delay: 1200, run: () => void llm(d, "builder", "super", "Install gcc, libc6-dev, and libpq-dev before pip install", [3105, 940], 0.0018) },
    {
      delay: 1800,
      run: () =>
        event(d, "builder", {
          type: "build_attempt",
          attempt: 2,
          max_attempts: 5,
          dockerfile_source: "fixed",
          dockerfile_diff: FIX_DIFF,
          rationale: "pg_config comes with libpq-dev; gcc and libc6-dev compile the extension.",
          ok: true,
          duration_ms: 96900,
          failing_step: null,
          error_summary: null,
          log_tail: null,
          image_ref: `gc-build/${APP}:${COMMIT.slice(0, 12)}`,
          model_call: {
            task: "fix_build",
            model_tier: "super",
            model_id: "nvidia/nemotron-3-super-120b-a12b",
            reasoning_mode: "on",
            escalated: false,
            prompt_tokens: 3105,
            completion_tokens: 940,
            cost_usd: 0.0018,
            step_seq: d.steps.length,
          },
        }),
    },
    {
      delay: 300,
      run: () =>
        event(d, "builder", {
          type: "stage_finished",
          stage: "build",
          agent: "builder",
          status: "succeeded",
          duration_ms: 176000,
          summary: "Built on attempt 2 after an automatic fix",
        }),
    },
    ...stage(d, "smoke", "builder", "/health answered 200 in the sandbox", 900),
    ...stage(d, "push", "builder", `Pushed ${deployPlan.image}`, 700),
    { delay: 300, run: () => event(d, "deployer", { type: "stage_started", stage: "plan", agent: "deployer" }) },
    { delay: 900, run: () => void llm(d, "deployer", "super", "1 replica, 250m / 512Mi; DATABASE_URL is a secret", [1560, 310], 0.0007) },
    {
      delay: 300,
      run: () =>
        event(d, "deployer", {
          type: "stage_finished",
          stage: "plan",
          agent: "deployer",
          status: "succeeded",
          duration_ms: 1300,
          summary: `${deployPlan.resources.length} resources on ${k8s ? "Kubernetes" : "AWS ECS"}`,
        }),
    },
    ...stage(d, "preflight", "deployer", "Image, env, port, and names checked", 600),
    {
      delay: 400,
      run: () => {
        const approval: Approval = {
          id: approvalId,
          run_id: d.run.id,
          workspace_id: d.run.workspace_id,
          run_agent: "deploy",
          run_goal: d.run.goal,
          tool_name: k8s ? "k8s_apply" : "ecs_apply",
          policy: "write",
          arguments: { plan: deployPlan.app, target: deployPlan.target },
          status: "pending",
          decided_by: null,
          decided_by_login: null,
          decided_by_name: null,
          decided_by_avatar_url: null,
          decided_at: null,
          executed_at: null,
          created_at: now(),
        };
        d.approvals[approvalId] = approval;
        d.detail = { ...d.detail, plan: deployPlan, status: "waiting_approval" };
        event(d, "deployer", { type: "stage_started", stage: "approval", agent: "deployer" });
        const s = step(d, "approval_requested", "deployer", { approval_id: approvalId, data: deployPlan, tool_name: approval.tool_name, tool_policy: "write" });
        d.steps.push(s);
        emit(d, { type: "approval_requested", event: { step: s, approval } });
        setRun(d, { status: "waiting_approval", stop_reason: "waiting_approval" });
        d.paused = true;
      },
    },
    // Resumed by decide(); a rejection replaces the rest of the script (see decide()).
    { delay: 300, run: () => setRun(d, { status: "running", stop_reason: null }) },
    ...stage(d, "apply", "deployer", k8s ? "Namespace, Deployment, Service, Ingress created" : "ECS service created", 1400),
    { delay: 300, run: () => event(d, "deployer", { type: "stage_started", stage: "verify", agent: "deployer" }) },
    {
      delay: 1500,
      run: () =>
        event(d, "deployer", {
          type: "verify_result",
          ok: true,
          duration_ms: k8s ? 6200 : 41000,
          checks: [
            {
              name: "rollout",
              ok: true,
              detail: k8s ? "1/1 replicas ready" : "service stable, 1 task running",
              url: null,
              status_code: null,
              latency_ms: null,
            },
            { name: "health", ok: true, detail: "HTTP 200", url: `${url}/health`, status_code: 200, latency_ms: 18 },
          ],
          logs_tail: null,
        }),
    },
    {
      delay: 300,
      run: () =>
        event(d, "deployer", {
          type: "stage_finished",
          stage: "verify",
          agent: "deployer",
          status: "succeeded",
          duration_ms: 1500,
          summary: "Rollout and /health passed",
        }),
    },
    { delay: 300, run: () => event(d, "deployer", { type: "live_url", url, target: deployPlan.target, app: APP, resource_id: null }) },
    { delay: 300, run: () => finish(d, "live", url) },
  ];
}

function finish(d: FakeDeploy, outcome: "live" | "stopped", url: string | null, error: string | null = null): void {
  const started = d.run.started_at ?? d.run.created_at;
  const duration = Date.now() - new Date(started).getTime();
  const status = outcome === "live" ? "succeeded" : "stopped";
  const attempts = d.steps.filter((s) => s.data?.type === "build_attempt").length;
  event(d, "deploy", {
    type: "run_finished",
    status,
    stop_reason: outcome === "live" ? "completed" : "stopped_by_user",
    error,
    live_url: url,
    build_attempts: attempts,
    automatic_fix: attempts > 1,
    duration_ms: duration,
    cost_usd: d.run.cost_usd,
    ultra_equiv_usd: null,
  });
  setRun(d, {
    status,
    stop_reason: outcome === "live" ? "completed" : "stopped_by_user",
    error,
    finished_at: now(),
    duration_ms: duration,
  });
  d.detail = {
    ...d.detail,
    status: outcome === "live" ? "live" : "stopped",
    run_status: status,
    stop_reason: d.run.stop_reason,
    live_url: url,
    build_attempts: attempts,
    automatic_fix: attempts > 1,
    cost_usd: d.run.cost_usd,
    error,
    finished_at: d.run.finished_at,
    duration_ms: duration,
  };
  emit(d, { type: "done", status: { ...omit(d.run, "steps", "approvals"), result: null, error } });
}

function tick(d: FakeDeploy): void {
  d.timer = null;
  if (d.paused || d.next >= d.beats.length || d.listeners.size === 0) return;
  const beat = d.beats[d.next]!;
  d.timer = setTimeout(() => {
    d.timer = null;
    d.next += 1;
    beat.run();
    tick(d);
  }, beat.delay);
}

// --- API stand-ins ----------------------------------------------------------------------

export function createDeploy(request: DeployCreate, labels: PreviewLabels): Schemas["DeployCreated"] {
  const deployId = crypto.randomUUID();
  const runId = crypto.randomUUID();
  const created = now();
  const source =
    "fixture" in request.repo
      ? { kind: "fixture" as const, repo: request.repo.fixture, ref: null, connection_id: null, commit: null }
      : {
          kind: "github" as const,
          repo: request.repo.repo,
          ref: request.repo.ref,
          connection_id: request.repo.connection_id,
          commit: COMMIT,
        };
  const d: FakeDeploy = {
    detail: {
      id: deployId,
      run_id: runId,
      workspace_id: request.workspace_id,
      status: "running",
      run_status: "queued",
      stop_reason: null,
      stage: "scout",
      app: APP,
      source,
      target: { connection_id: request.target_connection_id, kind: labels.targetKind, name: labels.targetName },
      live_url: null,
      build_attempts: 0,
      automatic_fix: false,
      cost_usd: null,
      ultra_equiv_usd: null,
      error: null,
      created_by: null,
      created_at: created,
      started_at: created,
      finished_at: null,
      duration_ms: null,
      env: (request.env ?? []).map((v) => ({
        name: v.name,
        secret: v.secret ?? false,
        required: v.name === "DATABASE_URL",
        provided: Boolean(v.value),
        value: null,
      })),
      allow_missing_env: request.options?.allow_missing_env ?? false,
      stages: [],
      plan: null,
      deploy: null,
    },
    run: {
      id: runId,
      workspace_id: request.workspace_id,
      agent: "deploy",
      goal: `Deploy ${labels.repo}${labels.ref ? `@${labels.ref}` : ""} to ${labels.targetName}`,
      status: "queued",
      stop_reason: null,
      result: null,
      error: null,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      cost_usd: 0,
      created_at: created,
      started_at: null,
      finished_at: null,
      duration_ms: null,
      rejected_actions: 0,
      steps: [],
      approvals: [],
    },
    steps: [],
    approvals: {},
    beats: [],
    next: 0,
    paused: false,
    listeners: new Set(),
    timer: null,
  };
  d.beats = script(d, request, labels);
  deploys.set(deployId, d);
  byRun.set(runId, deployId);
  return { deploy_id: deployId, run_id: runId };
}

export function listDeploys(workspaceId: string): Schemas["DeployPage"] {
  const items: DeploySummary[] = [...deploys.values()]
    .filter((d) => d.detail.workspace_id === workspaceId)
    .map((d) => omit(d.detail, "env", "allow_missing_env", "stages", "plan", "deploy"))
    .reverse();
  return { items, next_cursor: null };
}

export function getDeploy(deployId: string): DeployDetail | null {
  return deploys.get(deployId)?.detail ?? null;
}

export function isPreviewRun(runId: string): boolean {
  return byRun.has(runId);
}

/** Replays the run so far into `listener`, then streams the rest. Returns an unsubscribe. */
export function subscribe(runId: string, listener: (action: TimelineAction) => void): () => void {
  const d = deploys.get(byRun.get(runId) ?? "");
  if (!d) return () => {};
  listener({ type: "loaded", run: { ...d.run, steps: [...d.steps], approvals: Object.values(d.approvals) } });
  d.listeners.add(listener);
  if (!d.timer) tick(d);
  return () => {
    d.listeners.delete(listener);
    if (d.listeners.size === 0 && d.timer) {
      clearTimeout(d.timer);
      d.timer = null;
    }
  };
}

export function decide(runId: string, approvalId: string, decision: Decision): Approval {
  const d = deploys.get(byRun.get(runId) ?? "");
  const approval = d?.approvals[approvalId];
  if (!d || !approval || approval.status !== "pending") throw new Error("This approval was already decided.");
  const decided: Approval = {
    ...approval,
    status: decision === "approve" ? "approved" : "rejected",
    decided_by_name: "You (preview)",
    decided_at: now(),
  };
  d.approvals[approvalId] = decided;
  const s = step(d, "approval_decided", "deployer", { approval_id: approvalId });
  d.steps.push(s);
  emit(d, { type: "approval_decided", event: { step: s, approval: decided } });
  event(d, "deployer", {
    type: "stage_finished",
    stage: "approval",
    agent: "deployer",
    status: decision === "approve" ? "succeeded" : "failed",
    duration_ms: 0,
    summary: decision === "approve" ? "Plan approved" : "Plan rejected; nothing was changed",
  });
  if (decision === "reject") {
    d.beats = d.beats.slice(0, d.next);
    finish(d, "stopped", null, "The plan was rejected. Nothing was deployed.");
  }
  d.paused = false;
  tick(d);
  return decided;
}
