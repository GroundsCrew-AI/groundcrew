"use client";

import { type FormEvent, type ReactNode, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  type Connection,
  type K8sDeploySettingsIn,
  useK8sClearDeploySettings,
  useK8sSetDeploySettings,
} from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

function Field({ id, label, hint, children }: { id: string; label: string; hint?: ReactNode; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <Label htmlFor={id}>{label}</Label>
      {children}
      {hint && <p className="text-[12px] text-muted-foreground">{hint}</p>}
    </div>
  );
}

function Check({
  id,
  label,
  checked,
  onChange,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (checked: boolean) => void;
}) {
  return (
    <label htmlFor={id} className="flex min-h-6 cursor-pointer items-center gap-2.5 text-[13px]">
      <input
        id={id}
        type="checkbox"
        className="size-3.5 accent-[var(--primary)]"
        checked={checked}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

/** The request body from the form. A blank token keeps the saved one (null). */
export function deploySettingsBody(form: {
  registry: string;
  username: string;
  token: string;
  pullSecret: boolean;
  ingressClass: string;
  appDomain: string;
  tls: boolean;
  /** API-only settings (cert-manager issuer, verify URL) are kept as saved. */
  saved?: { tls_cluster_issuer?: string | null; public_url_base?: string | null } | null;
}): K8sDeploySettingsIn {
  const username = form.username.trim();
  return {
    registry: form.registry.trim(),
    username: username || null,
    token: username && form.token ? form.token : null,
    pull_secret: Boolean(username) && form.pullSecret,
    ingress_class: form.ingressClass.trim() || "nginx",
    app_domain: form.appDomain.trim() || null,
    tls: form.tls,
    tls_cluster_issuer: form.tls ? (form.saved?.tls_cluster_issuer ?? null) : null,
    public_url_base: form.saved?.public_url_base ?? null,
  };
}

/**
 * Where a real cluster's deploys go. Without these settings a cluster deploys like the local
 * kind cluster (localhost:5001), which EKS, GKE, or AKS cannot pull from.
 */
export function K8sDeploySettingsDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { workspace } = useWorkspace();
  const save = useK8sSetDeploySettings(workspace?.id);
  const clear = useK8sClearDeploySettings(workspace?.id);
  const saved = connection.deploy_settings ?? null;
  const [registry, setRegistry] = useState(saved?.registry ?? "");
  const [username, setUsername] = useState(saved?.username ?? "");
  const [token, setToken] = useState("");
  const [pullSecret, setPullSecret] = useState(saved?.pull_secret ?? false);
  const [ingressClass, setIngressClass] = useState(saved?.ingress_class ?? "nginx");
  const [appDomain, setAppDomain] = useState(saved?.app_domain ?? "");
  const [tls, setTls] = useState(saved?.tls ?? false);
  const tokenSaved = Boolean(saved?.token_saved) && username.trim() === (saved?.username ?? "");
  const error = save.error ?? clear.error;

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setToken("");
      save.reset();
      clear.reset();
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    save.mutate(
      {
        connectionId: connection.id,
        body: deploySettingsBody({ registry, username, token, pullSecret, ingressClass, appDomain, tls, saved }),
      },
      {
        onSuccess: () => {
          toast.success(`Saved deploy settings for ${connection.name}`);
          close(false);
        },
      },
    );
  }

  function reset() {
    clear.mutate(connection.id, {
      onSuccess: () => {
        toast.success(`${connection.name} deploys like the local kind cluster again`);
        close(false);
      },
    });
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto sm:max-w-lg">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Deploy settings</DialogTitle>
            <DialogDescription>
              Where deploys to {connection.name} push images and how the app is reached. Without them the cluster is
              treated as the local kind cluster, whose registry (localhost:5001) a real cluster cannot pull from.
            </DialogDescription>
          </DialogHeader>
          <Field
            id="k8s-registry"
            label="Registry"
            hint="An image repository prefix without https://, like ghcr.io/acme or 123456789012.dkr.ecr.us-east-1.amazonaws.com. Images are pushed as <registry>/<app>:<tag>."
          >
            <Input
              id="k8s-registry"
              required
              value={registry}
              onChange={(e) => setRegistry(e.target.value)}
              placeholder="ghcr.io/acme"
              autoComplete="off"
              spellCheck={false}
            />
          </Field>
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id="k8s-username" label="Username" hint="Optional. Leave empty to push without a login.">
              <Input
                id="k8s-username"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                autoComplete="off"
                spellCheck={false}
              />
            </Field>
            <Field
              id="k8s-token"
              label="Token"
              hint={tokenSaved ? "Saved. Leave empty to keep it." : "Stored encrypted, never shown again."}
            >
              <Input
                id="k8s-token"
                type="password"
                value={token}
                onChange={(e) => setToken(e.target.value)}
                placeholder={tokenSaved ? "saved" : ""}
                autoComplete="new-password"
                disabled={!username.trim()}
              />
            </Field>
          </div>
          <Check
            id="k8s-pull-secret"
            label="Also let the cluster pull with these credentials (imagePullSecret)"
            checked={pullSecret && Boolean(username.trim())}
            onChange={setPullSecret}
          />
          <div className="grid gap-4 sm:grid-cols-2">
            <Field id="k8s-ingress-class" label="Ingress class">
              <Input
                id="k8s-ingress-class"
                value={ingressClass}
                onChange={(e) => setIngressClass(e.target.value)}
                placeholder="nginx"
                spellCheck={false}
              />
            </Field>
            <Field id="k8s-app-domain" label="App domain" hint="Apps answer at <app>.<domain>.">
              <Input
                id="k8s-app-domain"
                value={appDomain}
                onChange={(e) => setAppDomain(e.target.value)}
                placeholder="apps.example.com"
                spellCheck={false}
              />
            </Field>
          </div>
          <Check id="k8s-tls" label="HTTPS (adds a TLS section to the Ingress)" checked={tls} onChange={setTls} />
          {error && (
            <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
              {error.message}
            </p>
          )}
          <DialogFooter className="gap-2">
            {saved && (
              <Button
                type="button"
                variant="ghost"
                className="sm:mr-auto"
                onClick={reset}
                disabled={clear.isPending || save.isPending}
              >
                Use kind defaults
              </Button>
            )}
            <Button type="button" variant="ghost" onClick={() => close(false)}>
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || !registry.trim()}>
              {save.isPending ? "Saving" : "Save"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
