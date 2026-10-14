"use client";

import { Download } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { type Connection, downloadAwsTemplate, useAwsSetOperate, useAwsTemplateUrl } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { CopyField } from "./copy-field";

export const OPERATE_ALLOWS = [
  "Roll an ECS service back to an earlier task definition revision, or restart it",
  "Pass only the roles you list to ECS tasks, which a rollback needs (no other IAM changes)",
  "Move a secret's AWSCURRENT stage back to its previous version (by version id; values are never read)",
  "Only on the services and secrets you list, and only after a person approves each fix",
];

export function isOperate(connection: Connection): boolean {
  return (connection.capabilities ?? []).includes("operate");
}

/**
 * Turn approved emergency fixes on or off for a connected AWS account. Turning them on means
 * updating the existing stack with EnableOperate=true and the services and secrets Groundcrew
 * may act on; Groundcrew then verifies the groundcrew-operate role before it uses it.
 */
export function AwsOperateDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { workspace } = useWorkspace();
  const setOperate = useAwsSetOperate(workspace?.id);
  const enabling = !isOperate(connection);
  const template = useAwsTemplateUrl(open && enabling);
  const templateUrl = template.data?.url;
  const region = "us-east-2";
  const account = connection.account ?? "ACCOUNT";
  const stacks = `https://${region}.console.aws.amazon.com/cloudformation/home?region=${region}#/stacks`;
  const services = `arn:aws:ecs:${region}:${account}:service/CLUSTER/*`;
  const secrets = `arn:aws:secretsmanager:${region}:${account}:secret:PREFIX/*`;
  // The demo's task roles; ECS needs iam:PassRole on a revision's roles to roll back to it.
  const passRoles = `arn:aws:iam::${account}:role/checkout-*`;

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
      <DialogContent className="max-h-[90dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{enabling ? "Turn on emergency fixes" : "Turn off emergency fixes"}</DialogTitle>
          <DialogDescription>
            {enabling
              ? "Lets Medic offer rollbacks and restarts when production is down. Every fix still waits for your approval."
              : "Medic stops offering emergency fixes now. Update the stack with EnableOperate=false to delete the role."}
          </DialogDescription>
        </DialogHeader>
        {enabling && (
          <>
            <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed">
              <li>
                Open{" "}
                <a href={stacks} target="_blank" rel="noreferrer" className="underline underline-offset-2">
                  CloudFormation stacks
                </a>{" "}
                in account <span className="font-mono">{account}</span>, select{" "}
                <span className="font-mono">groundcrew-read-only</span>, and click{" "}
                <span className="font-medium">Update</span>.
              </li>
              <li className="space-y-2">
                {templateUrl ? (
                  <>
                    <p>
                      Choose <span className="font-medium">Replace existing template</span>, then{" "}
                      <span className="font-medium">Amazon S3 URL</span>, and paste:
                    </p>
                    <CopyField label="Template URL" value={templateUrl} multiline />
                  </>
                ) : (
                  <>
                    <p>
                      Choose <span className="font-medium">Replace existing template</span>, then{" "}
                      <span className="font-medium">Upload a template file</span>, and upload this file:
                    </p>
                    <Button
                      size="sm"
                      variant="outline"
                      disabled={template.isLoading}
                      onClick={() => void downloadAwsTemplate().catch((e: Error) => toast.error(e.message))}
                    >
                      <Download aria-hidden="true" />
                      aws-connect.yaml
                    </Button>
                  </>
                )}
              </li>
              <li className="space-y-2">
                <p>
                  On the next page set <span className="font-mono">EnableOperate</span> to{" "}
                  <span className="font-mono">true</span> and list what Groundcrew may act on (comma separated;
                  replace CLUSTER and PREFIX). Leave the other parameters as they are.
                </p>
                <CopyField label="OperateServiceArns" value={services} />
                <CopyField label="OperateSecretArns" value={secrets} />
                <p>
                  A rollback hands the earlier revision&apos;s execution and task roles back to ECS, so list those
                  roles too (they are passed only to ECS tasks, never a groundcrew-* role):
                </p>
                <CopyField label="OperatePassRoleArns" value={passRoles} />
              </li>
              <li>
                Acknowledge that it creates IAM resources with custom names, submit, and wait for{" "}
                <span className="font-mono">UPDATE_COMPLETE</span>. Then click <span className="font-medium">Verify</span>.
              </li>
            </ol>
            <ul className="space-y-1 rounded-md border px-3 py-2 text-[12.5px] text-muted-foreground">
              {OPERATE_ALLOWS.map((line) => (
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
