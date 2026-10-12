import type { Metadata } from "next";

import { DeployRunView } from "./deploy-run-view";

export const metadata: Metadata = { title: "Deploy" };

export default async function DeployRunPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  return <DeployRunView deployId={id} />;
}
