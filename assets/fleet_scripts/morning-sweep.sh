#!/usr/bin/env bash
#
# morning-sweep.sh — read-only утренний проход по клиентским нодам (где живут программы).
# Ничего не меняет. Собирает за окно [--since, сейчас]:
#   1. логи всех программ под /home/vova/**/logs/**: error/critical/cookies/warning —
#      сообщения нормализуются (числа, токены, короткие кавычки) и считаются по типам,
#      с перечнем программ, где тип встретился; для error/critical — последняя строка целиком;
#   2. systemd: `--failed`-юниты и выходы по сбою / OOM из journal за окно;
#   3. память и load average ноды.
#
# Usage: morning-sweep.sh [--since "ГГГГ-ММ-ДД ЧЧ:ММ" | "yesterday 18:00"] [--full] [--dry-run]
#   --since    начало окна; всё, что понимает `date -d`. По умолчанию — вчера 18:00.
#   --full     последняя строка и у warning/cookies (не только у error/critical)
#   --dry-run  только показать, на какие узлы пойдём
#
# Время в логах программ — MSK, как и на нодах; окно сравнивается строкой в том же формате.
# События диспетчера (dispatcher.service_error_log / control_request) здесь НЕ смотрятся:
# это БД, а не ноды, — их даёт отдельный запрос (см. docs, «утренний проход»).
#
# Родился 21–23-09-2026: проход жил в /tmp и дважды пропал с перезагрузкой машины.
#
set -euo pipefail
source "$(dirname "$(readlink -f "$0")")/_nodes.sh"

SINCE_RAW="yesterday 18:00"; FULL=0; DRY_RUN=false
while [ $# -gt 0 ]; do case "$1" in
  --since) SINCE_RAW="${2:?--since требует значение}"; shift 2 ;;
  --full) FULL=1; shift ;;
  --dry-run) DRY_RUN=true; shift ;;
  *) echo "неизвестный аргумент: $1"; exit 2 ;;
esac; done

SINCE=$(date -d "$SINCE_RAW" '+%Y-%m-%d %H:%M') || { echo "не разобрал --since: $SINCE_RAW"; exit 2; }

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'

if $DRY_RUN; then
  echo -e "${Y}DRY-RUN${N} — окно с $SINCE; узлы: ${CLIENT_IPS[*]}"
  exit 0
fi

OUT=$(mktemp -d); trap 'rm -rf "$OUT"' EXIT

# ── Сборщик на ноде: печатает JSON {путь: [строки окна]} ─────────────────────────
read -r -d '' REMOTE_PY <<'PY' || true
import glob, json, os, re, sys
SINCE = sys.argv[1]
TS = re.compile(r'\[(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})')
out = {}
for path in glob.glob('/home/vova/**/logs/**/*.log', recursive=True):
    if '/venv/' in path or os.path.basename(path) not in (
            'error.log', 'warning.log', 'cookies.log', 'critical.log'):
        continue
    try:
        if os.path.getsize(path) == 0:
            continue
        with open(path, errors='replace') as f:
            lines = f.readlines()[-20000:]
    except OSError:
        continue
    keep, cur = [], False
    for ln in lines:
        m = TS.search(ln)
        if m:
            cur = m.group(1) >= SINCE
            if cur:
                keep.append(ln.rstrip())
        elif cur and keep:                      # продолжение многострочной записи
            keep[-1] += ' ⏎ ' + ln.strip()[:200]
    if keep:
        out[path.replace('/home/vova/', '')] = keep
print(json.dumps(out, ensure_ascii=False))
PY

# ── Агрегатор локально ─────────────────────────────────────────────────────────
read -r -d '' AGG_PY <<'PY' || true
import collections, json, os, re, sys
path, full = sys.argv[1], sys.argv[2] == '1'
MSG = re.compile(r'\]\s+(.*)$')
def norm(s):
    s = re.sub(r'bot\d+:[\w-]+', 'bot<T>', s)
    s = re.sub(r'\d+([.,]\d+)?', '#', s)
    s = re.sub(r"'[^']{0,40}'", "'…'", s)
    return s[:170]
def prog(p):
    head, _, tail = p.partition('/logs/')
    sub = tail.rsplit('/', 1)[0] if '/' in tail else ''
    return head.split('/')[-1] + ('/' + sub if sub else '')
try:
    data = json.load(open(path))
except Exception as e:
    print(f'  нет данных ({e})'); sys.exit(0)
if not data:
    print('  логи чистые'); sys.exit(0)
by = collections.defaultdict(lambda: collections.defaultdict(lambda: [0, set(), '']))
for p, lines in data.items():
    lvl = os.path.basename(p)[:-4]
    for ln in lines:
        m = MSG.search(ln.split(' ⏎ ')[0])
        e = by[lvl][norm(m.group(1) if m else ln)]
        e[0] += 1; e[1].add(prog(p)); e[2] = ln[:300]
for lvl in ('critical', 'error', 'cookies', 'warning'):
    if lvl not in by:
        continue
    print(f'  --- {lvl}')
    for k, (c, ps, last) in sorted(by[lvl].items(), key=lambda x: -x[1][0]):
        pl = sorted(ps)
        more = '…' if len(pl) > 4 else ''
        print(f'  {c:5d}  {k}   [{len(pl)} прог: {", ".join(pl[:4])}{more}]')
        if full or lvl in ('error', 'critical'):
            print(f'         последняя: {last}')
PY

echo -e "${G}Утренний проход: окно с $SINCE, узлов ${#CLIENT_IPS[@]}${N}"

# Сбор параллельно: на ноде 100+ каталогов логов, последовательно это минуты.
for ip in "${CLIENT_IPS[@]}"; do
  (
    ssh_node "$ip" "python3 - '$SINCE'" <<<"$REMOTE_PY" >"$OUT/$ip.json" 2>"$OUT/$ip.err" || true
    ssh_node "$ip" "systemctl --failed --no-legend --plain; echo '---'; \
      journalctl --since '$SINCE' --no-pager -o short-iso 2>/dev/null \
        | grep -E 'Main process exited|Failed with result|oom-kill|Out of memory' | tail -100; \
      echo '---'; free -m | sed -n 2p; uptime" >"$OUT/$ip.sys" 2>>"$OUT/$ip.err" || true
  ) &
done
wait

for ip in "${CLIENT_IPS[@]}"; do
  name="${IP_NAME[$ip]:-$ip}"
  echo; echo -e "${G}######## $name ($ip)${N}"
  if [ -s "$OUT/$ip.err" ]; then echo -e "${R}  ssh/stderr:${N} $(head -3 "$OUT/$ip.err")"; fi
  python3 -c "$AGG_PY" "$OUT/$ip.json" "$FULL"
  # .sys: failed-юниты --- выходы по сбою --- память/uptime
  awk -v R="$R" -v N="$N" 'BEGIN{s=0}
    /^---$/{s++; next}
    s==0 && NF {print "  " R "failed:" N " " $1}
    s==1 && NF {print "  journal: " $0}
    s==2 && /^Mem:/ {printf "  память: занято %s МБ, доступно %s МБ\n", $3, $7}
    s==2 && /load average/ {sub(/.*load average: /,""); print "  load: " $0}' "$OUT/$ip.sys" 2>/dev/null || true
done
