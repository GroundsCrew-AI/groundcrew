import type { Step } from "@/lib/api/client";
import type { BuildAttempt, DeployEventData, DeployPlan } from "@/lib/deploys";

import { step } from "./fixtures";

export const SECRET_VALUE = "postgresql://app:hunter2@db:5432/app";

/** A deploy step: kind is the event type, payload in `data`. */
export function dstep(seq: number, data: DeployEventData, overrides: Partial<Step> = {}): Step {
  return step(seq, {
    agent: "deployer",
    kind: data.type ?? "step",
    tool_name: null,
    tool_policy: null,
    output_summary: null,
    data,
    ...overrides,
  });
}

export function attempt(n: number, overrides: Partial<BuildAttempt> = {}): BuildAttempt {
  return {
    type: "build_attempt",
    attempt: n,
    max_attempts: 5,
    dockerfile_source: n === 1 ? "generated" : "fixed",
    dockerfile_diff: n === 1 ? "FROM python:3.12-slim\n" : "+RUN apt-get install -y libpq-dev\n",
    rationale: null,
    ok: n > 1,
    duration_ms: 1000 * n,
    failing_step: n === 1 ? "RUN pip install -r requirements.txt" : null,
    error_summary: n === 1 ? "pg_config executable not found" : null,
    log_tail: n === 1 ? "Error: pg_config executable not found." : null,
    image_ref: n > 1 ? "gc-build/app:1" : null,
    model_call: null,
    ...overrides,
  };
}

export function plan(overrides: Partial<DeployPlan> = {}): DeployPlan {
  return {
    type: "deploy_plan",
    target: "k8s",
    target_connection_id: "conn-k8s",
    app: "legacy-flask-app",
    image: "kind-registry:5000/legacy-flask-app:abc",
    source_image: "sha256:9c41e0d2b7a3",
    port: 8000,
    health_path: "/health",
    replicas: 1,
    cpu: "250m",
    memory: "512Mi",
    env: [
      { name: "DATABASE_URL", secret: true, required: true, provided: true, value: null },
      { name: "MODE", secret: false, required: false, provided: true, value: "production" },
    ],
    resources: [
      { kind: "image", name: "kind-registry:5000/legacy-flask-app:abc", action: "push", detail: null },
      { kind: "Namespace", name: "gc-legacy-flask-app", action: "create", detail: null },
      { kind: "Deployment", name: "legacy-flask-app", action: "create", detail: "1 replica" },
    ],
    preflight: [
      { name: "image", status: "pass", detail: "Built and smoke-tested (sha256:9c41e0d2b7a3)", overridden: false },
      { name: "env", status: "pass", detail: "All required env vars set", overridden: false },
    ],
    allow_missing_env: false,
    expected_url: "http://legacy-flask-app.localhost",
    rendered: 'kind: Secret\nstringData:\n  DATABASE_URL: "********"\n',
    rendered_format: "yaml",
    rollback_to_revision: null,
    ...overrides,
  };
}
