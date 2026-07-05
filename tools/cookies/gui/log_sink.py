"""Перенаправление stdout (print/логгер) в лог-панель Flet, построчно с подсветкой по
базовому маркеру строки (✅ зелёный / ❌·Ошибка красный / ⚠️ янтарный). Порт LogSink из
DeployManager без deploy-специфичной пер-значение подсветки.

Заодно дублирует вывод в настоящий терминал (sys.__stdout__) и пробрасывает fileno/encoding —
чтобы безопасно стоять вместо sys.stdout (библиотеки иногда их спрашивают)."""
import sys

import flet as ft


class LogSink:
    encoding = "utf-8"

    def __init__(self, view: ft.ListView, page: ft.Page):
        self.view = view
        self.page = page
        self._buf = ""
        self._tee = sys.__stdout__   # дублируем в реальный терминал

    def write(self, s: str) -> int:
        if self._tee is not None:
            try:
                self._tee.write(s)
                if "\n" in s:
                    self._tee.flush()   # построчный сброс (иначе print теряется в буфере файла)
            except (Exception,):
                pass
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self._emit(line)
        return len(s)

    def fileno(self):
        if self._tee is not None:
            return self._tee.fileno()
        raise OSError("LogSink has no fileno")

    def isatty(self) -> bool:
        return False

    def flush(self) -> None:
        if self._tee is not None:
            try:
                self._tee.flush()
            except (Exception,):
                pass
        if self._buf:
            self._emit(self._buf)
            self._buf = ""

    def clear(self) -> None:
        self.view.controls.clear()
        self._buf = ""
        self._update()

    _RED = ("❌", "⛔", "‼️", "🛑", "Ошибка", "ошибка", "FAIL", "⚠️⚠️")
    _AMBER = ("⚠️",)

    @classmethod
    def _base_color(cls, line: str):
        if any(m in line for m in cls._RED):
            return ft.Colors.RED
        if "✅" in line:
            return ft.Colors.GREEN
        if any(m in line for m in cls._AMBER):
            return ft.Colors.AMBER
        return None  # цвет по умолчанию (тема)

    def _emit(self, line: str) -> None:
        line = line or " "
        self.view.controls.append(ft.Text(
            line, selectable=True, font_family="monospace", size=12,
            color=self._base_color(line)))
        self._update()

    def _update(self) -> None:
        try:
            self.page.update()
        except (Exception,):
            pass
