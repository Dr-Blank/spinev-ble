#!/usr/bin/env bash
# Bump pyproject.toml version, commit, and create the matching git tag.
# Usage: release.sh <major|minor|patch>
set -euo pipefail

bump="${1:?usage: release.sh <major|minor|patch>}"

if [ -n "$(git status --porcelain)" ]; then
  echo "working tree not clean, commit or stash changes first" >&2
  exit 1
fi

uv version --bump "$bump"
version="$(uv version --short)"
tag="v${version}"

git add pyproject.toml uv.lock
git commit -m "chore: bump version to ${version}"
git tag -a "${tag}" -m "${tag}"

echo "bumped to ${version}, tagged ${tag}"
echo "run the 'Release: Push Tags to Origin' task (or 'git push --follow-tags') to publish"
