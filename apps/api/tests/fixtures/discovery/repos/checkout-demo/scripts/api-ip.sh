#!/usr/bin/env bash
# Print the public IP of the api service's newest running task. Used by deploy.yml and
# scenario.sh to point Vercel at the api after a deploy.
#
# Env: CLUSTER, SERVICE, REGION, optional PROFILE
set -euo pipefail

: "${CLUSTER:?}" "${SERVICE:?}" "${REGION:?}"

aws_() {
  if [ -n "${PROFILE:-}" ]; then
    aws --profile "$PROFILE" --region "$REGION" "$@"
  else
    aws --region "$REGION" "$@"
  fi
}

tasks=$(aws_ ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" \
  --desired-status RUNNING --query 'taskArns' --output text)
[ -n "$tasks" ] && [ "$tasks" != None ] || { echo "No running task in $SERVICE" >&2; exit 1; }

# shellcheck disable=SC2086
eni=$(aws_ ecs describe-tasks --cluster "$CLUSTER" --tasks $tasks --output json | jq -r '
  [.tasks[] | select(.lastStatus == "RUNNING")] | sort_by(.startedAt) | last
  | .attachments[0].details[] | select(.name == "networkInterfaceId") | .value')
[ -n "$eni" ] || { echo "No network interface on the running $SERVICE task" >&2; exit 1; }

ip=$(aws_ ec2 describe-network-interfaces --network-interface-ids "$eni" \
  --query 'NetworkInterfaces[0].Association.PublicIp' --output text)
[ -n "$ip" ] && [ "$ip" != None ] || { echo "Task interface $eni has no public IP" >&2; exit 1; }
echo "$ip"
