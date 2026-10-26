"use client";

import { Eye } from "lucide-react";
import Link from "next/link";

import { useWorkspace } from "@/lib/workspace";

/** The product video, when one is set (NEXT_PUBLIC_PRODUCT_VIDEO_URL); no link otherwise. */
export const PRODUCT_VIDEO_URL = process.env.NEXT_PUBLIC_PRODUCT_VIDEO_URL || "";

/**
 * Says so on every page of the example workspace: everything in it is a copy to look at, and
 * the API refuses every change. Connecting switches to the person's own workspace (or to
 * Settings, to make one, when they have none).
 */
export function ExampleBanner({ videoUrl = PRODUCT_VIDEO_URL }: { videoUrl?: string }) {
  const { workspace, workspaces, setWorkspaceId } = useWorkspace();
  if (!workspace?.example) return null;
  const own = workspaces.find((w) => !w.example);
  const link = "font-medium text-foreground underline underline-offset-4";
  return (
    <div
      role="note"
      className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b bg-[color-mix(in_oklab,var(--status-waiting)_10%,var(--background))] px-4 py-2 text-[13px] sm:px-6"
    >
      <p className="flex min-w-0 flex-1 items-center gap-2">
        <Eye aria-hidden="true" className="size-4 shrink-0 text-[var(--status-waiting)]" />
        <span>
          <span className="font-medium">Example workspace (read-only).</span>{" "}
          <span className="text-muted-foreground">
            Connect your cloud to use Groundcrew on your own infrastructure.
          </span>
        </span>
      </p>
      <p className="flex flex-wrap gap-x-4 gap-y-1">
        {own ? (
          <Link href="/connections" className={link} onClick={() => setWorkspaceId(own.id)}>
            Connect your cloud
          </Link>
        ) : (
          <Link href="/settings" className={link}>
            Create your workspace
          </Link>
        )}
        <Link href="/docs" className={link}>
          Docs
        </Link>
        {videoUrl && (
          <a href={videoUrl} target="_blank" rel="noreferrer" className={link}>
            Product video<span className="sr-only"> (opens in a new tab)</span>
          </a>
        )}
      </p>
    </div>
  );
}
