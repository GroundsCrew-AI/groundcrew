#!/usr/bin/env bash
# make kind-check: prove what groundcrew-deployer can and cannot do, as that ServiceAccount.
#   - RBAC with `kubectl auth can-i`
#   - the admission policy with server-side dry-run writes (can-i never reaches admission)
#   - the real flow: create gc-groundcrew-check, bind gc-namespace-admin there, use it, delete it
# Exits non-zero on the first surprise. KUBECONFIG_FILE overrides the kubeconfig used.
source "$(dirname "$0")/lib.sh"
ensure_tools
KCFG="${KUBECONFIG_FILE:-$STATE/groundcrew-deployer.host.kubeconfig}"
[ -f "$KCFG" ] || die "no $KCFG; run make kind-kubeconfig first."
CHECK_NS=gc-groundcrew-check
failures=0

k() { kubectl --kubeconfig "$KCFG" "$@"; }
pass() { printf '  ok    %s\n' "$*"; }
fail() { printf '  FAIL  %s\n' "$*"; failures=$((failures + 1)); }

can() {  # can <yes|no> <verb> <resource> [-n ns]
  local want=$1; shift
  local got
  got=$(k auth can-i "$@" 2>/dev/null || true)
  if [ "$got" = "$want" ]; then pass "can-i $* -> $got"; else fail "can-i $* -> $got (want $want)"; fi
}

admits() {  # admits <yes|no> <description> -- kubectl args (run with --dry-run=server)
  local want=$1 what=$2; shift 3
  local out
  if out=$(k "$@" --dry-run=server 2>&1); then got=yes; else got=no; fi
  if [ "$got" = "$want" ]; then
    pass "$what -> $([ "$got" = yes ] && echo allowed || echo denied)"
  else
    fail "$what -> $([ "$got" = yes ] && echo allowed || echo denied) (want $([ "$want" = yes ] && echo allowed || echo denied)): $out"
  fi
}

ns_yaml() { printf 'apiVersion: v1\nkind: Namespace\nmetadata:\n  name: %s\n%s' "$1" "${2:-}"; }
rb_yaml() {
  cat <<YAML
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata: {name: groundcrew-deployer, namespace: $1}
roleRef: {apiGroup: rbac.authorization.k8s.io, kind: ClusterRole, name: $2}
subjects: [{kind: ServiceAccount, name: $SA, namespace: $NAMESPACE}]
YAML
}

echo "Identity: $(k auth whoami -o jsonpath='{.status.userInfo.username}')"

echo "RBAC: read-only outside gc-*, no Secrets"
can yes list pods --all-namespaces
can yes get deployments -n kube-system
can no get secrets -n default
can no get secrets -n groundcrew-system
can no list secrets --all-namespaces
can no create deployments -n default
can no delete pods -n kube-system
can no create clusterrolebindings
can no create clusterroles
can no bind clusterroles/cluster-admin
can yes bind clusterroles/gc-namespace-admin

echo "RBAC: nothing in groundcrew-system (its own ServiceAccount, token, bindings)"
can no update serviceaccounts/groundcrew-deployer -n "$NAMESPACE"
can no delete serviceaccounts/groundcrew-deployer -n "$NAMESPACE"
can no create serviceaccounts/token -n "$NAMESPACE"
can no update secrets/groundcrew-deployer-token -n "$NAMESPACE"
can no delete secrets/groundcrew-deployer-token -n "$NAMESPACE"
can no update clusterrolebindings/groundcrew-deployer
can no delete clusterrolebindings/groundcrew-deployer-view
can no update clusterroles/gc-namespace-admin
can no delete validatingadmissionpolicies/groundcrew-deployer-scope
can no update validatingadmissionpolicybindings/groundcrew-deployer-scope
# RBAC allows these (creating RoleBindings and namespaces is cluster-wide in RBAC); the
# admission policy below is what denies them outside gc-*.
can yes create rolebindings -n "$NAMESPACE"
can yes delete namespaces/groundcrew-system

echo "Admission policy (server-side dry run)"
admits no "RoleBinding in groundcrew-system" -- create -f <(rb_yaml "$NAMESPACE" gc-namespace-admin)
admits no "RoleBinding in default" -- create -f <(rb_yaml default gc-namespace-admin)
admits no "delete namespace groundcrew-system" -- delete namespace "$NAMESPACE"
admits no "delete namespace default" -- delete namespace default
admits no "create namespace not named gc-*" -- create -f <(ns_yaml groundcrew-other $'  labels: {project: groundcrew}\n')
admits no "create gc-* namespace without project=groundcrew" -- create -f <(ns_yaml gc-unlabelled)
admits yes "create gc-* namespace with project=groundcrew" -- create -f <(ns_yaml "$CHECK_NS" $'  labels: {project: groundcrew}\n')

echo "Real flow in $CHECK_NS"
k delete namespace "$CHECK_NS" --ignore-not-found --wait=true >/dev/null 2>&1 || true
if k create -f <(ns_yaml "$CHECK_NS" $'  labels: {project: groundcrew}\n') >/dev/null; then
  pass "created namespace $CHECK_NS"
  admits no "RoleBinding in $CHECK_NS to the view ClusterRole" -- create -f <(rb_yaml "$CHECK_NS" view)
  admits no "RoleBinding in $CHECK_NS to cluster-admin" -- create -f <(rb_yaml "$CHECK_NS" cluster-admin)
  if k create -f <(rb_yaml "$CHECK_NS" gc-namespace-admin) >/dev/null; then
    pass "bound gc-namespace-admin in $CHECK_NS"
  else
    fail "could not bind gc-namespace-admin in $CHECK_NS"
  fi
  can yes create deployments -n "$CHECK_NS"
  can yes create ingresses.networking.k8s.io -n "$CHECK_NS"
  can yes get pods/log -n "$CHECK_NS"
  can yes get secrets -n "$CHECK_NS"
  can no create serviceaccounts/token -n "$CHECK_NS"
  can no create roles -n "$CHECK_NS"
  admits yes "create a Deployment in $CHECK_NS" -- create deployment check --image=busybox -n "$CHECK_NS"
  if k delete namespace "$CHECK_NS" --wait=false >/dev/null; then
    pass "deleted namespace $CHECK_NS"
  else
    fail "could not delete namespace $CHECK_NS"
  fi
else
  fail "could not create namespace $CHECK_NS"
fi

if [ "$failures" -gt 0 ]; then die "$failures check(s) failed"; fi
log "All checks passed."
