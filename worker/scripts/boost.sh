#!/bin/sh
# boost -- turn assignments into achievable daily tasks, from the terminal.
#
# Installed by:  curl -fsSL https://kojesko.dev/boost | sh
#
# Planning happens on the Worker, which holds the Anthropic key, so this script
# never needs a credential of its own. Everything else is local.
#
# Dependencies are curl, awk, sed and sort -- nothing outside a POSIX base
# system. In particular there is no jq: the API is asked for
# `text/tab-separated-values`, so responses are read with awk rather than a
# hand-rolled shell JSON parser.
#
# Environment:
#   BOOST_API   base URL of the deployment (default https://kojesko.dev/boost)
#   NO_COLOR    set to disable ANSI styling
#   EDITOR      used by `boost add --editor`

set -eu

BOOST_VERSION="0.1.0"
BOOST_API="${BOOST_API:-https://kojesko.dev/boost}"

DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/boostability"
STORE="$DATA_DIR/store.tsv"

TAB=$(printf '\t')

# ---------------------------------------------------------------------------
# styling
# ---------------------------------------------------------------------------

if [ -t 1 ] && [ -z "${NO_COLOR:-}" ] && [ "${TERM:-dumb}" != "dumb" ]; then
    C_RESET=$(printf '\033[0m')
    C_BOLD=$(printf '\033[1m')
    C_DIM=$(printf '\033[2m')
    C_ACCENT=$(printf '\033[38;5;33m')
    C_GREEN=$(printf '\033[32m')
    C_YELLOW=$(printf '\033[33m')
    C_RED=$(printf '\033[31m')
else
    C_RESET='' C_BOLD='' C_DIM='' C_ACCENT='' C_GREEN='' C_YELLOW='' C_RED=''
fi

die()  { printf '%sboost: %s%s\n' "$C_RED" "$*" "$C_RESET" >&2; exit 1; }
note() { printf '%s%s%s\n' "$C_DIM" "$*" "$C_RESET"; }

# ---------------------------------------------------------------------------
# dates
#
# `date -d` (GNU) and `date -v` (BSD) are incompatible, so offsets are resolved
# once here and all per-row arithmetic happens in awk via AWK_DATE below.
# ---------------------------------------------------------------------------

today() { date +%Y-%m-%d; }

date_offset() {
    if date -d '@0' >/dev/null 2>&1; then
        date -d "+$1 days" +%Y-%m-%d
    else
        date -v"+$1d" +%Y-%m-%d
    fi
}

# Accepts ISO, M/D/YYYY, today, tomorrow, or +N. Echoes ISO, or fails.
parse_date() {
    _raw=$(printf '%s' "${1:-}" | tr 'A-Z' 'a-z' | tr -d ' ')
    case "$_raw" in
        today|t)      today ;;
        tomorrow|tmr) date_offset 1 ;;
        +[0-9]*)      date_offset "${_raw#+}" ;;
        [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]) printf '%s' "$_raw" ;;
        [0-9]*/[0-9]*/[0-9]*)
            printf '%s' "$_raw" | awk -F/ '{
                y = $3 + 0; if (y < 100) y += 2000
                printf "%04d-%02d-%02d", y, $1, $2
            }' ;;
        *) return 1 ;;
    esac
}

# Julian Day Number and its inverse, so awk can do calendar maths on ISO dates
# without shelling out once per row.
AWK_DATE='
function jdn(d,   y, m, dd, a, yy, mm) {
    if (d !~ /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]$/) return 0
    y = substr(d,1,4)+0; m = substr(d,6,2)+0; dd = substr(d,9,2)+0
    a = int((14-m)/12); yy = y+4800-a; mm = m+12*a-3
    return dd + int((153*mm+2)/5) + 365*yy + int(yy/4) - int(yy/100) + int(yy/400) - 32045
}
function civil(j,   a, b, c, d, e, m, dd, mo, yr) {
    a = j + 32044; b = int((4*a+3)/146097); c = a - int(146097*b/4)
    d = int((4*c+3)/1461); e = c - int(1461*d/4); m = int((5*e+2)/153)
    dd = e - int((153*m+2)/5) + 1
    mo = m + 3 - 12*int(m/10)
    yr = 100*b + d - 4800 + int(m/10)
    return sprintf("%04d-%02d-%02d", yr, mo, dd)
}
function ord(n) {
    if (n % 100 >= 11 && n % 100 <= 13) return n "th"
    if (n % 10 == 1) return n "st"
    if (n % 10 == 2) return n "nd"
    if (n % 10 == 3) return n "rd"
    return n "th"
}
function longday(d,   mn, wd, j) {
    j = jdn(d); if (j == 0) return d
    split("January February March April May June July August September October November December", mn, " ")
    split("Monday Tuesday Wednesday Thursday Friday Saturday Sunday", wd, " ")
    return wd[(j % 7) + 1] ", " mn[substr(d,6,2)+0] " " ord(substr(d,9,2)+0)
}
function shortday(d,   mn) {
    if (jdn(d) == 0) return d
    split("Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec", mn, " ")
    return mn[substr(d,6,2)+0] " " ord(substr(d,9,2)+0)
}
function ring(done, total) {
    if (total == 0) return "o"
    if (done >= total) return "*"
    if (done * 4 >= total * 3) return "@"
    if (done * 2 >= total) return "0"
    if (done > 0) return "c"
    return "o"
}
'

# ---------------------------------------------------------------------------
# store
#
# Line-oriented TSV, tag in column 1. Every read is one awk pass; every write
# rewrites the whole stream to a temp file and renames, so an interrupted write
# can never leave a half-serialised store behind.
#
#   A <id> <name> <course> <start> <due> <summary>
#   D <id> <assignment> <availability> <focus> <other>
#   T <id> <done 0|1> <date> <start> <end> <title> <detail>
#
# A task's 1-based position among its activity's T lines is its index -- that is
# what the `3.2` in `boost done 3.2` refers to.
# ---------------------------------------------------------------------------

ensure_store() {
    mkdir -p "$DATA_DIR" || die "could not create $DATA_DIR"
    [ -f "$STORE" ] || : > "$STORE"
}

store_write() { cat > "$STORE.tmp" && mv "$STORE.tmp" "$STORE"; }

next_id() { awk -F"$TAB" '$1=="A" && $2+0>m { m=$2+0 } END { print m+1 }' "$STORE"; }

require_activity() {
    if ! awk -F"$TAB" -v a="$1" '$1=="A" && $2==a { f=1 } END { exit !f }' "$STORE"; then
        die "no activity with id $1. Run \`boost list\` to see them."
    fi
}

task_count() { awk -F"$TAB" -v a="$1" '$1=="T" && $2==a { n++ } END { print n+0 }' "$STORE"; }

# Strip field/record separators so user text can never break the store layout.
clean() {
    printf '%s' "${1:-}" | tr -d '\000-\010\013\014\016-\037\177' | tr '\011\012\015' '   '
}

# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

# stdin -> JSON string body (without the surrounding quotes).
json_str() {
    LC_ALL=C tr -d '\000-\010\013\014\016-\037\177' \
        | tr '\011' ' ' \
        | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' \
        | awk 'BEGIN { ORS="" } NR>1 { printf "%s", "\\n" } { printf "%s", $0 }'
}

field_json() { printf '%s' "${1:-}" | json_str; }

build_body() {
    printf '{'
    printf '"name":"%s",'       "$(field_json "$A_NAME")"
    printf '"course":"%s",'     "$(field_json "$A_COURSE")"
    printf '"start_date":"%s",' "$A_START"
    printf '"due_date":"%s",'   "$A_DUE"
    printf '"today":"%s",'      "$(today)"
    printf '"think":%s,'        "$A_THINK"
    printf '"details":{'
    printf '"assignment":"%s",'   "$(field_json "$D_ASSIGNMENT")"
    printf '"availability":"%s",' "$(field_json "$D_AVAILABILITY")"
    printf '"focus":"%s",'        "$(field_json "$D_FOCUS")"
    printf '"other":"%s"'         "$(field_json "$D_OTHER")"
    printf '}'
    if [ -n "$A_PDF" ]; then
        printf ',"pdf":{"name":"%s","data":"%s"}' \
            "$(field_json "$(basename "$A_PDF")")" \
            "$(base64 < "$A_PDF" | tr -d '\n\r')"
    fi
    printf '}'
}

# POST the plan request; echo TSV on stdout, or die with the server's message.
call_plan() {
    _body=$(mktemp "${TMPDIR:-/tmp}/boost-req.XXXXXX")
    _resp=$(mktemp "${TMPDIR:-/tmp}/boost-res.XXXXXX")

    build_body > "$_body"

    # No -f: a 4xx body carries the reason, and -f would throw it away.
    if ! _code=$(curl -sS -X POST "$BOOST_API/api/plan" \
            -H 'content-type: application/json' \
            -H 'accept: text/tab-separated-values' \
            --data-binary @"$_body" \
            -w '%{http_code}' -o "$_resp" 2>/dev/null); then
        rm -f "$_body" "$_resp"
        die "could not reach $BOOST_API. Check your connection."
    fi

    if [ "$_code" != "200" ]; then
        _msg=$(awk -F"$TAB" '$1=="E" { print $2; if ($3 != "") printf "\n  %s", $3; exit }' "$_resp")
        [ -n "$_msg" ] || _msg="server returned HTTP $_code"
        rm -f "$_body" "$_resp"
        die "$_msg"
    fi

    cat "$_resp"
    rm -f "$_body" "$_resp"
}

# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------

render_table() {
    awk -F"$TAB" "$AWK_DATE"'
    BEGIN { today = ENVIRON["BOOST_TODAY"] }
    $1=="A" { id=$2; name[id]=$3; course[id]=$4; due[id]=$6; ids[++n]=id }
    $1=="T" { total[$2]++; if ($3=="1") done[$2]++ }
    END {
        if (n == 0) {
            printf "\n  %sNothing tracked yet. Add your first assignment with `boost add`.%s\n\n", D, R
            exit
        }
        printf "\n  %s%-3s %-2s %-32s %-9s %8s   %s%s\n", B, "#", "", "Activity", "Class", "Progress", "Due", R
        for (i = 1; i <= n; i++) {
            id = ids[i]; t = total[id]+0; d = done[id]+0
            left = jdn(due[id]) - jdn(today)
            if (t > 0 && d == t) { col = G;  when = "done" }
            else if (left < 0)   { col = RD; when = sprintf("%s  (%dd over)", shortday(due[id]), -left) }
            else if (left == 0)  { col = RD; when = sprintf("%s  (today)", shortday(due[id])) }
            else if (left <= 2)  { col = Y;  when = sprintf("%s  (%dd)", shortday(due[id]), left) }
            else                 { col = A;  when = sprintf("%s  (%dd)", shortday(due[id]), left) }
            printf "  %s%-3s%s %s%-2s%s %-32.32s %s%-9.9s%s %8s   %s%s%s\n", \
                D, id, R, col, ring(d, t), R, name[id], D, course[id], R, \
                sprintf("%d/%d", d, t), col, when, R
        }
        printf "\n"
    }' A="$C_ACCENT" D="$C_DIM" B="$C_BOLD" R="$C_RESET" G="$C_GREEN" Y="$C_YELLOW" RD="$C_RED" "$STORE"
}

render_activity() {
    awk -F"$TAB" "$AWK_DATE"'
    BEGIN { want = ENVIRON["BOOST_WANT"]; today = ENVIRON["BOOST_TODAY"]; detail = ENVIRON["BOOST_DETAIL"] }
    $1=="A" && $2==want { name=$3; course=$4; start=$5; due=$6; summary=$7 }
    $1=="T" && $2==want {
        n++; tdone[n]=$3; tdate[n]=$4; tstart[n]=$5; tend[n]=$6; ttitle[n]=$7; tdetail[n]=$8
        if ($3=="1") ndone++
    }
    END {
        left = jdn(due) - jdn(today)
        if (n > 0 && ndone == n) { col = G;  badge = " Complete " }
        else if (left < 0)       { col = RD; badge = sprintf(" %dd overdue ", -left) }
        else                     { col = A;  badge = sprintf(" Due %s ", longday(due)) }

        printf "\n  %s%s%s", B, name, R
        if (course != "") printf "   %s%s%s", D, course, R
        printf "   %s%s%s\n", col, badge, R
        printf "  %s%s %d/%d%s   %s%s -> %s%s\n", col, ring(ndone+0, n), ndone+0, n, R, D, shortday(start), shortday(due), R
        if (summary != "") printf "\n  %s%s%s\n", D, summary, R

        for (i = 1; i <= n; i++) {
            if (tdate[i] != day) {
                day = tdate[i]
                printf "\n  %s%s%s", B, longday(day), R
                if (day == today) printf "   %stoday%s", A, R
                printf "\n"
            }
            mark = (tdone[i]=="1") ? "[x]" : "[ ]"
            mcol = (tdone[i]=="1") ? G : D
            slot = (tstart[i] != "" && tend[i] != "") ? sprintf("%s - %s", tstart[i], tend[i]) : ""
            printf "  %s%s%s %s%s.%d%s  %-42.42s %s%s%s\n", \
                mcol, mark, R, D, want, i, R, ttitle[i], D, slot, R
            if (detail == "1" && tdetail[i] != "") printf "        %s%s%s\n", D, tdetail[i], R
        }
        if (n == 0) printf "\n  %sNo tasks yet. Run `boost replan %s` to build a plan.%s\n", D, want, R
        printf "\n"
    }' A="$C_ACCENT" D="$C_DIM" B="$C_BOLD" R="$C_RESET" G="$C_GREEN" RD="$C_RED" "$STORE"
}

render_agenda() {
    awk -F"$TAB" "$AWK_DATE"'
    BEGIN { from = ENVIRON["BOOST_FROM"]; days = ENVIRON["BOOST_DAYS"] + 0; today = ENVIRON["BOOST_TODAY"] }
    $1=="A" { aname[$2]=$3 }
    $1=="T" { idx[$2]++
              rec[$4] = rec[$4] $2 SUBSEP idx[$2] SUBSEP $3 SUBSEP $7 SUBSEP $5 SUBSEP $6 "\n" }
    END {
        base = jdn(from)
        for (off = 0; off < days; off++) {
            day = civil(base + off)
            printf "\n  %s%s%s", B, longday(day), R
            if (day == today) printf "   %stoday%s", A, R
            printf "\n"
            if (!(day in rec)) { printf "\n  %sNothing scheduled. Enjoy it.%s\n", D, R; continue }
            printf "\n"
            n = split(rec[day], lines, "\n")
            for (i = 1; i <= n; i++) {
                if (lines[i] == "") continue
                split(lines[i], f, SUBSEP)
                mark = (f[3]=="1") ? "[x]" : "[ ]"
                mcol = (f[3]=="1") ? G : D
                slot = (f[5] != "" && f[6] != "") ? sprintf("%s - %s", f[5], f[6]) : ""
                printf "  %s%s%s %s%s.%s%s  %-38.38s %s%-14s%s %s%s%s\n", \
                    mcol, mark, R, D, f[1], f[2], R, f[4], D, slot, R, A, aname[f[1]], R
            }
        }
        printf "\n"
    }' A="$C_ACCENT" D="$C_DIM" B="$C_BOLD" R="$C_RESET" G="$C_GREEN" "$STORE"
}

# ---------------------------------------------------------------------------
# add / replan
# ---------------------------------------------------------------------------

A_NAME='' A_COURSE='' A_START='' A_DUE='' A_PDF='' A_THINK='false'
D_ASSIGNMENT='' D_AVAILABILITY='' D_FOCUS='' D_OTHER=''

DETAILS_TEMPLATE='My assignment (what I need to do, in details):


When I can study (days/times):


How long I can focus at a time:


Other details that might help:
'

# Split an editor buffer back into the four fields. Text before the first
# recognised heading counts as the assignment, so deleting the scaffolding and
# just writing prose still produces something usable.
load_details_file() {
    _p=$(awk '
    BEGIN { key = "assignment" }
    /^My assignment/    { key = "assignment";   next }
    /^When I can study/ { key = "availability"; next }
    /^How long I can/   { key = "focus";        next }
    /^Other details/    { key = "other";        next }
    { buf[key] = buf[key] $0 "\n" }
    END { for (k in buf) { gsub(/\n+$/, "", buf[k]); gsub(/\n/, "\\n", buf[k]); print k "\t" buf[k] } }
    ' "$1")
    D_ASSIGNMENT=$(printf '%s\n' "$_p" | awk -F"$TAB" '$1=="assignment"   { print $2 }')
    D_AVAILABILITY=$(printf '%s\n' "$_p" | awk -F"$TAB" '$1=="availability" { print $2 }')
    D_FOCUS=$(printf '%s\n' "$_p" | awk -F"$TAB" '$1=="focus"        { print $2 }')
    D_OTHER=$(printf '%s\n' "$_p" | awk -F"$TAB" '$1=="other"        { print $2 }')
}

open_editor() {
    _ed="${EDITOR:-${VISUAL:-nano}}"
    _tmp="${TMPDIR:-/tmp}/boost-details.$$.md"
    printf '%s' "$DETAILS_TEMPLATE" > "$_tmp"
    if ! $_ed "$_tmp"; then rm -f "$_tmp"; die "could not run editor '$_ed'"; fi
    load_details_file "$_tmp"
    rm -f "$_tmp"
}

# ask <varname> <label> <hint> <required>
ask() {
    _label=$2; _hint=$3; _req=$4
    printf '\n  %s%s%s' "$C_BOLD" "$_label" "$C_RESET"
    if [ "$_req" = "1" ]; then printf ' %s*required*%s' "$C_RED" "$C_RESET"; fi
    printf '\n'
    if [ -n "$_hint" ]; then printf '  %s%s%s\n' "$C_DIM" "$_hint" "$C_RESET"; fi
    while :; do
        printf '  > '
        IFS= read -r _val || _val=''
        if [ -n "$_val" ] || [ "$_req" != "1" ]; then
            eval "$1=\$_val"
            return 0
        fi
        printf '  %sThis one is required.%s\n' "$C_RED" "$C_RESET"
    done
}

collect_activity() {
    if [ ! -t 0 ]; then
        die "no terminal for input. Pass --name/--class/--due/--details, or run interactively."
    fi

    printf '\n  %s%sAdd Your Assignment%s\n' "$C_BOLD" "$C_ACCENT" "$C_RESET"
    note "  Boostability will turn it into achievable daily tasks."

    if [ -z "$A_NAME" ];   then ask A_NAME "Activity name" "e.g. Chapter 3 Lab, Midterm Exam, Weekly Quiz" 1; fi
    if [ -z "$A_COURSE" ]; then ask A_COURSE "Class" "e.g. BIO101, CHM202, ENG110" 1; fi

    if [ -z "$A_START" ]; then
        ask _s "Activity start date" "ISO, M/D/YYYY, today, tomorrow or +N. Blank = today." 0
        A_START=$(parse_date "${_s:-today}") || die "could not read that start date."
    fi
    if [ -z "$A_DUE" ]; then
        while :; do
            ask _d "Activity due date" "Blank = one week out." 0
            if ! A_DUE=$(parse_date "${_d:-+7}"); then
                note "  Not a date I understand."
                continue
            fi
            case "$A_DUE" in
                *) if [ "$A_DUE" \< "$A_START" ]; then
                       note "  The due date has to be on or after the start date."
                   else
                       break
                   fi ;;
            esac
        done
    fi

    if [ -z "$D_ASSIGNMENT" ]; then
        printf '\n  %sActivity details%s %s*required*%s\n' "$C_BOLD" "$C_RESET" "$C_RED" "$C_RESET"
        note "  More detail = better tasks. Include when you can study and how long you can focus."
        printf '  Open %s to write these? [y/N] ' "${EDITOR:-${VISUAL:-nano}}"
        IFS= read -r _use_ed || _use_ed=n
        case "$_use_ed" in
            [yY]*) open_editor ;;
            *)
                ask D_ASSIGNMENT "What you need to do" "" 1
                ask D_AVAILABILITY "When you can study (days/times)" "" 0
                ask D_FOCUS "How long you can focus at a time" "" 0
                ask D_OTHER "Anything else that might help" "" 0
                ;;
        esac
    fi

    [ -n "$D_ASSIGNMENT" ] || die "an assignment description is required."

    if [ -z "$A_PDF" ]; then
        ask _p "Activity attachments" "Path to a PDF brief, or blank to skip." 0
        if [ -n "${_p:-}" ]; then
            _p=$(printf '%s' "$_p" | sed "s|^~|$HOME|")
            [ -f "$_p" ] || die "attachment not found: $_p"
            A_PDF=$_p
        fi
    fi
}

# Replace an activity's tasks with a freshly generated plan.
# $1 = activity id, stdin = plan TSV.
apply_plan() {
    _id=$1
    _plan="${TMPDIR:-/tmp}/boost-plan.$$"
    cat > "$_plan"

    _summary=$(awk -F"$TAB" '$1=="S" { print $2; exit }' "$_plan")

    {
        # Every other activity passes through untouched.
        awk -F"$TAB" -v OFS="$TAB" -v a="$_id" -v s="$_summary" '
            $1=="A" && $2==a { $7 = s; print; next }
            $1=="T" && $2==a { next }
            { print }
        ' "$STORE"
        awk -F"$TAB" -v OFS="$TAB" -v a="$_id" '
            $1=="T" { print "T", a, "0", $2, $3, $4, $5, $6 }
        ' "$_plan"
    } | store_write

    awk -F"$TAB" -v Y="$C_YELLOW" -v R="$C_RESET" '
        $1=="W" {
            if (!h++) printf "\n  %sCheck these before you commit to the plan:%s\n", Y, R
            printf "    %s- %s%s\n", Y, $2, R
        }' "$_plan"

    rm -f "$_plan"
}

save_details() {
    printf 'D\t%s\t%s\t%s\t%s\t%s\n' "$1" \
        "$(clean "$D_ASSIGNMENT")" "$(clean "$D_AVAILABILITY")" \
        "$(clean "$D_FOCUS")" "$(clean "$D_OTHER")" >> "$STORE"
}

cmd_add() {
    ensure_store
    while [ $# -gt 0 ]; do
        case "$1" in
            --name)         A_NAME=${2:-}; shift 2 ;;
            --class)        A_COURSE=${2:-}; shift 2 ;;
            --start)        A_START=$(parse_date "${2:-}") || die "bad --start date"; shift 2 ;;
            --due)          A_DUE=$(parse_date "${2:-}") || die "bad --due date"; shift 2 ;;
            --details)      D_ASSIGNMENT=${2:-}; shift 2 ;;
            --details-file) [ -f "${2:-}" ] || die "no such file: ${2:-}"
                            load_details_file "$2"; shift 2 ;;
            --availability) D_AVAILABILITY=${2:-}; shift 2 ;;
            --focus)        D_FOCUS=${2:-}; shift 2 ;;
            --other)        D_OTHER=${2:-}; shift 2 ;;
            --pdf)          [ -f "${2:-}" ] || die "no such file: ${2:-}"
                            A_PDF=$2; shift 2 ;;
            --think)        A_THINK=true; shift ;;
            --editor)       open_editor; shift ;;
            -h|--help)      usage_add; return 0 ;;
            *)              die "unknown option for add: $1" ;;
        esac
    done

    if [ -z "$A_NAME" ] || [ -z "$A_COURSE" ] || [ -z "$D_ASSIGNMENT" ] || [ -z "$A_DUE" ]; then
        collect_activity
    fi
    [ -n "$A_START" ] || A_START=$(today)
    [ -n "$A_DUE" ]   || A_DUE=$(date_offset 7)
    if [ "$A_DUE" \< "$A_START" ]; then die "the due date is before the start date."; fi

    printf '\n  %sBreaking it down' "$C_ACCENT"
    if [ -n "$A_PDF" ]; then printf ' (+1 PDF)'; fi
    printf '...%s\n' "$C_RESET"

    # Plan first: a failed call should not leave a half-created activity behind.
    _plan=$(call_plan)

    _id=$(next_id)
    printf 'A\t%s\t%s\t%s\t%s\t%s\t\n' \
        "$_id" "$(clean "$A_NAME")" "$(clean "$A_COURSE")" "$A_START" "$A_DUE" >> "$STORE"
    save_details "$_id"

    printf '%s\n' "$_plan" | apply_plan "$_id"

    BOOST_WANT=$_id BOOST_TODAY=$(today) BOOST_DETAIL=1 render_activity
    printf '  %sSaved as activity %s.%s %sOpen it with `boost show %s`.%s\n\n' \
        "$C_ACCENT" "$_id" "$C_RESET" "$C_DIM" "$_id" "$C_RESET"
}

cmd_replan() {
    ensure_store
    _force=0; _id=''
    while [ $# -gt 0 ]; do
        case "$1" in
            -f|--force) _force=1; shift ;;
            --think)    A_THINK=true; shift ;;
            *)          _id=$1; shift ;;
        esac
    done
    [ -n "$_id" ] || die "usage: boost replan <id> [--force] [--think]"
    require_activity "$_id"

    _n=$(task_count "$_id")
    if [ "$_n" -gt 0 ] && [ "$_force" -eq 0 ]; then
        printf '\n  %sActivity %s already has %s tasks. Replanning replaces all of them.%s\n' \
            "$C_YELLOW" "$_id" "$_n" "$C_RESET"
        note "  Re-run with --force to go ahead."
        printf '\n'
        return 1
    fi

    # Rebuild the request from the stored A and D records. Reading field-by-field
    # avoids eval'ing anything derived from user text.
    A_NAME=$(awk   -F"$TAB" -v a="$_id" '$1=="A" && $2==a { print $3; exit }' "$STORE")
    A_COURSE=$(awk -F"$TAB" -v a="$_id" '$1=="A" && $2==a { print $4; exit }' "$STORE")
    A_START=$(awk  -F"$TAB" -v a="$_id" '$1=="A" && $2==a { print $5; exit }' "$STORE")
    A_DUE=$(awk    -F"$TAB" -v a="$_id" '$1=="A" && $2==a { print $6; exit }' "$STORE")
    D_ASSIGNMENT=$(awk   -F"$TAB" -v a="$_id" '$1=="D" && $2==a { print $3; exit }' "$STORE")
    D_AVAILABILITY=$(awk -F"$TAB" -v a="$_id" '$1=="D" && $2==a { print $4; exit }' "$STORE")
    D_FOCUS=$(awk        -F"$TAB" -v a="$_id" '$1=="D" && $2==a { print $5; exit }' "$STORE")
    D_OTHER=$(awk        -F"$TAB" -v a="$_id" '$1=="D" && $2==a { print $6; exit }' "$STORE")
    [ -n "$D_ASSIGNMENT" ] || D_ASSIGNMENT="$A_NAME for $A_COURSE"

    printf '\n  %sReplanning...%s\n' "$C_ACCENT" "$C_RESET"
    _plan=$(call_plan)
    printf '%s\n' "$_plan" | apply_plan "$_id"
    BOOST_WANT=$_id BOOST_TODAY=$(today) BOOST_DETAIL=1 render_activity
}

# ---------------------------------------------------------------------------
# other commands
# ---------------------------------------------------------------------------

cmd_list() { ensure_store; BOOST_TODAY=$(today) render_table; }

cmd_show() {
    ensure_store
    [ -n "${1:-}" ] || die "usage: boost show <id>"
    require_activity "$1"
    BOOST_WANT=$1 BOOST_TODAY=$(today) BOOST_DETAIL=1 render_activity
}

cmd_today() {
    ensure_store
    _days=1; _from=$(today)
    while [ $# -gt 0 ]; do
        case "$1" in
            -d|--days) _days=${2:-1}; shift 2 ;;
            --date)    _from=$(parse_date "${2:-}") || die "bad --date"; shift 2 ;;
            *)         die "unknown option: $1" ;;
        esac
    done
    BOOST_FROM=$_from BOOST_DAYS=$_days BOOST_TODAY=$(today) render_agenda
}

# set_done <0|1> <label> <ref>...
set_done() {
    _val=$1; _label=$2; shift 2
    [ $# -gt 0 ] || die "usage: boost $_label <activity.task> ..."
    for _ref in "$@"; do
        case "$_ref" in
            *.*) : ;;
            *) die "'$_ref' should look like 3.2 (activity.task)." ;;
        esac
        _a=${_ref%%.*}; _i=${_ref#*.}
        require_activity "$_a"
        _n=$(task_count "$_a")
        case "$_i" in
            ''|*[!0-9]*) die "'$_ref' should look like 3.2 (activity.task)." ;;
        esac
        if [ "$_i" -lt 1 ] || [ "$_i" -gt "$_n" ]; then
            die "activity $_a has no task $_i."
        fi

        awk -F"$TAB" -v OFS="$TAB" -v a="$_a" -v i="$_i" -v v="$_val" '
            $1=="T" && $2==a { n++; if (n == i) $3 = v }
            { print }
        ' "$STORE" | store_write

        awk -F"$TAB" -v a="$_a" -v i="$_i" -v v="$_val" \
            -v G="$C_GREEN" -v D="$C_DIM" -v B="$C_BOLD" -v R="$C_RESET" '
            $1=="T" && $2==a { n++; if (n == i) title = $7; tot++; if ($3=="1") dn++ }
            END {
                mark = (v == "1") ? "[x]" : "[ ]"
                col  = (v == "1") ? G : D
                printf "\n  %s%s %s.%s%s  %s%s%s   %s(%d/%d)%s\n", \
                    col, mark, a, i, R, B, title, R, D, dn+0, tot+0, R
                if (tot > 0 && dn == tot) printf "  %sAll done on this one.%s\n", G, R
            }' "$STORE"
    done
    printf '\n'
}

cmd_rm() {
    ensure_store
    _force=0; _id=''
    while [ $# -gt 0 ]; do
        case "$1" in
            -f|--force) _force=1; shift ;;
            *) _id=$1; shift ;;
        esac
    done
    [ -n "$_id" ] || die "usage: boost rm <id> [--force]"
    require_activity "$_id"

    if [ "$_force" -eq 0 ]; then
        _name=$(awk -F"$TAB" -v a="$_id" '$1=="A" && $2==a { print $3; exit }' "$STORE")
        printf '\n  %sThis deletes "%s" and its %s tasks.%s\n' \
            "$C_YELLOW" "$_name" "$(task_count "$_id")" "$C_RESET"
        note "  Re-run with --force to confirm."
        printf '\n'
        return 1
    fi

    awk -F"$TAB" -v a="$_id" '!(($1=="A" || $1=="D" || $1=="T") && $2==a)' "$STORE" | store_write
    printf '\n'; note "  Deleted activity $_id."; printf '\n'
}

cmd_export() {
    ensure_store
    _fmt=md; _out=''; _id=''
    while [ $# -gt 0 ]; do
        case "$1" in
            --format)    _fmt=${2:-md}; shift 2 ;;
            -o|--output) _out=${2:-}; shift 2 ;;
            *)           _id=$1; shift ;;
        esac
    done
    [ -n "$_id" ] || die "usage: boost export <id> [--format md|ics] [-o FILE]"
    require_activity "$_id"

    case "$_fmt" in
        md)  _text=$(export_md "$_id") ;;
        ics) _text=$(export_ics "$_id") ;;
        *)   die "unknown format '$_fmt' (want md or ics)" ;;
    esac

    if [ -n "$_out" ]; then
        printf '%s\n' "$_text" > "$_out"
        printf '\n'; note "  Wrote $_out"; printf '\n'
    else
        printf '%s\n' "$_text"
    fi
}

export_md() {
    # -v, not a `var=value` file argument: the latter sets an awk variable when
    # the argument is reached, which is after BEGIN and is not in ENVIRON.
    awk -F"$TAB" -v want="$1" "$AWK_DATE"'
    $1=="A" && $2==want {
        print "# " $3; print ""
        print "**Class:** " $4 "  "
        print "**Window:** " $5 " -> " $6 "  "
        if ($7 != "") { print ""; print "> " $7 }
        next
    }
    $1=="T" && $2==want {
        if ($4 != day) { day = $4; print ""; print "## " longday(day); print "" }
        box  = ($3=="1") ? "x" : " "
        slot = ($5 != "" && $6 != "") ? sprintf(" _%s - %s_", $5, $6) : ""
        printf "- [%s] **%s**%s\n", box, $7, slot
        if ($8 != "") printf "      %s\n", $8
    }' "$STORE"
}

export_ics() {
    # Floating local time (no TZ suffix) so an event lands at the same wall-clock
    # hour wherever the calendar is opened. CRLF line endings per RFC 5545.
    awk -F"$TAB" -v want="$1" -v stamp="$(date +%Y%m%dT%H%M%S)" '
    function esc(s) { gsub(/\\/, "\\\\", s); gsub(/;/, "\\;", s); gsub(/,/, "\\,", s); return s }
    function out(l) { printf "%s\r\n", l }
    BEGIN {
        out("BEGIN:VCALENDAR"); out("VERSION:2.0")
        out("PRODID:-//Boostability//boost-cli//EN"); out("CALSCALE:GREGORIAN")
    }
    $1=="A" && $2==want { course = $4 }
    $1=="T" && $2==want {
        n++
        d = $4; gsub(/-/, "", d)
        out("BEGIN:VEVENT")
        out("UID:boost-" want "-" n "@boostability.local")
        out("DTSTAMP:" stamp "Z")
        if ($5 != "" && $6 != "") {
            s = $5; gsub(/:/, "", s)
            e = $6; gsub(/:/, "", e)
            out("DTSTART:" d "T" s "00"); out("DTEND:" d "T" e "00")
        } else {
            out("DTSTART;VALUE=DATE:" d)
        }
        out("SUMMARY:" esc($7))
        desc = $8
        if (course != "") desc = "[" course "] " desc
        if (desc != "") out("DESCRIPTION:" esc(desc))
        out("STATUS:" (($3=="1") ? "COMPLETED" : "CONFIRMED"))
        out("END:VEVENT")
    }
    END { out("END:VCALENDAR") }' "$STORE"
}

cmd_where() {
    printf '\n'
    note "  store   $STORE"
    note "  api     $BOOST_API"
    _model=$(curl -fsS "$BOOST_API/api/health" 2>/dev/null \
        | sed -n 's/.*"model":"\([^"]*\)".*/\1/p') || _model=''
    note "  model   ${_model:-unreachable} (resolved server-side)"
    printf '\n'
}

cmd_update() {
    note "Re-running the installer from $BOOST_API ..."
    curl -fsSL "$BOOST_API" | BOOST_API="$BOOST_API" sh
}

usage_add() {
    cat <<'EOF'
Usage: boost add [options]

  --name TEXT           activity name
  --class TEXT          course code
  --start DATE          ISO / M/D/YYYY / today / tomorrow / +N   (default today)
  --due DATE            same formats                             (default +7)
  --details TEXT        what you need to do
  --details-file FILE   read the four detail fields from a file
  --availability TEXT   days/times you can study
  --focus TEXT          how long you can focus in one sitting
  --other TEXT          anything else worth knowing
  --pdf FILE            attach an assignment brief (PDF, ~6 MB max)
  --think               let the model reason before planning
  --editor              write the details in $EDITOR

With no options, add walks you through it interactively.
EOF
}

usage() {
    cat <<'EOF'

  boost -- turn assignments into achievable daily tasks.

  boost                       dashboard: everything tracked, plus today
  boost add                   add an assignment and break it down
  boost list                  list tracked activities
  boost show <id>             open one activity's plan
  boost today [-d N]          what is scheduled today (or the next N days)
  boost done <a.t> ...        tick tasks off, e.g. boost done 3.2
  boost undo <a.t> ...        un-tick tasks
  boost replan <id> [-f]      regenerate the plan
  boost rm <id> [-f]          delete an activity
  boost export <id> [--format md|ics] [-o FILE]
  boost where                 show store, API and model
  boost update                reinstall the latest version

  boost add --help            options for add

EOF
}

# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

cmd=${1:-}
if [ $# -gt 0 ]; then shift; fi

case "$cmd" in
    ''|dash|dashboard)
        ensure_store
        BOOST_TODAY=$(today) render_table
        BOOST_FROM=$(today) BOOST_DAYS=1 BOOST_TODAY=$(today) render_agenda
        note "  boost add     boost show <id>     boost today"
        printf '\n'
        ;;
    add)              cmd_add "$@" ;;
    list|ls)          cmd_list "$@" ;;
    show)             cmd_show "$@" ;;
    today)            cmd_today "$@" ;;
    done)             set_done 1 done "$@" ;;
    undo)             set_done 0 undo "$@" ;;
    replan)           cmd_replan "$@" ;;
    rm|remove|delete) cmd_rm "$@" ;;
    export)           cmd_export "$@" ;;
    where)            cmd_where ;;
    update|upgrade)   cmd_update ;;
    -v|--version)     printf '%s\n' "$BOOST_VERSION" ;;
    -h|--help|help)   usage ;;
    *)                die "unknown command '$cmd'. Try \`boost --help\`." ;;
esac
