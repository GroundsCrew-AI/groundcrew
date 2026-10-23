/**
 * The Groundcrew agent for Kubernetes (docs/agent-plan.md "A5 plan"): one manifest to
 * `kubectl apply`. The agent runs in its own namespace with a ServiceAccount whose Roles
 * reach only the namespaces listed; it enrolls on first start with the one-time join token
 * and keeps its key on a small volume.
 */

export type KubernetesAgent = {
  /** The one-time join token. */
  token: string;
  /** Groundcrew's API, as the agent's configuration names it. */
  apiUrl: string;
  /** The agent image, e.g. groundkrew.com/public/groundcrew-agent:0.6.0. */
  image: string;
  /** Namespaces whose Deployments it may read (and act on, with `act`). */
  namespaces: string[];
  /** Also restart, roll back, and deploy there (each still needs an approval in Groundcrew). */
  act: boolean;
};

export const AGENT_NAMESPACE = "groundcrew-agent";
const NAME = /^[a-z0-9]([-a-z0-9]{0,61}[a-z0-9])?$/;

/** "shop, default shop" -> ["default", "shop"]; problems for anything not a namespace name. */
export function parseNamespaces(text: string): { namespaces: string[]; problems: string[] } {
  const names = [...new Set(text.split(/[\s,]+/).map((n) => n.trim()).filter(Boolean))].sort();
  const problems = names.filter((n) => !NAME.test(n)).map((n) => `${n} is not a namespace name`);
  return { namespaces: names.filter((n) => NAME.test(n)), problems };
}

function indent(text: string, spaces: number): string {
  const pad = " ".repeat(spaces);
  return text
    .split("\n")
    .map((line) => (line ? pad + line : line))
    .join("\n");
}

function role(ns: string, act: boolean): string {
  const rules = [
    `  - apiGroups: [apps]
    resources: [deployments, replicasets]
    verbs: [get, list, watch]`,
    `  - apiGroups: [""]
    resources: [pods, pods/log]
    verbs: [get, list, watch]`,
  ];
  if (act) {
    rules.push(
      `  - apiGroups: [apps]
    resources: [deployments]
    verbs: [patch, create, delete]`,
      `  - apiGroups: [""]
    resources: [services]
    verbs: [create]`,
      `  - apiGroups: [""]
    resources: [secrets]
    verbs: [create, update]`,
    );
  }
  return `apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: groundcrew-agent
  namespace: ${ns}
rules:
${rules.join("\n")}
---
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: groundcrew-agent
  namespace: ${ns}
roleRef:
  apiGroup: rbac.authorization.k8s.io
  kind: Role
  name: groundcrew-agent
subjects:
  - kind: ServiceAccount
    name: groundcrew-agent
    namespace: ${AGENT_NAMESPACE}`;
}

/** The agent's configuration: one commented example service per namespace. */
export function kubernetesConfig(apiUrl: string, namespaces: string[], act: boolean): string {
  const allow = act ? "[restart, rollback, deploy]" : "[]";
  const examples = namespaces
    .map(
      (ns) => `# - name: api-${ns}
#   runtime: kubernetes
#   kubernetes: {namespace: ${ns}, deployment: api}
#   allow: ${allow}`,
    )
    .join("\n");
  return `groundcrew:
  url: ${apiUrl}
state_dir: /var/lib/groundcrew-agent
# The Deployments to watch (one entry each), then: kubectl -n ${AGENT_NAMESPACE} rollout restart deployment/groundcrew-agent
services: []
${examples}`;
}

export function kubernetesManifest(a: KubernetesAgent): string {
  const parts = [
    `apiVersion: v1
kind: Namespace
metadata:
  name: ${AGENT_NAMESPACE}`,
    `apiVersion: v1
kind: ServiceAccount
metadata:
  name: groundcrew-agent
  namespace: ${AGENT_NAMESPACE}`,
    `# Used once, on the agent's first start; delete it after the agent shows Connected.
apiVersion: v1
kind: Secret
metadata:
  name: groundcrew-agent-join
  namespace: ${AGENT_NAMESPACE}
type: Opaque
stringData:
  token: ${a.token}`,
    `apiVersion: v1
kind: ConfigMap
metadata:
  name: groundcrew-agent
  namespace: ${AGENT_NAMESPACE}
data:
  agent.yaml: |
${indent(kubernetesConfig(a.apiUrl, a.namespaces, a.act), 4)}`,
    `# The agent's key and enrollment.
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: groundcrew-agent-state
  namespace: ${AGENT_NAMESPACE}
spec:
  accessModes: [ReadWriteOnce]
  resources:
    requests:
      storage: 1Gi`,
    `apiVersion: apps/v1
kind: Deployment
metadata:
  name: groundcrew-agent
  namespace: ${AGENT_NAMESPACE}
spec:
  replicas: 1
  strategy: {type: Recreate}
  selector:
    matchLabels: {app.kubernetes.io/name: groundcrew-agent}
  template:
    metadata:
      labels: {app.kubernetes.io/name: groundcrew-agent}
    spec:
      serviceAccountName: groundcrew-agent
      securityContext: {runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532}
      containers:
        - name: agent
          image: ${a.image}
          env:
            - {name: GROUNDCREW_JOIN_TOKEN_FILE, value: /etc/groundcrew-agent/join/token}
          resources:
            requests: {cpu: 20m, memory: 32Mi}
            limits: {cpu: 200m, memory: 128Mi}
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities: {drop: [ALL]}
          volumeMounts:
            - {name: config, mountPath: /etc/groundcrew-agent/agent.yaml, subPath: agent.yaml, readOnly: true}
            - {name: join, mountPath: /etc/groundcrew-agent/join, readOnly: true}
            - {name: state, mountPath: /var/lib/groundcrew-agent}
      volumes:
        - {name: config, configMap: {name: groundcrew-agent}}
        - {name: join, secret: {secretName: groundcrew-agent-join, optional: true}}
        - {name: state, persistentVolumeClaim: {claimName: groundcrew-agent-state}}`,
    ...a.namespaces.map((ns) => role(ns, a.act)),
  ];
  return parts.join("\n---\n") + "\n";
}

/** The command that applies it: the manifest on stdin, nothing written to disk. */
export function kubernetesApply(a: KubernetesAgent): string {
  return `kubectl apply -f - <<'EOF'\n${kubernetesManifest(a)}EOF`;
}
