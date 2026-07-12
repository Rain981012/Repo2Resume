#!/usr/bin/env bash
# Generate or update PR title/body from commits + diffstat.
#
# Marker formats accepted:
#   <!-- auto-pr-desc -->
#   <!-- auto-pr-desc sha=<full-or-short-sha> -->
#
# If the marker is removed, synchronize will not overwrite the body (manual ownership).
set -euo pipefail

EVENT_ACTION="${EVENT_ACTION:-}"
BASE_SHA="${BASE_SHA:-}"
HEAD_SHA="${HEAD_SHA:-}"
PR_NUMBER="${PR_NUMBER:-}"
PR_TITLE="${PR_TITLE:-}"
PR_BODY="${PR_BODY:-}"

if [[ -z "$PR_NUMBER" || -z "$BASE_SHA" || -z "$HEAD_SHA" ]]; then
  echo "Missing PR_NUMBER / BASE_SHA / HEAD_SHA" >&2
  exit 1
fi

TRIVIAL_RE='^(chore|docs|style|ci|typo|wip|merge)([(/!]|$)|^Stop tracking|^Ignore |^[Gg]itignore'

is_trivial_commit() {
  local msg="$1"
  [[ "$msg" =~ $TRIVIAL_RE ]]
}

extract_marker_sha() {
  # prints sha or empty
  printf '%s' "$PR_BODY" | grep -oE '<!-- auto-pr-desc sha=[0-9a-fA-F]+ -->' | head -1 | grep -oE '[0-9a-fA-F]{7,}' | head -1 || true
}

has_auto_marker() {
  printf '%s' "$PR_BODY" | grep -qE '<!-- auto-pr-desc( sha=[0-9a-fA-F]+)? -->'
}

should_rewrite_on_sync() {
  if ! has_auto_marker; then
    echo "skip: PR body has no auto marker (manual ownership)"
    return 1
  fi

  local prev range msgs
  prev="$(extract_marker_sha || true)"
  if [[ -z "$prev" ]]; then
    echo "rewrite: marker present but no sha yet"
    return 0
  fi

  if [[ "$HEAD_SHA" == "$prev"* ]] || [[ "$prev" == "$HEAD_SHA"* ]]; then
    echo "skip: HEAD unchanged vs marker sha"
    return 1
  fi

  # Commits introduced since last auto-generated description
  if git cat-file -e "${prev}^{commit}" 2>/dev/null; then
    range="${prev}..${HEAD_SHA}"
  else
    echo "rewrite: previous marker sha not in history (rebase/force-push?)"
    return 0
  fi

  msgs="$(git log --format='%s' "$range" 2>/dev/null || true)"
  if [[ -z "$msgs" ]]; then
    echo "skip: no commits in $range"
    return 1
  fi

  local substantive=0
  while IFS= read -r msg; do
    [[ -z "$msg" ]] && continue
    if ! is_trivial_commit "$msg"; then
      substantive=1
      echo "substantive commit: $msg"
    else
      echo "trivial commit: $msg"
    fi
  done <<< "$msgs"

  if [[ "$substantive" -eq 1 ]]; then
    echo "rewrite: found substantive commit(s) since last description"
    return 0
  fi
  echo "skip: only trivial commits since last description"
  return 1
}

build_summary_bullets() {
  git log --format='%s' "${BASE_SHA}..${HEAD_SHA}" | while IFS= read -r msg; do
    [[ -z "$msg" ]] && continue
    [[ "$msg" =~ ^Merge ]] && continue
    echo "- $msg"
  done
}

build_test_plan() {
  local files
  files="$(git diff --name-only "${BASE_SHA}...${HEAD_SHA}" || true)"
  echo "- [ ] Review diff against base and confirm intent"
  if echo "$files" | grep -qE '\.(py|ts|js|go|rs)$'; then
    echo "- [ ] Run relevant tests / smoke the changed modules"
  fi
  if echo "$files" | grep -qE '(^|/)skill/'; then
    echo "- [ ] Trigger the Cursor skill and walk the documented steps"
  fi
  if echo "$files" | grep -qE '(^|/)\.github/'; then
    echo "- [ ] Confirm GitHub Actions / workflow changes on a draft PR"
  fi
  if echo "$files" | grep -qE '(README|DESIGN|MVP_PLAN|\.md$)'; then
    echo "- [ ] Skim updated docs for accuracy"
  fi
  echo "- [ ] Confirm secrets / local-only paths remain gitignored"
}

suggest_title() {
  local first count branch
  count="$(git rev-list --count "${BASE_SHA}..${HEAD_SHA}" 2>/dev/null || echo 0)"
  first="$(git log --format='%s' -1 "${BASE_SHA}..${HEAD_SHA}" 2>/dev/null || true)"
  if [[ -z "$first" ]]; then
    echo "Updates"
    return
  fi
  if [[ "$count" -gt 1 ]]; then
    branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")"
    branch="${branch##*/}"
    branch="$(echo "$branch" | tr '-' ' ' | sed 's/  */ /g' | sed 's/^ *//;s/ *$//')"
    if [[ -n "$branch" && "$branch" != "HEAD" ]]; then
      # Title-case-ish: keep as readable branch phrase
      echo "$branch"
      return
    fi
  fi
  echo "$first"
}

generate_body() {
  local summary testplan
  summary="$(build_summary_bullets)"
  if [[ -z "$summary" ]]; then
    summary="- (no commits found in range)"
  fi
  testplan="$(build_test_plan)"
  cat <<EOF
<!-- auto-pr-desc sha=${HEAD_SHA} -->
## Summary
$summary

## Test plan
$testplan
EOF
}

DO_UPDATE=0
DO_TITLE=0

case "$EVENT_ACTION" in
  opened|reopened|ready_for_review)
    DO_UPDATE=1
    # Set title when empty, GitHub default "X into Y", or looks like a raw branch name
    if [[ -z "$PR_TITLE" || "$PR_TITLE" =~ [Ii]nto || ( "$PR_TITLE" == *"-"* && "$PR_TITLE" =~ ^[A-Za-z0-9._/-]+$ ) ]]; then
      DO_TITLE=1
    fi
    ;;
  synchronize)
    if should_rewrite_on_sync; then
      DO_UPDATE=1
    fi
    ;;
  *)
    echo "No-op for action=$EVENT_ACTION"
    exit 0
    ;;
esac

NEW_BODY=""
NEW_TITLE=""
if [[ "$DO_UPDATE" -eq 1 ]]; then
  NEW_BODY="$(generate_body)"
fi
if [[ "$DO_TITLE" -eq 1 ]]; then
  NEW_TITLE="$(suggest_title)"
fi

if [[ "$DO_UPDATE" -eq 0 && "$DO_TITLE" -eq 0 ]]; then
  echo "Nothing to update."
  exit 0
fi

ARGS=(pr edit "$PR_NUMBER")
if [[ -n "$NEW_TITLE" ]]; then
  ARGS+=(--title "$NEW_TITLE")
  echo "New title: $NEW_TITLE"
fi
if [[ -n "$NEW_BODY" ]]; then
  ARGS+=(--body "$NEW_BODY")
  echo "Updating body (auto-generated)."
fi

gh "${ARGS[@]}"
echo "Done."
