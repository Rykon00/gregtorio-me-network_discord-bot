#!/usr/bin/env bash
# Runs discord_sync.py inside the GitHub workflow (.github/workflows/sync.yml).
#
# Environment:
#   MODE               plan or apply
#   CONFIG             layout file of the calling repository
#   TOOL               directory of this repository's checkout
#   DISCORD_BOT_TOKEN  may be empty on pull requests without access to secrets
#   PR_NUMBER, GH_TOKEN  optional: post the result as a pull request comment
#   COMMENT            edit (default: keep one comment up to date) or new
set -uo pipefail

case "$MODE" in
  plan|apply) ;;
  *) echo "::error::mode must be plan or apply, got '$MODE'"; exit 1 ;;
esac
if [ -z "${DISCORD_BOT_TOKEN:-}" ]; then
  if [ "$MODE" = apply ]; then
    echo "::error::The DISCORD_BOT_TOKEN secret is missing in this repository."
    exit 1
  fi
  echo "::warning::DISCORD_BOT_TOKEN is not available here (fork or Dependabot pull request?). Validating the config only."
  MODE=validate
fi

python -m pip install --quiet -r "$TOOL/requirements.txt" || exit 1

summary="$RUNNER_TEMP/discord-sync.md"
rm -f "$summary"
python "$TOOL/discord_sync.py" "$MODE" \
  --config "$CONFIG" \
  --guild-config "$TOOL/server.yml" \
  --summary-file "$summary"
status=$?

if [ -f "$summary" ]; then
  cat "$summary" >> "$GITHUB_STEP_SUMMARY"
  if [ -n "${PR_NUMBER:-}" ]; then
    flags=()
    [ "${COMMENT:-edit}" = edit ] && flags=(--edit-last --create-if-none)
    gh pr comment "$PR_NUMBER" --repo "$GITHUB_REPOSITORY" --body-file "$summary" "${flags[@]}" \
      || echo "::warning::Could not post the result as a pull request comment."
  fi
fi
exit "$status"
