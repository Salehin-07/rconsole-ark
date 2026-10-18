#!/bin/sh
# rfetch — responsive system summary for the rconsole VM.
#
# Installed in every VM as `neofetch`, `fetch` and `rfetch`, so the familiar
# command works no matter which system you chose (Alpine, Debian, Ubuntu, Kali).
# Pure POSIX sh with no dependencies; it adapts to the terminal width so it also
# looks right on a phone:
#
#   wide   logo on the left, details on the right
#   medium logo above the details
#   narrow details only (values are shortened to fit)
#
# Usage: rfetch [--no-logo] [--plain]       (honours COLUMNS and NO_COLOR)

logo=1 color=1
for a in "$@"; do
  case $a in
    --no-logo) logo=0 ;;
    --plain)   color=0 ;;
    -h|--help) echo "usage: rfetch [--no-logo] [--plain]"; exit 0 ;;
  esac
done
[ -n "${NO_COLOR:-}" ] && color=0

cols=${COLUMNS:-}
case $cols in ''|*[!0-9]*) cols=$(stty size 2>/dev/null | cut -d' ' -f2) ;; esac
case $cols in ''|*[!0-9]*|0) cols=80 ;; esac
[ "$cols" -lt 20 ] && cols=20

if [ "$color" = 1 ]; then
  ESC=$(printf '\033')
  R="${ESC}[0m"; B="${ESC}[1m"
else
  R=; B=
fi

# ---- facts -----------------------------------------------------------------
os_pretty=$( . /etc/os-release 2>/dev/null; printf %s "${PRETTY_NAME:-${NAME:-Linux}}" )
os_id=$( . /etc/os-release 2>/dev/null; printf %s "${ID:-linux}" )
arch=$(uname -m 2>/dev/null)
kernel=$(uname -r 2>/dev/null)
user=$(id -un 2>/dev/null || echo root)
host=${HOSTNAME:-$(cat /proc/sys/kernel/hostname 2>/dev/null)}
[ -n "$host" ] || host=rconsole
shell=$(basename "${SHELL:-sh}")

up=
{ read up _ < /proc/uptime; } 2>/dev/null && up=${up%.*}
uptime_s=
if [ -n "$up" ]; then
  d=$((up / 86400)); h=$((up % 86400 / 3600)); m=$((up % 3600 / 60))
  if [ "$d" -gt 0 ]; then uptime_s="${d}d ${h}h ${m}m"
  elif [ "$h" -gt 0 ]; then uptime_s="${h}h ${m}m"
  else uptime_s="${m}m"; fi
fi

mem=; mt=; ma=
{
  while read -r k v _; do
    case $k in MemTotal:) mt=$v ;; MemAvailable:) ma=$v ;; esac
  done < /proc/meminfo
} 2>/dev/null
if [ -n "$mt" ] && [ -n "$ma" ]; then
  mem="$(( (mt - ma) / 1024 ))M / $(( mt / 1024 ))M"
fi

cpu=; ncpu=0
{
  while IFS=: read -r k v; do
    case $k in
      "model name"*|"Model name"*|"Hardware"*) [ -z "$cpu" ] && cpu=${v# } ;;
      processor*) ncpu=$((ncpu + 1)) ;;
    esac
  done < /proc/cpuinfo
} 2>/dev/null
[ -n "$cpu" ] || cpu="${arch:-cpu}"
[ "$ncpu" -gt 0 ] && cpu="$cpu ($ncpu)"

pkgs=
if [ -r /var/lib/dpkg/status ]; then
  pkgs="$(grep -c '^Status: install ok installed' /var/lib/dpkg/status) (dpkg)"
elif [ -r /lib/apk/db/installed ]; then
  pkgs="$(grep -c '^P:' /lib/apk/db/installed) (apk)"
elif command -v apk >/dev/null 2>&1; then
  pkgs="$(apk info 2>/dev/null | wc -l | tr -d ' ') (apk)"
fi

disk=
set -- $(df -h / 2>/dev/null | tail -n 1)
[ -n "${3:-}" ] && disk="$3 / $2"

# ---- logo ------------------------------------------------------------------
case $os_id in
  alpine) LC="${ESC}[34m"; LOGO='      /\
     /  \
    / /\ \
   / /  \ \
  /_/    \_\' ;;
  debian) LC="${ESC}[31m"; LOGO='   _____
  /  __ \
 |  /    |
 |  \___-
 -_
   --_' ;;
  ubuntu) LC="${ESC}[33m"; LOGO='        _
    ---(_)
  _/  ---  \
 (_) |   |
   \  --- _/
    ---(_)' ;;
  kali)   LC="${ESC}[34m"; LOGO=' _  __     _     _     ___
| |/ /    / \   | |   |_ _|
| . /   / _ \  | |    | |
| . \  / ___ \ | |___ | |
|_|\_\/_/   \_\|_____|___|' ;;
  *)      LC="${ESC}[36m"; LOGO='    .--.
   |o_o |
   |:_/ |
  //   \ \
 (|     | )
/ `\_   _/`\
\___)=(___/' ;;
esac
[ "$color" = 1 ] || LC=

lw=0
OLDIFS=$IFS
IFS='
'
for l in $LOGO; do [ ${#l} -gt "$lw" ] && lw=${#l}; done
IFS=$OLDIFS

# ---- layout ----------------------------------------------------------------
layout=none
if [ "$logo" = 1 ]; then
  if [ "$cols" -ge $((lw + 2 + 30)) ]; then layout=side
  elif [ "$cols" -ge "$lw" ]; then layout=top
  fi
fi

if [ "$layout" = side ]; then avail=$((cols - lw - 2)); else avail=$cols; fi

# Rows are "label: value", shortened so they never wrap on a small screen.
INFO=
row() {  # $1=label $2=value
  [ -n "$2" ] || return 0
  lab=$1; val=$2
  room=$((avail - ${#lab} - 2))
  [ "$room" -lt 4 ] && room=4
  [ ${#val} -gt "$room" ] && val="$(printf %s "$val" | cut -c1-$((room - 1)))~"
  INFO="${INFO}${B}${lab}${R}: ${val}
"
}

title="${user}@${host:-rconsole}"
ul=$(printf %s "$title" | sed 's/./-/g')
INFO="${B}${title}${R}
${ul}
"
row OS     "$os_pretty${arch:+ $arch}"
row Host   "rconsole VM (proot)"
row Kernel "$kernel"
row Uptime "$uptime_s"
row Pkgs   "$pkgs"
row Shell  "$shell"
row CPU    "$cpu"
row Memory "$mem"
row Disk   "$disk"

# ---- print -----------------------------------------------------------------
pad() {  # left-align $1 in a field of $lw characters (POSIX: no printf '*')
  p=$1
  while [ ${#p} -lt "$lw" ]; do p="$p "; done
  printf %s "$p"
}

case $layout in
  side)
    ni=$(printf %s "$INFO" | wc -l)
    nl=$(printf '%s\n' "$LOGO" | wc -l)
    lines=$ni; [ "$nl" -gt "$lines" ] && lines=$nl
    n=1
    while [ "$n" -le "$lines" ]; do
      l=$(printf '%s\n' "$LOGO" | sed -n "${n}p")
      i=$(printf %s "$INFO" | sed -n "${n}p")
      printf '%s%s%s  %s\n' "$LC" "$(pad "$l")" "$R" "$i"
      n=$((n + 1))
    done
    ;;
  top)
    printf '%s%s%s\n' "$LC" "$LOGO" "$R"
    printf '%s' "$INFO"
    ;;
  *)
    printf '%s' "$INFO"
    ;;
esac
