"""Перенаправление stdout ядра (print) в лог-панель Flet (построчно)."""
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

    # красный: провал/конфуз + КЛЮЧЕВОЕ предупреждение (двойное ⚠️⚠️, напр. leader)
    _RED = ("❌", "⛔", "‼️", "🛑", "FAILED", "Ошибка", "ошибка", "⚠️⚠️")
    _AMBER = ("⚠️", "stale", "DIRTY")   # обычные (мелкие) предупреждения

    @staticmethod
    def _color(line: str):
        if any(m in line for m in LogSink._RED):       # сначала красный — ловит ⚠️⚠️ до ⚠️
            return ft.Colors.RED
        if "✅" in line or "up-to-date" in line:
            return ft.Colors.GREEN
        if any(m in line for m in LogSink._AMBER):
            return ft.Colors.AMBER
        return None  # цвет по умолчанию (тема)

    def _emit(self, line: str) -> None:
        self.view.controls.append(ft.Text(line or " ", selectable=True, font_family="monospace",
                                          size=12, color=self._color(line)))
        self._update()

    def _update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass