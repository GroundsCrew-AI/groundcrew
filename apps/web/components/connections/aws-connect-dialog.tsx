"use client";

import { CircleCheck, Download, ExternalLink } from "lucide-react";
import { type FormEvent, useEffect, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { downloadAwsTemplate, useAwsComplete, useAwsStart } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { CopyField } from "./copy-field";
import { RunDiscoveryButton } from "./run-discovery-button";

const CREATES = [
  "One IAM role that only Groundcrew can assume, and only with your ExternalId. Sessions last at most an hour.",
  "Read access to configuration and metadata: ECS, ECR, RDS, VPC, Lambda, CloudWatch, IAM, and secret names.",
  "An explicit deny on secret values, SSM parameters, KMS decrypt, and S3 objects.",
  "A Resource Explorer index in this region, so discovery is one search. AWS Config is not turned on.",
];

function Step({ n, title, children }: { n: number; title: string; children: React.ReactNode }) {
  return (
    <li className="grid grid-cols-[22px_1fr] gap-3">
      <span className="flex size-[22px] items-center justify-center rounded-full border font-mono text-[11px] text-muted-foreground">
        {n}
      </span>
      <div className="min-w-0 space-y-2.5">
        <h3 className="font-medium">{title}</h3>
        {children}
      </div>
    </li>
  );
}

/** Start -> CloudFormation -> paste the RoleArn -> verified. */
export function AwsConnectDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const { workspace } = useWorkspace();
  const start = useAwsStart();
  const complete = useAwsComplete(workspace?.id);
  const [roleArn, setRoleArn] = useState("");
  const startRequested = open && workspace !== undefined;

  useEffect(() => {
    if (startRequested && workspace && !start.data && !start.isPending && !start.error) start.mutate(workspace.id);
    // Start once per open; the mutation object changes identity on every state change.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [startRequested, workspace]);

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      // After a success the next account needs its own pending connection and ExternalId.
      if (complete.data) start.reset();
      complete.reset();
      setRoleArn("");
    }
  }

  function verify(event: FormEvent) {
    event.preventDefault();
    if (!start.data) return;
    complete.mutate(
      { connection_id: start.data.connection_id, role_arn: roleArn.trim() },
      { onSuccess: (conn) => toast.success(`Connected AWS account ${conn.account}`) },
    );
  }

  const info = start.data;
  const done = complete.data;
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>Connect an AWS account</DialogTitle>
          <DialogDescription>
            Groundcrew reads with a role you create. It never stores AWS keys: only the role ARN, your
            ExternalId, and the account ID.
          </DialogDescription>
        </DialogHeader>
        {done ? (
          <div className="space-y-4">
            <p className="flex items-start gap-2 text-[13px]">
              <CircleCheck aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-[var(--status-ok)]" />
              <span>
                Connected to account <span className="font-mono">{done.account}</span>. The role was refused
                without your ExternalId and worked with it, as it should.
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
          <p className="text-muted-foreground">Preparing your ExternalId</p>
        ) : (
          <ol className="space-y-6">
            <Step n={1} title={`Create the read-only role in ${info.region}`}>
              <ul className="space-y-1 text-[12.5px] text-muted-foreground">
                {CREATES.map((line) => (
                  <li key={line} className="flex gap-2">
                    <span aria-hidden="true" className="mt-[8px] size-1 shrink-0 rounded-full bg-subtle" />
                    {line}
                  </li>
                ))}
              </ul>
              {info.console_url ? (
                <Button asChild>
                  <a href={info.console_url} target="_blank" rel="noreferrer">
                    Open CloudFormation
                    <ExternalLink aria-hidden="true" />
                  </a>
                </Button>
              ) : (
                <div className="space-y-2.5">
                  <p className="text-[12.5px]">
                    Download the template, then create the stack with credentials for the account you are
                    connecting:
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => void downloadAwsTemplate().catch((e: Error) => toast.error(e.message))}
                  >
                    <Download aria-hidden="true" />
                    aws-connect.yaml
                  </Button>
                  <CopyField label="Create the stack" value={info.cli_command} multiline />
                </div>
              )}
              <div className="grid gap-2 sm:grid-cols-2">
                <CopyField label="ExternalId" value={info.external_id} />
                <CopyField label="GroundcrewPrincipalArn" value={info.principal_arn} />
              </div>
            </Step>
            <Step n={2} title="Paste the RoleArn output">
              <p className="text-[12.5px] text-muted-foreground">
                When the stack {info.stack_name} is complete, copy RoleArn from its Outputs tab.
              </p>
              {!info.console_url && <CopyField label="Or print it" value={info.role_arn_command} multiline />}
              <form onSubmit={verify} className="space-y-2">
                <Label htmlFor="role-arn">Role ARN</Label>
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
            </Step>
          </ol>
        )}
      </DialogContent>
    </Dialog>
  );
}
