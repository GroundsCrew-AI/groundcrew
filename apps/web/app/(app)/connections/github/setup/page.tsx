"use client";

import Link from "next/link";
import { useEffect, useRef } from "react";

import { PageBody, PageHeader } from "@/components/shell/page-header";
import { useGitHubAuthorize } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

/** The GitHub App's Setup URL: after an install (or a change to its repositories) GitHub comes
 * back here. The new installation is picked up the safe way: authorize on GitHub again, which
 * tells Groundcrew which installations this person can reach. */
export default function GitHubSetupPage() {
  const { workspace } = useWorkspace();
  const authorize = useGitHubAuthorize();
  const started = useRef(false);

  useEffect(() => {
    if (started.current || !workspace) return;
    started.current = true;
    authorize.mutate(workspace.id, { onSuccess: (r) => window.location.assign(r.authorize_url) });
  }, [authorize, workspace]);

  return (
    <>
      <PageHeader title="Connect GitHub" />
      <PageBody className="max-w-xl">
        {authorize.error ? (
          <p role="alert" className="text-[13px] text-[var(--status-failed)]">
            {authorize.error.message}{" "}
            <Link href="/connections" className="underline">
              Back to Connections
            </Link>
          </p>
        ) : (
          <p role="status" className="text-[13px] text-muted-foreground">
            The GitHub App is installed. Confirming it with GitHub for {workspace?.name ?? "your workspace"}...
          </p>
        )}
      </PageBody>
    </>
  );
}
