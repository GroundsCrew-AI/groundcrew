"use client";

import { type FormEvent, type ReactNode, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { useConnectAzure, useConnectKubernetes, useConnectVercel } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

type DialogProps = { open: boolean; onOpenChange: (open: boolean) => void };

function Field({ id, label, hint, children }: { id: string; label: string; hint?: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p className="text-[11.5px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

function FormDialog({
  open,
  onOpenChange,
  title,
  description,
  onSubmit,
  pending,
  error,
  submitLabel,
  children,
}: DialogProps & {
  title: string;
  description: ReactNode;
  onSubmit: () => void;
  pending: boolean;
  error: Error | null;
  submitLabel: string;
  children: ReactNode;
}) {
  function submit(event: FormEvent) {
    event.preventDefault();
    onSubmit();
  }
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-md">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>{title}</DialogTitle>
            <DialogDescription asChild>
              <div>{description}</div>
            </DialogDescription>
          </DialogHeader>
          {children}
          {error && (
            <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
              {error.message}
            </p>
          )}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={pending}>
              {pending ? "Checking" : submitLabel}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function PreviewNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border border-dashed px-3 py-2 text-[12px] text-muted-foreground">
      <span className="font-medium text-foreground">Preview.</span> {children}
    </p>
  );
}

export function VercelConnectDialog({ open, onOpenChange }: DialogProps) {
  const { workspace } = useWorkspace();
  const connect = useConnectVercel();
  const [token, setToken] = useState("");
  const [teamId, setTeamId] = useState("");
  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setToken("");
      connect.reset();
    }
  }
  return (
    <FormDialog
      open={open}
      onOpenChange={close}
      title="Connect Vercel"
      description={
        <>
          Groundcrew reads projects, production domains and deployments, and the names and types of env vars. It
          reads a value only for plain variables named *_URL or *_HOST, never encrypted, sensitive, or secret ones.
        </>
      }
      submitLabel="Connect"
      pending={connect.isPending}
      error={connect.error}
      onSubmit={() =>
        workspace &&
        connect.mutate(
          { workspace_id: workspace.id, token: token.trim(), team_id: teamId.trim() || null },
          {
            onSuccess: (conn) => {
              toast.success(`Connected ${conn.name}`);
              close(false);
            },
          },
        )
      }
    >
      <Field id="vercel-token" label="Token" hint="Create one scoped to your team at vercel.com/account/tokens. It is stored encrypted.">
        <Input
          id="vercel-token"
          type="password"
          value={token}
          onChange={(e) => setToken(e.target.value)}
          autoComplete="off"
          required
          minLength={8}
        />
      </Field>
      <Field id="vercel-team" label="Team ID" hint="Settings, General, Team ID (team_...). Leave empty for a personal account.">
        <Input
          id="vercel-team"
          value={teamId}
          onChange={(e) => setTeamId(e.target.value)}
          className="font-mono text-[12px]"
          autoComplete="off"
          spellCheck={false}
        />
      </Field>
    </FormDialog>
  );
}

export function AzureConnectDialog({ open, onOpenChange }: DialogProps) {
  const { workspace } = useWorkspace();
  const connect = useConnectAzure();
  const [form, setForm] = useState({
    tenant_id: "",
    client_id: "",
    client_secret: "",
    subscription_id: "",
    resource_group: "",
  });
  const set = (key: keyof typeof form) => (e: React.ChangeEvent<HTMLInputElement>) =>
    setForm({ ...form, [key]: e.target.value });
  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Connect Azure"
      description="A service principal with Reader on one resource group (infra/azure-connect.bicep)."
      submitLabel="Save"
      pending={connect.isPending}
      error={connect.error}
      onSubmit={() =>
        workspace &&
        connect.mutate(
          { workspace_id: workspace.id, ...form },
          {
            onSuccess: () => {
              toast.success("Azure connection saved (preview)");
              onOpenChange(false);
            },
          },
        )
      }
    >
      <PreviewNote>The connection is saved, but discovery does not read Azure live yet.</PreviewNote>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field id="az-tenant" label="Tenant ID">
          <Input id="az-tenant" value={form.tenant_id} onChange={set("tenant_id")} required />
        </Field>
        <Field id="az-client" label="Client ID">
          <Input id="az-client" value={form.client_id} onChange={set("client_id")} required />
        </Field>
        <Field id="az-sub" label="Subscription ID">
          <Input id="az-sub" value={form.subscription_id} onChange={set("subscription_id")} required />
        </Field>
        <Field id="az-rg" label="Resource group">
          <Input id="az-rg" value={form.resource_group} onChange={set("resource_group")} required />
        </Field>
      </div>
      <Field id="az-secret" label="Client secret" hint="Stored encrypted.">
        <Input id="az-secret" type="password" value={form.client_secret} onChange={set("client_secret")} autoComplete="off" required />
      </Field>
    </FormDialog>
  );
}

export function KubernetesConnectDialog({ open, onOpenChange }: DialogProps) {
  const { workspace } = useWorkspace();
  const connect = useConnectKubernetes();
  const [name, setName] = useState("");
  const [kubeconfig, setKubeconfig] = useState("");
  return (
    <FormDialog
      open={open}
      onOpenChange={onOpenChange}
      title="Connect Kubernetes"
      description="A kubeconfig for the read-only service account in infra/k8s-readonly-rbac.yaml. It cannot read Secrets."
      submitLabel="Save"
      pending={connect.isPending}
      error={connect.error}
      onSubmit={() =>
        workspace &&
        connect.mutate(
          { workspace_id: workspace.id, name: name.trim() || null, kubeconfig },
          {
            onSuccess: () => {
              toast.success("Kubernetes connection saved (preview)");
              setKubeconfig("");
              onOpenChange(false);
            },
          },
        )
      }
    >
      <PreviewNote>The kubeconfig is stored encrypted, but discovery does not read clusters live yet.</PreviewNote>
      <Field id="k8s-name" label="Name">
        <Input id="k8s-name" value={name} onChange={(e) => setName(e.target.value)} placeholder="production cluster" />
      </Field>
      <Field id="k8s-config" label="Kubeconfig">
        <Textarea
          id="k8s-config"
          value={kubeconfig}
          onChange={(e) => setKubeconfig(e.target.value)}
          rows={8}
          className="font-mono text-[11.5px]"
          spellCheck={false}
          required
        />
      </Field>
    </FormDialog>
  );
}
