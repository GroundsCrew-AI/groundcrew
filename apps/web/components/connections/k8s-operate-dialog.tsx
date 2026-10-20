"use client";

import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { type Connection, useK8sSetOperate } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { isOperate } from "./aws-operate-dialog";
import { CopyField } from "./copy-field";

export const K8S_OPERATE_ALLOWS = [
  "Roll a Deployment or StatefulSet back to its previous revision (kubectl rollout undo)",
  "Restart a Deployment's or StatefulSet's pods on the same template (kubectl rollout restart)",
  "Patch Deployments and StatefulSets and list their revisions, nothing else, and only after a person approves each fix",
];

/**
 * Turn approved emergency fixes on or off for a connected cluster. Turning them on means
 * granting the kubeconfig's identity patch on Deployments and StatefulSets
 * (infra/k8s-operate-rbac.yaml);
 * Groundcrew checks that with access reviews before it uses it.
 */
export function K8sOperateDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { workspace } = useWorkspace();
  const setOperate = useK8sSetOperate(workspace?.id);
  const enabling = !isOperate(connection);

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) setOperate.reset();
  }

  function submit() {
    setOperate.mutate(
      { connectionId: connection.id, enabled: enabling },
      {
        onSuccess: () => {
          toast.success(
            enabling ? `Emergency fixes are on for ${connection.name}` : `Emergency fixes are off for ${connection.name}`,
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
          <DialogTitle>{enabling ? "Turn on emergency fixes" : "Turn off emergency fixes"}</DialogTitle>
          <DialogDescription>
            {enabling
              ? "Lets Medic offer rollbacks and restarts of Deployments and StatefulSets when they break. Every fix still waits for your approval."
              : "Medic stops offering emergency fixes for this cluster now. Delete the role binding to remove the rights."}
          </DialogDescription>
        </DialogHeader>
        {enabling && (
          <>
            <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed">
              <li className="space-y-2">
                <p>
                  Give the service account in this kubeconfig the rights below. For the read-only account from{" "}
                  <span className="font-mono">infra/k8s-readonly-rbac.yaml</span>, apply:
                </p>
                <CopyField label="Command" value="kubectl apply -f infra/k8s-operate-rbac.yaml" />
                <p className="text-muted-foreground">
                  To limit it to your app&apos;s namespaces, bind the same ClusterRole with a RoleBinding in each one.
                </p>
              </li>
              <li>
                Click <span className="font-medium">Verify</span>: Groundcrew asks the cluster whether this kubeconfig may
                patch Deployments or StatefulSets in the namespaces of your discovered workloads.
              </li>
            </ol>
            <ul className="space-y-1 rounded-md border px-3 py-2 text-[12.5px] text-muted-foreground">
              {K8S_OPERATE_ALLOWS.map((line) => (
                <li key={line}>{line}</li>
              ))}
            </ul>
          </>
        )}
        {setOperate.error && (
          <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
            {setOperate.error.message}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={setOperate.isPending} variant={enabling ? "default" : "destructive"}>
            {setOperate.isPending ? "Checking" : enabling ? "Verify" : "Turn off emergency fixes"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
