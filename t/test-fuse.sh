#!/usr/bin/env bash
. ./wvtest-bup.sh

set -o pipefail

if ! fusermount -V; then
    echo 'skipping FUSE tests: fusermount does not appear to work'
    exit 0
fi

if ! groups | grep -q fuse && test "$(t/root-status)" != root; then
    echo 'skipping FUSE tests: you are not root and not in the fuse group'
    exit 0
fi

top="$(WVPASS pwd)" || exit $?
tmpdir="$(WVPASS wvmktempdir)" || exit $?

export TZ=UTC
export BUP_DIR="$tmpdir/bup"
export GIT_DIR="$tmpdir/bup"

bup() { "$top/bup" "$@"; }

readonly uid=$(WVPASS id -u) || $?
readonly gid=$(WVPASS id -g) || $?
readonly user=$(WVPASS id -un) || $?
readonly group=$(WVPASS id -gn) || $?

readonly other_uinfo=$(WVPASS t/id-other-than --user 0) || exit $?
readonly other_user="${other_uinfo%%:*}"
readonly other_uid="${other_uinfo##*:}"

readonly other_ginfo=$(WVPASS t/id-other-than --group 0) || exit $?
readonly other_group="${other_ginfo%%:*}"
readonly other_gid="${other_ginfo##*:}"

readonly user_0="$(WVPASS python -c 'import pwd, os;
print pwd.getpwuid(0).pw_name')" || exit $?

readonly group_0="$(WVPASS python -c 'import grp, os;
print grp.getgrgid(0).gr_name')" || exit $?


WVPASS bup init
WVPASS cd "$tmpdir"

savestamp1=$(WVPASS python -c 'import time; print int(time.time())') || exit $?
savestamp2=$(($savestamp1 + 1))
savename1="$(printf '%(%Y-%m-%d-%H%M%S)T' "$savestamp1")" || exit $?
savename2="$(printf '%(%Y-%m-%d-%H%M%S)T' "$savestamp2")" || exit $?

WVPASS mkdir src
WVPASS date > src/foo
WVPASS chmod 644 src/foo
WVPASS touch -t 201111111111 src/foo
# FUSE, python-fuse, something, can't handle negative epoch times.
# Use pre-epoch to make sure bup properly "bottoms out" at 0 for now.
WVPASS date > src/pre-epoch
WVPASS chmod 644 src/pre-epoch
WVPASS touch -t 196907202018 src/pre-epoch
WVPASS bup index src
WVPASS bup save -n src -d "$savestamp1" --strip src

WVSTART "basics"
WVPASS mkdir mnt
WVPASS bup fuse mnt

result=$(WVPASS ls mnt) || exit $?
WVPASSEQ src "$result"

result=$(WVPASS ls mnt/src) || exit $?
WVPASSEQ "$result" "$savename1
latest"

result=$(WVPASS ls mnt/src/latest) || exit $?
WVPASSEQ "$result" "foo
pre-epoch"

# Right now we don't detect new saves.
WVPASS bup save -n src -d "$savestamp2" --strip src
result=$(WVPASS ls mnt/src) || exit $?
savename="$(WVPASS printf '%(%Y-%m-%d-%H%M%S)T' "$savestamp1")" || exit $?
WVPASSEQ "$result" "$savename1
latest"

result=$(WVPASS ls -l mnt/src/latest/ | tr -s ' ' ' ') || exit $?
WVPASSEQ "$result" "total 0
-rw-r--r-- 1 $user_0 $group_0 29 Jan 1 1970 foo
-rw-r--r-- 1 $user_0 $group_0 29 Jan 1 1970 pre-epoch"

WVSTART "--meta"
WVPASS fusermount -uz mnt
WVPASS bup fuse --meta mnt
result=$(WVPASS ls -l mnt/src/latest/ | tr -s ' ' ' ') || exit $?
WVPASSEQ "$result" "total 0
-rw-r--r-- 1 $user $group 29 Nov 11 2011 foo
-rw-r--r-- 1 $user $group 29 Jan 1 1970 pre-epoch"

WVSTART "--map-uid/--map-gid (--no-meta)"
WVPASS fusermount -uz mnt
WVPASS bup fuse --map-uid "0=$other_uid" --map-gid "0=$other_uid" mnt
result=$(WVPASS ls -l mnt/src/latest/ | tr -s ' ' ' ') || exit $?
WVPASSEQ "$result" "total 0
-rw-r--r-- 1 $other_user $other_group 29 Jan 1 1970 foo
-rw-r--r-- 1 $other_user $other_group 29 Jan 1 1970 pre-epoch"

WVSTART "--map-uid/--map-gid (--no-meta, id other than 0)"
WVPASS fusermount -uz mnt
WVPASS bup fuse --map-uid "1=$other_uid" --map-gid "1=$other_uid" mnt
result=$(WVPASS ls -l mnt/src/latest/ | tr -s ' ' ' ') || exit $?
WVPASSEQ "$result" "total 0
-rw-r--r-- 1 $user_0 $group_0 29 Jan 1 1970 foo
-rw-r--r-- 1 $user_0 $group_0 29 Jan 1 1970 pre-epoch"

WVSTART "--map-uid/--map-gid (--meta)"
WVPASS fusermount -uz mnt
WVPASS bup fuse --meta --map-uid "$uid=$other_uid" --map-gid "$gid=$other_uid" mnt
result=$(WVPASS ls -l mnt/src/latest/ | tr -s ' ' ' ') || exit $?
WVPASSEQ "$result" "total 0
-rw-r--r-- 1 $other_user $other_group 29 Nov 11 2011 foo
-rw-r--r-- 1 $other_user $other_group 29 Jan 1 1970 pre-epoch"

WVSTART "--map-uid/--map-gid (--meta, id other than uid/gid)"
WVPASS fusermount -uz mnt
WVPASS bup fuse --meta --map-uid "$other_uid=$other_uid" --map-gid "$other_gid=$other_uid" mnt
result=$(WVPASS ls -l mnt/src/latest/ | tr -s ' ' ' ') || exit $?
WVPASSEQ "$result" "total 0
-rw-r--r-- 1 $user $group 29 Nov 11 2011 foo
-rw-r--r-- 1 $user $group 29 Jan 1 1970 pre-epoch"

# FIXME: add tests
#WVSTART "--map-uid/--map-gid (--no-meta, nonexistent user/group)"
#WVSTART "--map-uid/--map-gid (--meta, nonexistent user/group)"

WVPASS fusermount -uz mnt
WVPASS rm -rf "$tmpdir"
