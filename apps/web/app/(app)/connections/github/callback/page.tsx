"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useEffect, useRef } from "react";

import { PageBody, PageHeader } from "@/components/shell/page-header";
import { useClaimGitHub } from "@/lib/discovery";

/** GitHub sends the owner back here after authorization (the app's callback URL). The code is
 * spent once to learn which installations they can reach; then they pick repositories. */
function Callback() {
  const params = useSearchParams();
  const router = useRouter();
  const claim = useClaimGitHub();
  const sent = useRef(false);
  const code = params.get("code");
  const state = params.get("state");

  useEffect(() => {
    if (sent.current || !code || !state) return;
    sent.current = true;
    claim.mutate(
      { code, state },
      { onSuccess: () => router.replace("/connections?connect=github") },
    );
  }, [claim, code, state, router]);

  if (!code || !state || params.get("error")) {
    return (
      <p role="alert" className="text-[13px]">
        GitHub did not confirm the authorization. <Link href="/connections" className="underline">Back to Connections</Link>
      </p>
    );
  }
  if (claim.error) {
    return (
      <p role="alert" className="text-[13px] text-[var(--status-failed)]">
        {claim.error.message} <Link href="/connections" className="underline">Back to Connections</Link>
      </p>
    );
  }
  return (
    <p role="status" className="text-[13px] text-muted-foreground">
      Checking which GitHub installations you can reach...
    </p>
  );
}

export default function GitHubCallbackPage() {
  return (
    <>
      <PageHeader title="Connect GitHub" />
      <PageBody className="max-w-xl">
        <Suspense>
          <Callback />
        </Suspense>
      </PageBody>
    </>
  );
}
