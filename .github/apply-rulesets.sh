#!/usr/bin/env nix-shell
#!nix-shell -i bash -p gh jq

set -euo pipefail

REPO="${GITHUB_REPOSITORY:-adamcik/oauthclientbridge}"
ROOT_DIR="$(git rev-parse --show-toplevel)"
RULESET_DIR="${ROOT_DIR}/.github/rulesets"

upsert_ruleset() {
  local file="$1"

  local name
  name="$(jq -r '.name' "$file")"

  local target
  target="$(jq -r '.target' "$file")"

  local id
  id="$({
    gh api "repos/${REPO}/rulesets" | jq -r \
      --arg name "$name" \
      --arg target "$target" \
      'map(select(.name == $name and .target == $target))[0].id // empty'
  })"

  if [[ -n "$id" ]]; then
    gh api "repos/${REPO}/rulesets/${id}" --method PUT --input "$file" >/dev/null
    echo "Updated ruleset: ${name} (${target})"
  else
    gh api "repos/${REPO}/rulesets" --method POST --input "$file" >/dev/null
    echo "Created ruleset: ${name} (${target})"
  fi
}

upsert_ruleset "${RULESET_DIR}/branch-main.json"
upsert_ruleset "${RULESET_DIR}/tag-v.json"

echo "Done"
