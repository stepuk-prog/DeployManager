"""Перенаправление stdout ядра (print) в лог-панель Flet (построчно).

Цвет: базовый — на всю строку (отчёты деплоя: ✅ успех / ❌ провал / ⚠️ предупреждение).
Поверх — пер-значение (span'ы) для статуса привязки (leader/standby/unavailable),
run-state (active/inactive/…) и рассинхрона версий — каждое значение своим цветом в строке.
"""
import re

import flet as ft


class LogSink:
    def __init__(self, view: ft.ListView, page: ft.Page):
        self.view = view
        self.page = page
        self._buf = ""

    def write(self, s: str) -> int:
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        if self._buf == "" :  # обновим, если что-то добавили
            self._update()
        return len(s)

    def flush(self) -> None:
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def clear(self) -> None:
        """Очистить лог-панель (кнопка «Очистить лог»)."""
        self.view.controls.clear()
        self._buf = ""
        self._update()

    # базовый цвет всей строки (отчёты деплоя). красный — ловит ⚠️⚠️ до одиночного ⚠️
    _RED = ("❌", "⛔", "‼️", "🛑", "FAILED", "Ошибка", "ошибка", "⚠️⚠️")
    _AMBER = ("⚠️", "stale", "DIRTY")

    @staticmethod
    def _base_color(line: str):
        if any(m in line for m in LogSink._RED):
            return ft.Colors.RED
        if "✅" in line or "up-to-date" in line:
            return ft.Colors.GREEN
        if any(m in line for m in LogSink._AMBER):
            return ft.Colors.AMBER
        return None  # цвет по умолчанию (тема)

    # пер-значение (приоритет: больше — важнее при наложении).
    # зелёный — норма (leader / запущен / актуальная версия);
    # янтарный — внимание (standby / остановлен / отстаёт); красный — плохо (unavailable / сбой).
    _TOKENS = [
        (r"\bleader\b", ft.Colors.GREEN, 1),
        (r"▶ active|▶ running|up-to-date", ft.Colors.GREEN, 1),
        (r"\bstandby\b", ft.Colors.AMBER, 1),
        (r"■ inactive|■ stopped|✗ inactive", ft.Colors.AMBER, 1),
        (r"отстаёт|впереди|разошлись|нет VERSION|вне истории|версия неизвестна",
         ft.Colors.AMBER, 1),
        (r"\bunavailable\b", ft.Colors.RED, 2),
        (r"✗ (?!inactive)\w+", ft.Colors.RED, 2),
    ]

    @classmethod
    def _spans(cls, line: str):
        """Список TextSpan с пер-значение цветом (или None, если красить нечего)."""
        base = cls._base_color(line)
        hits = []
        for pat, color, prio in cls._TOKENS:
            for m in re.finditer(pat, line):
                hits.append((m.start(), m.end(), color, prio))
        if not hits:
            return None
        hits.sort(key=lambda h: (h[0], -h[3]))         # по позиции; при равной — важнее вперёд
        chosen, last = [], -1
        for s, e, color, _ in hits:
            if s >= last:                              # неперекрывающиеся
                chosen.append((s, e, color))
                last = e
        spans, idx = [], 0
        for s, e, color in chosen:
            if s > idx:
                spans.append(ft.TextSpan(line[idx:s], ft.TextStyle(color=base)))
            spans.append(ft.TextSpan(line[s:e], ft.TextStyle(color=color)))
            idx = e
        if idx < len(line):
            spans.append(ft.TextSpan(line[idx:], ft.TextStyle(color=base)))
        return spans

    def _emit(self, line: str) -> None:
        line = line or " "
        spans = self._spans(line)
        base = self._base_color(line)
        txt = (ft.Text(spans=spans, selectable=True, font_family="monospace", size=12, color=base)
               if spans else
               ft.Text(line, selectable=True, font_family="monospace", size=12, color=base))
        self.view.controls.append(txt)
        self._update()

    def _update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass