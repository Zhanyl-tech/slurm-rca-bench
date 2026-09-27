#!/bin/sh
# slurm-rca-bench S02 inject: hold a WRITE lock on slurmdbd's job table.
#
# Runs inside the slurmdbd container and connects the way the upstream
# entrypoint does (mysql -h mysql -u$MYSQL_USER -p$MYSQL_PASSWORD), because that
# path is known to work in this image. The first version ran `mariadb -uroot`
# with no password inside the database container; the image gives root a random
# password, so the login failed, the failure went to a log nobody read, and the
# injection reported success while doing nothing.
#
# Why LOCK TABLES rather than SELECT ... FOR UPDATE: InnoDB consistent reads
# ignore row locks (MySQL 8.4 manual, "Locking Reads"), so FOR UPDATE would not
# make sacct wait. While a WRITE table lock is held, no other session can read
# or write that table (MariaDB and MySQL manuals, LOCK TABLES), and the lock is
# released when the holding session ends (MySQL manual).
#
# Both statements carry the marker, so the heal can find the session whether it
# is still waiting for the lock or already holding it: the table alias in LOCK
# TABLES, and the column alias of the SLEEP that holds the lock (only that one
# ends in _held). The first version marked only the SLEEP, so a session still
# waiting in LOCK TABLES was invisible to the heal. `WAIT n` (MariaDB's
# per-statement lock wait timeout, https://mariadb.com/kb/en/wait-and-nowait/)
# bounds that wait, so a lock that cannot be taken ends the session by itself.
#
# The client's PID is recorded, and the heal stops it too. The first version
# did not record it, so after a failed inject the client kept running, the
# heal reported "nothing to heal", and the lock could be taken after a heal
# that had reported success.
#
# Fails loudly: if the lock is not held within SLURMRCA_S02_WAIT_S seconds, the
# client's output is printed, the client and its session are stopped, and the
# script exits non-zero.
set -eu

state_dir="${SLURMRCA_STATE_DIR:-/tmp/slurmrca}"
db_host="${SLURMRCA_DB_HOST:-mysql}"
db_name="${SLURMRCA_DB_NAME:-slurm_acct_db}"
hold_s="${SLURMRCA_S02_HOLD_S:-600}"
wait_s="${SLURMRCA_S02_WAIT_S:-10}"
marker="slurmrca_s02_lock"
pidfile="$state_dir/S02.pid"
: "${MYSQL_USER:?MYSQL_USER is not set; compose sets it on the slurmdbd service}"
: "${MYSQL_PASSWORD:?MYSQL_PASSWORD is not set; compose sets it on the slurmdbd service}"

for value in "$hold_s" "$wait_s"; do
    case "$value" in
        '' | *[!0-9]*)
            echo "SLURMRCA_S02_HOLD_S and SLURMRCA_S02_WAIT_S must be whole numbers of seconds" >&2
            exit 1
            ;;
    esac
done
if [ "$wait_s" -lt 1 ]; then
    echo "SLURMRCA_S02_WAIT_S must be at least 1" >&2
    exit 1
fi

sql() {
    mysql -h "$db_host" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" \
        --connect-timeout=10 --batch --skip-column-names "$db_name" -e "$1"
}

sessions() {
    # Sessions whose statement contains $1. `id <> CONNECTION_ID()` excludes
    # this query, whose own text contains the marker. The first heal matched
    # its own shell with `pkill -f`; this is the same trap in SQL. Without the
    # PROCESS privilege the slurm user sees only its own sessions, which is
    # where the lock session lives.
    sql "SELECT id FROM information_schema.processlist WHERE info LIKE '%$1%' AND id <> CONNECTION_ID()"
}

is_client() {
    # True while PID $1 is still a mysql client for this database. A PID
    # reused by an unrelated process is never signalled. -ww because the
    # database name comes last on a long command line and ps(1) leaves the
    # width of piped output undefined (procps cuts it at $COLUMNS). The name
    # must end the line: at full width this shell's own command line, which
    # is the whole script, contains "mysql" and the name as well.
    case "$1" in
        *[1-9]*) ;;
        *) return 1 ;; # PID 0 is no process, and `kill -0 0` tests this shell's group
    esac
    args=$(ps -ww -p "$1" -o args= 2>/dev/null || true)
    if [ -z "$args" ] && kill -0 "$1" 2>/dev/null; then
        # ps prints a line for every process that exists, zombies included, so
        # nothing for one that does means ps itself failed (missing, or it
        # rejected the options). Reading that as "gone" would drop the record
        # of a client still running and start another.
        echo "ps could not read PID $1, which exists; not treating it as gone" >&2
        exit 1
    fi
    case "$args" in
        *mysql*" $db_name") return 0 ;;
        *) return 1 ;;
    esac
}

stop_client() {
    # The process first, so it cannot open a session after the sessions have
    # been cleared; then any session it left behind, which the server keeps
    # until it notices the client is gone.
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
                    return 1
                fi
                sleep 1
            done
        fi
        rm -f "$pidfile"
    fi
    ids=$(sessions "$marker")
    for id in $ids; do
        case "$id" in
            *[!0-9]*)
                echo "ignoring non-numeric session id: $id" >&2
                continue
                ;;
        esac
        sql "KILL CONNECTION $id"
        echo "killed lock session $id" >&2
    done
}

held=$(sessions "${marker}_held")
if [ -n "$held" ]; then
    echo "S02 lock already held (session $held); nothing to do"
    exit 0
fi
if [ -e "$pidfile" ]; then
    old=$(cat "$pidfile")
    case "$old" in '' | *[!0-9]*) old="" ;; esac
    if [ -n "$old" ] && is_client "$old"; then
        echo "an earlier S02 lock client (PID $old) is still running without the lock; run the S02 heal first" >&2
        exit 1
    fi
    rm -f "$pidfile"
fi

# slurmdbd names the job table <ClusterName>_job_table (linux_job_table with
# the upstream slurm.conf). Discovered rather than hard-coded, and exactly one
# match is required so a surprise schema fails here instead of locking the
# wrong table.
tables=$(sql "SELECT table_name FROM information_schema.tables WHERE table_schema = DATABASE() AND table_name LIKE '%\\_job\\_table'")
count=$(printf '%s\n' "$tables" | grep -c . || true)
if [ "$count" -ne 1 ]; then
    echo "expected exactly one *_job_table in $db_name, found $count: $tables" >&2
    exit 1
fi
table=$tables
case "$table" in
    *[!A-Za-z0-9_]*)
        echo "refusing to lock unexpected table name: $table" >&2
        exit 1
        ;;
esac

mkdir -p "$state_dir"
# The SQL goes to the client on stdin from a file. The first version passed it
# through Python's repr(), which is not shell quoting, and the client received
# literal backslash-n sequences. A table locked under an alias is still that
# table to every other session; the alias only changes how this session refers
# to it, and the SLEEP refers to no table.
# shellcheck disable=SC2016  # the backticks are SQL identifier quotes, not a command substitution
printf 'LOCK TABLES `%s` AS %s WRITE WAIT %s;\nSELECT SLEEP(%s) AS %s_held;\n' \
    "$table" "$marker" "$wait_s" "$hold_s" "$marker" >"$state_dir/S02.sql"

nohup mysql -h "$db_host" -u"$MYSQL_USER" -p"$MYSQL_PASSWORD" "$db_name" \
    <"$state_dir/S02.sql" >"$state_dir/S02.log" 2>&1 &
echo "$!" >"$pidfile"

# Two seconds longer than the server's own WAIT, so a lock granted at the last
# moment is still seen.
tries=0
while [ "$tries" -lt $((wait_s + 2)) ]; do
    if [ -n "$(sessions "${marker}_held")" ]; then
        echo "holding a WRITE lock on $db_name.$table for up to ${hold_s}s (client PID $(cat "$pidfile"))"
        exit 0
    fi
    tries=$((tries + 1))
    sleep 1
done

echo "the lock was not held within ${wait_s}s; client output:" >&2
cat "$state_dir/S02.log" >&2 || true
stop_client || true
exit 1
