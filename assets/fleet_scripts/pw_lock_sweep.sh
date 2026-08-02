#!/usr/bin/env bash
# playwright_sweep.sh — БЕЗОПАСНЫЙ свип кэша Playwright под vova.
# Удаляет / освежает ТОЛЬКО:
#   (A) висячие firefox-lock симлинки в каталоге БИЛДА — ВСЕ, включая живые (см. ниже)
#   (B) протухшие .links (записанный путь venv не существует)
#   (C) mtime маркеров DEPENDENCIES_VALIDATED старше 20 дней (touch, файл не создаём)
# НЕ трогает: браузер-билды, профили, процессы — ничего не убивает.
# Идемпотентный, read-mostly; всё изменяемое печатает. Работает как root или vova.
#
# Грабли 2026-08-02 (node-6, BinoStoch): Playwright ≥1.5x перепроверяет host-requirements,
# если маркеру <build>/DEPENDENCIES_VALIDATED больше 30 дней (kMaximumReValidationPeriod).
# Проверка ОБХОДИТ каталог билда и stat-ит каждую запись — а lock висячий ПО ДИЗАЙНУ
# (цель "<ip>:+<pid>" не резолвится никогда). Итог: ENOENT на stat lock → BrowserType.launch
# падает у ВСЕХ, кто стартует этот билд, и маркер не обновляется → залипает навсегда.
# Поэтому «живой» лок опаснее протухшего: старое правило KEEP-if-alive сохраняло ровно тот
# лок, который всё и блокировал. Живому Firefox lock после старта не нужен (читается только
# при старте, профиль Playwright лежит в /tmp) — сносим независимо от PID.
set -u
CACHE="/home/vova/.cache/ms-playwright"
MARKER_MAX_AGE_DAYS=20            # < 30 (kMaximumReValidationPeriod) с запасом
host="$(hostname)"; who="$(whoami)"
lr=0; ll=0; kr=0; mt=0

if [ ! -d "$CACHE" ]; then
  echo "[$host] нет $CACHE — Playwright не установлен, пропуск"
  exit 0
fi

# ── (A) висячие Firefox-lock ─────────────────────────────────────────────────
# lock — симлинк с целью "<ip>:+<pid>" (никогда не резолвится, по дизайну «битый»).
# Сносим ВСЕ такие локи в каталоге билда — и мёртвые, и живые (см. шапку: живой лок
# ломает host-validation другим процессам, а владельцу после старта не нужен).
# Живые считаем отдельно (ll) — только для отчёта, поведение от этого не зависит.
while IFS= read -r lk; do
  [ -n "$lk" ] || continue
  tgt="$(readlink "$lk" 2>/dev/null)"
  pid="$(printf '%s' "$tgt" | sed -n 's/.*:+\([0-9][0-9]*\).*/\1/p')"
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    comm="$(cat "/proc/$pid/comm" 2>/dev/null || echo UNREADABLE)"
    ll=$((ll+1))
  else
    comm="dead"
  fi
  if rm -f "$lk" 2>/dev/null; then
    lr=$((lr+1)); echo "[$host]   RM    lock: $lk (-> $tgt, comm=$comm)"
  else
    echo "[$host]   FAIL  не смог удалить $lk (права?)"
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

# ── (C) освежить маркеры DEPENDENCIES_VALIDATED ──────────────────────────────
# Пока маркер моложе 30 дней, Playwright вообще не идёт обходить каталог билда — а значит
# и не спотыкается о lock, который другой процесс мог создать уже ПОСЛЕ нашего свипа (A).
# touch, а НЕ create: маркера нет → валидация ни разу не проходила, подделывать её нельзя.
while IFS= read -r mk; do
  [ -n "$mk" ] || continue
  if touch "$mk" 2>/dev/null; then
    mt=$((mt+1)); echo "[$host]   TOUCH marker: $mk"
  else
    echo "[$host]   FAIL  не смог освежить $mk (права?)"
  fi
done < <(find "$CACHE" -maxdepth 2 -name DEPENDENCIES_VALIDATED -type f \
              -mtime +"$MARKER_MAX_AGE_DAYS" 2>/dev/null)

builds="$(ls -d "$CACHE"/firefox-* 2>/dev/null | wc -l | tr -d ' ')"
size="$(du -sh "$CACHE" 2>/dev/null | cut -f1)"
echo "[$host] ИТОГ (user=$who): локи removed=$lr (из них живых=$ll) | .links removed=$kr | маркеров touched=$mt | firefox-билдов=$builds | кэш=$size"
