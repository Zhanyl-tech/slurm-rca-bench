#!/bin/sh
# slurm-rca-bench S08 heal: remove the benchmark-owned account the inject made.
#
# Deleting is only safe because the name is checked the same way the inject
# checks it: nothing outside rca* is ever modified or deleted, whatever the
# environment says.
set -eu

acct="${SLURMRCA_S08_ACCOUNT:-rcas08}"
case "$acct" in
    rca*) ;;
    *)
        echo "refusing account '$acct': only benchmark accounts named rca* are touched" >&2
        exit 1
        ;;
esac
case "$acct" in
    *[!a-z0-9]*)
        echo "refusing account '$acct': lower-case letters and digits only" >&2
        exit 1
        ;;
esac

# Assigned before it is tested, so a failed listing stops the heal under
# `set -e`. Inside `[ -z "$(...)" ]` the test's status replaced sacctmgr's, and
# a heal that could not reach slurmdbd reported "nothing to heal" and exit 0.
accounts=$(sacctmgr -n -P list account where name="$acct" format=account)
if [ -z "$accounts" ]; then
    echo "no account $acct; nothing to heal"
    exit 0
fi

sacctmgr -i modify account where name="$acct" set GrpJobs=-1
sacctmgr -i delete user where name=root account="$acct"
sacctmgr -i delete account where name="$acct"
echo "account $acct removed"
