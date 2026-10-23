import type { Metadata } from "next";

import { Callout, Code, DocSection, DocsArticle, Inline, Step, Steps, Table } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Install an agent",
  description:
    "Install the Groundcrew agent on a server or VM. It reports the services you list and connects out to Groundcrew.",
};

const TOC = [
  { id: "why", title: "When to use the agent" },
  { id: "install", title: "Install and enroll" },
  { id: "services", title: "List your services" },
  { id: "reads", title: "What the agent reads and sends" },
  { id: "fixes", title: "Incidents and fixes" },
  { id: "deploys", title: "Deploys" },
  { id: "permissions", title: "Permissions" },
  { id: "trouble", title: "Troubleshooting" },
  { id: "change", title: "Upgrade, reinstall, or remove" },
];

const CONFIG = `groundcrew:
  url: https://api.groundkrew.com
services:
  - name: web
    runtime: docker
    container: web
    health:
      url: http://127.0.0.1:8000/health
    logs: container
    allow: [restart]
  - name: shop
    runtime: compose
    compose: {project: shop, service: web}
  - name: nginx
    runtime: systemd
    unit: nginx.service`;

export default function ConnectAgentPage() {
  return (
    <DocsArticle
      path="/docs/connect/agent"
      title="Install an agent"
      lead="The Groundcrew agent is a small program you install on a server. It reports the services you list in its configuration file, and it connects out to Groundcrew, so nothing has to reach your server."
      toc={TOC}
    >
      <DocSection id="why" title="When to use the agent">
        <p>
          Use it for servers and VMs on any cloud or in your own data center: services run with Docker, Docker Compose,
          or systemd. AWS and Kubernetes connect without an agent, through a role or a kubeconfig.
        </p>
        <p>The agent runs on Linux with systemd, on amd64 or arm64.</p>
      </DocSection>

      <DocSection id="install" title="Install and enroll">
        <Steps>
          <Step title="Create a join token">
            <p>
              In Groundcrew, open <strong>Connections</strong> and choose <strong>Install an agent</strong> on the Agent
              card. Name the server and create the token. The token works once, for one hour, and is shown only then.
            </p>
          </Step>
          <Step title="Run the install command on the server">
            <p>Copy the command from the dialog and run it on the server. It looks like this:</p>
            <Code>{"curl -fsSL https://groundkrew.com/agent/install.sh | sudo sh -s -- --token gcj_..."}</Code>
            <p>
              It downloads the agent for your CPU, checks it against <Inline>SHA256SUMS</Inline>, installs it to{" "}
              <Inline>/usr/local/bin</Inline>, creates the <Inline>groundcrew-agent</Inline> user and service, then
              enrolls and starts it. The dialog shows <strong>Connected</strong> when the agent has enrolled.
            </p>
          </Step>
        </Steps>
        <Callout kind="safety">
          <p>
            On enrollment the agent makes its own key. The private key stays on the server; Groundcrew keeps only the
            public key and checks the signature on every request, so a copied request cannot be sent again.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="services" title="List your services">
        <p>
          Edit <Inline>/etc/groundcrew-agent/agent.yaml</Inline>. Each service has a name, a runtime, and what to find
          it by:
        </p>
        <Code>{CONFIG}</Code>
        <Table
          head={["Setting", "Meaning"]}
          rows={[
            [<Inline key="r">runtime</Inline>, "docker, compose, or systemd."],
            [<Inline key="c">container</Inline>, "Docker: the container name."],
            [<Inline key="p">compose</Inline>, "Compose: the project and the service."],
            [<Inline key="u">unit</Inline>, "systemd: the unit name."],
            [<Inline key="h">health.url</Inline>, "A health check the agent calls on the server."],
            [<Inline key="d">deploy.image</Inline>, "Optional: the only image repository deploys may come from."],
            [
              <Inline key="a">allow</Inline>,
              "What the agent may do to this service: restart, rollback, deploy (systemd: restart only). Leave it out to watch only.",
            ],
          ]}
        />
        <p>
          Then check the file and restart the agent: <Inline>sudo groundcrew-agent check</Inline> finds each service and
          says what is missing, and <Inline>sudo systemctl restart groundcrew-agent</Inline> loads the change. The
          services appear on the map under the server.
        </p>
      </DocSection>

      <DocSection id="reads" title="What the agent reads and sends">
        <ul>
          <li>The server&apos;s name, operating system, CPU architecture, and kernel.</li>
          <li>For each listed service: whether it was found, its image and image digest, and its ports.</li>
          <li>
            Every 10 seconds, each service&apos;s state: running or not, restarts, the last exit code, an out-of-memory
            kill, and failed health checks in a row. With it, what changed: state changes, failed and recovered health
            checks, image changes, and error lines from the service&apos;s logs (container logs, or the unit&apos;s
            journal), redacted on the server before they leave it.
          </li>
          <li>A heartbeat every 30 seconds, so Groundcrew shows the agent as online or offline.</li>
        </ul>
        <Callout kind="safety">
          <p>
            The agent never reads environment variables or secret values, and it reads only the services you list.
            Unknown keys in the configuration file are errors, so a typo never silently changes what it does.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="fixes" title="Incidents and fixes">
        <p>Groundcrew opens an incident on a service when:</p>
        <ul>
          <li>its container or unit stops running (exited, restarting, failed);</li>
          <li>its health check fails three times in a row;</li>
          <li>ten or more error lines show up in its logs within five minutes.</li>
        </ul>
        <p>
          Medic diagnoses it from what the agent reported. When the service is down, Medic proposes one fix:
        </p>
        <ul>
          <li>
            <strong>Roll back</strong>, when its image changed just before it broke and <Inline>rollback</Inline> is
            allowed. The agent recreates the container from the image it ran before (it remembers the last few images
            each container ran, while those are still on the server), with the same settings, ports, networks, and
            volumes. If the recreated container does not run and pass its health check, the agent puts the current one
            back.
          </li>
          <li>
            <strong>Restart</strong>, otherwise, when <Inline>restart</Inline> is allowed.
          </li>
        </ul>
        <p> After you approve it, Groundcrew sends the
          agent a command signed with Groundcrew&apos;s own key. The agent runs it only if the signature matches the key
          it pinned when it first connected, the command has not expired or run before, and its configuration allows
          it. Medic then checks the agent&apos;s next reports and resolves the incident once the service runs and passes
          its health checks.
        </p>
        <Callout kind="note">
          <p>
            Restarting a systemd unit needs permission for the agent&apos;s user. Install with{" "}
            <Inline>--restart-units nginx.service,app.service</Inline> to allow exactly those units (a polkit rule;
            polkit 0.106 or newer, as on Ubuntu 23.10+ and Debian 12+). Without it, the agent reports that the restart
            was refused. Rollback is for Docker and Compose services. Docker and Compose services need nothing more.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="deploys" title="Deploys">
        <p>
          A service whose <Inline>allow</Inline> list includes <Inline>deploy</Inline> shows up as a target on the{" "}
          <strong>Deploy</strong> page. Groundcrew builds and tests the app in its isolated build service; after you
          approve the plan, the image goes to a registry and the agent puts it in place:
        </p>
        <Steps>
          <Step title="Pull">
            <p>
              The agent pulls the image with a login Groundcrew gives it for this deploy only. It fetches that login and
              your env values once, over its signed connection; they are not stored on the server.
            </p>
          </Step>
          <Step title="Replace">
            <p>
              It recreates the container from the new image, keeping its name, ports, networks, volumes, and other
              settings, with your env values set on top. A service with no container yet gets a new one with the
              app&apos;s port published on the same host port, or on the next free one above it when something
              else on the server already uses that port. The deploy shows the host port it chose.
            </p>
          </Step>
          <Step title="Check, or go back">
            <p>
              The new container must keep running and pass its health check. Otherwise the agent removes it and starts
              the previous one again, and the deploy fails with the reason.
            </p>
          </Step>
        </Steps>
        <p>Where images are kept, on the agent&apos;s <strong>Registry</strong> setting in Connections:</p>
        <ul>
          <li>
            <strong>Groundcrew registry</strong> (the default): nothing to set up. Each workspace&apos;s images are
            readable only with that workspace&apos;s short-lived pull tokens.
          </li>
          <li>
            <strong>Your own registry</strong>: Docker Hub, GitHub Container Registry, or any registry with a username
            and token (stored encrypted, never shown again), or Amazon ECR through an AWS connection that can deploy.
          </li>
        </ul>
        <p>
          To restrict a service to one image repository, set <Inline>deploy.image</Inline> in its configuration; the
          agent refuses images from anywhere else. Rolling a deploy back returns the container to the image it ran
          before; env values set by the deploy stay as they are.
        </p>
      </DocSection>

      <DocSection id="permissions" title="Permissions">
        <p>
          The agent runs as its own system user, with a hardened systemd unit that can write only its state directory.
          To read Docker containers, the installer adds that user to the <Inline>docker</Inline> group. Members of that
          group can control Docker, which on most servers is equivalent to root. If you list only systemd services,
          install with <Inline>--no-docker</Inline>.
        </p>
        <p>
          Nothing changes on the server unless a service&apos;s <Inline>allow</Inline> list permits it, and every action
          still needs an approval in Groundcrew. The installer also adds the user to <Inline>systemd-journal</Inline>{" "}
          so it can read the logs of the units you list.
        </p>
      </DocSection>

      <DocSection id="trouble" title="Troubleshooting">
        <ul>
          <li>
            <strong>This join token is not valid.</strong> It was used already, expired, or was replaced. On the
            Connections page, choose <strong>New join token</strong> on the agent and run the install command again.
          </li>
          <li>
            <strong>The clock is off.</strong> Requests carry the time and Groundcrew refuses ones more than five minutes
            off. Turn on time sync (<Inline>timedatectl set-ntp true</Inline>).
          </li>
          <li>
            <strong>Offline.</strong> Groundcrew has not heard from the agent for three minutes. Check{" "}
            <Inline>systemctl status groundcrew-agent</Inline> and <Inline>journalctl -u groundcrew-agent</Inline>, and
            that the server can reach <Inline>api.groundkrew.com</Inline> on port 443.
          </li>
          <li>
            <strong>A service shows as missing.</strong> Run <Inline>sudo groundcrew-agent check</Inline>: it names the
            container, Compose service, or unit it could not find.
          </li>
        </ul>
      </DocSection>

      <DocSection id="change" title="Upgrade, reinstall, or remove">
        <p>
          To upgrade, run the install command again without a token: the configuration is kept and the agent restarts.
          To move the agent to a new server or reinstall it, choose <strong>New join token</strong> and install with it;
          the new key replaces the old one.
        </p>
        <p>
          To remove it, remove the connection on the Connections page, which stops the agent&apos;s key at once and
          deletes what it reported. Then, on the server: <Inline>sudo systemctl disable --now groundcrew-agent</Inline>{" "}
          and delete <Inline>/usr/local/bin/groundcrew-agent</Inline>, <Inline>/etc/groundcrew-agent</Inline>, and{" "}
          <Inline>/var/lib/groundcrew-agent</Inline>.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
