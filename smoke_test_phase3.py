"""Offline end-to-end smoke test for the phase-3 Qt completion path.

No real API key or network request is used. The fake provider exercises the
50-to-30 selection stage, two validation failures, draft-aware repair, translation,
database persistence, and the Qt completion path.
"""

from __future__ import annotations

import os
import json
import re
import tempfile
import time
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QTimer, Qt, QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from ai_parser import AIConfig, CompatibleChatClient  # noqa: E402
from database import VocabularyRepository  # noqa: E402
from main import MainWindow  # noqa: E402


def _test_words(count: int) -> list[str]:
    words: list[str] = []
    for index in range(count):
        first = chr(97 + (index // (26 * 26)) % 26)
        second = chr(97 + (index // 26) % 26)
        third = chr(97 + index % 26)
        words.append(f"v{first}{second}{third}".replace("v", "vocab"))
    return words


def main() -> int:
    workspace = Path(tempfile.mkdtemp(prefix="podcast-smoke-"))
    repository = VocabularyRepository(workspace / "smoke.db")
    repository.initialize()
    repository.import_cleaned_values(_test_words(1200), "smoke.csv")
    for vocabulary_id in range(1, 101):
        repository.update_word(vocabulary_id, "status", 1 + vocabulary_id % 4)
    baseline_status_sum = sum(
        int(row["status"]) for row in repository.fetch_vocabulary()
    )

    validator = CompatibleChatClient(AIConfig(api_key="offline-smoke"))
    validation_candidates = [
        {"id": index + 1, "word": word, "status": 0}
        for index, word in enumerate(_test_words(30))
    ]

    def validation_payload(total_words: int) -> dict[str, Any]:
        used = [str(row["word"]) for row in validation_candidates[:25]]
        tokens = [f"**{word}**" for word in used]
        tokens.extend(["context"] * (total_words - len(tokens)))
        return {
            "title": "Boundary Story",
            "content_en": " ".join(tokens),
            "matched_words": used,
        }

    boundary_rules_passed = all(
        len(validator._validate_podcast(  # noqa: SLF001 - intentional smoke coverage
            validation_payload(count),
            validation_candidates,
            require_translation=False,
        )["matched_words"]) == 25
        for count in (330, 480)
    )
    for rejected_count in (329, 481):
        try:
            validator._validate_podcast(  # noqa: SLF001 - intentional smoke coverage
                validation_payload(rejected_count),
                validation_candidates,
                require_translation=False,
            )
        except RuntimeError:
            continue
        boundary_rules_passed = False

    original_request = CompatibleChatClient._request_with_retry
    fake_calls = {
        "selection": 0,
        "english": 0,
        "translation": 0,
        "feedback_received": False,
        "draft_carried_forward": False,
        "translation_repair_received": False,
    }

    class FakeResponse:
        def __init__(
            self,
            content: dict[str, Any],
            finish_reason: str = "stop",
        ) -> None:
            self._content = content
            self._finish_reason = finish_reason

        def json(self) -> dict[str, Any]:
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(self._content, ensure_ascii=False)
                        },
                        "finish_reason": self._finish_reason,
                    }
                ]
            }

    def fake_request(
        client: CompatibleChatClient,
        payload: dict[str, Any],
        headers: dict[str, str],
        cancel_event: Any,
    ) -> FakeResponse:
        del client, headers, cancel_event
        time.sleep(0.06)  # Simulate each blocking HTTP call in the worker QThread.
        schema_name = str(
            payload.get("response_format", {}).get("json_schema", {}).get("name", "")
        )
        if schema_name == "podcast_translation":
            fake_calls["translation"] += 1
            user_content = str(payload["messages"][-1]["content"])
            if fake_calls["translation"] == 1:
                translated_words = re.findall(
                    r'"word"\s*:\s*"([A-Za-z]+)"',
                    user_content,
                )
                residue = translated_words[0] if translated_words else "vocabulary"
                return FakeResponse({"content_cn": f"中文翻译仍残留 **{residue}**"})
            fake_calls["translation_repair_received"] = "仍残留" in user_content
            return FakeResponse({"content_cn": "离线冒烟测试中文翻译"})
        if schema_name == "podcast_word_selection":
            fake_calls["selection"] += 1
            user_content = str(payload["messages"][-1]["content"])
            words = re.findall(r'"word"\s*:\s*"([A-Za-z]+)"', user_content)
            return FakeResponse({"selected_words": words[:30]})
        fake_calls["english"] += 1
        user_content = str(payload["messages"][-1]["content"])
        if fake_calls["english"] >= 3 and "超过 3 次" in user_content:
            fake_calls["feedback_received"] = True
        if fake_calls["english"] >= 2 and "PREVIOUS INVALID DRAFT" in user_content:
            fake_calls["draft_carried_forward"] = True
        words = re.findall(r'"word"\s*:\s*"([A-Za-z]+)"', user_content)
        if fake_calls["english"] == 1:
            story_words = [f"**{word}**" for word in words]
            target_count = 320  # Below the relaxed 330-word hard floor.
        elif fake_calls["english"] == 2:
            used = words[:25]
            story_words = [f"**{word}**" for word in used]
            story_words.extend([f"**{used[0]}**"] * 3)  # Four occurrences: reject.
            target_count = 400
        else:
            used = words[:25]
            story_words = [f"**{word}**" for word in used]
            for word in used[:5]:
                story_words.extend([f"**{word}**", f"**{word}**"])
            target_count = 400  # 25 distinct words; five occur exactly three times.
        filler_count = target_count - len(story_words)
        return FakeResponse({
            "title": "Offline Smoke Story",
            "narrative_blueprint": [
                {"beat": name, "planned_words": 107, "assigned_words": []}
                for name in ("setup", "development", "turn", "resolution")
            ],
            "content_en": " ".join(
                story_words + ["story"] * filler_count
            ),
            "matched_words": words,
        })

    CompatibleChatClient._request_with_retry = fake_request
    app = QApplication([])
    window = MainWindow(repository)
    window.show()

    started = time.perf_counter()
    last_tick = started
    max_gap = 0.0
    ticks = 0
    generation_started = False
    history_opened = False
    history_first_seen = False
    instant_story_visible = False
    reverse_started = False
    reverse_target_id: int | None = None
    checkbox_selection_priority_ok = False
    table_ready_at: float | None = None
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
        nonlocal generation_started, history_opened, reverse_started, outcome
        nonlocal reverse_target_id
        nonlocal history_first_seen, instant_story_visible
        nonlocal last_tick, max_gap, ticks
        nonlocal table_ready_at
        nonlocal checkbox_selection_priority_ok
        elapsed = time.perf_counter() - started
        if not generation_started and window.table.rowCount() == 1200:
            if not checkbox_selection_priority_ok:
                checked_row = 2
                window.table.selectRow(0)
                window.table.item(checked_row, 0).setCheckState(
                    Qt.CheckState.Checked
                )
                checked_id = int(window.table.item(checked_row, 1).text())
                checkbox_selection_priority_ok = (
                    window._selected_vocabulary_ids() == [checked_id]
                )
                window.table.item(checked_row, 0).setCheckState(
                    Qt.CheckState.Unchecked
                )
            if table_ready_at is None:
                table_ready_at = time.perf_counter()
                return
            if time.perf_counter() - table_ready_at < 0.7:
                return
            generation_started = True
            last_tick = time.perf_counter()
            max_gap = 0.0
            ticks = 0
            window._launch_podcast_worker(AIConfig(api_key="offline-smoke"))
            return

        stories = repository.fetch_podcasts()
        history = window._history_dialog
        if history is not None:
            if not history_first_seen:
                history_first_seen = True
                instant_story_visible = (
                    history.table.rowCount() == 1
                    and "Offline Smoke Story" in history.english_view.toPlainText()
                )
            history_opened = True
        elif (
            generation_started
            and not history_opened
            and stories
            and window._podcast_worker is None
        ):
            history_opened = True
            window.open_podcast_history(int(stories[0]["id"]))
            return
        if (
            generation_started
            and not reverse_started
            and stories
            and history is not None
            and history.table.rowCount() > 0
            and window._podcast_worker is None
        ):
            reverse_started = True
            story = repository.fetch_podcast(int(stories[0]["id"]))
            first = story["used_words"][0]
            reverse_target_id = int(first["id"])
            history._word_link_clicked(QUrl(f"vocab:{int(first['id'])}"))
            return

        if (
            reverse_started
            and reverse_target_id is not None
            and window._pending_focus_record is None
            and not window._threads
        ):
            story = repository.fetch_podcast(int(stories[0]["id"]))
            updated = repository.fetch_vocabulary()
            status_increment = (
                sum(int(row["status"]) for row in updated) - baseline_status_sum
            )
            used_word_texts = [str(item["word"]) for item in story["used_words"]]
            first_five_repeat_rule_ok = all(
                len(re.findall(rf"\b{re.escape(word)}\b", story["content_en"], re.I)) == 3
                and len(re.findall(
                    rf"\*\*\s*{re.escape(word)}\s*\*\*",
                    story["content_en"],
                    re.I,
                )) == 1
                for word in used_word_texts[:5]
            )
            selected_rows = window.table.selectionModel().selectedRows()
            selected_vocabulary_id = (
                int(window.table.item(selected_rows[0].row(), 1).text())
                if selected_rows
                else None
            )
            lightweight_navigation_ok = (
                selected_vocabulary_id == reverse_target_id
                and history.isVisible()
                and window._detail_dialog is None
            )
            outcome = {
                "passed": (
                    len(story["used_words"]) == 25
                    and status_increment == 25
                    and window.table.rowCount() == 1
                    and history.table.rowCount() == 1
                    and instant_story_visible
                    and fake_calls["selection"] == 1
                    and fake_calls["english"] == 3
                    and fake_calls["translation"] == 2
                    and bool(fake_calls["translation_repair_received"])
                    and bool(fake_calls["feedback_received"])
                    and bool(fake_calls["draft_carried_forward"])
                    and first_five_repeat_rule_ok
                    and boundary_rules_passed
                    and lightweight_navigation_ok
                    and checkbox_selection_priority_ok
                    and ticks >= 5
                    and max_gap < 2.0
                ),
                "candidate_count": 50,
                "used_word_count": len(story["used_words"]),
                "status_increment": status_increment,
                "table_rows": window.table.rowCount(),
                "history_rows": history.table.rowCount() if history else 0,
                "story_visible": (
                    "Offline Smoke Story" in history.english_view.toPlainText()
                    if history else False
                ),
                "story_visible_on_first_history_frame": instant_story_visible,
                "selection_calls": fake_calls["selection"],
                "english_generation_calls": fake_calls["english"],
                "translation_calls": fake_calls["translation"],
                "translation_repair_received": fake_calls[
                    "translation_repair_received"
                ],
                "retry_received_validation_feedback": fake_calls["feedback_received"],
                "retry_received_previous_draft": fake_calls["draft_carried_forward"],
                "repeat_up_to_three_first_bold_only": first_five_repeat_rule_ok,
                "word_count_boundaries_330_480": boundary_rules_passed,
                "word_located_in_main_table": selected_vocabulary_id
                == reverse_target_id,
                "history_remained_visible": history.isVisible(),
                "detail_dialog_suppressed": window._detail_dialog is None,
                "checked_rows_override_blue_selection": (
                    checkbox_selection_priority_ok
                ),
                "event_loop_ticks": ticks,
                "max_event_loop_gap_ms": round(max_gap * 1000, 1),
            }
            monitor.stop()
            heartbeat.stop()
            window.close()
            app.quit()
            return

        if elapsed > 20:
            monitor.stop()
            heartbeat.stop()
            app.quit()

    monitor.timeout.connect(check)
    monitor.start()
    app.exec()
    CompatibleChatClient._request_with_retry = original_request
    print(outcome)
    return 0 if outcome.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
