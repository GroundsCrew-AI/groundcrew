"use client";

import { Download } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { type Connection, downloadAwsTemplate, useAwsSetTier, useAwsTemplateUrl } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { DEPLOY_CREATES } from "./aws-connect-dialog";
import { CopyField } from "./copy-field";

/**
 * Switch a connected AWS account between observe and deploy. Quick-create links only create
 * stacks, so turning deploys on means updating the existing stack with Tier=deploy first;
 * Groundcrew then verifies the groundcrew-deploy role before it uses it.
 */
export function AwsTierDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { workspace } = useWorkspace();
  const setTier = useAwsSetTier(workspace?.id);
  const enabling = connection.tier !== "deploy";
  const template = useAwsTemplateUrl(open && enabling);
  const templateUrl = template.data?.url;
  const region = "us-east-2";
  const stacks = `https://${region}.console.aws.amazon.com/cloudformation/home?region=${region}#/stacks`;

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) setTier.reset();
  }

  function submit() {
    setTier.mutate(
      { connectionId: connection.id, tier: enabling ? "deploy" : "observe" },
      {
        onSuccess: () => {
          toast.success(enabling ? `Deploys are on for ${connection.name}` : `Deploys are off for ${connection.name}`);
          close(false);
        },
      },
    );
  }

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90dvh] grid-cols-[minmax(0,1fr)] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>{enabling ? "Turn on deploys" : "Turn off deploys"}</DialogTitle>
          <DialogDescription>
            {enabling
              ? "Update the groundcrew-read-only stack with Tier=deploy, then Groundcrew checks the new deploy role."
              : "Groundcrew stops using the deploy role now. Update the stack with Tier=observe to delete it."}
          </DialogDescription>
        </DialogHeader>
        {enabling ? (
          <ol className="list-decimal space-y-2 pl-5 text-[13px] leading-relaxed">
            <li>
              Open{" "}
              <a href={stacks} target="_blank" rel="noreferrer" className="underline underline-offset-2">
                CloudFormation stacks
              </a>{" "}
              in account <span className="font-mono">{connection.account}</span>, select{" "}
              <span className="font-mono">groundcrew-read-only</span>, and click <span className="font-medium">Update</span>.
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
              <p>
                On the next page set <span className="font-mono">Tier</span> to <span className="font-mono">deploy</span>.
                Leave the other parameters as they are.
              </p>
            </li>
            <li>
              Acknowledge that it creates IAM resources with custom names, submit, and wait for{" "}
              <span className="font-mono">UPDATE_COMPLETE</span>. Then click <span className="font-medium">Verify</span>.
            </li>
          </ol>
        ) : null}
        {enabling && (
          <ul className="space-y-1 rounded-md border px-3 py-2 text-[12.5px] text-muted-foreground">
            {DEPLOY_CREATES.map((line) => (
              <li key={line}>{line}</li>
            ))}
          </ul>
        )}
        {setTier.error && (
          <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
            {setTier.error.message}
          </p>
        )}
        <DialogFooter>
          <Button variant="ghost" onClick={() => close(false)}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={setTier.isPending} variant={enabling ? "default" : "destructive"}>
            {setTier.isPending ? "Checking" : enabling ? "Verify" : "Turn off deploys"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
