#!/usr/bin/env bash
# Deploy one selected GitHub commit from a bundle received over SSH.

set -euo pipefail

expected_commit="${1:?Expected commit SHA is required}"
bundle_name="${2:?Bundle name is required}"
deploy_dir=/opt/tg-job-searcher-bot

[[ "$expected_commit" =~ ^[0-9a-f]{40}$ ]] || { echo "Invalid commit SHA" >&2; exit 1; }
[[ "$bundle_name" =~ ^[0-9]+-[0-9]+\.bundle$ ]] || { echo "Invalid bundle name" >&2; exit 1; }

bundle_path="$HOME/.deploy/$bundle_name"
trap 'rm -f "$bundle_path"' EXIT

cd "$deploy_dir"
test -f .env || { echo "Missing $deploy_dir/.env" >&2; exit 1; }
test -f compose.yaml || { echo "Missing $deploy_dir/compose.yaml" >&2; exit 1; }
test -f "$bundle_path" || { echo "Missing uploaded Git bundle" >&2; exit 1; }
test "$(git branch --show-current)" = main || {
  echo "The server checkout must be on main" >&2
  exit 1
}
git diff-index --quiet HEAD -- || {
  echo "The server checkout has uncommitted changes" >&2
  exit 1
}

git fetch --no-tags "$bundle_path" HEAD
test "$(git rev-parse FETCH_HEAD)" = "$expected_commit" || {
  echo "The uploaded commit does not match this workflow run" >&2
  exit 1
}
git merge-base --is-ancestor HEAD "$expected_commit" || {
  echo "The server checkout cannot fast-forward to the selected commit" >&2
  exit 1
}
git merge --ff-only "$expected_commit"

docker compose config --quiet
previous_container_id="$(docker compose ps -q bot || true)"
previous_restarts=0
if [[ -n "$previous_container_id" ]]; then
  previous_restarts="$(docker inspect --format '{{.RestartCount}}' "$previous_container_id")"
fi
if ! docker compose up -d --build bot; then
  docker compose logs --tail=100 bot || true
  echo "Docker Compose failed to deploy the bot" >&2
  exit 1
fi
sleep 10

container_id="$(docker compose ps -q bot)"
test -n "$container_id" || {
  docker compose logs --tail=100 bot
  echo "Bot container was not created" >&2
  exit 1
}
running="$(docker inspect --format '{{.State.Running}}' "$container_id")"
restarts="$(docker inspect --format '{{.RestartCount}}' "$container_id")"
docker compose ps bot
docker compose logs --tail=50 bot
if [[ "$running" != true ]] ||
   { [[ "$container_id" == "$previous_container_id" ]] && (( restarts > previous_restarts )); } ||
   { [[ "$container_id" != "$previous_container_id" ]] && (( restarts > 0 )); }; then
  echo "Bot container did not start cleanly" >&2
  exit 1
fi
echo "Deployed commit $expected_commit"
