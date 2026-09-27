#!/bin/sh
# Build the pinned upstream Slurm image that cluster/docker-compose.yml uses.
#
# README and NOTICE said the harness clones upstream at the pinned commit;
# until 0.2.0 nothing did, and `make cluster-up` referenced an image no
# documented step built. This reads upstream.lock, fetches exactly that commit
# into vendor/ (gitignored), refuses to continue if the checked-out SHA differs
# or the working tree is not exactly that commit, and builds with the same
# build arguments upstream's compose file passes.
#
# The image is tagged slurmrca/slurm-docker-cluster:<slurm>-<short commit> and
# labelled org.slurmrca.upstream-commit=<commit>. The first version tagged it
# slurm-docker-cluster:<slurm>, which is also the name upstream's own compose
# file builds from any commit and with any local edits, and it skipped the
# fetch and the SHA check whenever that tag existed. So an image built from
# anything was used, and bumping the commit with the Slurm version unchanged
# silently kept the old image. An existing image is now reused only when its
# label names the pinned commit.
#
# Environment:
#   VENDOR_DIR  where to fetch upstream (default: <repo>/vendor/slurm-docker-cluster)
#   REBUILD=1   build even if an image built from the pinned commit exists
#   FETCH_ONLY=1  fetch and verify, but do not build (used by the unit tests)
set -eu

here=$(cd "$(dirname "$0")" && pwd)
lock="$here/upstream.lock"
vendor="${VENDOR_DIR:-$here/../vendor/slurm-docker-cluster}"
label="org.slurmrca.upstream-commit"

die() {
    echo "build-image: $*" >&2
    exit 1
}

field() {
    sed -n "s/^$1[[:space:]]*=[[:space:]]*//p" "$lock" | head -n 1
}

[ -f "$lock" ] || die "missing $lock"
repo=$(field repo)
commit=$(field commit)
slurm=$(field slurm)
[ -n "$repo" ] || die "upstream.lock has no repo"
[ -n "$slurm" ] || die "upstream.lock has no slurm version"
case "$slurm" in
    *[!0-9A-Za-z._-]*) die "slurm version '$slurm' cannot be part of an image tag" ;;
esac
case "$commit" in
    *[!0-9a-f]* | '') die "commit '$commit' is not a lower-case hex SHA" ;;
esac
[ "${#commit}" -eq 40 ] || die "commit '$commit' is not a full 40-character SHA"

# cluster/docker-compose.yml names this exact tag; tests/test_scripts.py checks
# that the two agree for the committed upstream.lock.
short=$(printf '%s' "$commit" | cut -c 1-7)
tag="slurmrca/slurm-docker-cluster:$slurm-$short"

built_from() {
    # The commit an existing image's label records, or nothing when there is
    # no such image or no such label. A missing image is an answer here, not an
    # error, hence `|| true`.
    docker image inspect --format "{{ index .Config.Labels \"$label\" }}" "$tag" 2>/dev/null || true
}

if [ "${FETCH_ONLY:-0}" != "1" ] && [ "${REBUILD:-0}" != "1" ]; then
    existing=$(built_from)
    if [ "$existing" = "$commit" ]; then
        echo "image $tag was built from $commit; REBUILD=1 to rebuild"
        docker image inspect --format '{{.Id}}' "$tag"
        exit 0
    fi
    if docker image inspect "$tag" >/dev/null 2>&1; then
        echo "image $tag exists but its $label label is '${existing:-none}', not $commit; rebuilding"
    fi
fi

if [ ! -d "$vendor/.git" ]; then
    mkdir -p "$vendor"
    git -C "$vendor" init -q
    git -C "$vendor" remote add origin "$repo"
fi
# The lock names the repository. An existing checkout fetches from it too, not
# from whatever origin it was first created with.
git -C "$vendor" remote set-url origin "$repo"
git -C "$vendor" fetch -q --depth 1 origin "$commit"
git -C "$vendor" checkout -q --detach FETCH_HEAD
head=$(git -C "$vendor" rev-parse HEAD)
[ "$head" = "$commit" ] || die "fetched $head, expected $commit"

# `docker build` sends the working tree, not the commit. Checking out the same
# commit keeps local edits and untracked files, so comparing HEAD alone called
# a modified tree verified. Ignored files are listed too, because they reach
# the build context unless upstream's .dockerignore happens to exclude them.
# Nothing is deleted: VENDOR_DIR may point at someone's own checkout, so a
# dirty tree is refused and left for whoever changed it.
dirty=$(git -C "$vendor" status --porcelain --ignored --untracked-files=all)
[ -z "$dirty" ] || die "$vendor is not a clean checkout of $commit; remove it or undo these changes, then rerun:
$dirty"
echo "upstream verified at $commit (clean working tree)"

if [ "${FETCH_ONLY:-0}" = "1" ]; then
    exit 0
fi

# The same build arguments, with the same default values, that upstream's
# docker-compose.yml passes at the pinned commit.
docker build \
    --build-arg SLURM_VERSION="$slurm" \
    --build-arg LMOD_VERSION=9.1.2 \
    --build-arg SPACK_VERSION=v1.1.1 \
    --build-arg GPU_ENABLE=false \
    --build-arg BUILDER_BASE=rockylinux/rockylinux:9 \
    --build-arg RUNTIME_BASE=rockylinux/rockylinux:9 \
    --label "$label=$commit" \
    -t "$tag" \
    "$vendor"
[ "$(built_from)" = "$commit" ] || die "built $tag, but its $label label does not read $commit"
docker image inspect --format '{{.Id}}' "$tag"
