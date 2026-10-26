"""Точка входа. GUI (PySide6) появится на этапе 5; пока доступно управление моделями."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from bookmind.main import cli  # noqa: E402

if __name__ == "__main__":
    cli()
