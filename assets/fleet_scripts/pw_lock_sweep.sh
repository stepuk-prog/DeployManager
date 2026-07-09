#!/usr/bin/env bash
# playwright_sweep.sh — БЕЗОПАСНЫЙ свип кэша Playwright под vova.
# Удаляет ТОЛЬКО:
#   (A) висячие firefox-lock симлинки, чей PID мёртв ИЛИ не является firefox
#   (B) протухшие .links (записанный путь venv не существует)
# НЕ трогает: живые локи (firefox с этим PID жив), браузер-билды, что-либо ещё.
# Идемпотентный, read-mostly; всё удаляемое печатает. Работает как root или vova.
set -u
CACHE="/home/vova/.cache/ms-playwright"
host="$(hostname)"; who="$(whoami)"
lr=0; ll=0; kr=0

if [ ! -d "$CACHE" ]; then
  echo "[$host] нет $CACHE — Playwright не установлен, пропуск"
  exit 0
fi

# ── (A) висячие Firefox-lock ─────────────────────────────────────────────────
# lock — симлинк с целью "<ip>:+<pid>" (никогда не резолвится, по дизайну «битый»).
# Живым считаем ТОЛЬКО если pid жив И его /proc/<pid>/comm начинается с firefox.
while IFS= read -r lk; do
  [ -n "$lk" ] || continue
  tgt="$(readlink "$lk" 2>/dev/null)"
  pid="$(printf '%s' "$tgt" | sed -n 's/.*:+\([0-9][0-9]*\).*/\1/p')"
  keep=0
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || echo UNREADABLE)"
    case "$comm" in
      firefox*|UNREADABLE) keep=1 ;;   # живой firefox / не смогли проверить → безопасно оставить
    esac
  else
    comm="dead"
  fi
  if [ "$keep" = 1 ]; then
    ll=$((ll+1)); [ -n "${PW_SWEEP_VERBOSE:-}" ] && echo "[$host]   KEEP  live lock: $lk (pid=$pid comm=$comm)"
  else
    if rm -f "$lk" 2>/dev/null; then
      lr=$((lr+1)); echo "[$host]   RM    stale lock: $lk (-> $tgt, comm=$comm)"
    else
      echo "[$host]   FAIL  не смог удалить $lk (права?)"
    fi
  fi
done < <(find "$CACHE" -path '*/firefox/lock' -type l 2>/dev/null)

# ── (B) протухшие .links (реестр venv'ов) ────────────────────────────────────
if [ -d "$CACHE/.links" ]; then
  for f in "$CACHE"/.links/*; do
    [ -e "$f" ] || continue
    p="$(cat "$f" 2>/dev/null)"
    if [ -n "$p" ] && [ ! -d "$p" ]; then
      if rm -f "$f" 2>/dev/null; then
        kr=$((kr+1)); echo "[$host]   RM    stale .links: $(basename "$f") -> $p"
      else
        echo "[$host]   FAIL  не смог удалить .links $(basename "$f")"
      fi
    fi
  done
fi

builds="$(ls -d "$CACHE"/firefox-* 2>/dev/null | wc -l | tr -d ' ')"
size="$(du -sh "$CACHE" 2>/dev/null | cut -f1)"
echo "[$host] ИТОГ (user=$who): локи removed=$lr live_kept=$ll | .links removed=$kr | firefox-билдов=$builds | кэш=$size"
