"""SQLite persistence and vocabulary cleaning for the podcast vocabulary app.

Every public repository method opens its own short-lived connection.  This makes
the repository safe to call from independent QThreads and leaves a clean seam
for later AI, script-generation, and TTS services.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


WORD_PATTERN = re.compile(r"^[A-Za-z]+$")

VOCABULARY_FIELDS = (
    "word",
    "soundMark",
    "definition",
    "collocation",
    "relatedWords",
    "exampleSentence",
    "status",
    "last_used_date",
    "notes",
)

EDITABLE_FIELDS = frozenset(VOCABULARY_FIELDS)
AI_CORE_FIELDS = (
    "soundMark",
    "definition",
    "collocation",
    "relatedWords",
    "exampleSentence",
)

# ``wordfreq`` is the primary offline corpus scorer.  The compact fallback keeps
# database tests and degraded environments deterministic; a normal installation
# uses the much broader corpus supplied by the dependency in requirements.txt.
_FALLBACK_FREQUENCY_ORDER = (
    "the", "be", "to", "of", "and", "a", "in", "that", "have", "i",
    "it", "for", "not", "on", "with", "he", "as", "you", "do", "at",
    "this", "but", "his", "by", "from", "they", "we", "say", "her", "she",
    "or", "an", "will", "my", "one", "all", "would", "there", "their", "what",
    "so", "up", "out", "if", "about", "who", "get", "which", "go", "me",
)
_FALLBACK_FREQUENCY = {
    word: float(len(_FALLBACK_FREQUENCY_ORDER) - index)
    for index, word in enumerate(_FALLBACK_FREQUENCY_ORDER)
}


@dataclass(frozen=True)
class ImportSummary:
    """Result returned after one atomic import operation."""

    inserted_count: int
    quarantined_count: int
    duplicate_count: int
    valid_count: int


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_word(value: Any) -> str:
    """Return the canonical form stored in the vocabulary table."""

    return str(value).strip().lower()


def validate_word(value: Any) -> tuple[bool, str, str]:
    """Validate one English word.

    Stage 1 intentionally uses a strict ASCII-only rule. Spaces, hyphens,
    apostrophes, digits, punctuation and sentence-like input are quarantined.
    Returns ``(is_valid, normalized_value, reason)``.
    """

    raw = "" if value is None else str(value).strip()
    if not raw:
        return False, "", "空白内容"
    if not WORD_PATTERN.fullmatch(raw):
        return False, raw, "仅允许不含空格、连字符或标点的英文字母单词"
    return True, normalize_word(raw), ""


def english_frequency_score(word: str) -> float:
    """Return an offline everyday-English corpus frequency score."""

    try:
        from wordfreq import zipf_frequency

        return float(zipf_frequency(word, "en"))
    except ImportError:
        return _FALLBACK_FREQUENCY.get(word, 0.0)


def _partition_import_values(
    raw_values: Iterable[Any],
    source_file: str,
) -> tuple[list[str], list[tuple[str, str, str]]]:
    valid_words: list[str] = []
    quarantined: list[tuple[str, str, str]] = []
    for value in raw_values:
        raw_text = "" if value is None else str(value).strip()
        if not raw_text:
            continue
        is_valid, normalized, reason = validate_word(raw_text)
        if is_valid:
            valid_words.append(normalized)
        else:
            quarantined.append((raw_text, reason, source_file))
    return valid_words, quarantined


def _unique_in_input_order(words: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(words))


def _frequency_sort(words: Iterable[str]) -> list[str]:
    # Python's sort is stable, so equal-frequency words retain their file order.
    return sorted(words, key=english_frequency_score, reverse=True)


class VocabularyRepository:
    """Thread-friendly repository for vocabulary and quarantine data."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path).expanduser().resolve()

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """Create the database and run all idempotent schema migrations."""

        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
            connection.executescript(
                """
                BEGIN;

                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS vocabulary (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    word TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    soundMark TEXT,
                    definition TEXT,
                    collocation TEXT,
                    relatedWords TEXT,
                    exampleSentence TEXT,
                    status INTEGER NOT NULL DEFAULT 0 CHECK (status >= -1),
                    last_used_date TEXT,
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS quarantine (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    raw_text TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    source_file TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS podcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    content_en TEXT NOT NULL,
                    content_cn TEXT NOT NULL,
                    used_words TEXT NOT NULL DEFAULT '[]',
                    style TEXT,
                    created_date TEXT NOT NULL,
                    audio_path TEXT,
                    audio_duration REAL,
                    audio_status TEXT NOT NULL DEFAULT 'not_generated'
                );

                CREATE INDEX IF NOT EXISTS idx_vocabulary_status
                    ON vocabulary(status);
                CREATE INDEX IF NOT EXISTS idx_vocabulary_word
                    ON vocabulary(word COLLATE NOCASE);
                CREATE INDEX IF NOT EXISTS idx_quarantine_created_at
                    ON quarantine(created_at);
                CREATE INDEX IF NOT EXISTS idx_podcasts_created_date
                    ON podcasts(created_date DESC);
                CREATE INDEX IF NOT EXISTS idx_podcasts_title
                    ON podcasts(title COLLATE NOCASE);

                INSERT INTO schema_meta(key, value)
                VALUES ('schema_version', '4')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value;

                COMMIT;
                """
            )
            # CREATE TABLE IF NOT EXISTS does not add columns to an existing
            # stage-3 database, so perform idempotent stage-4 migrations.
            podcast_columns = {
                str(row["name"])
                for row in connection.execute("PRAGMA table_info(podcasts)").fetchall()
            }
            if "audio_path" not in podcast_columns:
                connection.execute("ALTER TABLE podcasts ADD COLUMN audio_path TEXT")
            if "audio_duration" not in podcast_columns:
                connection.execute("ALTER TABLE podcasts ADD COLUMN audio_duration REAL")
            if "audio_status" not in podcast_columns:
                connection.execute(
                    "ALTER TABLE podcasts ADD COLUMN audio_status "
                    "TEXT NOT NULL DEFAULT 'not_generated'"
                )
            connection.execute(
                """
                INSERT INTO schema_meta(key, value)
                VALUES ('schema_version', '4')
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """
            )

    def select_podcast_candidates(
        self,
        minimum: int = 50,
        maximum: int = 50,
        rng: random.Random | None = None,
    ) -> list[dict[str, Any]]:
        """Draw a weighted candidate box without ever selecting archived words.

        The default box contains exactly 50 words. New words receive roughly
        80% of the box. Review words fill the other
        20%, sampled without replacement with ``1 / status`` weight so a word
        used once is more likely than one used many times. Pool shortages are
        filled from the other eligible pool; existing IDs and row order are not
        changed.
        """

        if minimum < 1 or maximum < minimum:
            raise ValueError("候选词数量范围无效")
        chooser = rng or random.SystemRandom()
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, word, soundMark, definition, collocation,
                       relatedWords, exampleSentence, status, notes
                FROM vocabulary
                WHERE status >= 0
                ORDER BY id ASC
                """
            ).fetchall()
        eligible = [dict(row) for row in rows]
        if len(eligible) < minimum:
            raise ValueError(
                f"可用且未归档的单词只有 {len(eligible)} 个；"
                f"固定候选池需要至少 {minimum} 个"
            )

        target = min(len(eligible), chooser.randint(minimum, maximum))
        new_words = [row for row in eligible if int(row["status"]) == 0]
        review_words = [row for row in eligible if int(row["status"]) >= 1]
        new_target = min(len(new_words), round(target * 0.8))
        review_target = min(len(review_words), target - new_target)

        # If one pool is short, fill the box from the other one.
        missing = target - new_target - review_target
        if missing:
            extra_new = min(missing, len(new_words) - new_target)
            new_target += extra_new
            missing -= extra_new
        if missing:
            review_target += min(missing, len(review_words) - review_target)

        selected_new = chooser.sample(new_words, new_target) if new_target else []
        selected_review = self._weighted_sample_without_replacement(
            review_words,
            review_target,
            chooser,
        )
        result = selected_new + selected_review
        chooser.shuffle(result)
        return result

    @staticmethod
    def _weighted_sample_without_replacement(
        rows: Sequence[dict[str, Any]],
        count: int,
        rng: random.Random,
    ) -> list[dict[str, Any]]:
        pool = list(rows)
        selected: list[dict[str, Any]] = []
        for _ in range(min(count, len(pool))):
            weights = [1.0 / max(1, int(row["status"])) for row in pool]
            pick = rng.choices(range(len(pool)), weights=weights, k=1)[0]
            selected.append(pool.pop(pick))
        return selected

    def save_podcast_and_update_words(
        self,
        result: Mapping[str, Any],
        candidates: Sequence[Mapping[str, Any]],
        style: str = "",
    ) -> dict[str, Any]:
        """Atomically save a story and close the vocabulary status loop."""

        title = str(result.get("title", "")).strip()
        content_en = str(result.get("content_en", "")).strip()
        content_cn = str(result.get("content_cn", "")).strip()
        raw_matched = result.get("matched_words", [])
        if not title or not content_en or not content_cn:
            raise ValueError("生成结果缺少标题、英文正文或中文翻译")
        if not isinstance(raw_matched, list):
            raise ValueError("生成结果中的 matched_words 必须是数组")

        candidate_by_word = {
            str(row["word"]).strip().casefold(): {
                "id": int(row["id"]),
                "word": str(row["word"]).strip(),
            }
            for row in candidates
        }
        used_words: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in raw_matched:
            key = str(value).strip().casefold()
            if key and key in candidate_by_word and key not in seen:
                used_words.append(candidate_by_word[key])
                seen.add(key)
        if not 25 <= len(used_words) <= 30:
            raise ValueError(
                f"模型实际采用 {len(used_words)} 个有效候选词，必须为 25-30 个"
            )

        now = utc_now_text()
        used_ids = [int(item["id"]) for item in used_words]
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                placeholders = ",".join("?" for _ in used_ids)
                current_rows = connection.execute(
                    f"SELECT id, word, status FROM vocabulary WHERE id IN ({placeholders})",
                    used_ids,
                ).fetchall()
                current_by_id = {int(row["id"]): row for row in current_rows}
                if any(
                    item["id"] not in current_by_id
                    or str(current_by_id[item["id"]]["word"]).casefold()
                    != str(item["word"]).casefold()
                    or int(current_by_id[item["id"]]["status"]) < 0
                    for item in used_words
                ):
                    raise ValueError(
                        "词库在生成期间发生了删除、改名或归档；本次结果未入库，请重新生成"
                    )
                cursor = connection.execute(
                    """
                    INSERT INTO podcasts(
                        title, content_en, content_cn, used_words, style, created_date
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        title,
                        content_en,
                        content_cn,
                        json.dumps(used_words, ensure_ascii=False),
                        style.strip(),
                        now,
                    ),
                )
                connection.execute(
                    f"""
                    UPDATE vocabulary
                    SET status = status + 1,
                        last_used_date = ?,
                        updated_at = ?
                    WHERE id IN ({placeholders}) AND status >= 0
                    """,
                    (datetime.now().astimezone().date().isoformat(), now, *used_ids),
                )
                podcast_id = int(cursor.lastrowid)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return {
            "id": podcast_id,
            "title": title,
            "content_en": content_en,
            "content_cn": content_cn,
            "used_words": used_words,
            "style": style.strip(),
            "created_date": now,
        }

    def fetch_podcasts(self, search_text: str = "") -> list[dict[str, Any]]:
        parameters: list[Any] = []
        where_sql = ""
        if search_text.strip():
            pattern = f"%{search_text.strip()}%"
            where_sql = "WHERE title LIKE ? OR content_en LIKE ? OR content_cn LIKE ?"
            parameters.extend((pattern, pattern, pattern))
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT id, title, used_words, style, created_date,
                       audio_path, audio_duration, audio_status
                FROM podcasts
                {where_sql}
                ORDER BY created_date DESC, id DESC
                """,
                parameters,
            ).fetchall()
        result = [dict(row) for row in rows]
        for row in result:
            row["used_word_count"] = len(self._decode_used_words(row.pop("used_words")))
        return result

    def fetch_podcast(self, podcast_id: int) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM podcasts WHERE id = ?",
                (int(podcast_id),),
            ).fetchone()
        if row is None:
            raise ValueError("这篇历史剧本已不存在")
        result = dict(row)
        used_words = self._decode_used_words(result.get("used_words"))
        used_ids = [
            int(item["id"])
            for item in used_words
            if item.get("id") is not None
        ]
        definition_by_id: dict[int, tuple[str, str]] = {}
        if used_ids:
            placeholders = ",".join("?" for _ in used_ids)
            with self._connection() as connection:
                definition_rows = connection.execute(
                    f"""
                    SELECT id, word, definition
                    FROM vocabulary
                    WHERE id IN ({placeholders})
                    """,
                    used_ids,
                ).fetchall()
            definition_by_id = {
                int(item["id"]): (
                    str(item["word"]),
                    str(item["definition"] or ""),
                )
                for item in definition_rows
            }
        for item in used_words:
            stored = definition_by_id.get(int(item.get("id") or 0))
            if stored and stored[0].casefold() == str(item.get("word") or "").casefold():
                item["definition"] = stored[1]
        result["used_words"] = used_words
        return result

    def update_podcast_audio(
        self,
        podcast_id: int,
        audio_path: str | Path,
        audio_duration: float,
    ) -> dict[str, Any]:
        """Persist a generated MP3 path and measured duration."""

        resolved_path = str(Path(audio_path).expanduser().resolve())
        duration = max(0.0, float(audio_duration))
        with self._connection() as connection:
            cursor = connection.execute(
                """
                UPDATE podcasts
                SET audio_path = ?, audio_duration = ?, audio_status = 'generated'
                WHERE id = ?
                """,
                (resolved_path, duration, int(podcast_id)),
            )
        if cursor.rowcount != 1:
            raise ValueError("这篇历史剧本已不存在，无法保存音频")
        return self.fetch_podcast(int(podcast_id))

    def update_podcast_audio_status(self, podcast_id: int, status: str) -> None:
        """Update transient generation status without touching cached audio."""

        normalized = status.strip().lower()
        if normalized not in {"not_generated", "generating", "generated", "failed"}:
            raise ValueError("无效的音频状态")
        with self._connection() as connection:
            cursor = connection.execute(
                "UPDATE podcasts SET audio_status = ? WHERE id = ?",
                (normalized, int(podcast_id)),
            )
        if cursor.rowcount != 1:
            raise ValueError("这篇历史剧本已不存在")

    def delete_podcasts(self, podcast_ids: Sequence[int]) -> int:
        ids = sorted({int(item) for item in podcast_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM podcasts WHERE id IN ({placeholders})",
                ids,
            )
        return int(cursor.rowcount)

    def fetch_vocabulary_record(
        self,
        vocabulary_id: int,
        expected_word: str = "",
    ) -> dict[str, Any]:
        with self._connection() as connection:
            row = connection.execute(
                f"SELECT id, {', '.join(VOCABULARY_FIELDS)} FROM vocabulary WHERE id = ?",
                (int(vocabulary_id),),
            ).fetchone()
            # A full vocabulary replacement resets IDs. Historical stories also
            # retain the spelling, so never allow an old ID to open a different
            # word after such a replacement.
            if expected_word.strip() and (
                row is None
                or str(row["word"]).casefold() != expected_word.strip().casefold()
            ):
                row = connection.execute(
                    f"""
                    SELECT id, {', '.join(VOCABULARY_FIELDS)}
                    FROM vocabulary
                    WHERE word = ? COLLATE NOCASE
                    """,
                    (expected_word.strip(),),
                ).fetchone()
        if row is None:
            raise ValueError("对应生词已从词库中删除")
        return dict(row)

    @staticmethod
    def _decode_used_words(value: Any) -> list[dict[str, Any]]:
        try:
            parsed = json.loads(str(value or "[]"))
        except json.JSONDecodeError:
            return []
        return [item for item in parsed if isinstance(item, dict)] if isinstance(parsed, list) else []

    def fetch_vocabulary(
        self,
        search_text: str = "",
        status_filter: str = "all",
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        parameters: list[Any] = []

        search_text = search_text.strip()
        if search_text:
            escaped = (
                search_text.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped}%"
            clauses.append(
                "(word LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR definition LIKE ? ESCAPE '\\' COLLATE NOCASE "
                "OR notes LIKE ? ESCAPE '\\' COLLATE NOCASE)"
            )
            parameters.extend((pattern, pattern, pattern))

        status_sql = {
            "all": "",
            "learning": "status >= 0",
            "unused": "status = 0",
            "archived": "status = -1",
        }.get(status_filter)
        if status_sql is None:
            raise ValueError(f"未知状态筛选条件: {status_filter}")
        if status_sql:
            clauses.append(status_sql)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"""
            SELECT id, {', '.join(VOCABULARY_FIELDS)}, created_at, updated_at
            FROM vocabulary
            {where_sql}
            ORDER BY id ASC
        """
        with self._connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def create_word(self, word: str, notes: str = "") -> int:
        is_valid, normalized, reason = validate_word(word)
        if not is_valid:
            raise ValueError(reason)

        with self._connection() as connection:
            try:
                cursor = connection.execute(
                    "INSERT INTO vocabulary(word, notes, status) VALUES (?, ?, 0)",
                    (normalized, notes.strip()),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"单词“{normalized}”已存在") from exc
        return int(cursor.lastrowid)

    def update_word(self, vocabulary_id: int, field: str, value: Any) -> None:
        if field not in EDITABLE_FIELDS:
            raise ValueError(f"字段不允许编辑: {field}")

        if field == "word":
            is_valid, normalized, reason = validate_word(value)
            if not is_valid:
                raise ValueError(reason)
            value = normalized
        elif field == "status":
            try:
                value = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError("status 必须是整数") from exc
            if value < -1:
                raise ValueError("status 仅允许 -1、0 或正整数")
        elif value is not None:
            value = str(value).strip()

        # field is selected only from the hard-coded allow-list above.
        query = f"UPDATE vocabulary SET {field} = ?, updated_at = ? WHERE id = ?"
        with self._connection() as connection:
            try:
                cursor = connection.execute(
                    query,
                    (value, utc_now_text(), int(vocabulary_id)),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("该单词已存在，无法保存重复值") from exc
            if cursor.rowcount == 0:
                raise ValueError("要修改的单词不存在或已被删除")

    def delete_words(self, vocabulary_ids: Sequence[int]) -> int:
        ids = sorted({int(item) for item in vocabulary_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM vocabulary WHERE id IN ({placeholders})",
                ids,
            )
        return int(cursor.rowcount)

    def import_cleaned_values(
        self,
        raw_values: Iterable[Any],
        source_file: str = "",
    ) -> ImportSummary:
        """Smart-append one batch, assigning contiguous IDs by frequency."""

        valid_words, quarantined = _partition_import_values(raw_values, source_file)
        unique_words = _unique_in_input_order(valid_words)
        frequency_sorted_words = _frequency_sort(unique_words)

        now = utc_now_text()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_words = {
                    str(row[0]).lower()
                    for row in connection.execute("SELECT word FROM vocabulary")
                }
                new_words = [
                    word
                    for word in frequency_sorted_words
                    if word not in existing_words
                ]
                connection.executemany(
                    """
                    INSERT INTO vocabulary(word, status, created_at, updated_at)
                    VALUES (?, 0, ?, ?)
                    """,
                    ((word, now, now) for word in new_words),
                )
                if quarantined:
                    connection.executemany(
                        """
                        INSERT INTO quarantine(raw_text, reason, source_file, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        ((raw, reason, source, now) for raw, reason, source in quarantined),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return ImportSummary(
            inserted_count=len(new_words),
            quarantined_count=len(quarantined),
            duplicate_count=len(valid_words) - len(new_words),
            valid_count=len(valid_words),
        )

    def replace_with_cleaned_values(
        self,
        raw_values: Iterable[Any],
        source_file: str = "",
    ) -> ImportSummary:
        """Replace vocabulary and quarantine atomically with one cleaned batch.

        Full replacement intentionally resets the vocabulary AUTOINCREMENT
        sequence; smart append never touches it.
        """

        valid_words, quarantined = _partition_import_values(raw_values, source_file)
        unique_words = _unique_in_input_order(valid_words)
        sorted_words = _frequency_sort(unique_words)
        if not sorted_words:
            raise ValueError("导入文件中没有合规的有效单词，原词库未被清空")
        now = utc_now_text()

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM vocabulary")
                connection.execute("DELETE FROM quarantine")
                connection.execute(
                    "DELETE FROM sqlite_sequence WHERE name = 'vocabulary'"
                )
                connection.executemany(
                    """
                    INSERT INTO vocabulary(word, status, created_at, updated_at)
                    VALUES (?, 0, ?, ?)
                    """,
                    ((word, now, now) for word in sorted_words),
                )
                if quarantined:
                    connection.executemany(
                        """
                        INSERT INTO quarantine(raw_text, reason, source_file, created_at)
                        VALUES (?, ?, ?, ?)
                        """,
                        ((raw, reason, source, now) for raw, reason, source in quarantined),
                    )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise

        return ImportSummary(
            inserted_count=len(sorted_words),
            quarantined_count=len(quarantined),
            duplicate_count=len(valid_words) - len(sorted_words),
            valid_count=len(valid_words),
        )

    def fetch_unparsed_words(self) -> list[dict[str, Any]]:
        """Return only rows with at least one missing AI-generated core field."""

        missing_clauses = [
            f"COALESCE(TRIM({field}), '') = ''"
            for field in AI_CORE_FIELDS
        ]
        query = f"""
            SELECT id, word
            FROM vocabulary
            WHERE {' OR '.join(missing_clauses)}
            ORDER BY id ASC
        """
        with self._connection() as connection:
            rows = connection.execute(query).fetchall()
        return [dict(row) for row in rows]

    def fetch_vocabulary_by_ids(
        self,
        vocabulary_ids: Sequence[int],
    ) -> list[dict[str, Any]]:
        """Fetch a small set of rows for lightweight incremental UI updates."""

        ids = sorted({int(item) for item in vocabulary_ids})
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        columns = ("id",) + AI_CORE_FIELDS + ("status", "last_used_date")
        with self._connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {', '.join(columns)}
                FROM vocabulary
                WHERE id IN ({placeholders})
                ORDER BY id ASC
                """,
                ids,
            ).fetchall()
        return [dict(row) for row in rows]

    def bulk_update_ai_fields(self, records: Iterable[Mapping[str, Any]]) -> list[int]:
        """Fill only currently-empty AI fields and preserve manual user edits."""

        prepared: list[tuple[int, dict[str, str]]] = []
        for record in records:
            try:
                vocabulary_id = int(record["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError("AI 回填记录缺少有效 id") from exc
            values = {
                field: "" if record.get(field) is None else str(record[field]).strip()
                for field in AI_CORE_FIELDS
            }
            if not all(values.values()):
                continue
            prepared.append((vocabulary_id, values))

        if not prepared:
            return []

        update_sql = """
            UPDATE vocabulary
            SET soundMark = CASE
                    WHEN COALESCE(TRIM(soundMark), '') = '' THEN ? ELSE soundMark END,
                definition = CASE
                    WHEN COALESCE(TRIM(definition), '') = '' THEN ? ELSE definition END,
                collocation = CASE
                    WHEN COALESCE(TRIM(collocation), '') = '' THEN ? ELSE collocation END,
                relatedWords = CASE
                    WHEN COALESCE(TRIM(relatedWords), '') = '' THEN ? ELSE relatedWords END,
                exampleSentence = CASE
                    WHEN COALESCE(TRIM(exampleSentence), '') = '' THEN ? ELSE exampleSentence END,
                updated_at = ?
            WHERE id = ?
        """
        saved_ids: list[int] = []
        now = utc_now_text()
        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                for vocabulary_id, values in prepared:
                    cursor = connection.execute(
                        update_sql,
                        (
                            values["soundMark"],
                            values["definition"],
                            values["collocation"],
                            values["relatedWords"],
                            values["exampleSentence"],
                            now,
                            vocabulary_id,
                        ),
                    )
                    if cursor.rowcount:
                        saved_ids.append(vocabulary_id)
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        return saved_ids

    def fetch_quarantine(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                """
                SELECT id, raw_text, reason, source_file, created_at
                FROM quarantine
                ORDER BY id DESC
                """
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_quarantine(self, quarantine_ids: Sequence[int]) -> int:
        ids = sorted({int(item) for item in quarantine_ids})
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with self._connection() as connection:
            cursor = connection.execute(
                f"DELETE FROM quarantine WHERE id IN ({placeholders})",
                ids,
            )
        return int(cursor.rowcount)

    def clear_quarantine(self) -> int:
        with self._connection() as connection:
            cursor = connection.execute("DELETE FROM quarantine")
        return int(cursor.rowcount)

    def export_rows(self) -> list[Mapping[str, Any]]:
        """Return an unfiltered backup view with all stage-1 fields."""

        columns = ("id",) + VOCABULARY_FIELDS
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT {', '.join(columns)} FROM vocabulary ORDER BY id ASC"
            ).fetchall()
        return [dict(row) for row in rows]
