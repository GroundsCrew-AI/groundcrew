"use client";

import { cn } from "cn";
import { Plus, Rocket, Trash2 } from "lucide-react";
import Link from "next/link";
import { type FormEvent, useId, useMemo, useState } from "react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import type { Connection } from "@/lib/discovery";
import type { DeployCreate, TargetKind } from "@/lib/deploys";

export type EnvRow = { key: number; name: string; value: string; secret: boolean; touched: boolean };

const ENV_NAME = /^[A-Za-z_][A-Za-z0-9_]{0,127}$/;
const SECRET_NAME = /(password|passwd|secret|token|api_?key|private_?key|credential|database_url|dsn)/i;

export type DeploySubmit = DeployCreate;

const REPO_NAME = /^[A-Za-z0-9][A-Za-z0-9-]{0,38}\/[A-Za-z0-9._-]{1,100}$/;
// The Select value that switches to typing owner/name (a repo the connection has not listed).
const OTHER_REPO = "__other__";

type Target = { connection: Connection; kind: TargetKind; disabled: boolean };

export function deployTargets(connections: Connection[]): Target[] {
  return connections
    .filter((c) => c.kind === "k8s" || c.kind === "aws" || c.kind === "agent")
    .map((c) => ({
      connection: c,
      kind: c.kind === "k8s" ? ("k8s" as const) : c.kind === "agent" ? ("agent" as const) : ("aws_ecs" as const),
      disabled:
        c.status !== "connected" ||
        (c.kind === "aws" && c.tier !== "deploy") ||
        (c.kind === "agent" && !c.agent?.deploy_services?.length),
    }));
}

function targetDetail(t: Target): string {
  const c = t.connection;
  if (t.kind === "k8s") return "Kubernetes";
  if (t.kind === "aws_ecs") return "AWS ECS (Fargate)";
  const services = c.agent?.deploy_services ?? [];
  const where = c.agent?.registry === "groundcrew" ? "Groundcrew registry" : c.agent?.registry;
  return services.length
    ? `Server with the Groundcrew agent, images in ${where}`
    : "Server with the Groundcrew agent: no service allows deploy yet";
}

/** Problems that keep the form from submitting, by field. */
export function envErrors(rows: EnvRow[]): Record<number, string> {
  const errors: Record<number, string> = {};
  const seen = new Set<string>();
  for (const row of rows) {
    const name = row.name.trim();
    if (!name) errors[row.key] = "Name the variable or remove the row.";
    else if (!ENV_NAME.test(name)) errors[row.key] = "Letters, digits, and _ only; not starting with a digit.";
    else if (seen.has(name)) errors[row.key] = `${name} is listed twice.`;
    seen.add(name);
  }
  return errors;
}

export function DeployForm({
  workspaceId,
  connections,
  submitting,
  onSubmit,
}: {
  workspaceId: string;
  connections: Connection[];
  submitting: boolean;
  onSubmit: (body: DeploySubmit) => void;
}) {
  const id = useId();
  const github = useMemo(() => connections.filter((c) => c.kind === "github" && c.status === "connected"), [connections]);
  const targets = useMemo(() => deployTargets(connections), [connections]);
  const [sourceId, setSourceId] = useState<string>(() => github[0]?.id ?? "");
  const source = github.find((c) => c.id === sourceId);
  const [repo, setRepo] = useState("");
  const [typedRepo, setTypedRepo] = useState("");
  const [ref, setRef] = useState("main");
  const [targetId, setTargetId] = useState<string>(() => targets.find((t) => !t.disabled)?.connection.id ?? "");
  const [service, setService] = useState("");
  const [rows, setRows] = useState<EnvRow[]>([]);
  const [nextKey, setNextKey] = useState(1);
  const [allowMissing, setAllowMissing] = useState(false);
  const [attempted, setAttempted] = useState(false);

  const repos = source?.repos ?? [];
  const typing = repo === OTHER_REPO || (Boolean(source) && repos.length === 0);
  const chosenRepo = typing ? typedRepo.trim() : repo && repos.includes(repo) ? repo : (repos[0] ?? "");
  const repoInvalid = typing && !REPO_NAME.test(chosenRepo);
  const target = targets.find((t) => t.connection.id === targetId && !t.disabled);
  const services = target?.kind === "agent" ? (target.connection.agent?.deploy_services ?? []) : [];
  const chosenService = services.includes(service) ? service : (services[0] ?? "");
  const errors = envErrors(rows);
  // Shown under their fields once a submit was tried.
  const repoError = attempted && repoInvalid;
  const refError = attempted && !ref.trim();
  const ready = Boolean(
    source && chosenRepo && !repoInvalid && ref.trim() && target && Object.keys(errors).length === 0,
  );

  function update(key: number, patch: Partial<EnvRow>) {
    setRows((current) =>
      current.map((row) => {
        if (row.key !== key) return row;
        const next = { ...row, ...patch };
        // Suggest "secret" from the name until the person sets the switch themselves.
        if (patch.name !== undefined && !row.touched) next.secret = SECRET_NAME.test(patch.name);
        return next;
      }),
    );
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    setAttempted(true);
    if (!ready || !source || !target) return;
    onSubmit({
      workspace_id: workspaceId,
      repo: { connection_id: source.id, repo: chosenRepo, ref: ref.trim() },
      target_connection_id: target.connection.id,
      ...(target.kind === "agent" ? { target_service: chosenService } : {}),
      env: rows.map((r) => ({ name: r.name.trim(), value: r.value, secret: r.secret })),
      options: { allow_missing_env: allowMissing },
    });
  }

  return (
    <form onSubmit={submit} className="space-y-6" aria-describedby={`${id}-intro`}>
      <p id={`${id}-intro`} className="text-[13px] text-muted-foreground">
        Scout reads the repository, Builder writes a Dockerfile and fixes it until the build passes, and
        Deployer plans the target config. Nothing touches your infrastructure until you approve the plan.
      </p>

      <fieldset className="space-y-3">
        <legend className="mb-2 text-[13px] font-medium">Source</legend>
        {github.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">
            No GitHub connection yet.{" "}
            <Link href="/connections" className="text-foreground underline underline-offset-4">
              Connect GitHub
            </Link>{" "}
            to pick a repository.
          </p>
        ) : (
          <div className="grid gap-3 sm:grid-cols-[1fr_1.4fr_0.8fr]">
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-gh`}>GitHub</Label>
              <Select value={sourceId} onValueChange={(v) => { setSourceId(v); setRepo(""); }}>
                <SelectTrigger id={`${id}-gh`} className="w-full"><SelectValue /></SelectTrigger>
                <SelectContent>
                  {github.map((c) => <SelectItem key={c.id} value={c.id}>{c.name}</SelectItem>)}
                </SelectContent>
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-repo`}>Repository</Label>
              {repos.length > 0 && (
                <Select value={typing ? OTHER_REPO : chosenRepo} onValueChange={setRepo}>
                  <SelectTrigger id={`${id}-repo`} className="w-full">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {repos.map((r) => <SelectItem key={r} value={r}>{r}</SelectItem>)}
                    <SelectItem value={OTHER_REPO}>Another repository</SelectItem>
                  </SelectContent>
                </Select>
              )}
              {typing && (
                <Input
                  id={repos.length > 0 ? `${id}-repo-typed` : `${id}-repo`}
                  aria-label={repos.length > 0 ? "Repository (owner/name)" : undefined}
                  placeholder="owner/name"
                  value={typedRepo}
                  onChange={(e) => setTypedRepo(e.target.value)}
                  aria-invalid={repoError}
                  aria-describedby={repoError ? `${id}-repo-error` : undefined}
                  className="font-mono text-[12.5px]"
                />
              )}
              {repoError && (
                <p id={`${id}-repo-error`} className="text-[12px] text-[var(--status-failed)]">
                  Enter the repository as owner/name.
                </p>
              )}
            </div>
            <div className="space-y-1.5">
              <Label htmlFor={`${id}-ref`}>Branch, tag, or commit</Label>
              <Input id={`${id}-ref`} value={ref} onChange={(e) => setRef(e.target.value)} maxLength={255} required
                aria-invalid={refError} aria-describedby={refError ? `${id}-ref-error` : undefined} />
              {refError && (
                <p id={`${id}-ref-error`} className="text-[12px] text-[var(--status-failed)]">
                  Enter a branch, tag, or commit.
                </p>
              )}
            </div>
          </div>
        )}
      </fieldset>

      <fieldset>
        <legend className="mb-2 text-[13px] font-medium">Target</legend>
        {targets.length === 0 ? (
          <p className="text-[12.5px] text-muted-foreground">
            No server, cluster, or AWS account yet.{" "}
            <Link href="/connections" className="text-foreground underline underline-offset-4">Add a connection</Link>.
          </p>
        ) : (
          <div role="radiogroup" aria-label="Target" className="grid gap-2 sm:grid-cols-2">
            {targets.map((t) => {
              const selected = t.connection.id === targetId && !t.disabled;
              const observe = t.connection.kind === "aws" && t.connection.tier !== "deploy";
              return (
                <label
                  key={t.connection.id}
                  className={cn(
                    "flex cursor-pointer items-start gap-2.5 rounded-lg border px-3 py-2.5 text-[13px] has-[:focus-visible]:ring-[3px] has-[:focus-visible]:ring-ring/50",
                    selected && "border-primary",
                    t.disabled && "cursor-not-allowed opacity-70",
                  )}
                >
                  <input
                    type="radio"
                    name={`${id}-target`}
                    value={t.connection.id}
                    checked={selected}
                    disabled={t.disabled}
                    onChange={() => setTargetId(t.connection.id)}
                    className="mt-0.5 accent-[var(--primary)]"
                  />
                  <span className="min-w-0 flex-1">
                    <span className="block font-medium">{t.connection.name}</span>
                    <span className="block text-[12px] text-muted-foreground">
                      {targetDetail(t)}
                      {observe && ", observe tier: read only"}
                      {t.connection.status !== "connected" && `, ${t.connection.status}`}
                    </span>
                    {observe && (
                      <Link href="/connections" className="mt-1 inline-block text-[12px] text-foreground underline underline-offset-4">
                        Turn on deploys
                      </Link>
                    )}
                  </span>
                </label>
              );
            })}
          </div>
        )}
        {target?.kind === "agent" && (
          <div className="mt-3 max-w-sm space-y-1.5">
            <Label htmlFor={`${id}-service`}>Service on {target.connection.agent?.hostname ?? "the server"}</Label>
            <Select value={chosenService} onValueChange={setService}>
              <SelectTrigger id={`${id}-service`} className="w-full"><SelectValue /></SelectTrigger>
              <SelectContent>
                {services.map((s) => <SelectItem key={s} value={s}>{s}</SelectItem>)}
              </SelectContent>
            </Select>
            <p className="text-[12px] text-muted-foreground">
              Services whose <code className="font-mono">allow</code> lists deploy in the agent&apos;s configuration.{" "}
              <Link href="/docs/connect/agent#deploys" className="text-foreground underline underline-offset-4">How deploys work</Link>
            </p>
          </div>
        )}
        {attempted && !target && targets.length > 0 && (
          <p className="mt-2 text-[12.5px] text-[var(--status-failed)]">Pick a target.</p>
        )}
      </fieldset>

      <fieldset className="space-y-2">
        <legend className="mb-1 text-[13px] font-medium">Env vars</legend>
        <p className="text-[12.5px] text-muted-foreground">
          Secret values go to a Kubernetes Secret or Secrets Manager. They are never shown again, not even to you.
        </p>
        {rows.length > 0 && (
          <ul className="space-y-2">
            {rows.map((row) => {
              const error = attempted ? errors[row.key] : undefined;
              return (
                // Phones stack name and value, with the switch and remove button under them.
                <li
                  key={row.key}
                  className="grid grid-cols-[1fr_auto] items-start gap-2 rounded-lg border p-2 sm:grid-cols-[1fr_1.4fr_auto_auto] sm:rounded-none sm:border-0 sm:p-0"
                >
                  <div className="col-span-2 sm:col-span-1">
                    <Label htmlFor={`${id}-n${row.key}`} className="sr-only">Name</Label>
                    <Input id={`${id}-n${row.key}`} placeholder="NAME" value={row.name} autoComplete="off"
                      className="font-mono" aria-invalid={Boolean(error)}
                      aria-describedby={error ? `${id}-e${row.key}` : undefined}
                      onChange={(e) => update(row.key, { name: e.target.value })} />
                    {error && <p id={`${id}-e${row.key}`} className="mt-1 text-[12px] text-[var(--status-failed)]">{error}</p>}
                  </div>
                  <div className="col-span-2 sm:col-span-1">
                    <Label htmlFor={`${id}-v${row.key}`} className="sr-only">Value of {row.name || "this variable"}</Label>
                    <Input id={`${id}-v${row.key}`} placeholder="value" value={row.value} autoComplete="off"
                      type={row.secret ? "password" : "text"} className="font-mono"
                      onChange={(e) => update(row.key, { value: e.target.value })} />
                  </div>
                  <label className="flex h-9 items-center gap-1.5 text-[12.5px]">
                    <Switch checked={row.secret} aria-label={`${row.name || "Variable"} is a secret`}
                      onCheckedChange={(v) => update(row.key, { secret: v, touched: true })} />
                    Secret
                  </label>
                  <Button type="button" variant="ghost" size="icon-sm" className="mt-1"
                    aria-label={`Remove ${row.name || "this variable"}`}
                    onClick={() => setRows((r) => r.filter((x) => x.key !== row.key))}>
                    <Trash2 />
                  </Button>
                </li>
              );
            })}
          </ul>
        )}
        <Button type="button" variant="outline" size="sm"
          onClick={() => {
            setRows((r) => [...r, { key: nextKey, name: "", value: "", secret: false, touched: false }]);
            setNextKey((k) => k + 1);
          }}>
          <Plus aria-hidden="true" />
          Add env var
        </Button>
        <label className="flex items-start gap-2 pt-2 text-[13px]">
          <Checkbox checked={allowMissing} onCheckedChange={(v) => setAllowMissing(v === true)} className="mt-0.5" />
          <span>
            Deploy with missing env vars
            <span className="block text-[12px] text-muted-foreground">
              If Scout finds a required variable you did not set, deploy anyway. The plan lists what is missing.
            </span>
          </span>
        </label>
      </fieldset>

      <Button type="submit" disabled={submitting || (attempted && !ready)}>
        <Rocket aria-hidden="true" />
        {submitting ? "Starting" : "Deploy"}
      </Button>
    </form>
  );
}
