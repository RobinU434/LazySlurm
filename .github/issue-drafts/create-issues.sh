#!/usr/bin/env bash
# File every draft in this directory as a GitHub issue.
#
#   gh auth login          # once
#   .github/issue-drafts/create-issues.sh
#
# Title and labels come from the YAML front matter of each file; the rest is the body.
# Already-filed drafts can simply be deleted from this directory.
set -euo pipefail

REPO="${REPO:-RobinU434/LazySlurm}"
DIR="$(cd "$(dirname "$0")" && pwd)"

for draft in "$DIR"/[0-9][0-9]-*.md; do
    title=$(sed -n 's/^title: *"\(.*\)"$/\1/p;s/^title: *\([^"].*\)$/\1/p' "$draft" | head -1)
    labels=$(sed -n 's/^labels: *//p' "$draft" | head -1)
    body=$(awk 'BEGIN{n=0} /^---$/{n++; next} n>=2' "$draft")

    if [ -z "$title" ]; then
        echo "skipping $draft: no title in front matter" >&2
        continue
    fi

    echo "creating: $title"
    if [ -n "$labels" ]; then
        printf '%s\n' "$body" | gh issue create --repo "$REPO" \
            --title "$title" --label "$labels" --body-file -
    else
        printf '%s\n' "$body" | gh issue create --repo "$REPO" \
            --title "$title" --body-file -
    fi
done
