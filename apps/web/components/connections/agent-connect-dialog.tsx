"use client";

import { CircleCheck, LoaderCircle } from "lucide-react";
import { type FormEvent, useState } from "react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { type AgentJoin, type Connection, useAgentToken, useConnectAgent, useConnections } from "@/lib/discovery";
import { useWorkspace } from "@/lib/workspace";

import { CopyField } from "./copy-field";
import { SetupNote, SetupStep, SetupSteps, Ui } from "./setup-steps";

type DialogProps = { open: boolean; onOpenChange: (open: boolean) => void };

const SERVICES_EXAMPLE = `services:
  - name: web
    runtime: docker          # docker | compose | systemd
    container: web
    health:
      url: http://127.0.0.1:8000/health
    allow: [restart]         # leave out to watch only`;

/** The install command: the script and binaries come from this site (/agent/). */
export function installCommand(origin: string, join: Pick<AgentJoin, "token" | "api_url">): string {
  return `curl -fsSL ${origin}/agent/install.sh | sudo sh -s -- --token ${join.token} --url ${join.api_url}`;
}

function expiresAt(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

/** Add an agent (name it, then install it with the one-time token), or, given `connection`, a
 * new token for an existing one (setup not finished in time, or a reinstall). */
export function AgentConnectDialog({ open, onOpenChange, connection }: DialogProps & { connection?: Connection }) {
  const { workspace } = useWorkspace();
  const create = useConnectAgent();
  const renew = useAgentToken();
  const [name, setName] = useState("");
  const [join, setJoin] = useState<AgentJoin | null>(null);
  const connections = useConnections(join ? workspace?.id : undefined);
  const live = connections.data?.find((c) => c.id === join?.connection.id) ?? join?.connection;
  const enrolled = Boolean(live?.agent?.enrolled && live.agent.enrolled_at && join && live.agent.enrolled_at > enrolledBefore(join));

  function close(next: boolean) {
    onOpenChange(next);
    if (!next) {
      setName("");
      setJoin(null);
      create.reset();
      renew.reset();
    }
  }

  function submit(event: FormEvent) {
    event.preventDefault();
    if (!workspace) return;
    create.mutate({ workspace_id: workspace.id, name: name.trim() }, { onSuccess: setJoin });
  }

  const error = create.error ?? renew.error;
  const origin = typeof window === "undefined" ? "https://groundkrew.com" : window.location.origin;

  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{connection ? `New join token for ${connection.name}` : "Install a Groundcrew agent"}</DialogTitle>
          <DialogDescription>
            The agent runs on your server and reports the services you list in its configuration file. It connects
            out to Groundcrew, so nothing has to reach in, and it never reads environment or secret values.
          </DialogDescription>
        </DialogHeader>

        {!join && !connection && (
          <form onSubmit={submit} className="space-y-5">
            <div className="space-y-1.5">
              <Label htmlFor="agent-name">Name</Label>
              <Input
                id="agent-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="web-1"
                maxLength={200}
                required
                autoFocus
              />
              <p className="text-[12px] text-muted-foreground">How this server shows in Groundcrew.</p>
            </div>
            {error && (
              <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
                {error.message}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => close(false)}>
                Cancel
              </Button>
              <Button type="submit" disabled={create.isPending || !name.trim()}>
                {create.isPending ? "Creating" : "Create join token"}
              </Button>
            </DialogFooter>
          </form>
        )}

        {!join && connection && (
          <div className="space-y-5">
            <p className="text-[13.5px] text-muted-foreground">
              The current token stops working. If the agent is already installed, enrolling again gives it a new key.
            </p>
            {error && (
              <p role="alert" className="text-[12.5px] text-[var(--status-failed)]">
                {error.message}
              </p>
            )}
            <DialogFooter>
              <Button type="button" variant="ghost" onClick={() => close(false)}>
                Cancel
              </Button>
              <Button
                disabled={renew.isPending}
                onClick={() => renew.mutate(connection, { onSuccess: setJoin, onError: (e) => toast.error(e.message) })}
              >
                {renew.isPending ? "Creating" : "Create join token"}
              </Button>
            </DialogFooter>
          </div>
        )}

        {join && (
          <div className="space-y-6">
            <SetupSteps>
              <SetupStep n={1} title="Install and enroll the agent on your server">
                <p>
                  Run this on the server (Linux with systemd, amd64 or arm64). It checks the download against its
                  checksum, installs the <Ui>groundcrew-agent</Ui> service, and enrolls it.
                </p>
                <CopyField label="Install command" value={installCommand(origin, join)} />
                <SetupNote tone="warn">
                  The token works once, until {expiresAt(join.expires_at)}. It is shown only now.
                </SetupNote>
              </SetupStep>
              <SetupStep n={2} title="List the services it should watch">
                <p>
                  Add them to <Ui>/etc/groundcrew-agent/agent.yaml</Ui>, then run{" "}
                  <code className="font-mono text-[12.5px]">sudo groundcrew-agent check</code> and{" "}
                  <code className="font-mono text-[12.5px]">sudo systemctl restart groundcrew-agent</code>.
                </p>
                <CopyField label="agent.yaml" value={SERVICES_EXAMPLE} multiline />
                <p>
                  The agent only watches until a service lists what it may do under <Ui>allow</Ui>, and each action
                  still needs your approval here.
                </p>
              </SetupStep>
            </SetupSteps>
            <p
              role="status"
              className="flex items-center gap-2 rounded-md border px-3 py-2 text-[13px]"
            >
              {enrolled ? (
                <>
                  <CircleCheck aria-hidden="true" className="size-4 text-[var(--status-ok)]" />
                  Connected: {live?.agent?.hostname}
                </>
              ) : (
                <>
                  <LoaderCircle aria-hidden="true" className="size-4 animate-spin text-muted-foreground" />
                  Waiting for the agent to enroll
                </>
              )}
            </p>
            <DialogFooter>
              <Button onClick={() => close(false)}>{enrolled ? "Done" : "Close"}</Button>
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}

/** When the agent last enrolled before this token was made: only a later enrollment used it. */
function enrolledBefore(join: AgentJoin): string {
  return join.connection.agent?.enrolled_at ?? "";
}
