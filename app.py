"""Точка входа. Без аргументов запускает окно; с командами status/install/update/... — управление моделями."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from bookmind.main import cli
        cli()
    else:
        from bookmind.ui.app import main
        sys.exit(main())
