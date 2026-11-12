"""Provider-compatible AI vocabulary parsing and podcast generation.

The HTTP client targets the OpenAI Chat Completions wire format used by
OpenAI, DeepSeek, OpenRouter, and many self-hosted gateways.  Network work runs
inside a small ThreadPoolExecutor owned by AIParseWorker; SQLite writes remain
serialized on the QThread that owns the orchestration loop.
"""

from __future__ import annotations

import json
import re
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

import requests
from PyQt6.QtCore import QThread, pyqtSignal


AI_FIELDS = (
    "soundMark",
    "definition",
    "collocation",
    "relatedWords",
    "exampleSentence",
)

SYSTEM_PROMPT = """你是专业英语词典编辑，擅长日常生活与职场英语。
请严格返回一个 JSON 对象，不要输出 Markdown、代码围栏或额外说明。格式必须是：
{"items":[{"word":"原单词","soundMark":"IPA 音标","definition":"三个常用中文释义，用中文分号分隔","collocation":"2-3 个最地道的 2-3 词固定搭配，用中文分号分隔","relatedWords":"2-3 个高频近/反义词，并附一句极简中文区别辨析","exampleSentence":"一条简短地道的生活或职场英文例句｜中文翻译"}]}
要求：
1. items 必须与输入单词一一对应，word 必须原样返回，不得遗漏或增加单词。
2. 所有字段都必须是非空字符串。
3. soundMark 使用标准 IPA；释义优先选择日常生活和职场最高频含义。
4. collocation 中每个搭配只能包含 2-3 个英文词。
5. 例句自然、简短、可直接用于英语学习播客。"""

VOCABULARY_JSON_SCHEMA = {
    "name": "vocabulary_batch",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "word": {"type": "string"},
                        "soundMark": {"type": "string"},
                        "definition": {"type": "string"},
                        "collocation": {"type": "string"},
                        "relatedWords": {"type": "string"},
                        "exampleSentence": {"type": "string"},
                    },
                    "required": ["word", *AI_FIELDS],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    },
}

PODCAST_SELECTION_PROMPT = """你是英语教学内容策划与故事编辑。你只负责选词，不写故事。
从用户提供的 50 个候选词中挑选恰好 30 个，要求它们能够在指定风格下自然组成一个主题统一、逻辑连贯的工作或生活微剧。
优先选择可以共享人物、场景和冲突的词；兼顾常用性与学习价值；尽量避开必须反复提及才能讲清情节的词。
selected_words 必须按计划在故事中出现的先后顺序排列，必须使用候选列表里的原始拼写，不得变形、重复、增加或遗漏到不足 30 个。
严格只返回 JSON：{"selected_words":["exactly","thirty","words"]}，不得写故事、Markdown 或额外说明。"""

PODCAST_SYSTEM_PROMPT = """You are an award-winning podcast scriptwriter and bilingual storyteller. Your goal is to craft an engaging, cohesive, and naturally paced audio story in the requested style.

The user will provide 30 pre-selected vocabulary words. Do not select again. Aim to use all 30 naturally; the final story must use at least 25 distinct target words.

MANDATORY WORKFLOW AND RULES:
1. First fill the narrative_blueprint JSON field. Adapt it to the requested style and plan exactly four organic beats. Give each beat a planned_words value of 95-110 and distribute the target words across the beats, about 6-8 per beat. The four planned_words values should total 380-440.
2. Then write content_en by following that blueprint. The program counts words with a normal English word-count regex. Aim for 380-450 English words and about 26-36 complete sentences.
3. Rich expansion: develop each beat with authentic dialogue, physical action, emotional subtext, transitions, sensory detail, and brief inner thought. Do not rush to string target words together. Every detail must advance character, conflict, or atmosphere.
4. Style-driven pacing: Daily Dialogue should have realistic back-and-forth turns; Workplace should build a natural situational progression; Suspense should pace clues toward a reveal; Fantasy should establish immersive world-building and sensory flow.
5. Vocabulary rule: use at least 25 distinct target words. A target word may appear up to three times when repetition is genuinely natural, but never more than three. Only its first occurrence may be Markdown **bold**; later occurrences must be plain text. matched_words must list the 25-30 target words actually used, once each, in their original spelling.
6. Use native collocations, transitions, idioms, and dialogue. Zero AI-stitching: no absurd plot, word-list paragraph, vocabulary recap, repetitive filler, forced scene changes, or Chinese-influenced English.
7. Generate English only. Return JSON only, without Markdown fences, commentary, or story/data/result wrappers.

Return exactly this structure:
{
  "title": "A concise natural title",
  "narrative_blueprint": [
    {"beat": "setup", "planned_words": 100, "assigned_words": ["..."]},
    {"beat": "development", "planned_words": 100, "assigned_words": ["..."]},
    {"beat": "turn", "planned_words": 100, "assigned_words": ["..."]},
    {"beat": "resolution", "planned_words": 100, "assigned_words": ["..."]}
  ],
  "content_en": "A 380-450 word English story using at least 25 target words",
  "matched_words": ["the", "25-to-30", "target", "words", "actually", "used"]
}
Before submitting, recount content_en, verify 380-450 words, 25-30 distinct matched_words, no target word over three occurrences, and only the first occurrence of each target word in bold."""

TRANSLATION_SYSTEM_PROMPT = """你是一位专业双语播客译者。请把给定英文剧本完整翻译成自然、准确、适合播客阅读的中文。
保持原文段落、人物关系、语气和信息完整，不要删减、总结或扩写。
所有学习词都必须根据上下文翻译成地道中文，严禁把学习词的英文原文直接夹在中文句子里，也不得采用“中文（英文）”形式保留。
删除所有 Markdown 标记，包括 **；人名、地名和公认缩写之外，不要残留不必要的英文字母词。
输出前逐项检查用户提供的“学习词及中文释义”，确认 content_cn 中不再出现这些英文学习词。
严格只返回 JSON：{"content_cn":"完整中文翻译"}，不得输出代码围栏或额外说明。"""

PODCAST_SELECTION_JSON_SCHEMA = {
    "name": "podcast_word_selection",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "selected_words": {
                "type": "array",
                "minItems": 30,
                "maxItems": 30,
                "items": {"type": "string"},
            }
        },
        "required": ["selected_words"],
        "additionalProperties": False,
    },
}

PODCAST_ENGLISH_JSON_SCHEMA = {
    "name": "podcast_english_draft",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "narrative_blueprint": {
                "type": "array",
                "minItems": 4,
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "beat": {"type": "string"},
                        "planned_words": {"type": "integer"},
                        "assigned_words": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                    "required": ["beat", "planned_words", "assigned_words"],
                    "additionalProperties": False,
                },
            },
            "content_en": {"type": "string"},
            "matched_words": {
                "type": "array",
                "minItems": 25,
                "maxItems": 30,
                "items": {"type": "string"},
            },
        },
        "required": ["title", "narrative_blueprint", "content_en", "matched_words"],
        "additionalProperties": False,
    },
}

TRANSLATION_JSON_SCHEMA = {
    "name": "podcast_translation",
    "strict": True,
    "schema": {
        "type": "object",
        "properties": {"content_cn": {"type": "string"}},
        "required": ["content_cn"],
        "additionalProperties": False,
    },
}


@dataclass(frozen=True)
class AIConfig:
    base_url: str = "https://api.openai.com/v1"
    api_key: str = ""
    model: str = "gpt-4o-mini"
    batch_size: int = 12
    max_concurrency: int = 3
    request_timeout: int = 60

    def validated(self) -> "AIConfig":
        base_url = self.base_url.strip().rstrip("/")
        api_key = self.api_key.strip()
        model = self.model.strip()
        if not base_url.startswith(("https://", "http://")):
            raise ValueError("API Base URL 必须以 http:// 或 https:// 开头")
        if not api_key:
            raise ValueError("API Key 不能为空")
        if not model:
            raise ValueError("Model Name 不能为空")
        batch_size = min(20, max(1, int(self.batch_size)))
        max_concurrency = min(8, max(1, int(self.max_concurrency)))
        request_timeout = min(300, max(15, int(self.request_timeout)))
        return AIConfig(
            base_url=base_url,
            api_key=api_key,
            model=model,
            batch_size=batch_size,
            max_concurrency=max_concurrency,
            request_timeout=request_timeout,
        )


class AIConfigStore:
    """Small JSON config store; call load/save from a background thread."""

    def __init__(self, config_path: str | Path) -> None:
        self.config_path = Path(config_path).expanduser().resolve()

    def load(self) -> AIConfig:
        if not self.config_path.exists():
            return AIConfig()
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                raise ValueError
            allowed = {field.name for field in AIConfig.__dataclass_fields__.values()}
            return AIConfig(**{key: value for key, value in payload.items() if key in allowed})
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError("API 配置文件损坏，无法读取") from exc

    def save(self, config: AIConfig) -> None:
        config = config.validated()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path = self.config_path.with_suffix(self.config_path.suffix + ".tmp")
        temporary_path.write_text(
            json.dumps(asdict(config), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary_path.replace(self.config_path)


class PodcastValidationError(RuntimeError):
    """A locally rejected draft that can be supplied to the next repair call."""

    def __init__(self, message: str, draft: dict[str, Any]) -> None:
        super().__init__(message)
        self.draft = draft


class CompatibleChatClient:
    """Minimal OpenAI-compatible Chat Completions client."""

    def __init__(self, config: AIConfig) -> None:
        self.config = config.validated()

    @property
    def endpoint(self) -> str:
        base_url = self.config.base_url.rstrip("/")
        if base_url.endswith("/chat/completions"):
            return base_url
        return f"{base_url}/chat/completions"

    def parse_batch(
        self,
        words: list[str],
        cancel_event: threading.Event,
    ) -> list[dict[str, str]]:
        if cancel_event.is_set():
            return []

        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "请解析以下单词：" + json.dumps(words, ensure_ascii=False),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": VOCABULARY_JSON_SCHEMA,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        response = self._request_with_retry(payload, headers, cancel_event)
        try:
            response_payload = response.json()
            content = response_payload["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError("API 响应缺少 choices[0].message.content") from exc

        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        parsed = self._decode_json(str(content))
        return self._validate_items(parsed, words)

    def select_podcast_words(
        self,
        candidates: list[dict[str, Any]],
        style: str,
        custom_style: str,
        cancel_event: threading.Event,
        stage_callback: Callable[[str], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Use a small, independent request to choose 30 cohesive words."""

        if cancel_event.is_set():
            raise RuntimeError("任务已终止")
        if len(candidates) < 30:
            raise RuntimeError(f"候选词只有 {len(candidates)} 个，无法选出 30 个")
        if stage_callback is not None:
            stage_callback("正在从 50 个候选词中选择 30 个主题一致的词…")

        candidate_payload = [
            {
                "word": str(row["word"]),
                "definition": str(row.get("definition") or ""),
                "collocation": str(row.get("collocation") or ""),
                "status": int(row.get("status", 0)),
            }
            for row in candidates
        ]
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0.2,
            "max_tokens": 1200,
            "messages": [
                {"role": "system", "content": PODCAST_SELECTION_PROMPT},
                {
                    "role": "user",
                    "content": (
                        f"故事风格：{style.strip() or '随机/盲盒'}\n"
                        f"用户额外风格要求：{custom_style.strip() or '无额外要求'}\n"
                        "请从以下候选词中选出并排序恰好 30 个：\n"
                        + json.dumps(candidate_payload, ensure_ascii=False)
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": PODCAST_SELECTION_JSON_SCHEMA,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        response = self._request_with_retry(payload, headers, cancel_event)
        selection_payload = self._decode_chat_response(response, "候选词筛选")
        if not isinstance(selection_payload, dict):
            raise RuntimeError("候选词筛选结果不是 JSON 对象")
        selection_object = self._find_podcast_object(selection_payload)
        raw_selected = self._first_alias(
            selection_object,
            ("selected_words", "matched_words", "words", "vocabulary", "入选单词"),
        )
        if not isinstance(raw_selected, list):
            raise RuntimeError("候选词筛选结果缺少 selected_words 数组")

        row_by_key = {
            str(row["word"]).casefold(): row
            for row in candidates
        }
        selected: list[dict[str, Any]] = []
        seen: set[str] = set()
        for value in raw_selected:
            if isinstance(value, dict):
                value = self._first_alias(value, ("word", "text", "词"))
            key = self._as_text(value).casefold()
            if key in row_by_key and key not in seen:
                selected.append(row_by_key[key])
                seen.add(key)
            if len(selected) == 30:
                break
        if len(selected) != 30:
            raise RuntimeError(
                f"模型只选出了 {len(selected)} 个有效且不重复的候选词，必须恰好 30 个"
            )
        return selected

    def generate_podcast(
        self,
        candidates: list[dict[str, Any]],
        style: str,
        custom_style: str,
        cancel_event: threading.Event,
        correction_feedback: str = "",
        stage_callback: Callable[[str], None] | None = None,
        previous_draft: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Generate and locally validate one structured podcast story."""

        if cancel_event.is_set():
            raise RuntimeError("任务已终止")
        if len(candidates) != 30:
            raise RuntimeError(f"英文剧本输入必须恰好 30 个词，实际为 {len(candidates)} 个")
        if stage_callback is not None:
            stage_callback("正在按四段蓝图生成 380-450 词英文剧本…")
        candidate_payload = [
            {
                "word": str(row["word"]),
                "definition": str(row.get("definition") or ""),
                "collocation": str(row.get("collocation") or ""),
                "status": int(row.get("status", 0)),
            }
            for row in candidates
        ]
        chosen_style = style.strip() or "随机/盲盒"
        custom_instruction = custom_style.strip() or "无额外要求"
        repair_instruction = ""
        if previous_draft:
            previous_object = self._find_podcast_object(previous_draft)
            previous_content = self._as_text(self._first_alias(
                previous_object,
                ("content_en", "english_content", "english_story", "story_en", "en"),
            ))
            previous_count = len(
                re.findall(
                    r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b",
                    previous_content.replace("**", ""),
                )
            )
            if previous_count < 400:
                length_action = (
                    f"The previous content_en has {previous_count} words. Preserve its useful "
                    f"plot, but add {400 - previous_count} to {430 - previous_count} meaningful "
                    "words through dialogue, action, emotional subtext, and sensory detail."
                )
            elif previous_count > 430:
                length_action = (
                    f"The previous content_en has {previous_count} words. Tighten it to 400-430 "
                    "words while retaining at least 25 distinct target words."
                )
            else:
                length_action = "Keep the revised content_en within 400-430 words."
            repair_instruction = (
                "\nREVISION MODE: Do not restart from a short summary. Revise the supplied draft "
                "directly, while correcting every validation problem. "
                + length_action
                + " Recount words before returning. Use at least 25 distinct target words, never "
                "repeat one more than three times, and bold only its first occurrence. Return a "
                "corrected four-beat narrative_blueprint too.\n"
                "PREVIOUS INVALID DRAFT:\n"
                + json.dumps(previous_draft, ensure_ascii=False)
                + "\nEND PREVIOUS DRAFT\n"
            )
        user_prompt = (
            f"故事风格：{chosen_style}\n"
            f"用户额外风格要求：{custom_instruction}\n"
            + (
                "上一次输出未通过程序校验，必须修正以下问题："
                f"{correction_feedback.strip()}\n"
                if correction_feedback.strip()
                else ""
            )
            + repair_instruction
            + "再次强调：请以 380-450 词为写作目标，程序本地容错范围为 330-480 词；"
            "以下 30 个指定词至少自然使用 25 个，每词最多出现 3 次，且只有首次出现加粗。\n"
            "请根据以下已经选定的 30 个词生成故事：\n"
            + json.dumps(candidate_payload, ensure_ascii=False)
        )
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0.6,
            "max_tokens": 4800,
            "messages": [
                {"role": "system", "content": PODCAST_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": PODCAST_ENGLISH_JSON_SCHEMA,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }
        response = self._request_with_retry(payload, headers, cancel_event)
        english_payload = self._decode_chat_response(response, "英文剧本")
        try:
            result = self._validate_podcast(
                english_payload,
                candidates,
                require_translation=False,
            )
        except RuntimeError as exc:
            draft = english_payload if isinstance(english_payload, dict) else {}
            raise PodcastValidationError(str(exc), draft) from exc
        if cancel_event.is_set():
            raise RuntimeError("任务已终止")
        if stage_callback is not None:
            stage_callback("英文剧本校验通过，正在生成完整中文翻译…")

        candidate_by_key = {
            str(item["word"]).casefold(): item
            for item in candidate_payload
        }
        translation_terms = [
            {
                "word": word,
                "definition": str(
                    candidate_by_key.get(word.casefold(), {}).get("definition") or ""
                ),
            }
            for word in result["matched_words"]
        ]
        previous_translation = ""
        content_cn = ""
        offenders: list[str] = []
        for translation_attempt in range(3):
            repair_note = ""
            if previous_translation:
                repair_note = (
                    "\n上一次中文翻译仍残留以下英文学习词："
                    + "、".join(offenders)
                    + "。请基于上一版逐一改成符合语境的中文，不得保留英文：\n"
                    + previous_translation
                    + "\n"
                )
            translation_payload: dict[str, Any] = {
                "model": self.config.model,
                "temperature": 0.15,
                "max_tokens": 5000,
                "messages": [
                    {"role": "system", "content": TRANSLATION_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": (
                            f"标题：{result['title']}\n\n"
                            "学习词及中文释义（这些英文词不得出现在 content_cn）：\n"
                            + json.dumps(translation_terms, ensure_ascii=False)
                            + repair_note
                            + f"\n英文正文：\n{result['content_en']}"
                        ),
                    },
                ],
                "response_format": {
                    "type": "json_schema",
                    "json_schema": TRANSLATION_JSON_SCHEMA,
                },
            }
            translation_response = self._request_with_retry(
                translation_payload,
                headers,
                cancel_event,
            )
            translated_payload = self._decode_chat_response(
                translation_response,
                "中文翻译",
            )
            if not isinstance(translated_payload, dict):
                raise RuntimeError("中文翻译结果不是 JSON 对象")
            translated_object = self._find_podcast_object(translated_payload)
            content_cn = self._as_text(self._first_alias(
                translated_object,
                (
                    "content_cn", "chinese_content", "chinese_translation",
                    "translation", "chinese", "cn", "中文正文", "中文翻译",
                ),
            ))
            if not content_cn:
                raise RuntimeError("中文翻译结果缺少 content_cn")
            content_cn = self._clean_translation_markdown(content_cn)
            offenders = self._translation_vocab_residues(
                content_cn,
                result["matched_words"],
            )
            if not offenders:
                break
            previous_translation = content_cn

        if offenders:
            # Last-resort deterministic cleanup prevents a provider that keeps
            # echoing target words from polluting the Chinese reading pane.
            for word in offenders:
                definition = str(
                    candidate_by_key.get(word.casefold(), {}).get("definition") or ""
                )
                replacement = re.split(r"[；;，,、/|]", definition, maxsplit=1)[0].strip()
                if not replacement or not re.search(r"[\u4e00-\u9fff]", replacement):
                    replacement = "相关含义"
                content_cn = re.sub(
                    rf"\b{re.escape(word)}\b",
                    replacement,
                    content_cn,
                    flags=re.IGNORECASE,
                )
            content_cn = self._clean_translation_markdown(content_cn)
        result["content_cn"] = content_cn
        return result

    @staticmethod
    def _clean_translation_markdown(content_cn: str) -> str:
        cleaned = str(content_cn).replace("**", "").replace("__", "")
        cleaned = cleaned.replace("`", "")
        return cleaned.strip()

    @staticmethod
    def _translation_vocab_residues(
        content_cn: str,
        matched_words: Iterable[str],
    ) -> list[str]:
        return [
            str(word)
            for word in matched_words
            if re.search(
                rf"\b{re.escape(str(word))}\b",
                content_cn,
                flags=re.IGNORECASE,
            )
        ]

    def _decode_chat_response(self, response: requests.Response, label: str) -> Any:
        try:
            response_payload = response.json()
            choice = response_payload["choices"][0]
            content = choice["message"]["content"]
            finish_reason = str(choice.get("finish_reason") or "")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(f"{label}响应缺少 choices[0].message.content") from exc
        if finish_reason == "length":
            raise RuntimeError(f"{label}达到输出长度上限而被截断")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) if isinstance(part, dict) else str(part)
                for part in content
            )
        if not str(content).strip():
            raise RuntimeError(f"{label}返回了空内容")
        return self._decode_json(str(content))

    def _validate_podcast(
        self,
        payload: Any,
        candidates: list[dict[str, Any]],
        require_translation: bool = True,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise RuntimeError("模型返回的播客结果不是 JSON 对象")
        normalized = self._find_podcast_object(payload)
        title = self._as_text(self._first_alias(
            normalized,
            ("title", "story_title", "podcast_title", "标题"),
        ))
        content_en = self._as_text(self._first_alias(
            normalized,
            (
                "content_en", "english_content", "english_story", "story_en",
                "english", "en", "英文正文", "英文",
            ),
        ))
        content_cn = self._as_text(self._first_alias(
            normalized,
            (
                "content_cn", "chinese_content", "chinese_translation",
                "translation", "story_cn", "chinese", "中文正文", "中文翻译",
                "cn",
            ),
        ))
        raw_matched = self._first_alias(
            normalized,
            (
                "matched_words", "used_words", "selected_words", "words_used",
                "vocabulary_used", "vocabulary", "adopted_words", "采用单词", "使用单词",
            ),
        )
        if not content_cn and not require_translation:
            content_cn = "<pending-translation>"
        if not title or not content_en or not content_cn or not isinstance(raw_matched, list):
            keys = ", ".join(str(key) for key in normalized.keys()) or "<空对象>"
            raise RuntimeError(
                "模型 JSON 未包含完整的 title/content_en/matched_words"
                + ("/content_cn" if require_translation else "")
                + "；"
                f"实际识别到的键：{keys}"
            )

        candidate_words = {
            str(row["word"]).casefold(): str(row["word"])
            for row in candidates
        }
        raw_valid: list[str] = []
        seen: set[str] = set()
        for value in raw_matched:
            if isinstance(value, dict):
                value = self._first_alias(value, ("word", "text", "词"))
            key = self._as_text(value).casefold()
            if key in candidate_words and key not in seen:
                raw_valid.append(candidate_words[key])
                seen.add(key)

        word_count = len(
            re.findall(
                r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b",
                content_en.replace("**", ""),
            )
        )
        if not 330 <= word_count <= 480:
            raise RuntimeError(
                f"英文正文实际为 {word_count} 词，本地可接受范围为 330-480 词"
            )

        # The story is the source of truth: accept every distinct candidate
        # actually used between one and three times, regardless of whether the
        # provider's matched_words list is complete.
        occurrence_count = {
            key: len(
                re.findall(
                    rf"\b{re.escape(word)}\b",
                    content_en,
                    re.IGNORECASE,
                )
            )
            for key, word in candidate_words.items()
        }
        overused = [
            f"{candidate_words[key]}×{count}"
            for key, count in occurrence_count.items()
            if count > 3
        ]
        if overused:
            raise RuntimeError(
                "以下候选词在正文中出现超过 3 次：" + "、".join(overused[:12])
            )

        bolded: list[str] = []
        bolded_seen: set[str] = set()
        for match in re.finditer(r"\*\*\s*([A-Za-z]+)\s*\*\*", content_en):
            key = match.group(1).casefold()
            if key in candidate_words and key not in bolded_seen:
                bolded.append(candidate_words[key])
                bolded_seen.add(key)

        matched: list[str] = []
        selected_keys: set[str] = set()
        preference_order = bolded + raw_valid + list(candidate_words.values())
        for word in preference_order:
            key = word.casefold()
            if key in selected_keys or occurrence_count.get(key, 0) < 1:
                continue
            matched.append(candidate_words[key])
            selected_keys.add(key)
        if len(matched) < 25:
            raise RuntimeError(
                f"正文实际只使用了 {len(matched)} 个独立候选词，至少需要 25 个"
            )

        # Remove candidate-word highlighting first, then add it back only to
        # the first occurrence. Subsequent legal repetitions remain plain.
        def strip_candidate_bold(match: re.Match[str]) -> str:
            visible = match.group(1).strip()
            if visible.casefold() in candidate_words:
                return visible
            return match.group(0)

        content_en = re.sub(
            r"\*\*\s*([A-Za-z]+)\s*\*\*",
            strip_candidate_bold,
            content_en,
        )
        for word in matched:
            content_en = re.sub(
                rf"\b({re.escape(word)})\b",
                r"**\1**",
                content_en,
                count=1,
                flags=re.IGNORECASE,
            )

        missing_bold = [
            word
            for word in matched
            if not re.search(
                rf"\*\*\s*{re.escape(word)}\s*\*\*",
                content_en,
                re.IGNORECASE,
            )
        ]
        if missing_bold:
            raise RuntimeError("以下 matched_words 未在英文正文中加粗：" + ", ".join(missing_bold))
        invalid_bold_count = [
            word
            for word in matched
            if len(
                re.findall(
                    rf"\*\*\s*{re.escape(word)}\s*\*\*",
                    content_en,
                    re.IGNORECASE,
                )
            )
            != 1
        ]
        if invalid_bold_count:
            raise RuntimeError(
                "以下候选词未能规范为仅首次加粗：" + ", ".join(invalid_bold_count)
            )
        result = {
            "title": title,
            "content_en": content_en,
            "content_cn": content_cn,
            "matched_words": matched,
        }
        if len(raw_valid) != len(matched) or set(
            word.casefold() for word in raw_valid
        ) != selected_keys:
            result["normalization_note"] = (
                f"模型返回 {len(raw_valid)} 个 matched_words；已按正文实际加粗内容"
                f"校正为 {len(matched)} 个实际命中的学习词"
            )
        return result

    @staticmethod
    def _first_alias(payload: dict[str, Any], aliases: tuple[str, ...]) -> Any:
        lower_keys = {str(key).casefold(): key for key in payload}
        for alias in aliases:
            actual_key = lower_keys.get(alias.casefold())
            if actual_key is not None:
                return payload[actual_key]
        return None

    def _find_podcast_object(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Accept common provider wrappers while preferring the richest object."""

        aliases = (
            "title", "story_title", "podcast_title", "标题",
            "content_en", "english_content", "english_story", "story_en", "en",
            "content_cn", "chinese_content", "chinese_translation", "translation", "cn",
            "matched_words", "used_words", "selected_words", "vocabulary_used", "vocabulary",
        )
        queue: list[tuple[dict[str, Any], int]] = [(payload, 0)]
        best = payload
        best_score = -1
        while queue:
            current, depth = queue.pop(0)
            flattened = dict(current)
            for value in current.values():
                if isinstance(value, dict):
                    for key, nested_value in value.items():
                        flattened.setdefault(key, nested_value)
            score = sum(
                self._first_alias(flattened, (alias,)) is not None
                for alias in aliases
            )
            if score > best_score:
                best, best_score = flattened, score
            if depth >= 3:
                continue
            for value in current.values():
                if isinstance(value, dict):
                    queue.append((value, depth + 1))
        return best

    def _request_with_retry(
        self,
        payload: dict[str, Any],
        headers: dict[str, str],
        cancel_event: threading.Event,
    ) -> requests.Response:
        last_error = "未知网络错误"
        if "deepseek.com" in self.config.base_url.casefold():
            # DeepSeek Chat Completions documents json_object, not the OpenAI
            # chat-completions json_schema envelope.
            format_options: list[dict[str, Any] | None] = [
                {"type": "json_object"},
                None,
            ]
        else:
            format_options = [
                payload.get("response_format"),
                {"type": "json_object"},
                None,
            ]
        format_index = 0
        transient_attempt = 0

        while transient_attempt < 3:
            if cancel_event.is_set():
                raise RuntimeError("任务已终止")
            current_payload = dict(payload)
            selected_format = format_options[format_index]
            if selected_format is None:
                current_payload.pop("response_format", None)
            else:
                current_payload["response_format"] = selected_format
            try:
                response = requests.post(
                    self.endpoint,
                    headers=headers,
                    json=current_payload,
                    timeout=(10, self.config.request_timeout),
                )
            except requests.RequestException as exc:
                last_error = f"网络请求失败：{exc}"
                transient_attempt += 1
                if transient_attempt < 3 and not cancel_event.wait(2 ** (transient_attempt - 1)):
                    continue
                raise RuntimeError(last_error) from exc

            response_text = response.text[:600].replace("\n", " ")
            if response.status_code == 400 and format_index < len(format_options) - 1:
                format_index += 1
                continue
            if response.status_code == 429 or response.status_code >= 500:
                last_error = f"API 暂时不可用（HTTP {response.status_code}）：{response_text}"
                transient_attempt += 1
                if transient_attempt < 3 and not cancel_event.wait(2 ** (transient_attempt - 1)):
                    continue
            if not response.ok:
                raise RuntimeError(
                    f"API 请求失败（HTTP {response.status_code}）：{response_text}"
                )
            return response

        raise RuntimeError(last_error)

    @staticmethod
    def _decode_json(content: str) -> Any:
        content = content.strip()
        if content.startswith("```"):
            lines = content.splitlines()
            if lines:
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            content = "\n".join(lines).strip()

        try:
            return json.loads(content)
        except json.JSONDecodeError:
            object_start, object_end = content.find("{"), content.rfind("}")
            array_start, array_end = content.find("["), content.rfind("]")
            candidates = []
            if object_start >= 0 and object_end > object_start:
                candidates.append(content[object_start : object_end + 1])
            if array_start >= 0 and array_end > array_start:
                candidates.append(content[array_start : array_end + 1])
            for candidate in candidates:
                try:
                    return json.loads(candidate)
                except json.JSONDecodeError:
                    continue
        raise RuntimeError("模型未返回可解析的 JSON")

    @staticmethod
    def _as_text(value: Any) -> str:
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, list):
            return "；".join(str(item).strip() for item in value if str(item).strip())
        if isinstance(value, dict):
            return json.dumps(value, ensure_ascii=False, separators=(",", "："))
        return "" if value is None else str(value).strip()

    def _validate_items(self, payload: Any, requested_words: Iterable[str]) -> list[dict[str, str]]:
        if isinstance(payload, dict):
            items = payload.get("items", payload.get("data", []))
        else:
            items = payload
        if not isinstance(items, list):
            raise RuntimeError("模型 JSON 中缺少 items 数组")

        requested = {word.lower(): word for word in requested_words}
        accepted: dict[str, dict[str, str]] = {}
        for raw_item in items:
            if not isinstance(raw_item, dict):
                continue
            returned_word = self._as_text(raw_item.get("word"))
            key = returned_word.lower()
            if key not in requested or key in accepted:
                continue
            item = {"word": requested[key]}
            for field in AI_FIELDS:
                item[field] = self._as_text(raw_item.get(field))
            if all(item[field] for field in AI_FIELDS):
                accepted[key] = item
        if not accepted:
            raise RuntimeError("模型返回的数据没有任何完整、可匹配的单词记录")
        return list(accepted.values())


class AIParseWorker(QThread):
    """Fetch missing rows, call batches concurrently, and persist each result."""

    total_ready = pyqtSignal(int)
    progress_changed = pyqtSignal(int, int, int, int, str)
    batch_saved = pyqtSignal(object)
    log_message = pyqtSignal(str)
    run_completed = pyqtSignal(object)
    fatal_error = pyqtSignal(str)

    def __init__(
        self,
        repository: Any,
        config: AIConfig,
        log_path: str | Path,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.config = config.validated()
        self.log_path = Path(log_path).expanduser().resolve()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        try:
            candidates = self.repository.fetch_unparsed_words()
            total = len(candidates)
            self.total_ready.emit(total)
            if total == 0:
                self.run_completed.emit(
                    {"total": 0, "processed": 0, "succeeded": 0, "failed": 0, "cancelled": False}
                )
                return
            self._run_batches(candidates)
        except Exception as exc:
            message = f"AI 解析任务无法启动：{exc}"
            self._log(message)
            self.fatal_error.emit(message)

    def _run_batches(self, candidates: list[dict[str, Any]]) -> None:
        total = len(candidates)
        batches = [
            candidates[index : index + self.config.batch_size]
            for index in range(0, total, self.config.batch_size)
        ]
        client = CompatibleChatClient(self.config)
        executor = ThreadPoolExecutor(
            max_workers=self.config.max_concurrency,
            thread_name_prefix="ai-vocab",
        )
        pending: dict[Future[list[dict[str, str]]], list[dict[str, Any]]] = {}
        next_batch = 0
        processed = succeeded = failed = 0

        def submit_available() -> None:
            nonlocal next_batch
            while (
                not self._cancel_event.is_set()
                and len(pending) < self.config.max_concurrency
                and next_batch < len(batches)
            ):
                batch = batches[next_batch]
                words = [str(row["word"]) for row in batch]
                future = executor.submit(client.parse_batch, words, self._cancel_event)
                pending[future] = batch
                next_batch += 1

        self._log(
            f"开始解析 {total} 个待回填单词；批大小 {self.config.batch_size}，"
            f"并发 {self.config.max_concurrency}，模型 {self.config.model}"
        )
        submit_available()
        try:
            while pending:
                done, _ = wait(tuple(pending), timeout=0.25, return_when=FIRST_COMPLETED)
                if not done:
                    continue
                for future in done:
                    batch = pending.pop(future)
                    words = [str(row["word"]) for row in batch]
                    batch_was_processed = True
                    try:
                        parsed_items = future.result()
                        if self._cancel_event.is_set() and not parsed_items:
                            batch_was_processed = False
                            continue
                        id_by_word = {
                            str(row["word"]).lower(): int(row["id"])
                            for row in batch
                        }
                        records = []
                        for item in parsed_items:
                            vocabulary_id = id_by_word.get(item["word"].lower())
                            if vocabulary_id is not None:
                                records.append({"id": vocabulary_id, **item})
                        saved_ids = self.repository.bulk_update_ai_fields(records)
                        batch_success = len(saved_ids)
                        batch_failed = len(batch) - batch_success
                        succeeded += batch_success
                        failed += batch_failed
                        if batch_failed:
                            missing = [
                                word
                                for word in words
                                if id_by_word[word.lower()] not in set(saved_ids)
                            ]
                            self._log("本批部分单词未获得完整结果：" + ", ".join(missing))
                        if saved_ids:
                            self.batch_saved.emit(saved_ids)
                    except Exception as exc:
                        if self._cancel_event.is_set():
                            batch_was_processed = False
                            self._log(f"终止后放弃未完成批次 [{', '.join(words)}]")
                        else:
                            batch_success = 0
                            batch_failed = len(batch)
                            failed += batch_failed
                            self._log(f"批次失败，已跳过 [{', '.join(words)}]：{exc}")

                    if batch_was_processed:
                        processed += len(batch)
                        current_word = words[-1] if words else ""
                        self.progress_changed.emit(
                            processed,
                            total,
                            succeeded,
                            failed,
                            current_word,
                        )
                submit_available()
        finally:
            # This wait occurs inside AIParseWorker's QThread, never in the GUI
            # thread. It lets in-flight HTTP calls finish safely after cancel.
            executor.shutdown(wait=True, cancel_futures=True)

        cancelled = self._cancel_event.is_set()
        summary = {
            "total": total,
            "processed": processed,
            "succeeded": succeeded,
            "failed": failed,
            "cancelled": cancelled,
        }
        self._log(
            ("任务已终止" if cancelled else "任务完成")
            + f"：成功 {succeeded}，失败 {failed}，已处理 {processed}/{total}"
        )
        self.run_completed.emit(summary)

    def _log(self, message: str) -> None:
        timestamped = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
        self.log_message.emit(timestamped)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(timestamped + "\n")
        except OSError:
            # Logging must never interrupt parsing or database persistence.
            pass


class PodcastGenerationWorker(QThread):
    """Run candidate selection, HTTP generation and atomic save off the GUI."""

    stage_changed = pyqtSignal(str)
    candidates_ready = pyqtSignal(int)
    log_message = pyqtSignal(str)
    run_completed = pyqtSignal(object)
    fatal_error = pyqtSignal(str)

    def __init__(
        self,
        repository: Any,
        config: AIConfig,
        style: str,
        custom_style: str,
        log_path: str | Path,
        parent: Any = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.config = config.validated()
        self.style = style
        self.custom_style = custom_style
        self.log_path = Path(log_path).expanduser().resolve()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()
        self.requestInterruption()

    def run(self) -> None:
        try:
            self.stage_changed.emit("正在抽取盲盒候选词…")
            candidates = self.repository.select_podcast_candidates()
            self.candidates_ready.emit(len(candidates))
            self._log(f"已抽取 {len(candidates)} 个候选词，开始生成播客剧本")
            if self._cancel_event.is_set():
                self.run_completed.emit({"cancelled": True})
                return

            client = CompatibleChatClient(self.config)
            selected_candidates: list[dict[str, Any]] | None = None
            for selection_attempt in range(2):
                try:
                    selected_candidates = client.select_podcast_words(
                        candidates,
                        self.style,
                        self.custom_style,
                        self._cancel_event,
                        self.stage_changed.emit,
                    )
                    break
                except Exception as exc:
                    if self._cancel_event.is_set() or selection_attempt == 1:
                        raise
                    self._log(f"首次选词结果无效，自动重试一次：{exc}")
                    self.stage_changed.emit("选词结果无效，正在重新筛选 30 个词…")
            if selected_candidates is None:
                raise RuntimeError("模型没有返回可用的 30 个入选词")
            self._log(
                "AI 已从 50 个候选词中选定 30 个主题一致词，"
                "开始生成目标 380-450 词英文剧本"
            )
            if self._cancel_event.is_set():
                self.run_completed.emit({"cancelled": True})
                return

            result: dict[str, Any] | None = None
            last_error: Exception | None = None
            previous_draft: dict[str, Any] | None = None
            max_story_attempts = 4
            for attempt in range(max_story_attempts):
                try:
                    result = client.generate_podcast(
                        selected_candidates,
                        self.style,
                        self.custom_style,
                        self._cancel_event,
                        "" if attempt == 0 else str(last_error or ""),
                        self.stage_changed.emit,
                        previous_draft=previous_draft,
                    )
                    break
                except Exception as exc:
                    last_error = exc
                    if isinstance(exc, PodcastValidationError) and exc.draft:
                        previous_draft = exc.draft
                    if self._cancel_event.is_set() or attempt == max_story_attempts - 1:
                        raise
                    self._log(
                        f"第 {attempt + 1} 次英文剧本未通过校验，"
                        f"将携带上一版草稿和原因进行第 {attempt + 2}/"
                        f"{max_story_attempts} 次定向修订：{exc}"
                    )
                    self.stage_changed.emit(
                        f"英文校验未通过，正在基于上一版进行第 {attempt + 2}/"
                        f"{max_story_attempts} 次定向修订…"
                    )
            if result is None:
                raise RuntimeError(f"模型没有返回可用剧本：{last_error}")
            normalization_note = str(result.pop("normalization_note", "")).strip()
            if normalization_note:
                self._log(normalization_note)
            if self._cancel_event.is_set():
                self._log("用户已终止任务；生成结果未入库，词库状态未改变")
                self.run_completed.emit({"cancelled": True})
                return

            self.stage_changed.emit("正在保存历史剧本并回写单词状态…")
            saved = self.repository.save_podcast_and_update_words(
                result,
                selected_candidates,
                f"{self.style} {self.custom_style}".strip(),
            )
            self._log(
                f"播客《{saved['title']}》已入库；"
                f"{len(saved['used_words'])} 个单词完成 status +1"
            )
            self.run_completed.emit({"cancelled": False, "podcast": saved})
        except Exception as exc:
            if self._cancel_event.is_set():
                self.run_completed.emit({"cancelled": True})
                return
            message = f"播客剧本生成失败：{exc}"
            self._log(message)
            self.fatal_error.emit(message)

    def _log(self, message: str) -> None:
        timestamped = f"[{datetime.now().isoformat(timespec='seconds')}] {message}"
        self.log_message.emit(timestamped)
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as stream:
                stream.write(timestamped + "\n")
        except OSError:
            pass
