import type { Metadata } from "next";

import { DeployIllustration } from "@/components/states/illustrations";
import { PlaceholderPage } from "@/components/states/placeholder";

export const metadata: Metadata = { title: "Deploy" };

export default function DeployPage() {
  return (
    <PlaceholderPage
      title="Deploy"
      illustration={<DeployIllustration />}
      heading="From repository to running app"
      description="Point Groundcrew at a repository. Scout reads it, Builder writes a Dockerfile and builds it in a sandbox until it passes, and Deployer plans the target config. Nothing ships until you approve the plan."
      points={[
        "Targets: Kubernetes, AWS ECS, and Nebius",
        "Build logs for every attempt, with what Builder changed between them",
        "The plan and its preflight checks, side by side, before you approve",
      ]}
      phase="Arrives in Phase 4."
    />
  );
}
