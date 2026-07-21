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

# Generate PR body via an OpenAI-compatible chat completions endpoint (default: Zhipu GLM).
# Falls back (returns non-zero) on any failure so the caller can use the deterministic generator.
#
# Config (env):
#   LLM_API_KEY   - required for the LLM path; empty → caller falls back
#   LLM_BASE_URL  - chat completions endpoint (default: Zhipu)
#   LLM_MODEL     - model id (default: glm-4.5-flash)
generate_body_llm() {
  local api_key="${LLM_API_KEY:-}"
  local base_url="${LLM_BASE_URL:-https://open.bigmodel.cn/api/paas/v4/chat/completions}"
  local model="${LLM_MODEL:-glm-4.5-flash}"

  if [[ -z "$api_key" ]]; then
    echo "no LLM_API_KEY; using deterministic fallback" >&2
    return 1
  fi
  if ! command -v jq >/dev/null 2>&1; then
    echo "jq not found; using deterministic fallback" >&2
    return 1
  fi

  local commits diffstat diff_body
  commits="$(git log --format='- %s' "${BASE_SHA}..${HEAD_SHA}" | grep -v '^- Merge' || true)"
  diffstat="$(git diff --stat "${BASE_SHA}...${HEAD_SHA}" || true)"
  diff_body="$(git diff "${BASE_SHA}...${HEAD_SHA}" || true)"
  # Cap diff size to keep the request small and within context limits.
  if [[ ${#diff_body} -gt 60000 ]]; then
    diff_body="${diff_body:0:60000}
...[diff truncated]..."
  fi

  local sys_prompt user_prompt
  sys_prompt='You write concise, factual GitHub pull request descriptions from a branch diff. Output ONLY Markdown, no preamble, no code fences. Use exactly this structure:
## Summary
- <one bullet per completed change; for each major change add a short clause on what it does and why it matters>
## Test plan
- [ ] <concrete checklist item>
Do not invent files or changes not present in the diff.'
  user_prompt="Pull request branch diff against base.

Commits in this PR:
${commits}

Diffstat:
${diffstat}

Full diff (may be truncated):
${diff_body}

Write the PR description now."

  local payload
  payload="$(jq -n \
    --arg sys "$sys_prompt" \
    --arg user "$user_prompt" \
    --arg model "$model" \
    '{model: $model, temperature: 0.3, max_tokens: 1200,
      messages: [{role: "system", content: $sys}, {role: "user", content: $user}]}')" \
    || { echo "jq build failed; using deterministic fallback" >&2; return 1; }

  local resp http_code body
  resp="$(curl -sS --max-time 60 -w $'\n%{http_code}' -X POST "$base_url" \
    -H "Authorization: Bearer $api_key" \
    -H "Content-Type: application/json" \
    -d "$payload" 2>&1)" || { echo "curl failed; using deterministic fallback" >&2; return 1; }
  http_code="$(printf '%s' "$resp" | tail -1)"
  body="$(printf '%s' "$resp" | sed '$d')"
  if [[ "$http_code" != "200" ]]; then
    echo "LLM HTTP $http_code; using deterministic fallback" >&2
    return 1
  fi

  local content
  content="$(printf '%s' "$body" | jq -r '.choices[0].message.content // empty' 2>/dev/null)" \
    || { echo "jq parse failed; using deterministic fallback" >&2; return 1; }
  if [[ -z "$content" ]]; then
    echo "empty LLM content; using deterministic fallback" >&2
    return 1
  fi

  printf '<!-- auto-pr-desc sha=%s -->\n%s\n' "$HEAD_SHA" "$content"
}

generate_body() {
  local llm
  if llm="$(generate_body_llm)" && [[ -n "$llm" ]]; then
    printf '%s' "$llm"
    return
  fi

  # Deterministic fallback: commit subjects + file-based test plan.
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
