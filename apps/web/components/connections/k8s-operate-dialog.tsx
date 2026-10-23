"use client";

import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { type Connection, useK8sSetOperate } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { isOperate } from "./aws-operate-dialog";
import { CopyField } from "./copy-field";
import { SetupStep, SetupSteps } from "./setup-steps";

export const K8S_OPERATE_ALLOWS = [
  "Roll a Deployment or StatefulSet back to its previous revision (kubectl rollout undo)",
  "Restart a Deployment's or StatefulSet's pods on the same template (kubectl rollout restart)",
  "Patch Deployments and StatefulSets and list their revisions, nothing else, and only after a person approves each fix",
];

/**
 * Turn approved emergency fixes on or off for a connected cluster. Turning them on means
 * granting the kubeconfig's identity patch on Deployments and StatefulSets
 * (k8s-operate-rbac.yaml, served at /k8s-operate-rbac.yaml);
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
  // The RBAC file is served by this Groundcrew site, so the command works from any machine.
  const origin = typeof window === "undefined" ? "" : window.location.origin;

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
            <SetupSteps>
              <SetupStep n={1} title="Give the service account the rights to roll back and restart">
                <p>Apply this with your own kubectl, as someone who may create RBAC objects in the cluster:</p>
                <CopyField label="Command" value={`kubectl apply -f ${origin}/k8s-operate-rbac.yaml`} />
                <p>
                  It binds a ClusterRole to the read-only service account from{" "}
                  <a href="/k8s-readonly-rbac.yaml" className="font-mono text-foreground underline underline-offset-2">
                    k8s-readonly-rbac.yaml
                  </a>
                  . To limit it to your app&apos;s namespaces, bind the same ClusterRole with a RoleBinding in each one.
                </p>
              </SetupStep>
              <SetupStep n={2} title="Click Verify">
                <p>
                  Groundcrew asks the cluster whether this kubeconfig may patch Deployments or StatefulSets in the
                  namespaces of your discovered workloads.
                </p>
              </SetupStep>
            </SetupSteps>
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
