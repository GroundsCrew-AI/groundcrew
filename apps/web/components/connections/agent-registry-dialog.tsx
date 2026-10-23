"use client";

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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  type Connection,
  useAgentRegistry,
  useClearAgentRegistry,
  useConnections,
} from "@/lib/discovery";

type Choice = "groundcrew" | "token" | "ecr";

/** Where deploys to this agent keep their images: the Groundcrew registry (nothing to set up),
 * or the team's own (Docker Hub, GHCR, any registry with a token; or Amazon ECR). */
export function AgentRegistryDialog({
  connection,
  open,
  onOpenChange,
}: {
  connection: Connection;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const agent = connection.agent;
  const current = (agent?.registry_auth ?? "groundcrew") as Choice;
  const [choice, setChoice] = useState<Choice>(current);
  const [registry, setRegistry] = useState(
    current === "groundcrew" ? "" : (agent?.registry ?? ""),
  );
  const [username, setUsername] = useState(agent?.registry_username ?? "");
  const [token, setToken] = useState("");
  const [awsId, setAwsId] = useState(agent?.registry_aws_connection_id ?? "");
  const save = useAgentRegistry();
  const clear = useClearAgentRegistry();
  const all = useConnections(open ? connection.workspace_id : undefined);
  const aws = (all.data ?? []).filter(
    (c) => c.kind === "aws" && c.capabilities?.includes("deploy"),
  );
  const error = save.error ?? clear.error;

  function submit(event: FormEvent) {
    event.preventDefault();
    const done = {
      onSuccess: () => {
        toast.success("Registry saved");
        onOpenChange(false);
      },
    };
    if (choice === "groundcrew") {
      clear.mutate(connection, done);
      return;
    }
    save.mutate(
      {
        connection,
        body:
          choice === "ecr"
            ? {
                registry: registry.trim(),
                registry_auth: "ecr",
                aws_connection_id: awsId || null,
              }
            : {
                registry: registry.trim(),
                registry_auth: "token",
                username: username.trim() || null,
                token: token || null,
              },
      },
      done,
    );
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-h-[90dvh] overflow-y-auto sm:max-w-md">
        <form onSubmit={submit} className="space-y-5">
          <DialogHeader>
            <DialogTitle>Registry for {connection.name}</DialogTitle>
            <DialogDescription>
              Where deploys to this server keep their images. The agent pulls
              them with a login Groundcrew gives it for that deploy only.
            </DialogDescription>
          </DialogHeader>
          <fieldset className="space-y-2" aria-label="Registry">
            {(
              [
                [
                  "groundcrew",
                  "Groundcrew registry",
                  "Nothing to set up. Images stay private to this workspace.",
                ],
                [
                  "token",
                  "Your own registry",
                  "Docker Hub, GitHub Container Registry, or any registry with a token.",
                ],
                [
                  "ecr",
                  "Amazon ECR",
                  "Through an AWS connection that can deploy; no token is stored.",
                ],
              ] as const
            ).map(([value, title, hint]) => (
              <label
                key={value}
                className={`flex cursor-pointer gap-2.5 rounded-lg border px-3 py-2.5 text-[13px] ${choice === value ? "border-primary" : ""}`}
              >
                <input
                  type="radio"
                  name="registry"
                  value={value}
                  checked={choice === value}
                  onChange={() => setChoice(value)}
                  className="mt-0.5 accent-[var(--primary)]"
                />
                <span>
                  <span className="block font-medium">{title}</span>
                  <span className="block text-[12px] text-muted-foreground">
                    {hint}
                  </span>
                </span>
              </label>
            ))}
          </fieldset>
          {choice !== "groundcrew" && (
            <div className="space-y-1.5">
              <Label htmlFor="agent-registry">Registry</Label>
              <Input
                id="agent-registry"
                value={registry}
                onChange={(e) => setRegistry(e.target.value)}
                placeholder={
                  choice === "ecr"
                    ? "123456789012.dkr.ecr.us-east-1.amazonaws.com"
                    : "ghcr.io/acme"
                }
                className="font-mono text-[12.5px]"
                required
              />
            </div>
          )}
          {choice === "token" && (
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="agent-registry-user">Username</Label>
                <Input
                  id="agent-registry-user"
                  value={username}
                  onChange={(e) => setUsername(e.target.value)}
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="agent-registry-token">Token</Label>
                <Input
                  id="agent-registry-token"
                  type="password"
                  value={token}
                  onChange={(e) => setToken(e.target.value)}
                  placeholder={
                    agent?.registry_token_saved
                      ? "saved (leave empty to keep)"
                      : ""
                  }
                />
              </div>
            </div>
          )}
          {choice === "ecr" && (
            <div className="space-y-1.5">
              <Label htmlFor="agent-registry-aws">AWS connection</Label>
              <Select value={awsId} onValueChange={setAwsId}>
                <SelectTrigger id="agent-registry-aws" className="w-full">
                  <SelectValue
                    placeholder={
                      aws.length ? "Choose" : "No AWS connection can deploy"
                    }
                  />
                </SelectTrigger>
                <SelectContent>
                  {aws.map((c) => (
                    <SelectItem key={c.id} value={c.id}>
                      {c.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            </div>
          )}
          {error && (
            <p
              role="alert"
              className="text-[12.5px] text-[var(--status-failed)]"
            >
              {error.message}
            </p>
          )}
          <DialogFooter>
            <Button
              type="button"
              variant="ghost"
              onClick={() => onOpenChange(false)}
            >
              Cancel
            </Button>
            <Button type="submit" disabled={save.isPending || clear.isPending}>
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
