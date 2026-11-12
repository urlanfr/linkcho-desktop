"""Background Edge-TTS synthesis and MP3 metadata helpers."""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QThread, pyqtSignal


DEFAULT_VOICE = "en-US-ChristopherNeural"
ALTERNATE_VOICE = "en-US-AriaNeural"


def clean_tts_text(text: str) -> str:
    """Strip Markdown emphasis and normalize whitespace before synthesis."""

    cleaned = re.sub(r"\*\*(.+?)\*\*", r"\1", str(text), flags=re.DOTALL)
    cleaned = cleaned.replace("**", "")
    cleaned = re.sub(r"[ \t]+", " ", cleaned)
    cleaned = re.sub(r"\s*\n\s*", "\n", cleaned)
    return cleaned.strip()


def generate_word_pronunciation(
    word: str,
    cache_directory: str | Path,
    voice: str = DEFAULT_VOICE,
) -> str:
    """Generate or reuse a small pronunciation MP3 for one English word."""

    normalized = re.sub(r"\s+", " ", str(word)).strip()
    if not normalized:
        raise ValueError("单词为空，无法生成发音")
    try:
        import edge_tts
    except ImportError as exc:
        raise RuntimeError("缺少 edge-tts，请先运行：pip install edge-tts") from exc

    directory = Path(cache_directory).expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(
        f"{voice.casefold()}\0{normalized.casefold()}".encode("utf-8")
    ).hexdigest()[:20]
    final_path = directory / f"word_{digest}.mp3"
    if final_path.is_file() and final_path.stat().st_size > 0:
        return str(final_path)
    temporary_path = directory / f"word_{digest}.tmp.mp3"
    temporary_path.unlink(missing_ok=True)

    async def synthesize() -> None:
        communicator = edge_tts.Communicate(normalized, voice)
        await communicator.save(str(temporary_path))

    try:
        asyncio.run(synthesize())
        if not temporary_path.is_file() or temporary_path.stat().st_size == 0:
            raise RuntimeError("TTS 服务未生成有效的单词发音")
        temporary_path.replace(final_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    return str(final_path)


@dataclass(frozen=True)
class AudioResult:
    podcast_id: int
    audio_path: str
    audio_duration: float
    voice: str


class EdgeTTSService:
    """Generate one MP3 with Edge-TTS and measure its real duration."""

    def __init__(
        self,
        cache_directory: str | Path,
        voice: str = DEFAULT_VOICE,
    ) -> None:
        self.cache_directory = Path(cache_directory).expanduser().resolve()
        self.voice = voice.strip() or DEFAULT_VOICE

    def generate_audio(
        self,
        podcast_id: int,
        content_en: str,
        cancel_event: threading.Event | None = None,
    ) -> AudioResult:
        text = clean_tts_text(content_en)
        if not text:
            raise ValueError("英文正文为空，无法生成音频")
        if cancel_event is not None and cancel_event.is_set():
            raise RuntimeError("音频生成已取消")

        try:
            import edge_tts
        except ImportError as exc:
            raise RuntimeError(
                "缺少 edge-tts，请先运行：pip install edge-tts"
            ) from exc
        try:
            from mutagen.mp3 import MP3
        except ImportError as exc:
            raise RuntimeError(
                "缺少 mutagen，请先运行：pip install mutagen"
            ) from exc

        self.cache_directory.mkdir(parents=True, exist_ok=True)
        final_path = self.cache_directory / f"podcast_{int(podcast_id)}.mp3"
        temporary_path = self.cache_directory / f"podcast_{int(podcast_id)}.tmp.mp3"
        temporary_path.unlink(missing_ok=True)

        async def synthesize() -> None:
            communicator = edge_tts.Communicate(text, self.voice)
            await communicator.save(str(temporary_path))

        try:
            asyncio.run(synthesize())
            if cancel_event is not None and cancel_event.is_set():
                temporary_path.unlink(missing_ok=True)
                raise RuntimeError("音频生成已取消")
            if not temporary_path.exists() or temporary_path.stat().st_size == 0:
                raise RuntimeError("TTS 服务未生成有效的 MP3 文件")
            temporary_path.replace(final_path)
            duration = float(MP3(str(final_path)).info.length)
            if duration <= 0:
                raise RuntimeError("无法读取生成音频的有效时长")
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise

        return AudioResult(
            podcast_id=int(podcast_id),
            audio_path=str(final_path),
            audio_duration=duration,
            voice=self.voice,
        )


class TTSGenerationWorker(QThread):
    """Run Edge-TTS and database persistence entirely off the GUI thread."""

    stage_changed = pyqtSignal(str)
    completed = pyqtSignal(object)
    failed = pyqtSignal(int, str)

    def __init__(
        self,
        repository: Any,
        podcast_id: int,
        content_en: str,
        cache_directory: str | Path,
        voice: str = DEFAULT_VOICE,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.podcast_id = int(podcast_id)
        self.content_en = content_en
        self.service = EdgeTTSService(cache_directory, voice)
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        try:
            self.repository.update_podcast_audio_status(self.podcast_id, "generating")
            self.stage_changed.emit("正在连接 Edge-TTS 并生成美音 MP3…")
            result = self.service.generate_audio(
                self.podcast_id,
                self.content_en,
                self._cancel_event,
            )
            if self._cancel_event.is_set():
                self.repository.update_podcast_audio_status(
                    self.podcast_id,
                    "not_generated",
                )
                return
            self.stage_changed.emit("正在读取音频时长并写回历史库…")
            story = self.repository.update_podcast_audio(
                self.podcast_id,
                result.audio_path,
                result.audio_duration,
            )
            self.completed.emit(story)
        except Exception as exc:
            try:
                self.repository.update_podcast_audio_status(self.podcast_id, "failed")
            except Exception:
                pass
            self.failed.emit(self.podcast_id, str(exc))
