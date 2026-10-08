#!/usr/bin/env bash
# Set API_URL on the Vercel project (Production and Preview) and redeploy the current
# production deployment so the page reads the new value. Used by deploy.yml and scenario.sh.
#
# Redeploying reuses the source of the last production deployment, so this never ships web
# code; it only refreshes the environment variable.
#
# Env: VERCEL_TOKEN, API_URL, and VERCEL_PROJECT_ID / VERCEL_ORG_ID (default: web/.vercel/project.json)
# In GitHub Actions it also writes the API_URL and the deployment's URLs to the job summary.
set -euo pipefail

: "${VERCEL_TOKEN:?}" "${API_URL:?}"
link="$(dirname "$0")/../web/.vercel/project.json"
project="${VERCEL_PROJECT_ID:-$(jq -r .projectId "$link" 2>/dev/null || true)}"
team="${VERCEL_ORG_ID:-$(jq -r .orgId "$link" 2>/dev/null || true)}"
[ -n "$project" ] && [ -n "$team" ] || { echo "Set VERCEL_PROJECT_ID and VERCEL_ORG_ID." >&2; exit 1; }

api="https://api.vercel.com"
vercel() {
  local method="$1" path="$2"
  shift 2
  curl -fsS -X "$method" -H "Authorization: Bearer $VERCEL_TOKEN" -H "Content-Type: application/json" \
    "$api$path$([[ $path == *\?* ]] && echo '&' || echo '?')teamId=$team" "$@"
}

# Replace every existing API_URL entry with one for Production and Preview.
for id in $(vercel GET "/v9/projects/$project/env" | jq -r '.envs[] | select(.key == "API_URL") | .id'); do
  vercel DELETE "/v9/projects/$project/env/$id" >/dev/null
done
jq -n --arg v "$API_URL" '{key: "API_URL", value: $v, type: "encrypted", target: ["production", "preview"]}' \
  | vercel POST "/v10/projects/$project/env" --data @- >/dev/null
echo "Vercel API_URL=$API_URL"

name=$(vercel GET "/v9/projects/$project" | jq -r .name)
current=$(vercel GET "/v6/deployments?projectId=$project&target=production&state=READY&limit=1" \
  | jq -r '.deployments[0].uid // empty')
[ -n "$current" ] || { echo "No production deployment to redeploy; run make web-deploy once." >&2; exit 1; }

deployment=$(jq -n --arg n "$name" --arg d "$current" '{name: $n, deploymentId: $d, target: "production"}' \
  | vercel POST "/v13/deployments?forceNew=1" --data @- | jq -r .id)
echo "Redeploying $current as $deployment"

summary() {
  [ -n "${GITHUB_STEP_SUMMARY:-}" ] || return 0
  {
    echo "### Web"
    echo "- API_URL: \`$API_URL\`"
    jq -r '"- Deployment: https://\(.url)", (.alias // [] | .[] | "- Production: https://\(.)")' <<<"$1"
  } >>"$GITHUB_STEP_SUMMARY"
}

for _ in $(seq 60); do
  info=$(vercel GET "/v13/deployments/$deployment")
  state=$(jq -r .readyState <<<"$info")
  case "$state" in
    READY)
      echo "Vercel production deployment ready:"
      jq -r '"  https://\(.url)", (.alias // [] | .[] | "  https://\(.)")' <<<"$info"
      summary "$info"
      exit 0
      ;;
    ERROR | CANCELED) echo "Vercel deployment $deployment ended $state" >&2; exit 1 ;;
  esac
  sleep 5
done
echo "Vercel deployment $deployment still $state after 5 minutes" >&2
exit 1
