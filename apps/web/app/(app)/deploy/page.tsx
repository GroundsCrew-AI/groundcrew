import type { Metadata } from "next";

import { DeployHome } from "./deploy-home";

export const metadata: Metadata = { title: "Deploy" };

export default function DeployPage() {
  return <DeployHome />;
}
