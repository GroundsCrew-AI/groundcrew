/**
 * Preview responses for the Deploy page while POST /deploys and GET /deploys answer 501.
 *
 * TODO(M6): delete this file and its imports once the pipeline is wired up. Nothing here is
 * used when the API answers: lib/deploys.ts falls back to it only on a 501, and the page
 * shows a "Preview" banner whenever it does.
 *
 * Every value is typed with the real M1 schema (lib/api/schema.d.ts), and the scripted run
 * emits the same reducer actions as the SSE stream, so the page is built against the real
 * contract. The script is GroundsCrew-AI/legacy-flask-app with one automatic build fix
 * (docs/demo.md), whatever repository was picked.
 *
 * Time is simulated: each beat plays in a second or two but advances a clock by what the step
 * would really take (a 97 s build, a 41 s ECS verify). Step times, stage durations, and the
 * run's elapsed time all come from that clock, so they add up.
 */
import type { Approval, Decision, RunDetail, Schemas, Step } from "@/lib/api/client";
import type { EnvFix } from "@/lib/deploys";
import type { TimelineAction } from "@/lib/timeline";

type DeployCreate = Schemas["DeployCreate"];
type DeploySummary = Schemas["DeploySummary"];
type DeployDetail = Schemas["DeployDetail"];
type DeployPlan = Schemas["DeployPlan"];
type EnvVarView = Schemas["EnvVarView"];
type Stage = Schemas["StageStarted"]["stage"];
type StageAgent = Schemas["StageStarted"]["agent"];
type TargetKind = DeployPlan["target"];
type EventData = NonNullable<Step["data"]>;

export type PreviewLabels = { repo: string; ref: string | null; targetName: string; targetKind: TargetKind };

/** `delay`: real ms before the beat plays. `ms`: simulated ms it stands for (default `delay`). */
type Beat = { delay: number; ms?: number; pause?: boolean; run: () => void };

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
  targetKind: TargetKind;
  targetConnectionId: string;
  // Env as the plan sees it. Secret values are never kept, only that one was given.
  env: Map<string, { secret: boolean; provided: boolean; value: string | null }>;
  allowMissing: boolean;
  // Simulated clock: ms since `anchor` (epoch ms), advanced by each beat.
  anchor: number;
  clock: number;
  startClock: number;
  lastReal: number;
  stageStart: Partial<Record<Stage, number>>;
};

const deploys = new Map<string, FakeDeploy>();
const byRun = new Map<string, string>();

const REPO = "GroundsCrew-AI/legacy-flask-app";
const APP = "legacy-flask-app";
const COMMIT = "3f2a9c1b7d4e5f60718293a4b5c6d7e8f9a0b1c2";
const IMAGE_ID = "sha256:9c41e0d2b7a35f1e86c0a4d9e2b7f3c15a8d6e0f4b2c9a71d3e5f8b0c6a4d2e1";
const REQUIRED = "DATABASE_URL";
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

function res(kind: string, name: string, detail: string | null = null, action: Resource["action"] = "create"): Resource {
  return { kind, name, action, detail };
}

function omit<T extends object, K extends keyof T>(value: T, ...keys: K[]): Omit<T, K> {
  const copy = { ...value };
  for (const key of keys) delete copy[key];
  return copy;
}

/** The simulated time now. */
function vnow(d: FakeDeploy): string {
  return new Date(d.anchor + d.clock).toISOString();
}

/** Moves the clock on by `ms`, or by the real time that passed if that is longer (a wait). */
function advance(d: FakeDeploy, ms: number): void {
  const real = Date.now() - d.lastReal;
  d.clock += Math.max(ms, real);
  d.lastReal = Date.now();
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
    created_at: vnow(d),
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

function startStage(d: FakeDeploy, stage: Stage, agent: StageAgent): void {
  d.stageStart[stage] = d.clock;
  event(d, agent, { type: "stage_started", stage, agent });
}

function finishStage(
  d: FakeDeploy,
  stage: Stage,
  agent: StageAgent,
  summary: string,
  status: Schemas["StageFinished"]["status"] = "succeeded",
): void {
  const duration = d.clock - (d.stageStart[stage] ?? d.clock);
  event(d, agent, { type: "stage_finished", stage, agent, status, duration_ms: duration, summary });
}

/** A stage with nothing in between: `real` ms on screen standing for `sim` ms. */
function stage(d: FakeDeploy, name: Stage, agent: StageAgent, summary: string, real: number, sim: number): Beat[] {
  return [
    { delay: 300, run: () => startStage(d, name, agent) },
    { delay: real, ms: sim, run: () => finishStage(d, name, agent, summary) },
  ];
}

const MODEL_ID = { nano: "nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B", super: "nvidia/nemotron-3-super-120b-a12b" };

function llmLatency(tokens: [number, number]): number {
  return 1800 + tokens[1];
}

/** A model call beat; its simulated time is the call's latency. */
function llm(
  d: FakeDeploy,
  delay: number,
  agent: string,
  tier: "nano" | "super",
  summary: string,
  tokens: [number, number],
  cost: number,
): Beat {
  return {
    delay,
    ms: llmLatency(tokens),
    run: () => {
      push(
        d,
        step(d, "llm", agent, {
          model_tier: tier,
          model_id: MODEL_ID[tier],
          reasoning_mode: tier === "nano" ? "low" : "on",
          prompt_tokens: tokens[0],
          completion_tokens: tokens[1],
          latency_ms: llmLatency(tokens),
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
    },
  };
}

function envViews(d: FakeDeploy): EnvVarView[] {
  const names = new Set([REQUIRED, ...d.env.keys()]);
  return [...names].sort().map((name) => {
    const v = d.env.get(name);
    const secret = v?.secret ?? name === REQUIRED;
    const provided = v?.provided ?? false;
    return { name, secret, required: name === REQUIRED, provided, value: !secret && provided ? (v?.value ?? null) : null };
  });
}

function missingEnv(d: FakeDeploy): string[] {
  return envViews(d)
    .filter((v) => v.required && !v.provided)
    .map((v) => v.name);
}

function plan(d: FakeDeploy): DeployPlan {
  const k8s = d.targetKind === "k8s";
  const tag = COMMIT.slice(0, 12);
  const image = k8s
    ? `kind-registry:5000/${APP}:${tag}`
    : `881050425883.dkr.ecr.us-east-2.amazonaws.com/groundcrew-${APP}:${tag}`;
  const env = envViews(d);
  const missing = missingEnv(d);
  const masked = env.map((v) => `${v.name}=${v.secret ? "********" : (v.value ?? "")}`);
  const secrets = env.filter((v) => v.secret && v.provided);
  const pushImage = res("image", image, `${IMAGE_ID.slice(0, 19)} from the sandbox`, "push");
  const rendered = k8s
    ? [
        `apiVersion: v1\nkind: Namespace\nmetadata:\n  name: gc-${APP}\n  labels: {project: groundcrew}`,
        `apiVersion: v1\nkind: Secret\nmetadata: {name: ${APP}-env, namespace: gc-${APP}}\nstringData:\n${
          secrets.map((v) => `  ${v.name}: "********"`).join("\n") || "  {}"
        }`,
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
                secrets: secrets.map((v) => ({ name: v.name, valueFrom: `groundcrew/${APP}/${v.name}` })),
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
    target: d.targetKind,
    target_connection_id: d.targetConnectionId,
    app: APP,
    image,
    source_image: IMAGE_ID,
    port: 8000,
    health_path: "/health",
    replicas: 1,
    cpu: k8s ? "250m" : "256",
    memory: k8s ? "512Mi" : "512",
    env,
    // In apply order: nothing leaves the sandbox before approval, so the push comes first.
    resources: k8s
      ? [
          pushImage,
          res("Namespace", `gc-${APP}`),
          res("RoleBinding", "groundcrew-deployer"),
          ...(secrets.length ? [res("Secret", `${APP}-env`)] : []),
          res("Deployment", APP, "1 replica, 250m / 512Mi"),
          res("Service", APP),
          res("Ingress", `${APP}.localhost`),
        ]
      : [
          res("ecr:Repository", `groundcrew-${APP}`),
          pushImage,
          res("logs:LogGroup", `/groundcrew/${APP}`),
          res("ec2:SecurityGroup", `groundcrew-${APP}`, "tcp/8000 from 0.0.0.0/0"),
          ...secrets.map((v) => res("secretsmanager:Secret", `groundcrew/${APP}/${v.name}`)),
          res("ecs:TaskDefinition", `groundcrew-${APP}`, "0.25 vCPU / 0.5 GB"),
          res("ecs:Service", APP, "groundcrew-deploys, public IP"),
        ],
    preflight: [
      {
        name: "image",
        status: "pass",
        detail: `Built and smoke-tested (${IMAGE_ID.slice(0, 19)}); pushed after approval`,
        overridden: false,
      },
      {
        name: "env",
        status: missing.length ? "fail" : "pass",
        detail: missing.length ? `Missing required: ${missing.join(", ")}` : "All required env vars set",
        overridden: missing.length > 0 && d.allowMissing,
      },
      { name: "port", status: "pass", detail: "8000 from the start command", overridden: false },
      { name: "name_collision", status: "pass", detail: `No existing ${k8s ? `gc-${APP}` : APP}`, overridden: false },
    ],
    allow_missing_env: d.allowMissing,
    expected_url: k8s ? `http://${APP}.localhost` : null,
    rendered,
    rendered_format: k8s ? "yaml" : "json",
    rollback_to_revision: null,
  };
}

/** Renders the plan from the current env and asks for approval; the run pauses here. */
function requestApproval(d: FakeDeploy): void {
  const deployPlan = plan(d);
  const id = crypto.randomUUID();
  const approval: Approval = {
    id,
    run_id: d.run.id,
    workspace_id: d.run.workspace_id,
    run_agent: "deploy",
    run_goal: d.run.goal,
    tool_name: d.targetKind === "k8s" ? "k8s_apply" : "ecs_apply",
    policy: "write",
    arguments: { plan: deployPlan.app, target: deployPlan.target },
    status: "pending",
    decided_by: null,
    decided_by_login: null,
    decided_by_name: null,
    decided_by_avatar_url: null,
    decided_at: null,
    executed_at: null,
    created_at: vnow(d),
  };
  d.approvals[id] = approval;
  d.detail = { ...d.detail, plan: deployPlan, status: "waiting_approval" };
  startStage(d, "approval", "deployer");
  const s = step(d, "approval_requested", "deployer", {
    approval_id: id,
    data: deployPlan,
    tool_name: approval.tool_name,
    tool_policy: "write",
  });
  d.steps.push(s);
  emit(d, { type: "approval_requested", event: { step: s, approval } });
  setRun(d, { status: "waiting_approval", stop_reason: "waiting_approval" });
  d.paused = true;
}

function script(d: FakeDeploy): Beat[] {
  const k8s = d.targetKind === "k8s";
  const url = k8s ? `http://${APP}.localhost` : "http://203.0.113.24:8000";
  const writeTokens: [number, number] = [2210, 486];
  const fixTokens: [number, number] = [3105, 940];
  return [
    {
      delay: 400,
      run: () => {
        d.startClock = d.clock;
        setRun(d, { status: "running", started_at: vnow(d) });
        d.detail = { ...d.detail, started_at: vnow(d) };
      },
    },
    { delay: 300, run: () => startStage(d, "scout", "scout") },
    llm(d, 900, "scout", "nano", "Flask app, gunicorn on port 8000, health at /health", [1840, 212], 0.00016),
    { delay: 300, run: () => finishStage(d, "scout", "scout", "Python 3.12, Flask, gunicorn on 8000, health /health") },
    { delay: 300, run: () => startStage(d, "build", "builder") },
    llm(d, 1100, "builder", "super", "Wrote a Dockerfile: python:3.12-slim, pip install, gunicorn", writeTokens, 0.0011),
    {
      delay: 1600,
      ms: 77600,
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
            model_id: MODEL_ID.super,
            reasoning_mode: "low",
            escalated: false,
            prompt_tokens: writeTokens[0],
            completion_tokens: writeTokens[1],
            cost_usd: 0.0011,
            step_seq: d.steps.length,
          },
        }),
    },
    llm(d, 1200, "builder", "super", "Install gcc, libc6-dev, and libpq-dev before pip install", fixTokens, 0.0018),
    {
      delay: 1800,
      ms: 96900,
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
            model_id: MODEL_ID.super,
            reasoning_mode: "on",
            escalated: false,
            prompt_tokens: fixTokens[0],
            completion_tokens: fixTokens[1],
            cost_usd: 0.0018,
            step_seq: d.steps.length,
          },
        }),
    },
    { delay: 300, run: () => finishStage(d, "build", "builder", "Built on attempt 2 after an automatic fix") },
    ...stage(d, "smoke", "builder", `/health answered 200 in the sandbox (${IMAGE_ID.slice(0, 19)})`, 900, 4200),
    { delay: 300, run: () => startStage(d, "plan", "deployer") },
    llm(d, 900, "deployer", "super", "1 replica, 250m / 512Mi; DATABASE_URL is a secret", [1560, 310], 0.0007),
    {
      delay: 300,
      run: () =>
        finishStage(d, "plan", "deployer", `${plan(d).resources.length} apply steps on ${k8s ? "Kubernetes" : "AWS ECS"}`),
    },
    ...stage(d, "preflight", "deployer", "Image built and smoke-tested; env, port, and names checked", 600, 1100),
    { delay: 400, pause: true, run: () => requestApproval(d) },
    // Resumed by decide(); a rejection replaces the rest of the script (see decide()).
    { delay: 300, run: () => setRun(d, { status: "running", stop_reason: null }) },
    ...stage(
      d,
      "apply",
      "deployer",
      k8s
        ? "Pushed the image to kind-registry:5000, then created the namespace, Deployment, Service, and Ingress"
        : "Created the ECR repository and pushed the image, then created the log group, security group, task definition, and service",
      1400,
      k8s ? 9000 : 48000,
    ),
    { delay: 300, run: () => startStage(d, "verify", "deployer") },
    {
      delay: 1500,
      ms: k8s ? 6200 : 41000,
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
    { delay: 300, run: () => finishStage(d, "verify", "deployer", "Rollout and /health passed") },
    {
      delay: 300,
      run: () => event(d, "deployer", { type: "live_url", url, target: d.targetKind, app: APP, resource_id: null }),
    },
    { delay: 300, run: () => finish(d, "live", url) },
  ];
}

function finish(d: FakeDeploy, outcome: "live" | "stopped", url: string | null, error: string | null = null): void {
  const duration = d.clock - d.startClock;
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
    finished_at: vnow(d),
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
    advance(d, beat.ms ?? beat.delay);
    beat.run();
    tick(d);
  }, beat.delay);
}

function pendingApproval(runId: string, approvalId: string): { d: FakeDeploy; approval: Approval } {
  const d = deploys.get(byRun.get(runId) ?? "");
  const approval = d?.approvals[approvalId];
  if (!d || !approval || approval.status !== "pending") throw new Error("This approval was already decided.");
  return { d, approval };
}

/** Closes the pending approval: approved, rejected, or replaced by a new plan (a rejection). */
function close(d: FakeDeploy, approval: Approval, status: "approved" | "rejected"): Approval {
  advance(d, 0);
  const decided: Approval = { ...approval, status, decided_by_name: "You (preview)", decided_at: vnow(d) };
  d.approvals[approval.id] = decided;
  const s = step(d, "approval_decided", "deployer", { approval_id: approval.id });
  d.steps.push(s);
  emit(d, { type: "approval_decided", event: { step: s, approval: decided } });
  return decided;
}

// --- API stand-ins ----------------------------------------------------------------------

export function createDeploy(request: DeployCreate, labels: PreviewLabels): Schemas["DeployCreated"] {
  const deployId = crypto.randomUUID();
  const runId = crypto.randomUUID();
  // The script is legacy-flask-app, so the preview says so whichever repository was picked.
  const source =
    "fixture" in request.repo
      ? { kind: "fixture" as const, repo: request.repo.fixture, ref: null, connection_id: null, commit: null }
      : {
          kind: "github" as const,
          repo: REPO,
          ref: request.repo.ref,
          connection_id: request.repo.connection_id,
          commit: COMMIT,
        };
  const env = new Map(
    (request.env ?? []).map((v) => {
      const secret = v.secret ?? false;
      return [v.name, { secret, provided: Boolean(v.value), value: secret ? null : v.value }] as const;
    }),
  );
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
      created_at: "",
      started_at: null,
      finished_at: null,
      duration_ms: null,
      env: [],
      allow_missing_env: request.options?.allow_missing_env ?? false,
      stages: [],
      plan: null,
      deploy: null,
    },
    run: {
      id: runId,
      workspace_id: request.workspace_id,
      agent: "deploy",
      goal: `Deploy ${source.kind === "github" ? REPO : source.repo}${source.ref ? `@${source.ref}` : ""} to ${labels.targetName}`,
      status: "queued",
      stop_reason: null,
      result: null,
      error: null,
      prompt_tokens: 0,
      completion_tokens: 0,
      total_tokens: 0,
      cost_usd: 0,
      created_at: "",
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
    targetKind: labels.targetKind,
    targetConnectionId: request.target_connection_id,
    env,
    allowMissing: request.options?.allow_missing_env ?? false,
    anchor: 0,
    clock: 0,
    startClock: 0,
    lastReal: Date.now(),
    stageStart: {},
  };
  d.beats = script(d);
  // Start the simulated clock in the past by the time the beats up to the approval stand for
  // beyond what they take on screen, so the approval is requested about "now".
  const pause = d.beats.findIndex((b) => b.pause);
  const ahead = d.beats.slice(0, pause + 1).reduce((sum, b) => sum + (b.ms ?? b.delay) - b.delay, 0);
  d.anchor = Date.now() - ahead;
  const created = vnow(d);
  d.detail = { ...d.detail, created_at: created, env: envViews(d).filter((v) => d.env.has(v.name)) };
  d.run = { ...d.run, created_at: created };
  deploys.set(deployId, d);
  byRun.set(runId, deployId);
  return { deploy_id: deployId, run_id: runId };
}

export function listDeploys(workspaceId: string): Schemas["DeployPage"] {
  const items: DeploySummary[] = [...deploys.values()]
    .filter((d) => d.detail.workspace_id === workspaceId)
    .map((d) => ({
      ...omit(d.detail, "env", "allow_missing_env", "stages", "plan", "deploy"),
      // Simulated time, like the run page (a real list computes it from started_at).
      duration_ms: d.detail.duration_ms ?? elapsed(d.run.id),
    }))
    .reverse();
  return { items, next_cursor: null };
}

export function getDeploy(deployId: string): DeployDetail | null {
  return deploys.get(deployId)?.detail ?? null;
}

export function isPreviewRun(runId: string): boolean {
  return byRun.has(runId);
}

/**
 * The run's elapsed time on the simulated clock (the page's "Elapsed"). Between beats it moves
 * at the pace of the beat being played, so a 97 s build counts up while it plays.
 */
export function elapsed(runId: string): number | null {
  const d = deploys.get(byRun.get(runId) ?? "");
  if (!d || !d.run.started_at) return null;
  if (d.run.duration_ms !== null) return d.run.duration_ms;
  const real = Date.now() - d.lastReal;
  const beat = d.beats[d.next];
  const ahead =
    d.paused || !beat ? real : Math.min(real / Math.max(beat.delay, 1), 1) * (beat.ms ?? beat.delay);
  return d.clock - d.startClock + ahead;
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
  const { d, approval } = pendingApproval(runId, approvalId);
  const decided = close(d, approval, decision === "approve" ? "approved" : "rejected");
  finishStage(
    d,
    "approval",
    "deployer",
    decision === "approve" ? "Plan approved" : "Plan rejected; nothing was changed",
    decision === "approve" ? "succeeded" : "failed",
  );
  if (decision === "reject") {
    d.beats = d.beats.slice(0, d.next);
    finish(d, "stopped", null, "The plan was rejected. Nothing was deployed.");
  }
  d.paused = false;
  tick(d);
  return decided;
}

/**
 * Stand-in for POST /deploys/{id}/env (M6): set the missing values, or continue without them.
 * The pending plan is replaced; only preflight runs again and the new plan reuses the image
 * that was already built and smoke-tested (no rebuild). Secret values are not kept.
 */
export function updateEnv(runId: string, approvalId: string, fix: EnvFix): Approval {
  const { d, approval } = pendingApproval(runId, approvalId);
  const before = missingEnv(d);
  for (const v of fix.env) {
    const secret = v.secret ?? false;
    d.env.set(v.name, { secret, provided: Boolean(v.value), value: secret ? null : v.value });
  }
  if (fix.allow_missing_env) d.allowMissing = true;
  const set = fix.env.filter((v) => v.value).map((v) => v.name);
  const still = missingEnv(d);
  const reason = set.length
    ? `${set.join(", ")} set`
    : `continuing without ${before.filter((n) => still.includes(n)).join(", ") || "the missing env vars"}`;

  const replaced = close(d, approval, "rejected");
  finishStage(d, "approval", "deployer", `Plan replaced: ${reason}`, "skipped");
  d.detail = {
    ...d.detail,
    env: envViews(d).filter((v) => d.env.has(v.name)),
    allow_missing_env: d.allowMissing,
    status: "running",
  };
  setRun(d, { status: "running", stop_reason: null });
  d.beats = [
    ...d.beats.slice(0, d.next),
    ...stage(d, "preflight", "deployer", `Re-checked with ${reason}; same image, no rebuild`, 700, 900),
    { delay: 400, run: () => requestApproval(d) },
    ...d.beats.slice(d.next),
  ];
  d.paused = false;
  tick(d);
  return replaced;
}
