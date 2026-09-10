from __future__ import annotations

from dataclasses import dataclass, field
import threading


class DownloadError(Exception):
    """User-facing download failure."""


class PrivateContentError(DownloadError):
    def __init__(self) -> None:
        super().__init__(
            "Это видео недоступно публично (приватный аккаунт, закрытый ролик "
            "или нужна авторизация). Бот работает только с открытыми ссылками."
        )


class FileTooLargeError(DownloadError):
    def __init__(self, size_mb: float, limit_mb: int, *, using_local_api: bool) -> None:
        extra = (
            " Выберите более низкое качество."
            if using_local_api
            else (
                " Через облако Telegram лимит ~50 МБ. Для файлов до 2 ГБ "
                "поднимите локальный Bot API — см. README."
            )
        )
        super().__init__(
            f"Файл получился {size_mb:.0f} МБ, лимит отправки {limit_mb} МБ.{extra}"
        )


class DownloadCancelled(DownloadError):
    def __init__(self) -> None:
        super().__init__("Скачивание отменено.")


@dataclass
class QualityOption:
    height: int
    label: str
    approx_bytes: int | None = None
    over_limit: bool = False

    @property
    def callback(self) -> str:
        return f"dl:{self.height}"


@dataclass
class VideoInfo:
    url: str
    title: str
    duration: int | None
    extractor: str
    qualities: list[QualityOption] = field(default_factory=list)
    has_audio: bool = False
    thumbnail_url: str | None = None


@dataclass
class Job:
    id: str
    user_id: int
    chat_id: int
    message_id: int
    url: str
    title: str
    extractor: str
    duration: int | None
    height: int | None
    label: str
    fallback_heights: list[int]
    cancel: threading.Event = field(default_factory=threading.Event)
    auto: bool = False
    notice: str = ""
