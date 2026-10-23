import type { Metadata } from "next";

import { REPOSITORY_URL } from "@/components/public/contact";
import { Callout, Code, DocSection, DocsArticle, Inline, SubHeading } from "@/components/public/docs-ui";

export const metadata: Metadata = {
  title: "Connect Kubernetes",
  description:
    "Give Groundcrew a read-only service account, paste its kubeconfig, and optionally allow deploys and emergency fixes.",
};

const TOC = [
  { id: "before", title: "Before you start" },
  { id: "read-only", title: "1. Create the service account" },
  { id: "kubeconfig", title: "2. Build a kubeconfig and connect" },
  { id: "optional-reads", title: "Optional: ConfigMap changes" },
  { id: "operate", title: "Optional: emergency fixes" },
  { id: "deploy", title: "Optional: deploys" },
  { id: "trouble", title: "Troubleshooting" },
  { id: "change", title: "Change or remove" },
];

const READ_ONLY = `kubectl apply -f k8s-readonly-rbac.yaml
kubectl -n groundcrew create token groundcrew-reader --duration=24h`;

const KUBECONFIG = `SERVER=$(kubectl config view --minify -o jsonpath='{.clusters[0].cluster.server}')
CA=$(kubectl config view --minify --raw -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')
TOKEN=<the token from the command above>
cat > groundcrew.kubeconfig <<EOF
apiVersion: v1
kind: Config
clusters:
  - name: cluster
    cluster:
      server: $SERVER
      certificate-authority-data: $CA
users:
  - name: groundcrew-reader
    user:
      token: $TOKEN
contexts:
  - name: groundcrew
    context: {cluster: cluster, user: groundcrew-reader}
current-context: groundcrew
EOF`;

export default function ConnectKubernetesPage() {
  return (
    <DocsArticle
      path="/docs/connect/kubernetes"
      title="Connect Kubernetes"
      lead="Groundcrew reads a cluster with a service account you create from a manifest you can read first. The account is read-only and cannot see Secrets."
      toc={TOC}
    >
      <DocSection id="before" title="Before you start">
        <ul>
          <li>You are an owner of the Groundcrew workspace.</li>
          <li>
            You have <Inline>kubectl</Inline> with a cluster-admin context for the cluster, to apply the RBAC manifest.
          </li>
          <li>
            The cluster&apos;s API server has a public HTTPS address. Groundcrew reaches clusters over the internet and
            refuses private and loopback addresses.
          </li>
          <li>For deploys: Kubernetes 1.30 or later, an Ingress controller, and a registry the cluster can pull from.</li>
        </ul>
      </DocSection>

      <DocSection id="read-only" title="1. Create the service account">
        <p>
          Download <a href="/k8s-readonly-rbac.yaml">k8s-readonly-rbac.yaml</a>, read it, and apply it. It creates the
          namespace <Inline>groundcrew</Inline>, the service account <Inline>groundcrew-reader</Inline>, and a
          ClusterRole that can get, list, and watch namespaces, Deployments, ReplicaSets, StatefulSets,
          ControllerRevisions, Services, Ingresses, Pods, and events, and read pod logs.
        </p>
        <Callout kind="safety">
          <p>No access to Secrets or ConfigMaps, no exec into pods, and no write verb of any kind.</p>
        </Callout>
        <Code>{READ_ONLY}</Code>
      </DocSection>

      <DocSection id="kubeconfig" title="2. Build a kubeconfig and connect">
        <p>
          Groundcrew accepts a kubeconfig with a service account token (or inline client certificate data) and the
          cluster&apos;s CA inline. It refuses exec plugins, auth providers, file paths, proxies, and plain HTTP.
        </p>
        <Code>{KUBECONFIG}</Code>
        <p>
          In Groundcrew, open <strong>Connections</strong> and choose <strong>Upload kubeconfig</strong>. Give the
          cluster a name, upload <Inline>groundcrew.kubeconfig</Inline> or paste its contents, and choose{" "}
          <strong>Verify and connect</strong>. Groundcrew lists namespaces with it before saving, then stores it
          encrypted. Then run discovery.
        </p>
        <Callout kind="warning">
          <p>
            A token created with <Inline>--duration</Inline> expires (24 hours above). When it does, discovery and the
            incident watcher stop working for the cluster: create a new token and connect again with an updated
            kubeconfig. Choose a longer duration if your cluster allows it.
          </p>
        </Callout>
      </DocSection>

      <DocSection id="optional-reads" title="Optional: ConfigMap changes">
        <p>
          A changed ConfigMap or Secret is a common cause of a broken rollout. The read-only manifest has two optional,
          commented-out rules that let Groundcrew see when the ones your workloads use changed. Groundcrew only ever asks
          for their metadata (names and change times), never their contents. Kubernetes has no metadata-only
          permission, though: the <Inline>list</Inline> right would let a client that asked differently read contents,
          so add them only if you accept that, and preferably with a RoleBinding in your app&apos;s namespaces.
        </p>
        <p>
          Then choose <strong>Detect ConfigMap changes</strong> on the connection. Medic will show a changed ConfigMap as
          a possible cause, with a note to restore it, since it cannot restore what it does not read.
        </p>
      </DocSection>

      <DocSection id="operate" title="Optional: emergency fixes">
        <p>
          Apply <a href="/k8s-operate-rbac.yaml">k8s-operate-rbac.yaml</a> to let the same service account roll a
          Deployment or StatefulSet back to its previous revision or restart it. It can only patch those two kinds and
          read their revisions. Then choose <strong>Enable emergency fixes</strong> on the connection; Groundcrew checks
          the permissions first. Every fix still waits for an approval.
        </p>
      </DocSection>

      <DocSection id="deploy" title="Optional: deploys">
        <SubHeading>A deployer account</SubHeading>
        <p>
          For deploys, apply the deployer manifests from Groundcrew&apos;s repository, <a href={`${REPOSITORY_URL}/blob/main/infra/kind/rbac.yaml`}>rbac.yaml</a> and{" "}
          <a href={`${REPOSITORY_URL}/blob/main/infra/kind/admission-policy.yaml`}>admission-policy.yaml</a>. They create a{" "}
          <Inline>groundcrew-deployer</Inline> service account that can create only namespaces named{" "}
          <Inline>gc-*</Inline> labelled <Inline>project=groundcrew</Inline>, and work only inside them, enforced by a
          ValidatingAdmissionPolicy (Kubernetes 1.30 or later). Connect the cluster with that account&apos;s
          kubeconfig.
        </p>
        <SubHeading>Deploy settings</SubHeading>
        <p>
          Choose <strong>Deploy settings</strong> on the connection to set where images go and how apps are reached: the
          registry (with a push token, or Amazon ECR through an AWS connection that has deploys on), an optional image
          pull secret, the Ingress class, the app domain (apps answer at <Inline>&lt;app&gt;.&lt;domain&gt;</Inline>),
          and TLS with an optional cert-manager cluster issuer. Registry tokens are stored encrypted and never shown
          again.
        </p>
      </DocSection>

      <DocSection id="trouble" title="Troubleshooting">
        <ul>
          <li>
            <strong>&quot;Groundcrew only accepts a service account token or inline client certificate data&quot;.</strong>{" "}
            The kubeconfig uses an exec plugin or auth provider, which is what EKS, GKE, and AKS kubeconfigs do by
            default. Build one with a service account token as above.
          </li>
          <li>
            <strong>&quot;Put the CA inline&quot;.</strong> The kubeconfig points at a CA file. Use{" "}
            <Inline>certificate-authority-data</Inline>, as the script above does with <Inline>--raw</Inline>.
          </li>
          <li>
            <strong>&quot;The cluster&apos;s server … is a private or internal address&quot;.</strong> The API server is
            only reachable inside your network. Groundcrew cannot connect to it. Use a cluster with a public endpoint, or
            enable one with access limited by your provider&apos;s settings.
          </li>
          <li>
            <strong>&quot;The cluster&apos;s server must use https&quot;.</strong> Plain HTTP API servers are refused.
          </li>
          <li>
            <strong>&quot;rejected the kubeconfig&apos;s credentials (401)&quot;.</strong> The token expired or was
            revoked. Create a new one and connect again.
          </li>
          <li>
            <strong>&quot;may not list … (403)&quot;.</strong> The service account&apos;s RBAC is missing that right. Apply
            the manifest again, or check you used the right service account.
          </li>
        </ul>
      </DocSection>

      <DocSection id="change" title="Change or remove">
        <p>
          To replace an expired token or change the service account, remove the connection and connect again with the
          new kubeconfig. To disconnect, remove the connection on the Connections page, which deletes everything
          Groundcrew discovered through it, then remove access in the cluster:
        </p>
        <Code>{`kubectl delete -f k8s-operate-rbac.yaml   # if you applied it
kubectl delete -f k8s-readonly-rbac.yaml`}</Code>
        <p>
          Apps Groundcrew deployed live in <Inline>gc-*</Inline> namespaces. Delete those namespaces to remove them.
        </p>
      </DocSection>
    </DocsArticle>
  );
}
