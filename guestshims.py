"""Small replacements for process tools in VMs that have no ``/proc/self`` (no PID namespace).

Each shim runs the system tool when ``/proc/self`` exists (a real, private /proc) and otherwise works from the
VM's own built /proc (see procview.py), so ``pgrep``, ``pkill``, ``pidof``, ``killall``, ``pstree`` and ``htop``
behave in every distro, not only where the system tool happens to cope. ``ps`` and ``top`` live in sandbox.py.
"""

PROCLIST_TEXT = """#!/bin/sh
# rconsole: one line per process of this VM: pid, ppid, state, cpu seconds, comm, command line (tab separated).
for d in /proc/[0-9]*; do
  [ -r "$d/stat" ] || continue
  pid=${d#/proc/}
  st=$(cat "$d/stat" 2>/dev/null) || continue
  c=${st#*(}; c=${c%%)*}
  rest=${st##*) }
  set -- $rest
  cmd=$(tr '\\0' ' ' < "$d/cmdline" 2>/dev/null)
  printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' "$pid" "$2" "$1" "$(( (${12:-0} + ${13:-0}) / 100 ))" "$c" "${cmd% }"
done
"""

# one multiplexer: rconsole-proc pgrep|pkill|pidof|killall|pstree ARGS
PROC_TOOL_TEXT = """#!/bin/sh
# rconsole: pgrep, pkill, pidof, killall and pstree from this VM's own /proc view.
tool=$1; shift
SELF=$$
full=0; exact=0; long=0; cnt=0; newest=0; oldest=0; icase=0; invert=0; delim='
'; sig=TERM; quiet=0; pat=; names=
while [ $# -gt 0 ]; do
  case "$1" in
    --) shift; break;;
    -f|--full) full=1;; -x|--exact) exact=1;; -l|--list-name) long=1;; -a|--list-full) long=2;;
    -c|--count) cnt=1;; -n|--newest) newest=1;; -o|--oldest) oldest=1;; -i|--ignore-case) icase=1;;
    -v|--inverse) invert=1;; -q|--quiet) quiet=1;; -e|-w|-s|-x) ;;
    -d) delim=$2; shift;; -u|-U|-G|-g|-P|-t|-O) shift;;
    -[0-9]*|-[A-Z][A-Z]*) sig=${1#-};;
    -*) ;;
    *) if [ "$tool" = pidof ] || [ "$tool" = killall ]; then names="$names $1"; else pat=$1; fi;;
  esac
  shift
done
for a in "$@"; do
  if [ "$tool" = pidof ] || [ "$tool" = killall ]; then names="$names $a"; else pat=$a; fi
done
list() { /usr/local/bin/rconsole-proclist; }
# selected processes as "pid<TAB>comm<TAB>cmdline", never this script or what it started
select_procs() {
  list | awk -F'\\t' -v self="$SELF" -v pat="$1" -v full="$full" -v exact="$exact" -v icase="$icase" -v invert="$invert" -v names="$2" '
    { pid[NR]=$1; par[$1]=$2; st[NR]=$3; comm[NR]=$5; cmd[NR]=$6; n=NR }
    END {
      nn = split(names, nm, " ")
      for (i = 1; i <= n; i++) {
        if (st[i] == "Z") continue
        p = pid[i]; skip = 0; q = p
        for (d = 0; d < 64 && q != "" && q != 0; d++) { if (q == self) { skip = 1; break } q = par[q] }
        if (skip) continue
        subj = (full ? cmd[i] : comm[i]); if (subj == "") subj = comm[i]
        argv0 = cmd[i]; sub(/ .*/, "", argv0); sub(/.*\\//, "", argv0)
        ok = 0
        if (nn > 0) { for (j = 1; j <= nn; j++) if (comm[i] == nm[j] || argv0 == nm[j]) ok = 1 }
        else {
          s = subj; pt = pat
          if (icase) { s = tolower(s); pt = tolower(pt) }
          if (exact) pt = "^(" pt ")$"
          ok = (pt == "" ? 1 : (s ~ pt))
        }
        if (invert) ok = !ok
        if (ok) printf "%s\\t%s\\t%s\\n", p, comm[i], cmd[i]
      }
    }'
}
case "$tool" in
  pgrep|pkill)
    if [ "$tool" = pgrep ] && [ -z "$pat" ] && [ $invert = 0 ]; then
      echo "$tool: no matching criteria specified" >&2; exit 2
    fi
    sel=$(select_procs "$pat" "" | sort -n -k1,1)
    [ $newest = 1 ] && sel=$(echo "$sel" | tail -n 1)
    [ $oldest = 1 ] && sel=$(echo "$sel" | head -n 1)
    [ -n "$sel" ] || { [ $cnt = 1 ] && echo 0; exit 1; }
    if [ "$tool" = pkill ]; then
      echo "$sel" | while IFS='	' read -r p _ _; do kill -"$sig" "$p" 2>/dev/null; done
      exit 0
    fi
    if [ $cnt = 1 ]; then echo "$sel" | wc -l | tr -d ' '; exit 0; fi
    if [ $long = 1 ]; then echo "$sel" | while IFS='	' read -r p c _; do echo "$p $c"; done
    elif [ $long = 2 ]; then echo "$sel" | while IFS='	' read -r p c m; do echo "$p ${m:-$c}"; done
    else
      if [ "$delim" = '
' ]; then echo "$sel" | while IFS='	' read -r p _ _; do echo "$p"; done
      else echo "$sel" | while IFS='	' read -r p _ _; do printf '%s' "$p"; done | tr -s '\\n' "$delim"; echo; fi
    fi ;;
  pidof)
    [ -n "$names" ] || exit 1
    sel=$(select_procs "" "$names" | sort -rn -k1,1 | while IFS='	' read -r p _ _; do echo "$p"; done)
    [ -n "$sel" ] || exit 1
    echo $sel ;;
  killall)
    [ -n "$names" ] || { echo "Usage: killall [-SIGNAL] name ..." >&2; exit 1; }
    rc=0
    for nme in $names; do
      sel=$(select_procs "" "$nme" | while IFS='	' read -r p _ _; do echo "$p"; done)
      if [ -z "$sel" ]; then [ $quiet = 1 ] || echo "$nme: no process found" >&2; rc=1; continue; fi
      for p in $sel; do kill -"$sig" "$p" 2>/dev/null; done
    done
    exit $rc ;;
  pstree)
    list | awk -F'\\t' '
      { par[$1]=$2; nm[$1]=$5; kids[$2] = kids[$2] " " $1; have[$1]=1 }
      function show(p, ind,   a, k, i) {
        print ind nm[p] (cnt[p] ? "" : "")
        k = split(kids[p], a, " ")
        for (i = 1; i <= k; i++) show(a[i], ind "  ")
      }
      END { for (p in have) if (!(par[p] in have)) show(p, "") }' ;;
esac
"""


def tool_shim(name):
    """A wrapper for ``name``: the system tool when /proc/self exists, else the built-/proc implementation."""
    return f"""#!/bin/sh
# rconsole: {name} for VMs without /proc/self (no PID namespace); the system {name} is used when there is one.
if [ -e /proc/self ]; then
  for p in /bin/{name} /usr/bin/{name} /usr/sbin/{name} /sbin/{name}; do [ -x "$p" ] && exec "$p" "$@"; done
fi
exec /usr/local/bin/rconsole-proc {name} "$@"
"""


HTOP_SHIM_TEXT = """#!/bin/sh
# rconsole: htop needs /proc/self; without it show the VM's own processes with the built-in top.
if [ -e /proc/self ]; then
  for p in /bin/htop /usr/bin/htop; do [ -x "$p" ] && exec "$p" "$@"; done
fi
exec /usr/local/bin/top "$@"
"""

PROC_TOOLS = ("pgrep", "pkill", "pidof", "killall", "pstree")


def all_shims():
    """``{file name: text}`` of every process-tool shim (besides ps and top)."""
    out = {"rconsole-proclist": PROCLIST_TEXT, "rconsole-proc": PROC_TOOL_TEXT, "htop": HTOP_SHIM_TEXT}
    for n in PROC_TOOLS:
        out[n] = tool_shim(n)
    return out
