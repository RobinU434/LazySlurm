#!/usr/bin/env bash
# File every draft in this directory as a GitHub issue.
#
#   gh auth login          # once
#   .github/issue-drafts/create-issues.sh
#
# Title and labels come from the YAML front matter of each file; the rest is the body.
# Safe to re-run: drafts whose title already exists as an issue are skipped, and any
# label the repo does not have yet is created first.
set -euo pipefail

REPO="${REPO:-RobinU434/LazySlurm}"
DIR="$(cd "$(dirname "$0")" && pwd)"

existing=$(gh issue list --repo "$REPO" --state all --limit 500 --json title --jq '.[].title')

for draft in "$DIR"/[0-9][0-9]-*.md; do
    title=$(sed -n 's/^title: *"\(.*\)"$/\1/p;s/^title: *\([^"].*\)$/\1/p' "$draft" | head -1)
    label_line=$(sed -n 's/^labels: *//p' "$draft" | head -1)
    body=$(awk 'BEGIN{n=0} /^---$/{n++; next} n>=2' "$draft")

    if [ -z "$title" ]; then
        echo "skip $(basename "$draft"): no title in front matter" >&2
        continue
    fi
    if grep -Fxq "$title" <<<"$existing"; then
        echo "skip: already filed — $title"
        continue
    fi

    # "enhancement, ui" -> one --label per name, whitespace trimmed. Passing the whole
    # string would make gh look for a label literally called " ui".
    args=()
    if [ -n "$label_line" ]; then
        IFS=',' read -ra names <<<"$label_line"
        for name in "${names[@]}"; do
            name="$(printf '%s' "$name" | sed 's/^ *//;s/ *$//')"
            [ -z "$name" ] && continue
            # Create it if the repo does not have it yet; harmless if it does.
            gh label create "$name" --repo "$REPO" >/dev/null 2>&1 || true
            args+=(--label "$name")
        done
    fi

    echo "creating: $title"
    printf '%s\n' "$body" | gh issue create --repo "$REPO" \
        --title "$title" "${args[@]}" --body-file -
done
