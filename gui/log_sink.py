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

    def _emit(self, line: str) -> None:
        self.view.controls.append(ft.Text(line or " ", selectable=True,
                                          font_family="monospace", size=12))
        self._update()

    def _update(self) -> None:
        try:
            self.page.update()
        except Exception:
            pass