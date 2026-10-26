"""Запуск графического приложения."""
from __future__ import annotations

import sys


def _setup_logging(data_dir) -> None:
    import logging
    from logging.handlers import RotatingFileHandler
    try:
        h = RotatingFileHandler(data_dir / "logs" / "bookmind.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
        h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(h)
        logging.getLogger().setLevel(logging.INFO)
    except OSError:
        pass


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
    _setup_logging(ctx.data_dir)
    win = MainWindow(ctx)
    win.show()
    code = app.exec()
    ctx.close()
    return code


if __name__ == "__main__":
    sys.exit(main())
