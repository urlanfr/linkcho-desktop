"""Offline stage-4 smoke test: migration, TTS worker, persistence and player UI."""

from __future__ import annotations

import os
import hashlib
import sqlite3
import tempfile
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer, QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication, QPushButton  # noqa: E402

from database import VocabularyRepository  # noqa: E402
from main import PodcastHistoryDialog, TaskThread  # noqa: E402
from tts_service import (  # noqa: E402
    DEFAULT_VOICE,
    AudioResult,
    EdgeTTSService,
    clean_tts_text,
    generate_word_pronunciation,
)


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="tts-smoke-"))
    database_path = workspace / "legacy.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE podcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                content_en TEXT NOT NULL,
                content_cn TEXT NOT NULL,
                used_words TEXT NOT NULL DEFAULT '[]',
                style TEXT,
                created_date TEXT NOT NULL
            )
            """
        )

    repository = VocabularyRepository(database_path)
    repository.initialize()
    with sqlite3.connect(database_path) as connection:
        migrated_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(podcasts)")
        }
    migration_ok = {"audio_path", "audio_duration", "audio_status"}.issubset(
        migrated_columns
    )

    words = [f"stageword{chr(97 + index)}" for index in range(25)]
    repository.import_cleaned_values(words, "stage4-smoke.csv")
    initial_candidates = repository.fetch_vocabulary()[:25]
    repository.update_word(int(initial_candidates[0]["id"]), "definition", "测试译词")
    candidates = repository.fetch_vocabulary()[:25]
    story = repository.save_podcast_and_update_words(
        {
            "title": "Stage Four Offline Story",
            "content_en": " ".join(f"**{word}**" for word in words),
            "content_cn": f"阶段四翻译仍残留 **{words[0]}**。",
            "matched_words": words,
        },
        candidates,
        "smoke",
    )
    clean_ok = clean_tts_text("A **clear** voice with **no markdown**.") == (
        "A clear voice with no markdown."
    )
    pronunciation_cache = workspace / "pronunciation_cache"
    pronunciation_cache.mkdir()
    pronunciation_digest = hashlib.sha256(
        f"{DEFAULT_VOICE.casefold()}\0model".encode("utf-8")
    ).hexdigest()[:20]
    cached_pronunciation = pronunciation_cache / (
        f"word_{pronunciation_digest}.mp3"
    )
    cached_pronunciation.write_bytes(b"ID3-cached-pronunciation")
    pronunciation_cache_ok = (
        generate_word_pronunciation("model", pronunciation_cache)
        == str(cached_pronunciation.resolve())
    )

    original_generate = EdgeTTSService.generate_audio

    def fake_generate(
        service: EdgeTTSService,
        podcast_id: int,
        content_en: str,
        cancel_event: Any = None,
    ) -> AudioResult:
        del content_en, cancel_event
        time.sleep(0.12)
        service.cache_directory.mkdir(parents=True, exist_ok=True)
        path = service.cache_directory / f"podcast_{podcast_id}.mp3"
        path.write_bytes(b"ID3-offline-smoke")
        return AudioResult(podcast_id, str(path), 183.25, service.voice)

    EdgeTTSService.generate_audio = fake_generate
    app = QApplication([])
    active_tasks: set[TaskThread] = set()

    def run_task(task: Any, on_success: Any, on_error: Any = None) -> TaskThread:
        worker = TaskThread(task)
        active_tasks.add(worker)
        worker.succeeded.connect(on_success)
        if on_error is not None:
            worker.failed.connect(lambda message, _details: on_error(message))
        worker.finished.connect(lambda: active_tasks.discard(worker))
        worker.finished.connect(worker.deleteLater)
        worker.start()
        return worker

    history = PodcastHistoryDialog(
        repository,
        run_task,
        workspace / "audio_cache",
    )
    history.show()
    history.reload(int(story["id"]), story)
    requested_vocabulary: list[tuple[int, str]] = []
    history.vocabulary_requested.connect(
        lambda vocabulary_id, word: requested_vocabulary.append(
            (int(vocabulary_id), str(word))
        )
    )

    started = time.perf_counter()
    clicked = False
    max_gap = 0.0
    last_tick = started
    ticks = 0
    outcome: dict[str, Any] = {"passed": False, "reason": "timeout"}

    heartbeat = QTimer()
    heartbeat.setInterval(20)

    def beat() -> None:
        nonlocal last_tick, max_gap, ticks
        now = time.perf_counter()
        max_gap = max(max_gap, now - last_tick)
        last_tick = now
        ticks += 1

    heartbeat.timeout.connect(beat)
    heartbeat.start()
    monitor = QTimer()
    monitor.setInterval(40)

    def check() -> None:
        nonlocal clicked, outcome
        if not clicked and history.table.rowCount() == 1:
            controls = history.table.cellWidget(0, 5)
            buttons = controls.findChildren(QPushButton) if controls is not None else []
            generate_button = next(
                (button for button in buttons if "生成音频" in button.text()),
                None,
            )
            if generate_button is not None:
                clicked = True
                generate_button.click()
                return

        player = history._player_dialog
        saved = repository.fetch_podcast(int(story["id"]))
        audio_path = Path(str(saved.get("audio_path") or ""))
        controls = history.table.cellWidget(0, 5)
        button_texts = (
            [button.text() for button in controls.findChildren(QPushButton)]
            if controls is not None
            else []
        )
        button_ready = "▶ 播放" in button_texts and "⬇ 下载" in button_texts
        if (
            clicked
            and player is not None
            and audio_path.is_file()
            and not history._tts_workers
            and not active_tasks
        ):
            player._position_changed(round(float(saved["audio_duration"]) * 500))
            embedded_player_shown = (
                player.embedded
                and history.pages.currentWidget() is player
            )
            first_word_id = int(saved["used_words"][0]["id"])
            player._word_link_clicked(QUrl(f"vocab:{first_word_id}"))
            linked_word_count = player.english_view.toHtml().count("vocab:")
            sentence_highlighted = (
                player._active_segment_index >= 0
                and "#fff1a8" in player.english_view.toHtml().lower()
            )
            chinese_sentence_highlighted = (
                player._active_chinese_segment_index >= 0
                and "#fff1a8" in player.chinese_view.toHtml().lower()
            )
            chinese_html = player.chinese_view.toHtml().lower()
            chinese_cleaned = (
                words[0].lower() not in chinese_html
                and "测试译词" in player.chinese_view.toPlainText()
                and "**" not in player.chinese_view.toPlainText()
            )
            playback_navigation_preserved = (
                player.isVisible()
                and history.isVisible()
                and requested_vocabulary
                and requested_vocabulary[-1][0] == first_word_id
            )
            player.back_requested.emit()
            returned_to_library = (
                history.pages.currentWidget() is history.library_page
            )
            outcome = {
                "passed": (
                    migration_ok
                    and clean_ok
                    and pronunciation_cache_ok
                    and abs(float(saved["audio_duration"]) - 183.25) < 0.01
                    and saved["audio_status"] == "generated"
                    and button_ready
                    and linked_word_count == 25
                    and sentence_highlighted
                    and chinese_sentence_highlighted
                    and chinese_cleaned
                    and playback_navigation_preserved
                    and embedded_player_shown
                    and returned_to_library
                    and ticks >= 4
                    and max_gap < 1.5
                ),
                "migration_columns": sorted(
                    {"audio_path", "audio_duration", "audio_status"}
                    & migrated_columns
                ),
                "markdown_cleaned": clean_ok,
                "pronunciation_cache_reused": pronunciation_cache_ok,
                "audio_path_exists": audio_path.is_file(),
                "audio_duration": saved["audio_duration"],
                "audio_status": saved["audio_status"],
                "history_button_ready": button_ready,
                "player_created": player is not None,
                "linked_word_count": linked_word_count,
                "sentence_highlighted": sentence_highlighted,
                "chinese_sentence_highlighted": chinese_sentence_highlighted,
                "historical_chinese_cleaned": chinese_cleaned,
                "playback_navigation_preserved": bool(
                    playback_navigation_preserved
                ),
                "embedded_player_shown": embedded_player_shown,
                "returned_to_same_library_window": returned_to_library,
                "event_loop_ticks": ticks,
                "max_event_loop_gap_ms": round(max_gap * 1000, 1),
            }
            monitor.stop()
            heartbeat.stop()
            player.close()
            history.close()
            app.quit()
            return
        if time.perf_counter() - started > 12:
            monitor.stop()
            heartbeat.stop()
            history.close()
            app.quit()

    monitor.timeout.connect(check)
    monitor.start()
    app.exec()
    EdgeTTSService.generate_audio = original_generate
    print(outcome)
    return 0 if outcome.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
