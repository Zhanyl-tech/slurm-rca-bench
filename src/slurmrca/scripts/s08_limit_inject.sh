#!/bin/sh
# slurm-rca-bench S08 inject: starve one dedicated account with GrpJobs=0.
#
# S08 is BLOCKED (see its scenario file): the pinned cluster runs with
# AccountingStorageEnforce unset, so association limits are recorded and not
# enforced, and nothing submits jobs under this account yet.
#
# The first version set GrpJobs=0 on the `root` account, the parent of every
# account, which contradicts a ticket saying other accounts run normally. This
# version creates and limits a benchmark-owned account instead, and refuses any
# account name that does not start with `rca`, because the heal deletes it.
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

# Each listing is assigned before it is tested. Inside `[ -z "$(...)" ]` the
# test's status replaces sacctmgr's, so `set -e` never saw a failed listing
# and "cannot reach slurmdbd" read as "no such account".
accounts=$(sacctmgr -n -P list account where name="$acct" format=account)
if [ -z "$accounts" ]; then
    sacctmgr -i add account "$acct" Description="slurm-rca-bench S08" Organization=slurmrca
fi
associations=$(sacctmgr -n -P list association where user=root account="$acct" format=user)
if [ -z "$associations" ]; then
    sacctmgr -i add user root Account="$acct"
fi
sacctmgr -i modify account where name="$acct" set GrpJobs=0
echo "account $acct limited to GrpJobs=0"
