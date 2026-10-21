import type { Metadata } from "next";

import { REPOSITORY_URL } from "@/components/public/contact";
import { Article, Code, Inline, Section, SubHeading } from "@/components/public/prose";

export const metadata: Metadata = {
  title: "Connect Kubernetes",
  description:
    "Give Groundcrew a read-only service account, paste its kubeconfig, and optionally allow deploys and emergency fixes.",
};

const INFRA = `${REPOSITORY_URL}/blob/main/infra`;

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
    <Article
      title="Connect Kubernetes"
      lead="Groundcrew reads a cluster with a service account you create from a manifest you can read first. The account is read-only and cannot see Secrets."
    >
      <Section id="read-only" title="1. Create the read-only service account">
        <p>
          Download <a href={`${INFRA}/k8s-readonly-rbac.yaml`}>k8s-readonly-rbac.yaml</a>, read it, and apply it with a
          cluster-admin context. It creates the namespace <Inline>groundcrew</Inline>, the service account{" "}
          <Inline>groundcrew-reader</Inline>, and a ClusterRole that can get, list, and watch namespaces, Deployments,
          ReplicaSets, StatefulSets, ControllerRevisions, Services, Ingresses, Pods, and events, and read pod logs. It has
          no access to Secrets or ConfigMaps, no exec, and no write verb.
        </p>
        <Code>{READ_ONLY}</Code>
      </Section>

      <Section id="kubeconfig" title="2. Build a kubeconfig and paste it">
        <p>
          Groundcrew accepts a kubeconfig with a service account token and the cluster&apos;s CA inline. It refuses exec
          plugins, auth providers, file paths, and API servers on private or loopback addresses, so the cluster&apos;s API
          must be reachable from the internet.
        </p>
        <Code>{KUBECONFIG}</Code>
        <p>
          In Groundcrew, open <strong>Connections</strong>, add a Kubernetes cluster, and paste the file&apos;s contents.
          Groundcrew lists namespaces with it before saving, then stores it encrypted. A token created with{" "}
          <Inline>--duration</Inline> expires; when it does, create a new one and paste an updated kubeconfig.
        </p>
      </Section>

      <Section id="optional-reads" title="Optional: ConfigMap and Secret changes">
        <p>
          A changed ConfigMap or Secret is a common cause of a broken rollout. The read-only manifest has two optional,
          commented-out rules that let Groundcrew see when the ones your workloads use changed. Groundcrew only ever asks for
          their metadata (names and change times), never their contents. Kubernetes has no metadata-only permission,
          though: the <Inline>list</Inline> right would let a client that asked differently read contents, so add them
          only if you accept that, and preferably with a RoleBinding in your app&apos;s namespaces.
        </p>
      </Section>

      <Section id="operate" title="Optional: emergency fixes">
        <p>
          Apply <a href={`${INFRA}/k8s-operate-rbac.yaml`}>k8s-operate-rbac.yaml</a> to let the same service account roll a
          Deployment or StatefulSet back to its previous revision or restart it. It can only patch those two kinds and read
          their revisions. Then turn on emergency fixes for the cluster on the Connections page; Groundcrew checks the
          permissions first. Every fix still waits for an approval.
        </p>
      </Section>

      <Section id="deploy" title="Optional: deploys">
        <SubHeading>A deployer account</SubHeading>
        <p>
          For deploys, apply <a href={`${INFRA}/kind/rbac.yaml`}>kind/rbac.yaml</a> and{" "}
          <a href={`${INFRA}/kind/admission-policy.yaml`}>kind/admission-policy.yaml</a> (Kubernetes 1.30 or later). They
          create a <Inline>groundcrew-deployer</Inline> service account that can create only namespaces named{" "}
          <Inline>gc-*</Inline> labelled <Inline>project=groundcrew</Inline> and work only inside them, enforced by a
          ValidatingAdmissionPolicy. Connect the cluster with that account&apos;s kubeconfig.
        </p>
        <SubHeading>Deploy settings</SubHeading>
        <p>
          On the connection, an owner sets where images go and how apps are reached: the registry (with a push token, or
          Amazon ECR through an AWS connection that has deploys on), an optional image pull secret, the Ingress class, the
          app domain (apps answer at <Inline>&lt;app&gt;.&lt;domain&gt;</Inline>), and TLS with an optional cert-manager
          cluster issuer. Registry tokens are stored encrypted and never shown again.
        </p>
      </Section>

      <Section id="disconnect" title="Disconnect">
        <p>
          Delete the connection on the Connections page, then remove access in the cluster:
        </p>
        <Code>{`kubectl delete -f k8s-operate-rbac.yaml   # if you applied it
kubectl delete -f k8s-readonly-rbac.yaml`}</Code>
      </Section>
    </Article>
  );
}
