import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { auth } from "@/auth";
import { AttentionAlerts } from "@/components/shell/attention-alerts";
import { ShellProvider } from "@/components/shell/shell-context";
import { Sidebar } from "@/components/shell/sidebar";
import { WorkspaceGate } from "@/components/shell/workspace-gate";
import { WorkspaceProvider } from "@/lib/workspace";

export default async function AppLayout({ children }: { children: ReactNode }) {
  // The middleware already redirects; this also covers a session that expired mid-request.
  const session = await auth();
  if (!session?.user?.githubId) redirect("/signin");

  return (
    <ShellProvider>
      <WorkspaceProvider>
        <AttentionAlerts />
        <div className="flex min-h-dvh">
          <Sidebar />
          <main id="main" className="flex min-w-0 flex-1 flex-col">
            <WorkspaceGate>{children}</WorkspaceGate>
          </main>
        </div>
      </WorkspaceProvider>
    </ShellProvider>
  );
}
