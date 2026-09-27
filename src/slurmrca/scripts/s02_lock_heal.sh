#!/bin/sh
# slurm-rca-bench S02 heal: stop the lock client and end its database session.
#
# Stops only the client process the inject recorded (after checking it is
# still a mysql client for this database), then kills only processlist entries
# whose statement carries the injection's marker, never this heal's own
# connection, and never anything by process name. The first heal ran
# `pkill mariadb`, which also matches `mariadbd`, PID 1 of the database
# container: the "heal" shut the accounting database down, and the service has
# no restart policy. The second found the session only by the marker on the
# SLEEP that holds the lock, so a client still waiting in LOCK TABLES, or not
# yet connected, was invisible and the heal reported "nothing to heal" while it
# ran on. Ending a connection releases its table locks (MySQL manual, LOCK
# TABLES), and a user may kill its own sessions without extra privileges
# (MariaDB KB, KILL).
#
# Any failure, including a database it cannot reach, exits non-zero: `heal
# --all` must not report S02 healed when it could not look.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
db_host="${SLURMRCA_DB_HOST:-mysql}"
db_name="${SLURMRCA_DB_NAME:-slurm_acct_db}"
marker="slurmrca_s02_lock"
pidfile="$state_dir/S02.pid"
: "${MYSQL_USER:?MYSQL_USER is not set; compose sets it on the slurmdbd service}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is not set; compose sets it on the slurmdbd service}"

sql() {
    mysql -h "$db_host" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
        --connect-timeout=10 --batch --skip-column-names "$db_name" -e "$1"
}

is_client() {
    # True while PID $1 is still a mysql client for this database. A PID
    # reused by an unrelated process is never signalled.
    args=$(ps -p "$1" -o args= 2>/dev/null || true)
    case "$args" in
        *mysql*"$db_name"*) return 0 ;;
        *) return 1 ;;
    esac
}

# The client first, so it cannot open a session after the sessions have been
# cleared. The record goes only once the process is gone.
stopped=""
if [ -e "$pidfile" ]; then
    pid=$(cat "$pidfile")
    case "$pid" in '' | *[!0-9]*) pid="" ;; esac
    if [ -n "$pid" ] && is_client "$pid"; then
        kill "$pid" 2>/dev/null || true
        tries=0
        while is_client "$pid"; do
            tries=$((tries + 1))
            if [ "$tries" -ge 10 ]; then
                echo "lock client $pid still running after SIGTERM" >&2
                exit 1
            fi
            sleep 1
        done
        stopped="$pid"
        echo "stopped lock client $pid"
    fi
    rm -f "$pidfile"
fi

# Then every session the client left behind, waiting for the lock or holding
# it. Assigned rather than tested inline, so a failed query stops the heal
# under `set -e` instead of reading as "no sessions".
ids=$(sql "SELECT id FROM information_schema.processlist WHERE info LIKE '%${marker}%' AND id <> CONNECTION_ID()")

if [ -z "$ids" ] && [ -z "$stopped" ]; then
    echo "no S02 lock session or client; nothing to heal"
fi
for id in $ids; do
    case "$id" in
        *[!0-9]*)
            echo "ignoring non-numeric session id: $id" >&2
            continue
            ;;
    esac
    if ! sql "KILL CONNECTION $id"; then
        # The session can end by itself between the query and the kill, once
        # its client has been stopped. Only a session still there is a failure.
        left=$(sql "SELECT id FROM information_schema.processlist WHERE id = $id")
        if [ -n "$left" ]; then
            echo "could not kill lock session $id" >&2
            exit 1
        fi
        echo "lock session $id had already ended"
        continue
    fi
    echo "killed lock session $id"
done

rm -f "$state_dir/S02.sql" "$state_dir/S02.log"
