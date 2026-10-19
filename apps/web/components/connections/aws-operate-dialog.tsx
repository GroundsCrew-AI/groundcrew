"use client";

import { Download } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { type Connection, downloadAwsTemplate, useAwsSetOperate, useAwsTemplateUrl } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { cloudFormationStacksUrl, REGION_PLACEHOLDER } from "./aws-console";
import { CopyField } from "./copy-field";

/** The role name pattern in the PassRole ARN, for the customer to replace with their own. */
export const PASS_ROLE_PLACEHOLDER = "YOUR-APP-*";

export const OPERATE_ALLOWS = [
  "Roll an ECS service back to an earlier task definition revision, or restart it",
  "Pass only the roles you list to ECS tasks, which a rollback needs (no other IAM changes)",
  "Move a secret's AWSCURRENT stage back to its previous version (by version id; values are never read)",
  "Only on the services and secrets you list, and only after a person approves each fix",
];

/** "CLUSTER and PREFIX", "CLUSTER, PREFIX, and REGION" */
function placeholderList(names: string[]): string {
  if (names.length <= 2) return names.join(" and ");
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`;
}

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
  const region = connection.region ?? REGION_PLACEHOLDER;
  const account = connection.account ?? "ACCOUNT";
  const stacks = cloudFormationStacksUrl(connection.region);
  const services = `arn:aws:ecs:${region}:${account}:service/CLUSTER/*`;
  const secrets = `arn:aws:secretsmanager:${region}:${account}:secret:PREFIX/*`;
  // ECS needs iam:PassRole on a revision's task and execution roles to roll back to it. Those
  // are the customer's own roles, so this is a pattern for them to edit, not a guess.
  const passRoles = `arn:aws:iam::${account}:role/${PASS_ROLE_PLACEHOLDER}`;
  const placeholders = ["CLUSTER", "PREFIX", ...(connection.region ? [] : [REGION_PLACEHOLDER])];

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
                in account <span className="font-mono">{account}</span>
                {connection.region ? (
                  <>
                    {" "}
                    (<span className="font-mono">{connection.region}</span>)
                  </>
                ) : (
                  " in the region where you created the stack"
                )}
                , select{" "}
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
                  replace {placeholderList(placeholders)}). Leave the other parameters as they are.
                </p>
                <CopyField label="OperateServiceArns" value={services} />
                <CopyField label="OperateSecretArns" value={secrets} />
                <p>
                  A rollback hands the earlier revision&apos;s execution and task roles back to ECS, so list those
                  roles too: replace <span className="font-mono">{PASS_ROLE_PLACEHOLDER}</span> with your
                  services&apos; role names, or a prefix they share. They are passed only to ECS tasks, never to a
                  groundcrew-* role.
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
