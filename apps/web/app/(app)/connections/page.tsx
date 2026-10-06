import type { Metadata } from "next";

import { PageBody, PageHeader } from "@/components/shell/page-header";

export const metadata: Metadata = { title: "Connections" };

const PROVIDERS = [
  { name: "AWS", mark: "AWS", access: "Cross-account role with an external ID. No stored keys." },
  { name: "Azure", mark: "Az", access: "Federated identity. No client secrets." },
  { name: "GitHub", mark: "GH", access: "GitHub App on the repositories you choose." },
  { name: "Kubernetes", mark: "K8s", access: "A service account scoped to the namespaces you pick." },
  { name: "VM", mark: "VM", access: "SSH key, encrypted at rest. Read commands only." },
  { name: "Vercel", mark: "Vc", access: "Project token with read access." },
];

export default function ConnectionsPage() {
  return (
    <>
      <PageHeader title="Connections" />
      <PageBody className="max-w-4xl">
        <div className="max-w-[62ch]">
          <h2 className="text-[17px] font-semibold tracking-tight">Connect the infrastructure you already have</h2>
          <p className="mt-2 text-[13.5px] leading-relaxed text-muted-foreground">
            Groundcrew reads with scoped access. Anything that changes a resource goes through an
            approval or a pull request.
          </p>
        </div>
        <ul className="mt-6 grid gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-2">
          {PROVIDERS.map((provider) => (
            <li key={provider.name} className="flex gap-3 bg-background px-4 py-4">
              <span
                aria-hidden="true"
                className="flex size-9 shrink-0 items-center justify-center rounded-md border bg-card font-mono text-[11px] text-muted-foreground"
              >
                {provider.mark}
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center justify-between gap-2">
                  <h3 className="font-medium">{provider.name}</h3>
                  <span className="text-[11.5px] text-subtle">Coming in Phase 3</span>
                </div>
                <p className="mt-0.5 text-[12.5px] text-muted-foreground">{provider.access}</p>
              </div>
            </li>
          ))}
        </ul>
      </PageBody>
    </>
  );
}
