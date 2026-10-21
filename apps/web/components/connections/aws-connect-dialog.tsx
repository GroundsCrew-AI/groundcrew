"use client";

import { ChevronRight, CircleCheck, Download, ExternalLink } from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { type AwsStart, type AwsTier, useAwsComplete, useAwsStart } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { CopyField } from "./copy-field";
import { RunDiscoveryButton } from "./run-discovery-button";

const CREATES = [
  "One IAM role, groundcrew-read-only, that only Groundcrew can assume, and only with your ExternalId. Sessions last at most an hour.",
  "Read access to configuration and metadata: ECS, ECR, RDS, VPC, Lambda, CloudWatch, IAM, and secret names.",
  "An explicit deny on secret values, SSM parameters, KMS decrypt, and S3 objects.",
  "A Resource Explorer index in this region, so discovery is one search. AWS Config is not turned on.",
];

export const DEPLOY_CREATES = [
  "An ECS cluster, groundcrew-deploys (Fargate, no running cost until something is deployed).",
  "A role, groundcrew-deploy, with the same trust and ExternalId. It may only create and change ECS services in groundcrew-deploys, ECR repositories named groundcrew-*, log groups under /groundcrew/, security groups tagged project=groundcrew, and secrets under groundcrew/ (write only).",
  "A role, groundcrew-task-execution, that ECS uses to pull groundcrew-* images, write /groundcrew/ logs, and inject groundcrew/ secrets when an app starts.",
  "The same explicit deny on reading secret values. Nothing else in the account can be changed.",
];

function Step({ n, children }: { n: number; children: ReactNode }) {
  return (
    <li className="grid grid-cols-[22px_1fr] gap-3 text-[13px] leading-relaxed">
      <span className="flex size-[22px] items-center justify-center rounded-full border font-mono text-[11px] text-muted-foreground">
        {n}
      </span>
      <div className="min-w-0 space-y-2">{children}</div>
    </li>
  );
}

/** Create the stack by uploading the template, which works in any AWS account. */
function UploadSteps({ info }: { info: AwsStart }) {
  return (
    <ol className="space-y-4">
      <Step n={1}>
        <p>Download the template. It is the same file as in Groundcrew&apos;s repository.</p>
        <Button asChild size="sm" variant="outline">
          <a href={info.template_download_url} download="aws-connect.yaml">
            <Download aria-hidden="true" />
            aws-connect.yaml
          </a>
        </Button>
      </Step>
      <Step n={2}>
        <p>
          Open CloudFormation in the account you want to connect, in{" "}
          <span className="font-mono">{info.region}</span>. Choose{" "}
          <span className="font-medium">Choose an existing template</span>, then{" "}
          <span className="font-medium">Upload a template file</span>, pick aws-connect.yaml, and click Next.
        </p>
        <Button asChild size="sm" variant="outline">
          <a href={info.upload_console_url} target="_blank" rel="noreferrer">
            Open CloudFormation
            <ExternalLink aria-hidden="true" />
          </a>
        </Button>
      </Step>
      <Step n={3}>
        <p>
          Name the stack <span className="font-mono">{info.stack_name}</span> and set these parameters. Leave the
          others as they are.
        </p>
        <div className="grid gap-2">
          <CopyField label="Stack name" value={info.stack_name} />
          {info.parameters.map((p) => (
            <CopyField key={p.key} label={p.key} value={p.value} />
          ))}
        </div>
      </Step>
      <Step n={4}>
        <p>
          Click Next twice, tick <span className="italic">I acknowledge that AWS CloudFormation might create IAM
          resources with custom names</span>, and Submit. Wait about a minute for{" "}
          <span className="font-mono">CREATE_COMPLETE</span>, then copy <span className="font-mono">RoleArn</span>{" "}
          from the <span className="font-medium">Outputs</span> tab and paste it below.
        </p>
      </Step>
    </ol>
  );
}

function Disclosure({ title, defaultOpen, children }: { title: string; defaultOpen?: boolean; children: ReactNode }) {
  return (
    <details open={defaultOpen} className="group rounded-md border px-3 py-2">
      <summary className="flex cursor-pointer list-none items-center gap-1.5 text-[12.5px] font-medium text-muted-foreground">
        <ChevronRight aria-hidden="true" className="size-3.5 transition-transform group-open:rotate-90" />
        {title}
      </summary>
      <div className="mt-3 space-y-2.5 pb-1">{children}</div>
    </details>
  );
}

function OtherWays({ info }: { info: AwsStart }) {
  return (
    <Disclosure title="With the AWS CLI instead">
      <p className="text-[12.5px] text-muted-foreground">
        With credentials for the account you are connecting, in the folder where you saved aws-connect.yaml:
      </p>
      <CopyField label="Create the stack" value={info.cli_command} multiline />
      <CopyField label="Print the RoleArn" value={info.role_arn_command} multiline />
      {info.console_url && (
        <p className="text-[12.5px] text-muted-foreground">
          In the account this Groundcrew server runs in, a{" "}
          <a href={info.console_url} target="_blank" rel="noreferrer" className="underline underline-offset-2">
            filled-in CloudFormation link
          </a>{" "}
          also works.
        </p>
      )}
    </Disclosure>
  );
}

/** Connect AWS: upload the template in CloudFormation with this workspace's parameters, paste
 * the RoleArn, verified. */
export function AwsConnectDialog({
  open,
  onOpenChange,
  tier = "observe",
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** observe: read-only. deploy: also the deploy role and cluster (Tier=deploy). */
  tier?: AwsTier;
}) {
  const { workspace } = useWorkspace();
  const start = useAwsStart();
  const complete = useAwsComplete(workspace?.id);
  const [roleArn, setRoleArn] = useState("");
  const startRequested = open && workspace !== undefined;

  useEffect(() => {
    if (!startRequested || !workspace) return;
    if ((start.data && start.data.tier === tier) || start.isPending) return;
    start.mutate({ workspaceId: workspace.id, tier });
    // Once per open; the mutation object changes identity on every state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startRequested, workspace, tier]);

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      // After a success the next account needs its own pending connection and ExternalId;
      // after a failure, the next open asks again.
      if (complete.data || start.error) start.reset();
      complete.reset();
      setRoleArn("");
    }
  }

  function verify(event: FormEvent) {
    event.preventDefault();
    if (!start.data) return;
    complete.mutate(
      { connection_id: start.data.connection_id, role_arn: roleArn.trim() },
      {
        onSuccess: (conn) =>
          toast.success(`Connected AWS account ${conn.account}${conn.tier === "deploy" ? " with deploys" : ""}`),
      },
    );
  }

  const info = start.data;
  const done = complete.data;
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{tier === "deploy" ? "Connect an AWS account for deploys" : "Connect an AWS account"}</DialogTitle>
          <DialogDescription>
            {tier === "deploy"
              ? "One CloudFormation stack creates a read-only role plus a deploy role limited to Groundcrew's own resources. "
              : "You create a read-only role in your account with one CloudFormation stack. "}
            Groundcrew never stores AWS keys: only the role ARN, your ExternalId, and the account ID.
          </DialogDescription>
        </DialogHeader>
        {done ? (
          <div className="space-y-4">
            <p className="flex items-start gap-2 text-[13px]">
              <CircleCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-[var(--status-ok)]" />
              <span>
                Connected to account <span className="font-mono">{done.account}</span>. The role was refused
                without your ExternalId and worked with it, as it should.
                {done.tier === "deploy" && " So was the deploy role: Groundcrew can deploy into groundcrew-deploys."}
              </span>
            </p>
            <div className="flex justify-end gap-2">
              <Button variant="ghost" onClick={() => close(false)}>
                Close
              </Button>
              <RunDiscoveryButton size="default" />
            </div>
          </div>
        ) : start.error ? (
          <p role="alert" className="text-[var(--status-failed)]">
            {start.error.message}
          </p>
        ) : !info ? (
          <p className="text-muted-foreground">Preparing this workspace&apos;s parameters</p>
        ) : (
          <div className="space-y-5">
            <UploadSteps info={info} />
            <form onSubmit={verify} className="space-y-2">
              <Label htmlFor="role-arn">RoleArn</Label>
              <div className="flex gap-2">
                <Input
                  id="role-arn"
                  value={roleArn}
                  onChange={(e) => setRoleArn(e.target.value)}
                  placeholder="arn:aws:iam::123456789012:role/groundcrew-read-only"
                  className="font-mono text-[12px]"
                  autoComplete="off"
                  spellCheck={false}
                  required
                />
                <Button type="submit" disabled={!roleArn.trim() || complete.isPending}>
                  {complete.isPending ? "Verifying" : "Verify"}
                </Button>
              </div>
              {complete.error && (
                <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
                  {complete.error.message}
                </p>
              )}
            </form>
            <Disclosure title="What the stack creates">
              <ul className="space-y-1 text-[12.5px] text-muted-foreground">
                {[...CREATES, ...(tier === "deploy" ? DEPLOY_CREATES : [])].map((line) => (
                  <li key={line} className="flex gap-2">
                    <span aria-hidden="true" className="mt-[8px] size-1 shrink-0 rounded-full bg-subtle" />
                    {line}
                  </li>
                ))}
              </ul>
            </Disclosure>
            <OtherWays info={info} />
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
