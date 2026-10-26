"""CLI управления моделями: bookmind-models <команда>."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from bookmind.config import Settings, default_data_dir
from bookmind.domain.exceptions import BookMindError, OllamaUnavailableError
from bookmind.llm.catalog import install_catalog, load_catalog
from bookmind.llm.model_manager import ModelManager
from bookmind.llm.ollama_client import OllamaClient


def _progress(event: dict) -> None:
    total, done = event.get("total"), event.get("completed")
    if total and done is not None:
        print(f"\r{event.get('status', '')}: {done * 100 // total}%   ", end="", flush=True)
    else:
        print(f"\r{event.get('status', '')}" + " " * 20, end="", flush=True)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="bookmind-models", description="Управление моделями Qwen в Ollama")
    p.add_argument("--data-dir", type=Path, default=None)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", help="Каталог и установленные модели")
    sub.add_parser("check", help="Проверить наличие обновлений (best-effort, обращается к registry.ollama.ai)")
    for name, h in (("install", "Установить модель"), ("update", "Обновить модель"), ("select", "Выбрать модель")):
        s = sub.add_parser(name, help=h)
        s.add_argument("tag")
    sub.add_parser("update-all", help="Обновить все установленные модели")
    cu = sub.add_parser("catalog-update", help="Обновить каталог моделей из JSON-файла")
    cu.add_argument("file", type=Path)
    fit = sub.add_parser("recommend", help="Модели из каталога, оценочно помещающиеся в VRAM")
    fit.add_argument("--vram", type=float, default=12.0)
    return p


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    data_dir = args.data_dir or default_data_dir()
    settings = Settings.load(data_dir)
    catalog = load_catalog(data_dir)

    if args.cmd == "catalog-update":
        cat = install_catalog(args.file.read_text(encoding="utf-8"), data_dir)
        print(f"Каталог обновлён: {len(cat.models)} моделей")
        return 0
    if args.cmd == "recommend":
        for m in catalog.fitting(args.vram):
            print(f"{m.tag:24} ~{m.approx_vram_gb:g} ГБ  {m.description}")
        return 0

    client = OllamaClient(settings.ollama_host, settings.request_timeout_s, settings.connect_timeout_s,
                          settings.max_retries, settings.max_concurrent_requests)
    mgr = ModelManager(client, catalog, settings)
    try:
        if args.cmd == "status":
            for s in mgr.statuses():
                mark = ("*" if s.selected else " ") + ("[+]" if s.installed else "[ ]")
                print(f"{mark} {s.tag:24} {'' if s.in_catalog else '(вне каталога)'}")
        elif args.cmd == "check":
            for tag, upd in mgr.check_updates().items():
                print(f"{tag:24} {'неизвестно' if upd is None else 'ЕСТЬ ОБНОВЛЕНИЕ' if upd else 'актуальна'}")
        elif args.cmd == "install":
            mgr.install(args.tag, _progress); print("\nГотово")
        elif args.cmd == "update":
            r = mgr.update(args.tag, _progress); print("\n" + ("Обновлена" if r.changed else "Уже актуальна"))
        elif args.cmd == "update-all":
            for r in mgr.update_all(lambda t, e: _progress(e)):
                print(f"\n{r.tag}: {'обновлена' if r.changed else 'актуальна'}")
        elif args.cmd == "select":
            mgr.select(args.tag); settings.save(data_dir); print(f"Выбрана модель {args.tag}")
    except OllamaUnavailableError as exc:
        print(f"Ошибка: {exc}\nЗапустите Ollama (ollama serve).", file=sys.stderr)
        return 2
    except BookMindError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


def cli() -> None:
    sys.exit(run())


if __name__ == "__main__":
    cli()
