"use client";

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
import { type Connection, useK8sSetConfigMapChanges } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { CopyField } from "./copy-field";

// The optional ConfigMap rule in infra/k8s-readonly-rbac.yaml, as commands.
export const K8S_CONFIGMAPS_RBAC = [
  "kubectl create clusterrole groundcrew-configmaps-metadata --verb=list --resource=configmaps",
  "kubectl create clusterrolebinding groundcrew-configmaps-metadata --clusterrole=groundcrew-configmaps-metadata --serviceaccount=groundcrew:groundcrew-reader",
];

/**
 * Opt a cluster in to (or out of) ConfigMap changes as incident causes. Off by default: the
 * `list configmaps` right it needs also grants read access to ConfigMap contents, although
 * Groundcrew only ever asks for metadata. Groundcrew checks the right before turning it on.
 */
export function K8sConfigMapsDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { workspace } = useWorkspace();
  const setChanges = useK8sSetConfigMapChanges(workspace?.id);
  const enabling = !connection.configmap_changes;

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) setChanges.reset();
  }

  function submit() {
    setChanges.mutate(
      { connectionId: connection.id, enabled: enabling },
      {
        onSuccess: () => {
          toast.success(
            enabling
              ? `ConfigMap changes are detected for ${connection.name}`
              : `ConfigMap changes are no longer detected for ${connection.name}`,
          );
          close(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="grid-cols-[minmax(0,1fr)] sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>
            {enabling
              ? "Detect ConfigMap changes"
              : "Stop detecting ConfigMap changes"}
          </DialogTitle>
          <DialogDescription>
            {enabling
              ? "Lets Medic name a changed ConfigMap as the cause of a broken Deployment or StatefulSet. Groundcrew reads only names and change times, never contents."
              : "Incidents will note that ConfigMap changes are not visible. Delete the role binding to remove the right."}
          </DialogDescription>
        </DialogHeader>
        {enabling && (
          <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed">
            <li className="space-y-2">
              <p>
                Give the service account in this kubeconfig{" "}
                <span className="font-mono">list</span> on ConfigMaps (the
                optional block in{" "}
                <span className="font-mono">infra/k8s-readonly-rbac.yaml</span>
                ):
              </p>
              {K8S_CONFIGMAPS_RBAC.map((command, i) => (
                <CopyField
                  key={command}
                  label={`Command ${i + 1}`}
                  value={command}
                />
              ))}
              <p className="rounded-md border px-3 py-2 text-[12.5px] text-muted-foreground">
                This right grants read access to ConfigMap contents to anyone
                holding the kubeconfig&apos;s token. Groundcrew only asks for
                metadata, but Kubernetes has no metadata-only right. To limit
                it, bind the role with a RoleBinding in your app&apos;s
                namespaces instead.
              </p>
            </li>
            <li>
              Click <span className="font-medium">Verify</span>: Groundcrew asks
              the cluster whether this kubeconfig may list ConfigMaps.
            </li>
          </ol>
        )}
        {setChanges.error && (
          <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
            {setChanges.error.message}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button
            onClick={submit}
            disabled={setChanges.isPending}
            variant={enabling ? "default" : "destructive"}
          >
            {setChanges.isPending
              ? "Checking"
              : enabling
                ? "Verify"
                : "Stop detecting"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
