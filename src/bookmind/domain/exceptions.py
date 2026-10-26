"""Исключения BookMind."""


class BookMindError(Exception):
    """Базовое исключение приложения."""


class OllamaUnavailableError(BookMindError):
    """Сервер Ollama недоступен (не запущен или неверный адрес)."""


class OllamaRequestError(BookMindError):
    """Ollama вернул ошибку на запрос."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class ModelNotInstalledError(BookMindError):
    """Запрошенная модель не установлена в Ollama."""


class CatalogError(BookMindError):
    """Некорректный каталог моделей."""


class CancelledError(BookMindError):
    """Операция отменена пользователем."""
