#!/usr/bin/env bash
# The durable, private home for transcripts and full answers.
#
# Speech-to-text is the expensive step, so its output is worth keeping
# forever. The Actions cache is fast but forgets: GitHub drops an entry after
# seven days unused, and a branch cannot read main's twin. This keeps a copy
# in a private repository (TRANSCRIPTS_REPO) instead, and leaves the public
# repo with only short excerpts.
#
#   tools/transcript_store.sh pull [.cache/qa]   copy the store into the cache
#   tools/transcript_store.sh push "message" [.cache/qa]
#                                                 copy the cache into the store
#
# Needs TRANSCRIPTS_TOKEN (a token that can write TRANSCRIPTS_REPO) and
# TRANSCRIPTS_REPO (owner/name). The token is passed per command as a header
# and never written to disk: the cache folder is uploaded to the Actions
# cache, and anything in it would travel with it.
set -euo pipefail

cmd=${1:?usage: transcript_store.sh pull|push ...}
: "${TRANSCRIPTS_TOKEN:?TRANSCRIPTS_TOKEN is not set}"
: "${TRANSCRIPTS_REPO:?TRANSCRIPTS_REPO is not set}"
store="${RUNNER_TEMP:-/tmp}/transcript-store"
url="${TRANSCRIPTS_URL:-https://github.com/${TRANSCRIPTS_REPO}.git}"  # URL override for tests
header="AUTHORIZATION: basic $(printf 'x-access-token:%s' "$TRANSCRIPTS_TOKEN" | base64 | tr -d '\n')"
# Copy a tree without its .git folder (tar is on every runner; rsync is not).
copy_tree() { mkdir -p "$2"; (cd "$1" && tar --exclude=./.git -cf - .) | (cd "$2" && tar -xf -); }
git_auth() { git -c "http.https://github.com/.extraheader=${header}" "$@"; }
fresh_clone() { rm -rf "$store"; git_auth clone -q --depth 1 "$url" "$store"; }

case "$cmd" in
  pull)
    cache=${2:-.cache/qa}
    fresh_clone
    copy_tree "$store" "$cache"
    echo "store: $(find "$store" -path "$store/.git" -prune -o -type f -name '*.json' -print | wc -l) files pulled from $TRANSCRIPTS_REPO"
    ;;
  push)
    message=${2:?push needs a commit message}
    cache=${3:-.cache/qa}
    # Shards push at the same time. Each one writes different episodes, so on
    # a rejected push it starts again from a fresh clone rather than merging.
    for attempt in 1 2 3 4 5 6; do
      fresh_clone
      copy_tree "$cache" "$store"
      git -C "$store" add -A
      if git -C "$store" diff --cached --quiet; then echo "store: nothing new"; exit 0; fi
      git -C "$store" -c user.name="github-actions[bot]" \
        -c user.email="41898282+github-actions[bot]@users.noreply.github.com" commit -q -m "$message"
      if git_auth -C "$store" push -q origin HEAD; then
        echo "store: pushed to $TRANSCRIPTS_REPO"
        exit 0
      fi
      sleep $((attempt * 4))
    done
    echo "::error::could not push to $TRANSCRIPTS_REPO after 6 attempts"
    exit 1
    ;;
  *)
    echo "unknown command: $cmd" >&2
    exit 2
    ;;
esac
