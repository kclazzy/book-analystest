"""Запуск графического приложения."""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    try:
        from PySide6.QtWidgets import QApplication
    except ImportError:
        print("PySide6 не установлен. Установите: pip install PySide6", file=sys.stderr)
        return 3
    from bookmind.ui.context import AppContext
    from bookmind.ui.main_window import MainWindow

    app = QApplication(argv if argv is not None else sys.argv)
    ctx = AppContext()
    win = MainWindow(ctx)
    win.show()
    code = app.exec()
    ctx.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
