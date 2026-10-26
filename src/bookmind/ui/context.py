"""Контекст приложения без зависимостей от Qt: общие сервисы, которыми пользуется окно и потоки."""
from __future__ import annotations

from pathlib import Path

from bookmind.analysis.extractor import StructuredCaller
from bookmind.analysis.pipeline import AnalysisPipeline
from bookmind.config import Settings, default_data_dir
from bookmind.creation.service import AdaptationService
from bookmind.creation.writer import BookWriter
from bookmind.llm.catalog import load_catalog
from bookmind.llm.model_manager import ModelManager
from bookmind.llm.ollama_client import OllamaClient
from bookmind.llm.prompts import PromptStore
from bookmind.services.backup import BackupService
from bookmind.services.entities import EntityService
from bookmind.services.exports import ExportService
from bookmind.services.knowledge import KnowledgeService
from bookmind.services.library import LibraryService
from bookmind.services.qa import QAService
from bookmind.services.reports import ReportService
from bookmind.services.search import SearchIndex
from bookmind.storage.database import Database
from bookmind.system.updater import Updater, launch_installer
from bookmind.storage.repositories import LibraryRepo, TaskRepo


class AppContext:
    def __init__(self, data_dir: Path | None = None, client: OllamaClient | None = None):
        self.data_dir = Path(data_dir) if data_dir else default_data_dir()
        for sub in ("database", "books", "exports", "backups", "logs", "cache"):
            (self.data_dir / sub).mkdir(parents=True, exist_ok=True)
        self.settings = Settings.load(self.data_dir)
        self.client = client or OllamaClient(
            self.settings.ollama_host, self.settings.request_timeout_s, self.settings.connect_timeout_s,
            self.settings.max_retries, self.settings.max_concurrent_requests)
        self.catalog = load_catalog(self.data_dir)
        self.models = ModelManager(self.client, self.catalog, self.settings)
        self.prompts = PromptStore(self.data_dir)
        self.open_db()

    def open_db(self) -> None:
        self.db = Database(self.data_dir / "database" / "bookmind.sqlite")
        self.repo = LibraryRepo(self.db)
        self.tasks = TaskRepo(self.db)
        self.library = LibraryService(self.db, self.settings, self.data_dir)
        self.knowledge = KnowledgeService(self.db)
        self.entities = EntityService(self.db)
        self.exports = ExportService(self.db)
        self.reports = ReportService(self.db)
        self.search = SearchIndex(self.db)
        self.backup = BackupService(self.data_dir)

    def pipeline(self) -> AnalysisPipeline:
        return AnalysisPipeline(self.db, self.client, self.settings, self.data_dir, self.prompts)

    def adaptation(self) -> AdaptationService:
        return AdaptationService(self.db, StructuredCaller(self.client, self.settings), self.settings, self.prompts)

    def writer(self) -> BookWriter:
        return BookWriter(self.adaptation())

    def qa(self) -> QAService:
        return QAService(self.db, StructuredCaller(self.client, self.settings), self.prompts)

    def updater(self) -> Updater:
        return Updater(self.settings.update_repo)

    def apply_update(self, installer: Path, launch=launch_installer) -> Path | None:
        """Резервная копия данных и запуск установщика. Возвращает путь к копии (None, если создать не удалось)."""
        from bookmind import __version__
        backup = None
        try:
            backup = self.backup.create(self.data_dir / "backups" / f"pre-update-{__version__}.zip")
        except Exception:      # обновление не должно срываться из-за копии; пользователь увидит предупреждение
            backup = None
        launch(installer)
        return backup

    def save_settings(self) -> None:
        self.settings.save(self.data_dir)

    def close(self) -> None:
        self.db.close()
        self.client.close()
