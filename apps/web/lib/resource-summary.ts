/** Short, human configuration rows for the map's side panel, per resource type. Built from the
 * API's (already redacted) config; secret values never reach the API, so none can show here. */

type Config = Record<string, unknown>;
export type Row = { label: string; value: string };

function str(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (Array.isArray(value)) return value.length ? value.map((v) => String(v)).join(", ") : null;
  if (typeof value === "boolean") return value ? "Yes" : "No";
  return String(value);
}

function list<T = Config>(value: unknown): T[] {
  return Array.isArray(value) ? (value as T[]) : [];
}

function obj(value: unknown): Config {
  return value && typeof value === "object" && !Array.isArray(value) ? (value as Config) : {};
}

function rows(pairs: [string, unknown][]): Row[] {
  return pairs.flatMap(([label, value]) => {
    const text = str(value);
    return text === null ? [] : [{ label, value: text }];
  });
}

export function configRows(type: string, config: Config): Row[] {
  switch (type) {
    case "ecs:service": {
      const containers = list(config.containers);
      const network = obj(config.network);
      return rows([
        ["Tasks", `${config.running_count ?? 0} running of ${config.desired_count ?? 0}`],
        ["Task definition", config.task_family ? `${config.task_family}:${config.task_revision}` : null],
        ["Size", config.cpu ? `${config.cpu} CPU units, ${config.memory} MiB` : null],
        ["Images", containers.map((c) => `${c.image_repository}:${String(c.image_tag ?? "").slice(0, 12)}`)],
        ["Ports", containers.flatMap((c) => list(c.ports).map((p) => String(p.port)))],
        ["Public IPs", config.public_ips],
        ["Load balancer", config.has_load_balancer ? "Yes" : "None"],
        ["Secrets injected", containers.flatMap((c) => list(c.secrets).map((s) => String(s.name)))],
        ["Env var names", containers.flatMap((c) => list<string>(c.env_names))],
        ["Log groups", containers.map((c) => obj(c.log).group).filter(Boolean)],
        ["Security groups", network.security_groups],
      ]);
    }
    case "rds:db": {
      const endpoint = obj(config.endpoint);
      return rows([
        ["Engine", `${config.engine} ${config.engine_version}`],
        ["Class", config.instance_class],
        ["Status", config.status],
        ["Endpoint", endpoint.address ? `${endpoint.address}:${endpoint.port}` : null],
        ["Publicly accessible", config.publicly_accessible],
        ["Multi-AZ", config.multi_az],
        ["Encrypted", config.storage_encrypted],
        ["Security groups", config.security_groups],
      ]);
    }
    case "secretsmanager:secret":
      return rows([
        ["Value", "Never read by Groundcrew"],
        ["Description", config.description],
        ["Last changed", config.last_changed_at],
        ["Rotation", config.rotation_enabled ? "On" : "Off"],
        ["Versions", Object.entries(obj(config.version_stages)).map(([id, stages]) => `${id} ${str(stages)}`)],
      ]);
    case "ecr:repository":
      return rows([
        ["URI", config.uri],
        ["Scan on push", obj(config).scan_on_push],
        ["Latest images", list(config.images).map((i) => str(i.tags)?.slice(0, 12) ?? "untagged")],
      ]);
    case "vercel:project": {
      const latest = obj(config.latest_production);
      return rows([
        ["Framework", config.framework],
        ["Domains", config.production_aliases],
        ["Git repository", obj(config.link).repo ?? "Not linked"],
        ["Env vars", list(config.env).map((e) => `${e.key} (${e.type})`)],
        ["Readable values", Object.entries(obj(config.env_values)).map(([k, v]) => `${k}=${String(v)}`)],
        ["Latest production", latest.state ? `${String(latest.state).toLowerCase()}, ${latest.url}` : null],
      ]);
    }
    case "github:workflow": {
      const last = obj(config.last_run);
      return rows([
        ["File", config.path],
        ["Triggers", config.triggers],
        ["Branches", config.branches],
        ["Jobs", list(config.jobs).map((j) => String(j.id))],
        ["Last run", last.status ? `${last.conclusion ?? last.status}, ${String(last.head_sha ?? "").slice(0, 7)}` : null],
      ]);
    }
    case "github:repository":
      return rows([
        ["Default branch", config.default_branch],
        ["Head commit", String(config.head_commit ?? "").slice(0, 7) || null],
        ["Workflows", config.workflows],
        ["Terraform resources", config.terraform_resources],
      ]);
    case "logs:log-group":
      return rows([["Retention", config.retention_days ? `${config.retention_days} days` : "Never expires"]]);
    case "iam:role":
      return rows([
        ["Trusted by", [...list<string>(config.trusted_services), ...list<string>(config.github_oidc_subjects)]],
        ["Inline policies", Object.keys(obj(config.inline_policies))],
        ["Managed policies", config.attached_policies],
      ]);
    case "ec2:security-group":
      return rows([
        ["Open to the internet", config.open_to_world],
        ["Inbound rules", list(config.ingress).length],
        ["VPC", config.vpc_id],
      ]);
    default:
      return rows(
        Object.entries(config)
          .filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v))
          .slice(0, 8)
          .map(([k, v]) => [k.replaceAll("_", " "), v]),
      );
  }
}
