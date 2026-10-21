"use client";

import { ChevronRight, Download, ExternalLink } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type AwsOperateValues,
  type Connection,
  downloadAwsTemplate,
  useAwsOperateValues,
  useAwsSetOperate,
  useAwsTemplateUrl,
} from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { cloudFormationStacksUrl, REGION_PLACEHOLDER } from "./aws-console";
import { CopyField } from "./copy-field";
import { SetupNote, SetupStep, SetupSteps, Ui } from "./setup-steps";

/** Words in a suggested value the person must replace (only when discovery found nothing). */
export const PASS_ROLE_PLACEHOLDER = "YOUR-APP-*";
const PLACEHOLDERS = ["CLUSTER", "PREFIX", PASS_ROLE_PLACEHOLDER, REGION_PLACEHOLDER, "ACCOUNT"];

export const OPERATE_ALLOWS = [
  "Roll an ECS service back to an earlier task definition revision, or restart it",
  "Pass only the roles you list to ECS tasks, which a rollback needs (no other IAM changes)",
  "Move a secret's AWSCURRENT stage back to its previous version (by version id; values are never read)",
  "Point a Lambda function's alias back at its previous published version",
  "Reboot an EC2 instance whose status check fails",
  "Only on the services and secrets you list, and only after a person approves each fix",
];

export function isOperate(connection: Connection): boolean {
  return (connection.capabilities ?? []).includes("operate");
}

type Parameter = { key: string; value: string; hint: string };

/** The parameters to set, from discovery; patterns with placeholders where it found nothing. */
function parameters(values: AwsOperateValues | undefined, region: string, account: string): Parameter[] {
  const join = (list: string[] | undefined) => (list && list.length > 0 ? list.join(",") : null);
  const services = join(values?.services);
  const roles = join(values?.pass_roles);
  const out: Parameter[] = [{ key: "EnableOperate", value: "true", hint: "turns emergency fixes on" }];
  out.push({
    key: "OperateServiceArns",
    value: services ?? `arn:aws:ecs:${region}:${account}:service/CLUSTER/*`,
    hint: services
      ? `every service in ${values!.services.map((s) => s.split("/").slice(-2, -1)[0]).join(", ")}`
      : "the ECS services Medic may roll back or restart",
  });
  out.push({
    key: "OperatePassRoleArns",
    value: roles ?? `arn:aws:iam::${account}:role/${PASS_ROLE_PLACEHOLDER}`,
    hint: roles ? "the roles those services run with" : "your services' task and execution roles",
  });
  const optional: [string, string | null, string][] = [
    ["OperateFunctionArns", join(values?.functions), "Lambda functions whose alias Medic may roll back"],
    ["OperateSecretArns", join(values?.secrets), "secrets whose previous version Medic may restore"],
    ["OperateInstanceArns", join(values?.instances), "EC2 instances Medic may reboot"],
  ];
  for (const [key, value, hint] of optional) if (value) out.push({ key, value, hint });
  return out;
}

/**
 * Turn approved emergency fixes on or off for a connected AWS account. Turning them on means
 * updating the existing stack with EnableOperate=true and what Groundcrew may act on, filled in
 * from discovery; Groundcrew then verifies the groundcrew-operate role and that it reaches the
 * services it found before it uses it.
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
  const values = useAwsOperateValues(connection.id, open && enabling);
  const templateUrl = template.data?.url;
  const region = connection.region ?? REGION_PLACEHOLDER;
  const account = connection.account ?? "ACCOUNT";
  const stacks = cloudFormationStacksUrl(connection.region);
  const params = parameters(values.data, region, account);
  const guessed = params.some((p) => PLACEHOLDERS.some((w) => p.value.includes(w)));
  const optionalLeft = ["OperateFunctionArns", "OperateSecretArns", "OperateInstanceArns"].filter(
    (k) => !params.some((p) => p.key === k),
  );

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
          <DialogDescription className="text-[13.5px]">
            {enabling
              ? "Lets Medic offer rollbacks and restarts when production is down. Every fix still waits for your approval."
              : "Medic stops offering emergency fixes now. Update the stack with EnableOperate=false to delete the role."}
          </DialogDescription>
        </DialogHeader>
        {enabling && (
          <div className="space-y-6">
            <SetupSteps>
              <SetupStep n={1} title="Open the groundcrew-read-only stack">
                <p>
                  In account <span className="font-mono text-foreground">{account}</span>
                  {connection.region && (
                    <>
                      , region <span className="font-mono text-foreground">{connection.region}</span>
                    </>
                  )}
                  , select <span className="font-mono text-foreground">groundcrew-read-only</span> and click{" "}
                  <Ui>Update</Ui>.
                </p>
                <Button asChild size="sm" variant="outline">
                  <a href={stacks} target="_blank" rel="noreferrer">
                    CloudFormation stacks
                    <ExternalLink aria-hidden="true" />
                  </a>
                </Button>
              </SetupStep>

              <SetupStep n={2} title="Use the latest template">
                {templateUrl ? (
                  <>
                    <p>
                      Choose <Ui>Replace existing template</Ui>, then <Ui>Amazon S3 URL</Ui>, and paste:
                    </p>
                    <CopyField label="Template URL" value={templateUrl} />
                  </>
                ) : (
                  <>
                    <p>
                      Choose <Ui>Replace existing template</Ui>, then <Ui>Upload a template file</Ui>, and upload:
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
              </SetupStep>

              <SetupStep n={3} title="Set these parameters, and leave the others as they are">
                {values.isLoading ? (
                  <div className="space-y-2">
                    <Skeleton className="h-9 w-full" />
                    <Skeleton className="h-9 w-full" />
                  </div>
                ) : (
                  <>
                    {guessed ? (
                      <SetupNote tone="warn">
                        Groundcrew has not found services in this account yet, so some values below are patterns. Run
                        discovery first to fill them in, or replace the capitalized words with your own names.
                      </SetupNote>
                    ) : (
                      <p>Filled in from what Groundcrew found in this account. Copy each value as it is.</p>
                    )}
                    <div className="space-y-3">
                      {params.map((p) => (
                        <CopyField key={p.key} label={p.key} value={p.value} hint={p.hint} placeholders={PLACEHOLDERS} />
                      ))}
                    </div>
                    {optionalLeft.length > 0 && (
                      <p className="text-[12.5px]">
                        Nothing was found for <span className="font-mono">{optionalLeft.join(", ")}</span>: leave{" "}
                        {optionalLeft.length === 1 ? "it" : "them"} at the default.
                      </p>
                    )}
                    <p className="text-[12.5px]">
                      Groundcrew&apos;s own server, cluster, and roles are never listed, so a fix cannot touch them.
                    </p>
                  </>
                )}
              </SetupStep>

              <SetupStep n={4} title="Submit and wait for UPDATE_COMPLETE">
                <p>
                  Click <Ui>Next</Ui> twice, tick the box that says CloudFormation might create IAM resources with custom
                  names, and click <Ui>Submit</Ui>. It takes about a minute.
                </p>
              </SetupStep>

              <SetupStep n={5} title="Click Verify">
                <p>Groundcrew checks the new role works only with your ExternalId and can reach the services above.</p>
              </SetupStep>
            </SetupSteps>

            <details className="group rounded-md border px-3 py-2">
              <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[13px] font-medium text-muted-foreground [&::-webkit-details-marker]:hidden">
                <ChevronRight aria-hidden="true" className="size-3.5 transition-transform group-open:rotate-90" />
                What the role may do
              </summary>
              <ul className="mt-2 space-y-1.5 pb-1 text-[13px] text-muted-foreground">
                {OPERATE_ALLOWS.map((line) => (
                  <li key={line} className="flex gap-2">
                    <span aria-hidden="true" className="mt-[9px] size-1 shrink-0 rounded-full bg-subtle" />
                    {line}
                  </li>
                ))}
              </ul>
            </details>
          </div>
        )}
        {setOperate.error && (
          <p role="alert" className="text-[13px] text-[var(--status-failed)]">
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
