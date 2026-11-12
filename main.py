"""Linkcho desktop client: turn vocabulary into short audio stories."""

from __future__ import annotations

import csv
import html
import re
import shutil
import sys
import traceback
from pathlib import Path
from typing import Any, Callable

import pandas as pd
from PyQt6.QtCore import QStandardPaths, QThread, QTimer, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QFont
from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QProgressBar,
    QPlainTextEdit,
    QRadioButton,
    QSlider,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from ai_parser import (
    AIConfig,
    AIConfigStore,
    AIParseWorker,
    PodcastGenerationWorker,
)
from database import (
    AI_CORE_FIELDS,
    VOCABULARY_FIELDS,
    ImportSummary,
    VocabularyRepository,
    validate_word,
)
from tts_service import (
    DEFAULT_VOICE,
    TTSGenerationWorker,
    generate_word_pronunciation,
)


APP_DIR = Path(__file__).resolve().parent

TABLE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("selected", "选择"),
    ("id", "ID"),
    ("word", "单词"),
    ("soundMark", "音标 🔊"),
    ("definition", "中文释义"),
    ("collocation", "核心搭配"),
    ("relatedWords", "近反义词辨析"),
    ("exampleSentence", "原生例句"),
    ("status", "状态"),
    ("last_used_date", "最近使用日期"),
    ("notes", "备注 / Tag"),
)

EXPORT_COLUMNS = ("id",) + VOCABULARY_FIELDS
QUARANTINE_EXPORT_COLUMNS = ("id", "raw_text", "reason", "source_file", "created_at")


def cleaned_chinese_content(story: dict[str, Any]) -> str:
    """Remove Markdown and replace historical target-word residues in Chinese."""

    content = str(story.get("content_cn") or "")
    content = content.replace("**", "").replace("__", "").replace("`", "")
    for item in story.get("used_words", []):
        word = str(item.get("word") or "").strip()
        if not word or not re.search(rf"\b{re.escape(word)}\b", content, re.IGNORECASE):
            continue
        definition = str(item.get("definition") or "")
        replacement = re.split(r"[；;，,、/|]", definition, maxsplit=1)[0].strip()
        replacement = re.sub(
            r"^(?:n|v|vt|vi|adj|adv|prep|conj|pron)\.?\s*",
            "",
            replacement,
            flags=re.IGNORECASE,
        ).strip()
        if not replacement or not re.search(r"[\u4e00-\u9fff]", replacement):
            replacement = "对应含义"
        content = re.sub(
            rf"\b{re.escape(word)}\b",
            replacement,
            content,
            flags=re.IGNORECASE,
        )
    return content.strip()


class TaskThread(QThread):
    """Run one repository or file task without blocking the GUI thread."""

    succeeded = pyqtSignal(object)
    failed = pyqtSignal(str, str)

    def __init__(self, task: Callable[[], Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._task = task

    def run(self) -> None:
        try:
            self.succeeded.emit(self._task())
        except Exception as exc:  # UI boundary: surface a friendly error and diagnostics.
            self.failed.emit(str(exc), traceback.format_exc())


def read_first_column(file_path: str) -> list[str]:
    """Read the first column from Excel or CSV in a worker thread."""

    path = Path(file_path)
    suffix = path.suffix.lower()
    if suffix in {".xlsx", ".xls"}:
        frame = pd.read_excel(path, header=None, usecols=[0], dtype=str)
    elif suffix == ".csv":
        last_error: Exception | None = None
        for encoding in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                frame = pd.read_csv(
                    path,
                    header=None,
                    usecols=[0],
                    dtype=str,
                    keep_default_na=False,
                    encoding=encoding,
                    on_bad_lines="skip",
                )
                break
            except UnicodeDecodeError as exc:
                last_error = exc
        else:
            raise ValueError("无法识别 CSV 编码，请另存为 UTF-8 CSV 后重试") from last_error
    else:
        raise ValueError("仅支持 .xlsx、.xls 或 .csv 文件")

    values = frame.iloc[:, 0].fillna("").astype(str).tolist()
    if values and values[0].strip().lower() in {
        "word",
        "words",
        "vocabulary",
        "单词",
        "生词",
        "词汇",
    }:
        values = values[1:]
    return values


def application_database_path() -> Path:
    """Use a per-user writable location that also works in a packaged .exe."""

    data_location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppDataLocation
    )
    if not data_location:
        data_location = str(APP_DIR / "data")
    return Path(data_location) / "vocabulary.db"


def application_config_directory() -> Path:
    config_location = QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.AppConfigLocation
    )
    if not config_location:
        config_location = str(APP_DIR / "data")
    return Path(config_location)


def import_file(repository: VocabularyRepository, file_path: str) -> ImportSummary:
    values = read_first_column(file_path)
    return repository.import_cleaned_values(values, Path(file_path).name)


def replace_from_file(
    repository: VocabularyRepository,
    file_path: str,
    backup_path: str,
) -> ImportSummary:
    """Force a successful backup before reading and atomically replacing the DB."""

    export_csv(repository, backup_path)
    values = read_first_column(file_path)
    return repository.replace_with_cleaned_values(values, Path(file_path).name)


def export_csv(repository: VocabularyRepository, file_path: str) -> int:
    rows = repository.export_rows()
    with open(file_path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


def export_quarantine_csv(repository: VocabularyRepository, file_path: str) -> int:
    rows = repository.fetch_quarantine()
    with open(file_path, "w", newline="", encoding="utf-8-sig") as stream:
        writer = csv.DictWriter(stream, fieldnames=QUARANTINE_EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return len(rows)


class ImportModeDialog(QDialog):
    """Minimal second-step confirmation for append versus replacement."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("选择导入方式")
        self.setMinimumWidth(520)

        title = QLabel("如何处理这次导入？")
        title.setObjectName("dialogTitle")
        self.append_radio = QRadioButton("智能追加（推荐）")
        self.append_radio.setChecked(True)
        append_help = QLabel("跳过已有单词，只追加新词；保留全部学习进度。")
        append_help.setObjectName("mutedLabel")
        self.replace_radio = QRadioButton("清空并完全覆盖")
        replace_help = QLabel("先强制导出当前词库备份，再清空旧数据并重建。")
        replace_help.setObjectName("warningLabel")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("继续")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(10)
        layout.addWidget(title)
        layout.addSpacing(4)
        layout.addWidget(self.append_radio)
        layout.addWidget(append_help)
        layout.addSpacing(10)
        layout.addWidget(self.replace_radio)
        layout.addWidget(replace_help)
        layout.addSpacing(12)
        layout.addWidget(buttons)

    @property
    def selected_mode(self) -> str:
        return "replace" if self.replace_radio.isChecked() else "append"


class APISettingsDialog(QDialog):
    def __init__(self, config: AIConfig, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._original_config = config
        self._selected_config = config
        self.setWindowTitle("API 设置")
        self.setMinimumWidth(560)

        self.base_url_edit = QLineEdit(config.base_url)
        self.base_url_edit.setPlaceholderText("https://api.openai.com/v1")
        self.api_key_edit = QLineEdit(config.api_key)
        self.api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.api_key_edit.setPlaceholderText("sk-…")
        self.model_edit = QLineEdit(config.model)
        self.model_edit.setPlaceholderText("gpt-4o-mini / deepseek-chat")

        show_key = QCheckBox("显示 API Key")
        show_key.toggled.connect(
            lambda checked: self.api_key_edit.setEchoMode(
                QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
            )
        )

        form = QFormLayout()
        form.setSpacing(13)
        form.addRow("API Base URL：", self.base_url_edit)
        form.addRow("API Key：", self.api_key_edit)
        form.addRow("", show_key)
        form.addRow("Model Name：", self.model_edit)

        note = QLabel(
            "兼容 OpenAI Chat Completions 协议。API Key 仅保存在本机配置文件中，"
            "不会显示在日志里。"
        )
        note.setWordWrap(True)
        note.setObjectName("mutedLabel")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(16)
        layout.addWidget(QLabel("大模型 API 配置"), 0)
        layout.addLayout(form)
        layout.addWidget(note)
        layout.addWidget(buttons)

    @property
    def selected_config(self) -> AIConfig:
        return self._selected_config

    def _validate_and_accept(self) -> None:
        candidate = AIConfig(
            base_url=self.base_url_edit.text(),
            api_key=self.api_key_edit.text(),
            model=self.model_edit.text(),
            batch_size=self._original_config.batch_size,
            max_concurrency=self._original_config.max_concurrency,
            request_timeout=self._original_config.request_timeout,
        )
        try:
            self._selected_config = candidate.validated()
        except ValueError as exc:
            QMessageBox.warning(self, "配置不完整", str(exc))
            return
        self.accept()


class AIProgressDialog(QDialog):
    cancel_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("AI 批量解析")
        # This is deliberately modeless: the main window keeps processing mouse,
        # paint, timer and queued-signal events while the worker is active.
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(620, 400)
        self._active = True
        self._cancel_sent = False

        self.status_label = QLabel("正在查找未回填单词…")
        self.status_label.setObjectName("dialogTitle")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)

        self.detail_label = QLabel("成功 0 · 失败 0")
        self.detail_label.setObjectName("mutedLabel")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("批次运行日志将在这里显示…")
        self.log_view.document().setMaximumBlockCount(300)

        self.action_button = QPushButton("终止任务")
        self.action_button.setObjectName("dangerButton")
        self.action_button.clicked.connect(self._action_clicked)

        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(self.action_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(13)
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.detail_label)
        layout.addWidget(self.log_view, 1)
        layout.addLayout(button_row)

    def set_total(self, total: int) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(0)
        self.status_label.setText(
            "没有需要解析的单词" if total == 0 else f"正在解析：0 / {total}…"
        )

    def update_progress(
        self,
        processed: int,
        total: int,
        succeeded: int,
        failed: int,
        current_word: str,
    ) -> None:
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(processed)
        self.status_label.setText(f"正在解析：{processed} / {total}…")
        self.detail_label.setText(
            f"成功 {succeeded} · 失败 {failed} · 最近批次：{current_word}"
        )

    def append_log(self, message: str) -> None:
        self.log_view.appendPlainText(message)

    def mark_completed(self, summary: dict[str, Any]) -> None:
        self._active = False
        total = int(summary.get("total", 0))
        processed = int(summary.get("processed", 0))
        succeeded = int(summary.get("succeeded", 0))
        failed = int(summary.get("failed", 0))
        cancelled = bool(summary.get("cancelled", False))
        self.progress_bar.setRange(0, max(1, total))
        self.progress_bar.setValue(processed)
        if total == 0:
            self.status_label.setText("所有单词均已完成回填")
        elif cancelled:
            self.status_label.setText(f"任务已终止：已处理 {processed} / {total}")
        else:
            self.status_label.setText(f"解析完成：{processed} / {total}")
        self.detail_label.setText(f"成功 {succeeded} · 失败 {failed}")
        self.action_button.setDisabled(False)
        self.action_button.setObjectName("")
        self.action_button.setText("关闭")

    def mark_error(self, message: str) -> None:
        self._active = False
        self.status_label.setText("AI 解析任务启动失败")
        self.detail_label.setText(message)
        self.append_log(message)
        self.action_button.setDisabled(False)
        self.action_button.setObjectName("")
        self.action_button.setText("关闭")

    def _action_clicked(self) -> None:
        if not self._active:
            self.accept()
            return
        if self._cancel_sent:
            return
        self._cancel_sent = True
        self.action_button.setDisabled(True)
        self.action_button.setText("正在终止…")
        self.status_label.setText("正在等待当前请求结束并终止后续批次…")
        self.cancel_requested.emit()

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API name
        if self._active:
            self._action_clicked()
            event.ignore()
            return
        event.accept()


class AddWordDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("手动新增单词")
        self.setMinimumWidth(420)

        self.word_edit = QLineEdit()
        self.word_edit.setPlaceholderText("仅输入一个英文单词，例如 serendipity")
        self.notes_edit = QLineEdit()
        self.notes_edit.setPlaceholderText("可选：备注或 Tag")

        form = QFormLayout()
        form.setSpacing(14)
        form.addRow("英文单词：", self.word_edit)
        form.addRow("备注 / Tag：", self.notes_edit)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("取消")
        buttons.accepted.connect(self._validate_and_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 22, 22, 22)
        layout.setSpacing(18)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _validate_and_accept(self) -> None:
        is_valid, _, reason = validate_word(self.word_edit.text())
        if not is_valid:
            QMessageBox.warning(self, "输入不合规", reason)
            self.word_edit.setFocus()
            return
        self.accept()


class QuarantineDialog(QDialog):
    def __init__(
        self,
        repository: VocabularyRepository,
        run_task: Callable[..., TaskThread],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.run_task = run_task
        self.setWindowTitle("过滤杂质词区 · Quarantine Box")
        self.resize(920, 560)

        intro = QLabel("这里保存导入时被规则拦截的词组、句子和含标点内容。")
        intro.setObjectName("mutedLabel")

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(
            ("选择", "ID", "拦截内容", "原因", "来源文件", "拦截时间")
        )
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)

        refresh_button = QPushButton("刷新")
        refresh_button.clicked.connect(self.reload)
        self.export_button = QPushButton("📥 导出拦截词为 CSV")
        self.export_button.clicked.connect(self.export_all)
        delete_button = QPushButton("🗑️ 删除选中")
        delete_button.clicked.connect(self.delete_selected)
        clear_button = QPushButton("清空全部")
        clear_button.setObjectName("dangerButton")
        clear_button.clicked.connect(self.clear_all)
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)

        button_row = QHBoxLayout()
        button_row.addWidget(refresh_button)
        button_row.addWidget(self.export_button)
        button_row.addWidget(delete_button)
        button_row.addWidget(clear_button)
        button_row.addStretch(1)
        button_row.addWidget(close_button)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.setSpacing(14)
        layout.addWidget(intro)
        layout.addWidget(self.table, 1)
        layout.addLayout(button_row)
        self.reload()

    def reload(self) -> None:
        self.run_task(self.repository.fetch_quarantine, self._populate)

    def _populate(self, rows: list[dict[str, Any]]) -> None:
        self.table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row_index, 0, check_item)
            values = (
                row["id"],
                row["raw_text"],
                row["reason"],
                row["source_file"] or "",
                row["created_at"],
            )
            for column, value in enumerate(values, start=1):
                self.table.setItem(row_index, column, QTableWidgetItem(str(value)))

    def _selected_ids(self) -> list[int]:
        selected_rows = {index.row() for index in self.table.selectionModel().selectedRows()}
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                selected_rows.add(row)
        return [int(self.table.item(row, 1).text()) for row in sorted(selected_rows)]

    def delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请勾选或选中要删除的拦截项。")
            return
        if QMessageBox.question(
            self,
            "确认删除",
            f"确定删除选中的 {len(ids)} 条拦截记录吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.run_task(lambda: self.repository.delete_quarantine(ids), lambda _: self.reload())

    def clear_all(self) -> None:
        if self.table.rowCount() == 0:
            return
        if QMessageBox.question(
            self,
            "确认清空",
            "确定永久删除过滤杂质词区中的全部记录吗？",
        ) != QMessageBox.StandardButton.Yes:
            return
        self.run_task(self.repository.clear_quarantine, lambda _: self.reload())

    def export_all(self) -> None:
        documents_directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        ) or str(Path.home())
        default_path = Path(documents_directory) / "linkcho_quarantine_export.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出全部拦截词",
            str(default_path),
            "CSV 文件 (*.csv)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".csv"):
            file_path += ".csv"

        self.export_button.setDisabled(True)

        def exported(count: int) -> None:
            self.export_button.setDisabled(False)
            QMessageBox.information(
                self,
                "导出完成",
                f"已导出 {count} 条拦截记录到：\n{file_path}",
            )

        def failed(message: str) -> None:
            self.export_button.setDisabled(False)
            QMessageBox.critical(self, "导出失败", message)

        self.run_task(
            lambda: export_quarantine_csv(self.repository, file_path),
            exported,
            failed,
        )


class PodcastProgressDialog(QDialog):
    """Modeless progress window; it never starts a nested modal event loop."""

    cancel_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Linkcho · 生成播客故事")
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.resize(580, 310)
        self._active = True
        self._cancel_sent = False
        self.status_label = QLabel("正在准备盲盒抽词…")
        self.status_label.setObjectName("dialogTitle")
        self.candidate_label = QLabel("候选词：等待抽取")
        self.candidate_label.setObjectName("mutedLabel")
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.document().setMaximumBlockCount(100)
        self.action_button = QPushButton("终止任务")
        self.action_button.setObjectName("dangerButton")
        self.action_button.clicked.connect(self._action_clicked)
        buttons = QHBoxLayout()
        buttons.addStretch(1)
        buttons.addWidget(self.action_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 20)
        layout.setSpacing(12)
        layout.addWidget(self.status_label)
        layout.addWidget(self.candidate_label)
        layout.addWidget(self.progress_bar)
        layout.addWidget(self.log_view, 1)
        layout.addLayout(buttons)

    def set_stage(self, text: str) -> None:
        self.status_label.setText(text)

    def set_candidate_count(self, count: int) -> None:
        self.candidate_label.setText(
            f"候选词：{count} 个；模型先筛选 30 个，正文实际采用 25-30 个"
        )

    def append_log(self, text: str) -> None:
        self.log_view.appendPlainText(text)

    def mark_completed(self, title: str) -> None:
        self._active = False
        self.progress_bar.setRange(0, 1)
        self.progress_bar.setValue(1)
        self.status_label.setText(f"生成完成：《{title}》")
        self.action_button.setObjectName("")
        self.action_button.setDisabled(False)
        self.action_button.setText("关闭")

    def mark_cancelled(self) -> None:
        self._active = False
        self.progress_bar.setRange(0, 1)
        self.status_label.setText("任务已终止；未入库内容不会改变单词状态")
        self.action_button.setObjectName("")
        self.action_button.setDisabled(False)
        self.action_button.setText("关闭")

    def mark_error(self, message: str) -> None:
        self._active = False
        self.progress_bar.setRange(0, 1)
        self.status_label.setText("生成失败")
        self.candidate_label.setText(message)
        self.append_log(message)
        self.action_button.setObjectName("")
        self.action_button.setDisabled(False)
        self.action_button.setText("关闭")

    def _action_clicked(self) -> None:
        if not self._active:
            self.accept()
            return
        if self._cancel_sent:
            return
        self._cancel_sent = True
        self.action_button.setDisabled(True)
        self.action_button.setText("正在终止…")
        self.status_label.setText("正在等待当前网络请求安全结束…")
        self.cancel_requested.emit()

    def closeEvent(self, event: Any) -> None:  # noqa: N802
        if self._active:
            self._action_clicked()
            event.ignore()
        else:
            event.accept()


class VocabularyDetailDialog(QDialog):
    def __init__(self, record: dict[str, Any], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"生词详情 · {record['word']}")
        self.resize(650, 520)
        view = QTextBrowser()
        view.setOpenExternalLinks(False)
        rows = (
            ("单词", record.get("word")),
            ("音标", record.get("soundMark")),
            ("中文释义", record.get("definition")),
            ("核心搭配", record.get("collocation")),
            ("近反义词辨析", record.get("relatedWords")),
            ("原生例句", record.get("exampleSentence")),
            ("备注 / Tag", record.get("notes")),
            ("状态", record.get("status")),
            ("最近使用", record.get("last_used_date")),
        )
        body = "".join(
            f"<p><b>{html.escape(label)}</b><br>{html.escape(str(value or '—'))}</p>"
            for label, value in rows
        )
        view.setHtml(f"<h2>{html.escape(str(record['word']))}</h2>{body}")
        close_button = QPushButton("关闭")
        close_button.clicked.connect(self.accept)
        button_row = QHBoxLayout()
        button_row.addStretch(1)
        button_row.addWidget(close_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 20, 20, 20)
        layout.addWidget(view, 1)
        layout.addLayout(button_row)


class PodcastPlayerDialog(QDialog):
    """Modeless MP3 player with linked English and Chinese reading panes."""

    vocabulary_requested = pyqtSignal(int, str)
    back_requested = pyqtSignal()

    def __init__(
        self,
        story: dict[str, Any],
        parent: QWidget | None = None,
        embedded: bool = False,
    ) -> None:
        super().__init__(parent)
        self.story = story
        self.embedded = embedded
        if embedded:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.podcast_id = int(story["id"])
        self._seeking = False
        self._known_duration_ms = max(
            0,
            int(float(story.get("audio_duration") or 0.0) * 1000),
        )
        self._word_by_id = {
            int(item["id"]): str(item["word"])
            for item in story.get("used_words", [])
            if item.get("word") and item.get("id") is not None
        }
        self._english_segments = self._split_english_segments(
            str(story["content_en"])
        )
        self._segment_word_counts = [
            max(
                1,
                len(re.findall(r"\b[A-Za-z]+(?:'[A-Za-z]+)?\b", segment)),
            )
            for segment in self._english_segments
        ]
        self._clean_chinese_text = cleaned_chinese_content(story)
        self._chinese_segments = self._split_chinese_segments(
            self._clean_chinese_text
        )
        self._active_segment_index = -1
        self._active_chinese_segment_index = -1

        self.setWindowTitle(f"沉浸式双语播客 · {story['title']}")
        self.resize(1120, 760)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)

        self.audio_output = QAudioOutput(self)
        self.audio_output.setVolume(1.0)
        self.player = QMediaPlayer(self)
        self.player.setAudioOutput(self.audio_output)
        self.player.positionChanged.connect(self._position_changed)
        self.player.durationChanged.connect(self._duration_changed)
        self.player.playbackStateChanged.connect(self._playback_state_changed)
        self.player.errorOccurred.connect(self._media_error)

        self.play_button = QPushButton("▶ 播放")
        self.play_button.clicked.connect(self.toggle_playback)
        self.seek_slider = QSlider(Qt.Orientation.Horizontal)
        self.seek_slider.setRange(0, max(1, self._known_duration_ms))
        self.seek_slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.seek_slider.sliderReleased.connect(self._seek_released)
        self.time_label = QLabel(
            f"00:00 / {self._format_time(self._known_duration_ms)}"
        )
        self.time_label.setMinimumWidth(110)
        self.status_label = QLabel("准备播放")
        self.status_label.setObjectName("mutedLabel")

        controls = QHBoxLayout()
        controls.setSpacing(10)
        controls.addWidget(self.play_button)
        controls.addWidget(self.seek_slider, 1)
        controls.addWidget(self.time_label)

        self.english_view = QTextBrowser()
        self.english_view.setOpenLinks(False)
        self.english_view.anchorClicked.connect(self._word_link_clicked)
        self.chinese_view = QTextBrowser()
        self._render_story()

        text_splitter = QSplitter(Qt.Orientation.Horizontal)
        text_splitter.addWidget(self.english_view)
        text_splitter.addWidget(self.chinese_view)
        text_splitter.setSizes([560, 560])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4 if embedded else 20, 4 if embedded else 18, 4 if embedded else 20, 4 if embedded else 18)
        layout.setSpacing(10)
        title = QLabel(str(story["title"]))
        title.setObjectName("dialogTitle")
        title_row = QHBoxLayout()
        if embedded:
            back_button = QPushButton("← 返回剧本库")
            back_button.setObjectName("secondaryButton")
            back_button.clicked.connect(self.back_requested.emit)
            title_row.addWidget(back_button)
        title_row.addWidget(title)
        title_row.addStretch(1)
        layout.addLayout(title_row)
        layout.addLayout(controls)
        layout.addWidget(self.status_label)
        layout.addWidget(text_splitter, 1)

        audio_path = Path(str(story.get("audio_path") or ""))
        self.player.setSource(QUrl.fromLocalFile(str(audio_path.resolve())))

    def _render_story(self) -> None:
        self._render_english(-1)
        self._render_chinese(-1)

    def _render_english(self, active_index: int) -> None:
        word_ids = {
            word.casefold(): vocabulary_id
            for vocabulary_id, word in self._word_by_id.items()
        }

        rendered_segments: list[str] = []
        for index, segment in enumerate(self._english_segments):
            escaped = html.escape(segment)

            def bold_to_link(match: re.Match[str]) -> str:
                text = html.unescape(match.group(1)).strip()
                vocabulary_id = word_ids.get(text.casefold())
                strong = f"<strong>{html.escape(text)}</strong>"
                return (
                    strong
                    if vocabulary_id is None
                    else f'<a href="vocab:{vocabulary_id}">{strong}</a>'
                )

            sentence_html = re.sub(r"\*\*(.+?)\*\*", bold_to_link, escaped)
            sentence_html = sentence_html.replace("\n", "<br>")
            style = (
                "background-color:#fff1a8; color:#18212f; "
                "padding:2px 3px;"
                if index == active_index
                else ""
            )
            rendered_segments.append(
                f'<a name="segment-{index}"></a>'
                f'<span style="{style}">{sentence_html}</span> '
            )
        self.english_view.setHtml(
            "<h3>English · 当前句子将随播放自动高亮</h3>"
            "<p style='line-height:1.75'>"
            + "".join(rendered_segments)
            + "</p>"
        )

    def _render_chinese(self, active_index: int) -> None:
        rendered_segments: list[str] = []
        for index, segment in enumerate(self._chinese_segments):
            sentence_html = html.escape(segment).replace("\n", "<br>")
            style = (
                "background-color:#fff1a8; color:#18212f; "
                "padding:2px 3px;"
                if index == active_index
                else ""
            )
            rendered_segments.append(
                f'<a name="cn-segment-{index}"></a>'
                f'<span style="{style}">{sentence_html}</span> '
            )
        self.chinese_view.setHtml(
            "<h3>中文翻译 · 对应句子将同步高亮</h3>"
            "<p style='line-height:1.75'>"
            + "".join(rendered_segments)
            + "</p>"
        )

    @staticmethod
    def _split_english_segments(text: str) -> list[str]:
        """Split a script into readable sentence-sized tracking segments."""

        normalized = str(text).replace("\r\n", "\n").strip()
        segments = [
            match.group(0).strip()
            for match in re.finditer(
                r".+?(?:[.!?](?:[\"”’])?(?=\s|$)|$)",
                normalized,
                flags=re.DOTALL,
            )
            if match.group(0).strip()
        ]
        return segments or [normalized]

    @staticmethod
    def _split_chinese_segments(text: str) -> list[str]:
        """Split Chinese translation without requiring spaces after punctuation."""

        normalized = str(text).replace("\r\n", "\n").strip()
        segments = [
            match.group(0).strip()
            for match in re.finditer(
                r".+?(?:[。！？!?](?:[\"”’])?|$)",
                normalized,
                flags=re.DOTALL,
            )
            if match.group(0).strip()
        ]
        return segments or [normalized]

    def toggle_playback(self) -> None:
        if self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.player.pause()
        else:
            self.player.play()

    def start_playback(self) -> None:
        if not self.embedded:
            self.show()
            self.raise_()
            self.activateWindow()
        QTimer.singleShot(0, self.player.play)

    def _seek_released(self) -> None:
        self.player.setPosition(self.seek_slider.value())
        self._seeking = False

    def _duration_changed(self, duration_ms: int) -> None:
        if duration_ms > 0:
            self._known_duration_ms = duration_ms
            self.seek_slider.setRange(0, duration_ms)
            self._update_time_label(self.player.position())

    def _position_changed(self, position_ms: int) -> None:
        if not self._seeking:
            self.seek_slider.setValue(position_ms)
        self._update_time_label(position_ms)
        duration = max(1, self.player.duration(), self._known_duration_ms)
        ratio = min(1.0, max(0.0, position_ms / duration))
        if self._english_segments:
            total_words = max(1, sum(self._segment_word_counts))
            target_word = ratio * total_words
            active_index = len(self._english_segments) - 1
            cumulative = 0
            for index, count in enumerate(self._segment_word_counts):
                cumulative += count
                if target_word < cumulative:
                    active_index = index
                    break
            if active_index != self._active_segment_index:
                self._active_segment_index = active_index
                self._render_english(active_index)
                self.english_view.scrollToAnchor(f"segment-{active_index}")
                if self._chinese_segments:
                    chinese_index = min(
                        len(self._chinese_segments) - 1,
                        int(
                            (active_index + 0.5)
                            / max(1, len(self._english_segments))
                            * len(self._chinese_segments)
                        ),
                    )
                    if chinese_index != self._active_chinese_segment_index:
                        self._active_chinese_segment_index = chinese_index
                        self._render_chinese(chinese_index)
                        self.chinese_view.scrollToAnchor(
                            f"cn-segment-{chinese_index}"
                        )

    def _update_time_label(self, position_ms: int) -> None:
        duration = max(self.player.duration(), self._known_duration_ms)
        self.time_label.setText(
            f"{self._format_time(position_ms)} / {self._format_time(duration)}"
        )

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        if state == QMediaPlayer.PlaybackState.PlayingState:
            self.play_button.setText("⏸ 暂停")
            self.status_label.setText("正在播放 · 双语正文随进度联动")
        else:
            self.play_button.setText("▶ 播放")
            self.status_label.setText("已暂停" if self.player.position() else "准备播放")

    def _media_error(self, _error: QMediaPlayer.Error, message: str) -> None:
        if message:
            self.status_label.setText(f"播放失败：{message}")

    def _word_link_clicked(self, url: QUrl) -> None:
        if url.scheme() != "vocab":
            return
        try:
            vocabulary_id = int(url.path() or url.toString().split(":", 1)[1])
        except (ValueError, IndexError):
            return
        self.vocabulary_requested.emit(
            vocabulary_id,
            self._word_by_id.get(vocabulary_id, ""),
        )

    @staticmethod
    def _format_time(milliseconds: int) -> str:
        seconds = max(0, int(milliseconds) // 1000)
        return f"{seconds // 60:02d}:{seconds % 60:02d}"

    def closeEvent(self, event: Any) -> None:
        self.player.stop()
        super().closeEvent(event)


class PodcastHistoryDialog(QDialog):
    vocabulary_requested = pyqtSignal(int, str)

    def __init__(
        self,
        repository: VocabularyRepository,
        run_task: Callable[..., TaskThread],
        audio_cache_directory: str | Path | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.repository = repository
        self.run_task = run_task
        self.setWindowTitle("Linkcho · 历史播客剧本库")
        self.resize(1250, 760)
        self.setModal(False)
        self.setWindowModality(Qt.WindowModality.NonModal)
        self._load_revision = 0
        self._word_by_id: dict[int, str] = {}
        self._story_cache: dict[int, dict[str, Any]] = {}
        self.audio_cache_directory = Path(
            audio_cache_directory
            or application_config_directory() / "audio_cache"
        ).expanduser().resolve()
        self._tts_workers: dict[int, TTSGenerationWorker] = {}
        self._player_dialog: PodcastPlayerDialog | None = None

        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("搜索标题、英文正文或中文翻译…")
        self.search_timer = QTimer(self)
        self.search_timer.setSingleShot(True)
        self.search_timer.setInterval(220)
        self.search_timer.timeout.connect(self.reload)
        self.search_edit.textChanged.connect(lambda _: self.search_timer.start())
        delete_button = QPushButton("🗑️ 删除选中剧本")
        delete_button.setObjectName("dangerButton")
        delete_button.clicked.connect(self.delete_selected)
        self.audio_status_label = QLabel("音频就绪")
        self.audio_status_label.setObjectName("mutedLabel")
        top_row = QHBoxLayout()
        top_row.addWidget(self.search_edit, 1)
        top_row.addWidget(self.audio_status_label)
        top_row.addWidget(delete_button)

        self.table = QTableWidget(0, 6)
        self.table.setHorizontalHeaderLabels(("选择", "ID", "标题", "风格", "生成日期", "音频"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(34)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        for column in (0, 1, 3, 4, 5):
            self.table.horizontalHeader().setSectionResizeMode(
                column, QHeaderView.ResizeMode.ResizeToContents
            )
        self.table.itemSelectionChanged.connect(self._load_selected_story)

        self.english_view = QTextBrowser()
        self.english_view.setOpenLinks(False)
        self.english_view.anchorClicked.connect(self._word_link_clicked)
        self.chinese_view = QTextBrowser()
        tabs = QTabWidget()
        tabs.addTab(self.english_view, "英文正文 · 点击加粗生词可定位")
        tabs.addTab(self.chinese_view, "中文翻译")
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self.table)
        splitter.addWidget(tabs)
        splitter.setSizes([470, 760])
        self.library_page = QWidget()
        library_layout = QVBoxLayout(self.library_page)
        library_layout.setContentsMargins(20, 18, 20, 18)
        library_layout.setSpacing(12)
        library_layout.addLayout(top_row)
        library_layout.addWidget(splitter, 1)

        self.pages = QStackedWidget()
        self.pages.addWidget(self.library_page)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.pages)

    def reload(
        self,
        select_id: int | None = None,
        initial_story: dict[str, Any] | None = None,
    ) -> None:
        self._load_revision += 1
        revision = self._load_revision
        query = self.search_edit.text()
        if initial_story is not None:
            initial_id = int(initial_story["id"])
            self._story_cache[initial_id] = initial_story
            self._populate_library(
                [self._story_to_summary(initial_story)],
                initial_story,
                initial_id,
            )

        def load_library() -> dict[str, Any]:
            rows = self.repository.fetch_podcasts(query)
            if not rows:
                return {"rows": [], "story": None}
            available_ids = {int(row["id"]) for row in rows}
            selected_id = (
                int(select_id)
                if select_id is not None and int(select_id) in available_ids
                else int(rows[0]["id"])
            )
            story = self._story_cache.get(selected_id)
            if story is None:
                story = self.repository.fetch_podcast(selected_id)
            return {"rows": rows, "story": story}

        def loaded(payload: dict[str, Any]) -> None:
            if revision != self._load_revision:
                return
            rows = payload.get("rows", [])
            story = payload.get("story")
            if isinstance(story, dict):
                self._story_cache[int(story["id"])] = story
            self._populate_library(rows, story, select_id)

        self.run_task(load_library, loaded)

    def _populate_library(
        self,
        rows: list[dict[str, Any]],
        story: dict[str, Any] | None,
        select_id: int | None,
    ) -> None:
        self.table.blockSignals(True)
        try:
            self.table.setRowCount(len(rows))
            row_to_select = 0
            for index, record in enumerate(rows):
                checkbox = QTableWidgetItem()
                checkbox.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
                checkbox.setCheckState(Qt.CheckState.Unchecked)
                self.table.setItem(index, 0, checkbox)
                self.table.setItem(index, 1, QTableWidgetItem(str(record["id"])))
                title = f"{record['title']}  ·  {record['used_word_count']} 词"
                self.table.setItem(index, 2, QTableWidgetItem(title))
                self.table.setItem(index, 3, QTableWidgetItem(str(record.get("style") or "")))
                self.table.setItem(index, 4, QTableWidgetItem(str(record["created_date"])))
                self.table.setCellWidget(
                    index,
                    5,
                    self._build_audio_controls(record),
                )
                if select_id is not None and int(record["id"]) == select_id:
                    row_to_select = index
            if rows:
                self.table.selectRow(row_to_select)
            else:
                self.english_view.setHtml("<p>还没有历史剧本。</p>")
                self.chinese_view.clear()
        finally:
            self.table.blockSignals(False)
        if isinstance(story, dict):
            self._display_story(story)

    def _build_audio_controls(self, record: dict[str, Any]) -> QWidget:
        podcast_id = int(record["id"])
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(4)

        def compact_button(text: str) -> QPushButton:
            button = QPushButton(text)
            button.setFixedHeight(27)
            button.setStyleSheet("QPushButton { padding: 2px 7px; }")
            return button

        audio_path_text = str(record.get("audio_path") or "")
        audio_ready = bool(audio_path_text) and Path(audio_path_text).is_file()
        if audio_ready:
            play_button = compact_button("▶ 播放")
            play_button.setToolTip("打开沉浸式双语播放面板")
            play_button.clicked.connect(
                lambda _checked=False, item_id=podcast_id: self._audio_action(item_id)
            )
            download_button = compact_button("⬇ 下载")
            download_button.setToolTip("将缓存 MP3 另存到指定位置")
            download_button.clicked.connect(
                lambda _checked=False, item_id=podcast_id: self._download_audio(item_id)
            )
            row.addWidget(play_button)
            row.addWidget(download_button)
        elif podcast_id in self._tts_workers:
            generating_button = compact_button("⏳ 生成中")
            generating_button.setDisabled(True)
            row.addWidget(generating_button)
        else:
            generate_button = compact_button("🔊 生成音频")
            generate_button.clicked.connect(
                lambda _checked=False, item_id=podcast_id: self._audio_action(item_id)
            )
            row.addWidget(generate_button)
        return container

    @staticmethod
    def _story_to_summary(story: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": int(story["id"]),
            "title": str(story["title"]),
            "style": str(story.get("style") or ""),
            "created_date": str(story.get("created_date") or ""),
            "audio_status": str(story.get("audio_status") or "not_generated"),
            "audio_path": str(story.get("audio_path") or ""),
            "audio_duration": float(story.get("audio_duration") or 0.0),
            "used_word_count": len(story.get("used_words", [])),
        }

    def _selected_id(self) -> int | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        item = self.table.item(rows[0].row(), 1)
        return int(item.text()) if item else None

    def _load_selected_story(self) -> None:
        podcast_id = self._selected_id()
        if podcast_id is None:
            return
        cached = self._story_cache.get(podcast_id)
        if cached is not None:
            self._display_story(cached)
            return
        self.english_view.setHtml("<p>正在加载剧本…</p>")
        self.chinese_view.setHtml("<p>正在加载中文翻译…</p>")

        def loaded(story: dict[str, Any]) -> None:
            self._story_cache[podcast_id] = story
            if self._selected_id() == podcast_id:
                self._display_story(story)

        self.run_task(
            lambda: self.repository.fetch_podcast(podcast_id),
            loaded,
        )

    def _display_story(self, story: dict[str, Any]) -> None:
        word_ids = {
            str(item.get("word", "")).casefold(): int(item["id"])
            for item in story.get("used_words", [])
            if item.get("word") and item.get("id") is not None
        }
        self._word_by_id = {
            int(item["id"]): str(item["word"])
            for item in story.get("used_words", [])
            if item.get("word") and item.get("id") is not None
        }
        escaped = html.escape(str(story["content_en"]))

        def bold_to_link(match: re.Match[str]) -> str:
            text = html.unescape(match.group(1)).strip()
            vocabulary_id = word_ids.get(text.casefold())
            strong = f"<strong>{html.escape(text)}</strong>"
            return strong if vocabulary_id is None else f'<a href="vocab:{vocabulary_id}">{strong}</a>'

        rendered = re.sub(r"\*\*(.+?)\*\*", bold_to_link, escaped)
        rendered = rendered.replace("\n", "<br>")
        self.english_view.setHtml(
            f"<h2>{html.escape(str(story['title']))}</h2><p>{rendered}</p>"
        )
        cn = html.escape(cleaned_chinese_content(story)).replace("\n", "<br>")
        self.chinese_view.setHtml(f"<h2>{html.escape(str(story['title']))}</h2><p>{cn}</p>")

    def _word_link_clicked(self, url: QUrl) -> None:
        if url.scheme() != "vocab":
            return
        try:
            vocabulary_id = int(url.path() or url.toString().split(":", 1)[1])
        except (ValueError, IndexError):
            return
        self.vocabulary_requested.emit(
            vocabulary_id,
            self._word_by_id.get(vocabulary_id, ""),
        )

    def _selected_ids(self) -> list[int]:
        checked_rows: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                checked_rows.append(row)
        rows = checked_rows or [
            index.row() for index in self.table.selectionModel().selectedRows()
        ]
        return [int(self.table.item(row, 1).text()) for row in sorted(set(rows))]

    def delete_selected(self) -> None:
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请勾选或选中要删除的历史剧本。")
            return
        if QMessageBox.question(self, "确认删除", f"确定删除 {len(ids)} 篇历史剧本吗？") != QMessageBox.StandardButton.Yes:
            return
        self.run_task(lambda: self.repository.delete_podcasts(ids), lambda _: self.reload())

    def _audio_action(self, podcast_id: int) -> None:
        story = self._story_cache.get(int(podcast_id))
        if story is not None:
            self._start_or_play_audio(story)
            return
        self.audio_status_label.setText("正在读取剧本…")
        self.run_task(
            lambda: self.repository.fetch_podcast(int(podcast_id)),
            self._start_or_play_audio,
            lambda message: QMessageBox.warning(self, "无法读取剧本", message),
        )

    def _start_or_play_audio(self, story: dict[str, Any]) -> None:
        podcast_id = int(story["id"])
        self._story_cache[podcast_id] = story
        audio_path_text = str(story.get("audio_path") or "")
        if audio_path_text and Path(audio_path_text).is_file():
            self.audio_status_label.setText("音频已就绪")
            self._open_audio_player(story)
            return
        if podcast_id in self._tts_workers:
            self.audio_status_label.setText("这篇播客正在生成音频…")
            return

        parent = self.parentWidget() or self
        worker = TTSGenerationWorker(
            self.repository,
            podcast_id,
            str(story["content_en"]),
            self.audio_cache_directory,
            DEFAULT_VOICE,
            parent,
        )
        self._tts_workers[podcast_id] = worker
        worker.stage_changed.connect(self.audio_status_label.setText)
        worker.completed.connect(self._audio_generated)
        worker.failed.connect(self._audio_failed)
        worker.finished.connect(
            lambda item_id=podcast_id, item_worker=worker: self._tts_finished(
                item_id,
                item_worker,
            )
        )
        worker.finished.connect(worker.deleteLater)
        self.audio_status_label.setText("正在准备生成美音 MP3…")
        self._set_audio_status_control(podcast_id, "⏳ 生成中", disabled=True)
        worker.start()

    def _audio_generated(self, story_object: object) -> None:
        if not isinstance(story_object, dict):
            return
        story = story_object
        podcast_id = int(story["id"])
        self._story_cache[podcast_id] = story
        duration = float(story.get("audio_duration") or 0.0)
        self.audio_status_label.setText(
            "音频生成完成 · "
            + PodcastPlayerDialog._format_time(round(duration * 1000))
        )
        self._replace_audio_controls(podcast_id, story)
        if self.isVisible():
            self._open_audio_player(story)

    def _audio_failed(self, podcast_id: int, message: str) -> None:
        self.audio_status_label.setText("音频生成失败，可点击重试")
        self._set_audio_status_control(int(podcast_id), "🔊 重新生成")
        QMessageBox.warning(self, "音频生成失败", message)

    def _tts_finished(
        self,
        podcast_id: int,
        worker: TTSGenerationWorker,
    ) -> None:
        if self._tts_workers.get(int(podcast_id)) is worker:
            self._tts_workers.pop(int(podcast_id), None)

    def _replace_audio_controls(
        self,
        podcast_id: int,
        record: dict[str, Any],
    ) -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 1)
            if item is None or int(item.text()) != int(podcast_id):
                continue
            self.table.setCellWidget(row, 5, self._build_audio_controls(record))
            break

    def _set_audio_status_control(
        self,
        podcast_id: int,
        text: str,
        disabled: bool = False,
    ) -> None:
        for row_index in range(self.table.rowCount()):
            item = self.table.item(row_index, 1)
            if item is None or int(item.text()) != int(podcast_id):
                continue
            container = QWidget()
            layout = QHBoxLayout(container)
            layout.setContentsMargins(2, 2, 2, 2)
            button = QPushButton(text)
            button.setFixedHeight(27)
            button.setStyleSheet("QPushButton { padding: 2px 7px; }")
            button.setDisabled(disabled)
            if not disabled:
                button.clicked.connect(
                    lambda _checked=False, item_id=podcast_id: self._audio_action(item_id)
                )
            layout.addWidget(button)
            self.table.setCellWidget(row_index, 5, container)
            break

    def _download_audio(self, podcast_id: int) -> None:
        story = self._story_cache.get(int(podcast_id))
        if story is not None:
            self._choose_audio_destination(story)
            return
        self.run_task(
            lambda: self.repository.fetch_podcast(int(podcast_id)),
            self._choose_audio_destination,
            lambda message: QMessageBox.warning(self, "无法下载音频", message),
        )

    def _choose_audio_destination(self, story: dict[str, Any]) -> None:
        source = Path(str(story.get("audio_path") or ""))
        if not source.is_file():
            QMessageBox.warning(self, "音频不存在", "缓存音频已丢失，请重新生成。")
            self._set_audio_status_control(int(story["id"]), "🔊 重新生成")
            return
        safe_title = re.sub(r'[<>:"/\\|?*]+', "_", str(story["title"])).strip(" .")
        safe_title = safe_title or f"podcast_{int(story['id'])}"
        download_directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DownloadLocation
        ) or str(APP_DIR)
        destination, _ = QFileDialog.getSaveFileName(
            self,
            "保存播客音频",
            str(Path(download_directory) / f"{safe_title}.mp3"),
            "MP3 音频 (*.mp3)",
        )
        if not destination:
            return
        target = Path(destination)
        if target.suffix.lower() != ".mp3":
            target = target.with_suffix(".mp3")
        if source.resolve() == target.expanduser().resolve():
            self.audio_status_label.setText("音频已经位于所选位置")
            return

        self.audio_status_label.setText("正在复制音频…")
        self.run_task(
            lambda: shutil.copy2(source, target),
            lambda _result: self.audio_status_label.setText(
                f"音频已保存到：{target}"
            ),
            lambda message: QMessageBox.warning(self, "保存失败", message),
        )

    def _open_audio_player(self, story: dict[str, Any]) -> None:
        podcast_id = int(story["id"])
        if (
            self._player_dialog is not None
            and self._player_dialog.podcast_id == podcast_id
        ):
            self.pages.setCurrentWidget(self._player_dialog)
            self._player_dialog.start_playback()
            return
        if self._player_dialog is not None:
            self.pages.removeWidget(self._player_dialog)
            self._player_dialog.close()
            self._player_dialog.deleteLater()
        dialog = PodcastPlayerDialog(story, self.pages, embedded=True)
        self._player_dialog = dialog
        dialog.vocabulary_requested.connect(self._player_vocabulary_requested)
        dialog.back_requested.connect(self._show_library_page)
        dialog.finished.connect(
            lambda _result, item=dialog: self._player_closed(item)
        )
        self.pages.addWidget(dialog)
        self.pages.setCurrentWidget(dialog)
        dialog.start_playback()

    def _show_library_page(self) -> None:
        if self._player_dialog is not None:
            self._player_dialog.player.pause()
        self.pages.setCurrentWidget(self.library_page)

    def _player_vocabulary_requested(self, vocabulary_id: int, word: str) -> None:
        self.vocabulary_requested.emit(vocabulary_id, word)
        if self._player_dialog is not None:
            display_word = word or str(vocabulary_id)
            self._player_dialog.status_label.setText(
                f"已在主词库定位：{display_word} · 播放窗口保持打开"
            )

    def _player_closed(self, dialog: PodcastPlayerDialog) -> None:
        if self._player_dialog is dialog:
            self.pages.setCurrentWidget(self.library_page)
            self.pages.removeWidget(dialog)
            self._player_dialog = None

    def closeEvent(self, event: Any) -> None:
        if any(worker.isRunning() for worker in self._tts_workers.values()):
            # Keep the dialog object and its signal connections alive while the
            # background network request finishes; closing merely hides it.
            event.ignore()
            self.hide()
            return
        if self._player_dialog is not None:
            self._player_dialog.close()
        super().closeEvent(event)


class MainWindow(QMainWindow):
    def __init__(
        self,
        repository: VocabularyRepository,
        config_store: AIConfigStore | None = None,
        ai_log_path: str | Path | None = None,
    ) -> None:
        super().__init__()
        self.repository = repository
        config_directory = application_config_directory()
        self.config_store = config_store or AIConfigStore(
            config_directory / "ai_config.json"
        )
        self.ai_log_path = Path(ai_log_path or config_directory / "ai_parser.log")
        self._threads: set[TaskThread] = set()
        self._ai_worker: AIParseWorker | None = None
        self._ai_progress_dialog: AIProgressDialog | None = None
        self._podcast_worker: PodcastGenerationWorker | None = None
        self._podcast_progress_dialog: PodcastProgressDialog | None = None
        self._podcast_launch_pending = False
        self._history_dialog: PodcastHistoryDialog | None = None
        self._detail_dialog: VocabularyDetailDialog | None = None
        self._pending_focus_record: dict[str, Any] | None = None
        self._ai_launch_pending = False
        self._visible_row_by_id: dict[int, int] = {}
        self._ai_pending_refresh_ids: set[int] = set()
        self._ai_patch_inflight = False
        self._load_revision = 0
        self._populating_table = False
        self._busy_count = 0
        self._pronunciation_request = 0
        self._pronunciation_audio_output = QAudioOutput(self)
        self._pronunciation_audio_output.setVolume(1.0)
        self._pronunciation_player = QMediaPlayer(self)
        self._pronunciation_player.setAudioOutput(
            self._pronunciation_audio_output
        )

        self.setWindowTitle("Linkcho 桌面版 · 把单词串成故事，听成播客")
        self.resize(1440, 820)
        self.setMinimumSize(1080, 680)

        self._build_ui()
        self._apply_styles()
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(180)
        self._search_timer.timeout.connect(self.reload_table)
        self._ai_refresh_timer = QTimer(self)
        self._ai_refresh_timer.setSingleShot(True)
        self._ai_refresh_timer.setInterval(350)
        self._ai_refresh_timer.timeout.connect(self._flush_ai_incremental_refresh)
        self.reload_table()

    def _build_ui(self) -> None:
        central = QWidget()
        self.setCentralWidget(central)

        title = QLabel("Linkcho 桌面版")
        title.setObjectName("titleLabel")
        subtitle = QLabel("把想记住的单词，串成故事，听成播客。")
        subtitle.setObjectName("mutedLabel")

        heading = QVBoxLayout()
        heading.setSpacing(2)
        heading.addWidget(title)
        heading.addWidget(subtitle)

        self.api_settings_button = QPushButton("⚙️ API 与模型设置")
        self.api_settings_button.clicked.connect(self.open_api_settings)
        brand_row = QHBoxLayout()
        brand_row.addLayout(heading)
        brand_row.addStretch(1)
        brand_row.addWidget(self.api_settings_button)

        self.workspace_tabs = QTabWidget()
        self.workspace_tabs.setObjectName("workspaceTabs")

        # Vocabulary workspace: importing, cleaning, parsing and CRUD live together.
        vocabulary_page = QWidget()
        vocabulary_layout = QVBoxLayout(vocabulary_page)
        vocabulary_layout.setContentsMargins(14, 16, 14, 14)
        vocabulary_layout.setSpacing(12)

        vocabulary_title = QLabel("词库管理")
        vocabulary_title.setObjectName("sectionTitle")
        vocabulary_hint = QLabel("导入与清洗数据、AI 回填字段，并在表格中直接维护学习状态。")
        vocabulary_hint.setObjectName("mutedLabel")
        vocabulary_heading = QVBoxLayout()
        vocabulary_heading.setSpacing(2)
        vocabulary_heading.addWidget(vocabulary_title)
        vocabulary_heading.addWidget(vocabulary_hint)

        self.import_button = QPushButton("📂 导入 Excel / CSV 词库")
        self.import_button.setObjectName("primaryButton")
        self.import_button.clicked.connect(self.choose_import_file)
        quarantine_button = QPushButton("🔍 查看拦截词组/句子")
        quarantine_button.clicked.connect(self.open_quarantine)
        self.ai_parse_button = QPushButton("✨ 一键 AI 解析未回填单词")
        self.ai_parse_button.setObjectName("aiButton")
        self.ai_parse_button.clicked.connect(self.start_ai_parse)
        self.export_button = QPushButton("📤 导出当前词库")
        self.export_button.clicked.connect(self.choose_export_file)

        data_tools = QHBoxLayout()
        data_tools.setSpacing(8)
        data_tools.addWidget(self.import_button)
        data_tools.addWidget(self.export_button)
        data_tools.addWidget(quarantine_button)
        data_tools.addStretch(1)
        data_tools.addWidget(self.ai_parse_button)

        self.search_edit = QLineEdit()
        self.search_edit.setClearButtonEnabled(True)
        self.search_edit.setPlaceholderText("搜索单词、中文释义或备注…")
        self.search_edit.textChanged.connect(lambda _: self._search_timer.start())

        self.status_filter = QComboBox()
        self.status_filter.addItem("全部状态", "all")
        self.status_filter.addItem("待学习 (status ≥ 0)", "learning")
        self.status_filter.addItem("未用过 (status = 0)", "unused")
        self.status_filter.addItem("已归档 (status = -1)", "archived")
        self.status_filter.currentIndexChanged.connect(lambda _: self.reload_table())

        filter_row = QHBoxLayout()
        filter_row.setSpacing(12)
        filter_row.addWidget(self.search_edit, 1)
        filter_row.addWidget(self.status_filter)

        self.table = QTableWidget(0, len(TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels([label for _, label in TABLE_COLUMNS])
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setEditTriggers(
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
        )
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(False)
        self.table.setWordWrap(False)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(38)
        self.table.cellChanged.connect(self._cell_changed)
        self.table.cellClicked.connect(self._table_cell_clicked)

        header = self.table.horizontalHeader()
        header.setStretchLastSection(True)
        for column in (0, 1, 8, 9):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.ResizeToContents)
        for column in (4, 5, 6, 7, 10):
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        self.table.setColumnWidth(4, 180)
        self.table.setColumnWidth(5, 170)
        self.table.setColumnWidth(6, 190)
        self.table.setColumnWidth(7, 260)
        self.table.setColumnWidth(10, 180)

        add_button = QPushButton("➕ 手动新增单词")
        add_button.clicked.connect(self.add_word)
        delete_button = QPushButton("🗑️ 批量/单个删除")
        delete_button.setObjectName("dangerButton")
        delete_button.clicked.connect(self.delete_words)

        self.count_label = QLabel("正在加载…")
        self.count_label.setObjectName("mutedLabel")
        button_row = QHBoxLayout()
        button_row.setSpacing(10)
        button_row.addWidget(add_button)
        button_row.addWidget(delete_button)
        button_row.addStretch(1)
        button_row.addWidget(self.count_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(3)
        self.progress.hide()

        vocabulary_layout.addLayout(vocabulary_heading)
        vocabulary_layout.addLayout(data_tools)
        vocabulary_layout.addLayout(filter_row)
        vocabulary_layout.addWidget(self.progress)
        vocabulary_layout.addWidget(self.table, 1)
        vocabulary_layout.addLayout(button_row)

        # Podcast workspace: style, generation and history form one clear flow.
        podcast_page = QWidget()
        podcast_layout = QVBoxLayout(podcast_page)
        podcast_layout.setContentsMargins(22, 22, 22, 22)
        podcast_layout.setSpacing(16)

        studio_title = QLabel("播客创作工作台")
        studio_title.setObjectName("sectionTitle")
        studio_hint = QLabel("从词库盲盒抽取候选词，生成一篇自然短故事，并转换为可跟读的双语语音播客。")
        studio_hint.setObjectName("mutedLabel")
        podcast_layout.addWidget(studio_title)
        podcast_layout.addWidget(studio_hint)

        studio_card = QFrame()
        studio_card.setObjectName("card")
        studio_card_layout = QVBoxLayout(studio_card)
        studio_card_layout.setContentsMargins(22, 20, 22, 22)
        studio_card_layout.setSpacing(12)
        step_label = QLabel("1  选择故事风格")
        step_label.setObjectName("cardTitle")
        studio_card_layout.addWidget(step_label)

        self.podcast_style_combo = QComboBox()
        self.podcast_style_combo.setMinimumWidth(240)
        self.podcast_style_combo.setMaxVisibleItems(8)
        self.podcast_style_combo.addItem("🎲 随机 / 盲盒", "随机/盲盒")
        self.podcast_style_combo.addItem("💼 职场插曲", "职场插曲")
        self.podcast_style_combo.addItem("🕵️ 悬疑 / 反转短剧", "悬疑/反转短剧")
        self.podcast_style_combo.addItem("☕ 日常情景对话", "日常情景对话")
        self.podcast_style_combo.addItem("🧙 虚构 / 奇幻", "虚构/奇幻")
        style_row = QHBoxLayout()
        style_row.addWidget(QLabel("预设风格"))
        style_row.addWidget(self.podcast_style_combo)
        style_row.addStretch(1)
        studio_card_layout.addLayout(style_row)

        custom_label = QLabel("2  补充自定义要求（可选）")
        custom_label.setObjectName("cardTitle")
        studio_card_layout.addWidget(custom_label)
        self.custom_style_edit = QLineEdit()
        self.custom_style_edit.setPlaceholderText("例如：两位同事在雨夜咖啡馆复盘项目，语气轻松，结尾有小反转…")
        studio_card_layout.addWidget(self.custom_style_edit)

        self.generate_podcast_button = QPushButton("✨ 生成新的播客故事")
        self.generate_podcast_button.setObjectName("aiButton")
        self.generate_podcast_button.clicked.connect(self.start_podcast_generation)
        self.history_button = QPushButton("📚 打开历史播客剧本库")
        self.history_button.setObjectName("primaryButton")
        self.history_button.clicked.connect(self.open_podcast_history)
        studio_actions = QHBoxLayout()
        studio_actions.addWidget(self.generate_podcast_button)
        studio_actions.addWidget(self.history_button)
        studio_actions.addStretch(1)
        studio_card_layout.addLayout(studio_actions)

        workflow_card = QFrame()
        workflow_card.setObjectName("softCard")
        workflow_layout = QVBoxLayout(workflow_card)
        workflow_layout.setContentsMargins(18, 16, 18, 16)
        workflow_title = QLabel("创作流程")
        workflow_title.setObjectName("cardTitle")
        workflow_text = QLabel(
            "① 从未归档词汇中抽取 50 个候选词   →   "
            "② AI 选择至少 25 个并创作故事   →   "
            "③ 在历史库生成音频并双语跟读"
        )
        workflow_text.setWordWrap(True)
        workflow_text.setObjectName("mutedLabel")
        workflow_layout.addWidget(workflow_title)
        workflow_layout.addWidget(workflow_text)

        podcast_layout.addWidget(studio_card)
        podcast_layout.addWidget(workflow_card)
        podcast_layout.addStretch(1)

        self.workspace_tabs.addTab(vocabulary_page, "  📖 词库管理  ")
        self.workspace_tabs.addTab(podcast_page, "  🎧 播客创作  ")

        layout = QVBoxLayout(central)
        layout.setContentsMargins(24, 18, 24, 18)
        layout.setSpacing(12)
        layout.addLayout(brand_row)
        layout.addWidget(self.workspace_tabs, 1)

        refresh_action = QAction("刷新", self)
        refresh_action.setShortcut("F5")
        refresh_action.triggered.connect(self.reload_table)
        self.addAction(refresh_action)
        self.statusBar().showMessage(f"数据库：{self.repository.database_path}")

    def _apply_styles(self) -> None:
        self.setFont(QFont("Microsoft YaHei UI", 10))
        self.setStyleSheet(
            """
            QMainWindow, QDialog, QWidget { background: #f4f7fb; color: #18212f; }
            QLabel#titleLabel { font-size: 25px; font-weight: 750; color: #13213c; }
            QLabel#dialogTitle { font-size: 17px; font-weight: 700; color: #14213d; }
            QLabel#sectionTitle { font-size: 19px; font-weight: 700; color: #17233b; }
            QLabel#cardTitle { font-size: 13px; font-weight: 650; color: #24334f; }
            QLabel#mutedLabel { color: #6b778c; }
            QLabel#warningLabel { color: #b54708; }
            QFrame#card {
                background: white; border: 1px solid #dfe5ef; border-radius: 12px;
            }
            QFrame#softCard {
                background: #eef3ff; border: 1px solid #dbe5ff; border-radius: 10px;
            }
            QTabWidget#workspaceTabs::pane {
                background: white; border: 1px solid #dfe5ef; border-radius: 10px;
                top: -1px;
            }
            QTabWidget#workspaceTabs QTabBar::tab {
                background: transparent; color: #68758b; border: 0;
                padding: 11px 20px; margin-right: 4px; font-weight: 600;
            }
            QTabWidget#workspaceTabs QTabBar::tab:selected {
                color: #3557d4; background: #e9efff; border-radius: 8px;
            }
            QTabWidget#workspaceTabs QTabBar::tab:hover { color: #3557d4; }
            QLineEdit, QComboBox {
                background: white; border: 1px solid #d8dee9; border-radius: 7px;
                padding: 8px 10px; min-height: 20px;
            }
            QLineEdit:focus, QComboBox:focus { border: 1px solid #5b7cfa; }
            QComboBox { padding-right: 34px; }
            QComboBox::drop-down {
                subcontrol-origin: padding; subcontrol-position: top right;
                width: 30px; border: 0; border-left: 1px solid #e6eaf1;
            }
            QComboBox QAbstractItemView {
                background: white; color: #18212f; border: 1px solid #ccd5e3;
                border-radius: 7px; outline: 0; padding: 5px;
                selection-background-color: #e5ecff; selection-color: #20386f;
            }
            QComboBox QAbstractItemView::item {
                min-height: 32px; padding: 4px 10px; border-radius: 5px;
            }
            QPushButton {
                background: white; border: 1px solid #d8dee9; border-radius: 7px;
                padding: 8px 14px; min-height: 20px;
            }
            QPushButton:hover { background: #eef2ff; border-color: #aab8fa; }
            QPushButton:disabled { color: #a6afbd; background: #f1f3f6; }
            QPushButton#primaryButton { background: #4667e9; color: white; border: 0; }
            QPushButton#primaryButton:hover { background: #3857d2; }
            QPushButton#aiButton { background: #6d4aff; color: white; border: 0; }
            QPushButton#aiButton:hover { background: #5938df; }
            QPushButton#secondaryButton { color: #3557d4; background: #f5f7ff; }
            QPushButton#dangerButton { color: #b42318; }
            QPushButton#dangerButton:hover { background: #fff0ee; border-color: #f2b8b5; }
            QTableWidget {
                background: white; alternate-background-color: #f8faff;
                border: 1px solid #dfe4ec; border-radius: 8px; gridline-color: #edf0f5;
                selection-background-color: #dfe7ff; selection-color: #18212f;
            }
            QHeaderView::section {
                background: #edf1f7; border: 0; border-right: 1px solid #dfe4ec;
                border-bottom: 1px solid #dfe4ec; padding: 9px; font-weight: 600;
            }
            QProgressBar { border: 0; background: transparent; }
            QProgressBar::chunk { background: #5b7cfa; border-radius: 1px; }
            QPlainTextEdit {
                background: #111827; color: #d1d5db; border: 1px solid #273244;
                border-radius: 7px; padding: 8px; font-family: Consolas;
            }
            QTextBrowser {
                background: white; border: 1px solid #dfe5ef; border-radius: 8px;
                padding: 6px;
            }
            QStatusBar { background: #eef1f6; color: #647084; }
            """
        )

    def run_task(
        self,
        task: Callable[[], Any],
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[str], None] | None = None,
    ) -> TaskThread:
        thread = TaskThread(task, self)
        self._threads.add(thread)
        if on_success is not None:
            thread.succeeded.connect(on_success)

        def handle_error(message: str, diagnostics: str) -> None:
            print(diagnostics, file=sys.stderr)
            if on_error is not None:
                on_error(message)
            else:
                QMessageBox.critical(self, "操作失败", message or "发生未知错误")

        thread.failed.connect(handle_error)
        thread.finished.connect(lambda: self._release_thread(thread))
        thread.start()
        return thread

    def _release_thread(self, thread: TaskThread) -> None:
        self._threads.discard(thread)
        thread.deleteLater()

    def _set_busy(self, busy: bool, message: str = "") -> None:
        self._busy_count += 1 if busy else -1
        self._busy_count = max(0, self._busy_count)
        active = self._busy_count > 0
        self.progress.setVisible(active)
        self.import_button.setDisabled(active)
        self.export_button.setDisabled(active)
        ai_running = self._ai_worker is not None and self._ai_worker.isRunning()
        podcast_running = (
            self._podcast_worker is not None and self._podcast_worker.isRunning()
        )
        self.ai_parse_button.setDisabled(active or ai_running)
        self.generate_podcast_button.setDisabled(active or ai_running or podcast_running)
        if message:
            self.statusBar().showMessage(message)

    def reload_table(self) -> None:
        self._load_revision += 1
        revision = self._load_revision
        search_text = self.search_edit.text()
        status_filter = str(self.status_filter.currentData())

        def apply_if_current(rows: list[dict[str, Any]]) -> None:
            if revision == self._load_revision:
                self._populate_table(rows)

        self.run_task(
            lambda: self.repository.fetch_vocabulary(search_text, status_filter),
            apply_if_current,
        )

    def _populate_table(self, rows: list[dict[str, Any]]) -> None:
        self._populating_table = True
        self.table.blockSignals(True)
        self.table.setRowCount(len(rows))
        self._visible_row_by_id.clear()
        editable_fields = set(VOCABULARY_FIELDS)

        for row_index, row in enumerate(rows):
            self._visible_row_by_id[int(row["id"])] = row_index
            check_item = QTableWidgetItem()
            check_item.setFlags(
                Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable
            )
            check_item.setCheckState(Qt.CheckState.Unchecked)
            self.table.setItem(row_index, 0, check_item)

            for column, (field, _) in enumerate(TABLE_COLUMNS[1:], start=1):
                value = row.get(field)
                item = QTableWidgetItem("" if value is None else str(value))
                if field not in editable_fields:
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
                    )
                    item.setForeground(QColor("#778195"))
                else:
                    item.setFlags(
                        Qt.ItemFlag.ItemIsEnabled
                        | Qt.ItemFlag.ItemIsSelectable
                        | Qt.ItemFlag.ItemIsEditable
                    )
                if field == "soundMark":
                    item.setForeground(QColor("#3557d4"))
                    item.setToolTip("单击音标播放该单词的美式发音")
                self.table.setItem(row_index, column, item)

        self.table.blockSignals(False)
        self._populating_table = False
        self.count_label.setText(f"当前显示 {len(rows)} 条")
        self.statusBar().showMessage("词库已更新", 2500)
        if self._pending_focus_record is not None:
            vocabulary_id = int(self._pending_focus_record["id"])
            row_index = self._visible_row_by_id.get(vocabulary_id)
            if row_index is not None:
                self.table.selectRow(row_index)
                self.table.setCurrentCell(row_index, 2)
                self.table.scrollToItem(
                    self.table.item(row_index, 2),
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )
                word = str(self._pending_focus_record.get("word") or vocabulary_id)
                self.statusBar().showMessage(
                    f"已定位生词：{word}。双击可编辑，播客播放窗口保持打开。",
                    6000,
                )
                self.activateWindow()
                self.raise_()
                self._pending_focus_record = None

    def _table_cell_clicked(self, row: int, column: int) -> None:
        sound_mark_column = next(
            index
            for index, (field, _label) in enumerate(TABLE_COLUMNS)
            if field == "soundMark"
        )
        if column != sound_mark_column:
            return
        word_item = self.table.item(row, 2)
        if word_item is None or not word_item.text().strip():
            return
        word = word_item.text().strip()
        self._pronunciation_request += 1
        request_id = self._pronunciation_request
        self.statusBar().showMessage(f"正在准备 {word} 的发音…")

        def ready(audio_path: str) -> None:
            if request_id != self._pronunciation_request:
                return
            self._pronunciation_player.stop()
            self._pronunciation_player.setSource(
                QUrl.fromLocalFile(str(Path(audio_path).resolve()))
            )
            self._pronunciation_player.play()
            self.statusBar().showMessage(f"正在播放：{word}", 3000)

        self.run_task(
            lambda: generate_word_pronunciation(
                word,
                application_config_directory() / "pronunciation_cache",
                DEFAULT_VOICE,
            ),
            ready,
            lambda message: QMessageBox.warning(self, "发音失败", message),
        )

    def _cell_changed(self, row: int, column: int) -> None:
        if self._populating_table or column < 2:
            return
        field = TABLE_COLUMNS[column][0]
        if field not in VOCABULARY_FIELDS:
            return
        id_item = self.table.item(row, 1)
        value_item = self.table.item(row, column)
        if id_item is None or value_item is None:
            return
        vocabulary_id = int(id_item.text())
        value: Any = value_item.text()

        if field == "word":
            is_valid, normalized, reason = validate_word(value)
            if not is_valid:
                QMessageBox.warning(self, "无法保存", reason)
                self.reload_table()
                return
            value = normalized
        elif field == "status":
            try:
                value = int(value)
            except ValueError:
                QMessageBox.warning(self, "无法保存", "status 必须是整数。")
                self.reload_table()
                return

        def saved(_: Any) -> None:
            if field == "word" and value_item.text() != value:
                self.table.blockSignals(True)
                value_item.setText(str(value))
                self.table.blockSignals(False)
            self.statusBar().showMessage("修改已自动保存", 2000)

        self.run_task(
            lambda: self.repository.update_word(vocabulary_id, field, value),
            saved,
            lambda message: self._edit_failed(message),
        )

    def _edit_failed(self, message: str) -> None:
        QMessageBox.warning(self, "保存失败", message)
        self.reload_table()

    def open_api_settings(self) -> None:
        self.run_task(
            self.config_store.load,
            lambda config: self._show_api_settings(config, False),
            lambda message: self._api_config_load_failed(message, False),
        )

    def start_ai_parse(self) -> None:
        if self._busy_count or self._ai_launch_pending:
            return
        if self._podcast_worker is not None and self._podcast_worker.isRunning():
            QMessageBox.information(self, "任务运行中", "请先完成或终止播客生成任务。")
            return
        if self._ai_worker is not None and self._ai_worker.isRunning():
            QMessageBox.information(self, "任务运行中", "AI 解析任务已经在运行。")
            return
        self._ai_launch_pending = True
        self.ai_parse_button.setDisabled(True)
        self.run_task(
            self.config_store.load,
            self._config_ready_for_parse,
            lambda message: self._api_config_load_failed(message, True),
        )

    def _config_ready_for_parse(self, config: AIConfig) -> None:
        self._ai_launch_pending = False
        try:
            validated = config.validated()
        except ValueError:
            QMessageBox.information(
                self,
                "请先配置 API",
                "首次使用 AI 解析前，请填写 API Base URL、API Key 和 Model Name。",
            )
            self._show_api_settings(config, True)
            return
        self._launch_ai_worker(validated)

    def _api_config_load_failed(self, message: str, start_after_save: bool) -> None:
        self._ai_launch_pending = False
        self.ai_parse_button.setDisabled(False)
        QMessageBox.warning(self, "配置读取失败", f"{message}\n请重新填写并保存配置。")
        self._show_api_settings(AIConfig(), start_after_save)

    def _show_api_settings(self, config: AIConfig, start_after_save: bool) -> None:
        dialog = APISettingsDialog(config, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            self._ai_launch_pending = False
            self.ai_parse_button.setDisabled(
                self._busy_count > 0
                or (self._ai_worker is not None and self._ai_worker.isRunning())
            )
            return
        selected_config = dialog.selected_config
        if start_after_save:
            self._ai_launch_pending = True
            self.ai_parse_button.setDisabled(True)

        def saved(_: Any) -> None:
            self.statusBar().showMessage("API 配置已保存到本机", 3000)
            self._ai_launch_pending = False
            if start_after_save:
                self._launch_ai_worker(selected_config)
            else:
                self.ai_parse_button.setDisabled(False)

        def failed(message: str) -> None:
            self._ai_launch_pending = False
            self.ai_parse_button.setDisabled(False)
            QMessageBox.critical(self, "配置保存失败", message)

        self.run_task(
            lambda: self.config_store.save(selected_config),
            saved,
            failed,
        )

    def _launch_ai_worker(self, config: AIConfig) -> None:
        if self._ai_worker is not None and self._ai_worker.isRunning():
            return
        progress_dialog = AIProgressDialog(self)
        worker = AIParseWorker(
            self.repository,
            config,
            self.ai_log_path,
            self,
        )
        self._ai_worker = worker
        self._ai_progress_dialog = progress_dialog

        queued = Qt.ConnectionType.QueuedConnection
        # Explicit queued connections guarantee that every widget mutation runs
        # on the GUI thread even though signals originate in AIParseWorker.run().
        worker.total_ready.connect(progress_dialog.set_total, queued)
        worker.progress_changed.connect(progress_dialog.update_progress, queued)
        worker.log_message.connect(progress_dialog.append_log, queued)
        worker.batch_saved.connect(self._ai_batch_saved, queued)
        worker.run_completed.connect(self._ai_task_completed, queued)
        worker.fatal_error.connect(self._ai_task_failed, queued)
        worker.finished.connect(lambda: self._release_ai_worker(worker), queued)
        progress_dialog.cancel_requested.connect(worker.cancel)
        progress_dialog.finished.connect(
            lambda _: self._clear_ai_progress_dialog(progress_dialog)
        )

        self.ai_parse_button.setDisabled(True)
        self.generate_podcast_button.setDisabled(True)
        self.api_settings_button.setDisabled(True)
        self.import_button.setDisabled(True)
        self.export_button.setDisabled(True)
        self.statusBar().showMessage("AI 解析任务正在后台运行…")
        progress_dialog.show()
        worker.start()

    def _ai_batch_saved(self, saved_ids: object) -> None:
        if not isinstance(saved_ids, list):
            return
        normalized_ids = {int(item) for item in saved_ids}
        self._ai_pending_refresh_ids.update(normalized_ids)
        self.statusBar().showMessage(
            f"AI 已实时回填 {len(normalized_ids)} 个单词",
            2000,
        )
        # Concurrent batches often finish almost together. Coalescing their IDs
        # avoids rebuilding a potentially large QTableWidget several times in
        # one GUI event-loop turn.
        if not self._ai_refresh_timer.isActive():
            self._ai_refresh_timer.start()

    def _flush_ai_incremental_refresh(self) -> None:
        if self._ai_patch_inflight:
            self._ai_refresh_timer.start()
            return
        if not self._ai_pending_refresh_ids:
            return

        ids = sorted(self._ai_pending_refresh_ids)
        self._ai_pending_refresh_ids.clear()

        # definition participates in fuzzy search. With an active query a row
        # can enter the result set after AI enrichment, so use one coalesced full
        # reload; the normal unfiltered view receives only lightweight patches.
        if self.search_edit.text().strip():
            self.reload_table()
            return

        self._ai_patch_inflight = True

        def loaded(rows: list[dict[str, Any]]) -> None:
            self._apply_ai_row_updates(rows)
            self._finish_ai_patch()

        def failed(message: str) -> None:
            self.statusBar().showMessage(f"AI 表格增量刷新失败：{message}", 4000)
            self._finish_ai_patch()

        self.run_task(
            lambda: self.repository.fetch_vocabulary_by_ids(ids),
            loaded,
            failed,
        )

    def _apply_ai_row_updates(self, rows: list[dict[str, Any]]) -> None:
        column_by_field = {
            field: column
            for column, (field, _) in enumerate(TABLE_COLUMNS)
            if field in AI_CORE_FIELDS
        }
        self.table.blockSignals(True)
        try:
            for record in rows:
                row_index = self._visible_row_by_id.get(int(record["id"]))
                if row_index is None:
                    continue
                for field in AI_CORE_FIELDS:
                    item = self.table.item(row_index, column_by_field[field])
                    if item is not None:
                        value = record.get(field)
                        text = "" if value is None else str(value)
                        if item.text() != text:
                            item.setText(text)
        finally:
            self.table.blockSignals(False)

    def _finish_ai_patch(self) -> None:
        self._ai_patch_inflight = False
        if self._ai_pending_refresh_ids and not self._ai_refresh_timer.isActive():
            self._ai_refresh_timer.start()

    def _ai_task_completed(self, summary: object) -> None:
        if not isinstance(summary, dict):
            summary = {}
        if self._ai_progress_dialog is not None:
            self._ai_progress_dialog.mark_completed(summary)
        self._ai_refresh_timer.stop()
        self._ai_pending_refresh_ids.clear()
        # One final reload reconciles active filters and any rows changed while
        # the last incremental patch was in flight.
        self.reload_table()
        self.statusBar().showMessage(
            f"AI 解析结束：成功 {summary.get('succeeded', 0)}，"
            f"失败 {summary.get('failed', 0)}",
            5000,
        )

    def _ai_task_failed(self, message: str) -> None:
        if self._ai_progress_dialog is not None:
            self._ai_progress_dialog.mark_error(message)
        self.statusBar().showMessage("AI 解析任务失败", 5000)

    def _release_ai_worker(self, worker: AIParseWorker) -> None:
        if self._ai_worker is worker:
            self._ai_worker = None
        worker.deleteLater()
        busy = self._busy_count > 0
        self.ai_parse_button.setDisabled(busy)
        podcast_running = (
            self._podcast_worker is not None and self._podcast_worker.isRunning()
        )
        self.generate_podcast_button.setDisabled(busy or podcast_running)
        self.api_settings_button.setDisabled(False)
        self.import_button.setDisabled(busy)
        self.export_button.setDisabled(busy)

    def _clear_ai_progress_dialog(self, dialog: AIProgressDialog) -> None:
        if self._ai_progress_dialog is dialog:
            self._ai_progress_dialog = None

    def start_podcast_generation(self) -> None:
        if self._busy_count or self._podcast_launch_pending:
            return
        if self._ai_worker is not None and self._ai_worker.isRunning():
            QMessageBox.information(self, "任务运行中", "请先完成或终止 AI 回填任务。")
            return
        if self._podcast_worker is not None and self._podcast_worker.isRunning():
            QMessageBox.information(self, "任务运行中", "播客剧本正在生成。")
            return
        self._podcast_launch_pending = True
        self.generate_podcast_button.setDisabled(True)
        self.run_task(
            self.config_store.load,
            self._config_ready_for_podcast,
            self._podcast_config_failed,
        )

    def _config_ready_for_podcast(self, config: AIConfig) -> None:
        self._podcast_launch_pending = False
        try:
            validated = config.validated()
        except ValueError:
            self.generate_podcast_button.setDisabled(False)
            QMessageBox.information(
                self,
                "请先配置 API",
                "生成播客前，请先在“API 设置”中填写并保存连接信息。",
            )
            self._show_api_settings(config, False)
            return
        self._launch_podcast_worker(validated)

    def _podcast_config_failed(self, message: str) -> None:
        self._podcast_launch_pending = False
        self.generate_podcast_button.setDisabled(False)
        QMessageBox.warning(self, "配置读取失败", message)

    def _launch_podcast_worker(self, config: AIConfig) -> None:
        style = str(self.podcast_style_combo.currentData())
        custom_style = self.custom_style_edit.text().strip()
        dialog = PodcastProgressDialog(self)
        worker = PodcastGenerationWorker(
            self.repository,
            config,
            style,
            custom_style,
            self.ai_log_path.with_name("podcast_generator.log"),
            self,
        )
        self._podcast_worker = worker
        self._podcast_progress_dialog = dialog
        queued = Qt.ConnectionType.QueuedConnection
        worker.stage_changed.connect(dialog.set_stage, queued)
        worker.candidates_ready.connect(dialog.set_candidate_count, queued)
        worker.log_message.connect(dialog.append_log, queued)
        worker.run_completed.connect(self._podcast_task_completed, queued)
        worker.fatal_error.connect(self._podcast_task_failed, queued)
        worker.finished.connect(lambda: self._release_podcast_worker(worker), queued)
        dialog.cancel_requested.connect(worker.cancel)
        dialog.finished.connect(lambda _: self._clear_podcast_dialog(dialog))
        self.generate_podcast_button.setDisabled(True)
        self.ai_parse_button.setDisabled(True)
        self.import_button.setDisabled(True)
        self.api_settings_button.setDisabled(True)
        self.statusBar().showMessage("播客剧本正在后台生成，主界面仍可浏览…")
        dialog.show()
        worker.start()

    def _podcast_task_completed(self, summary: object) -> None:
        data = summary if isinstance(summary, dict) else {}
        if data.get("cancelled"):
            if self._podcast_progress_dialog is not None:
                self._podcast_progress_dialog.mark_cancelled()
            self.statusBar().showMessage("播客生成已终止，词库状态未改变", 5000)
            return
        podcast = data.get("podcast") if isinstance(data.get("podcast"), dict) else {}
        title = str(podcast.get("title", "新播客"))
        if self._podcast_progress_dialog is not None:
            self._podcast_progress_dialog.mark_completed(title)
            self._podcast_progress_dialog.accept()
        used_words = podcast.get("used_words", [])
        used_ids = [
            int(item["id"])
            for item in used_words
            if isinstance(item, dict) and item.get("id") is not None
        ]
        # Rebuilding a 1,000+ row QTableWidget in the GUI thread can make
        # Windows report the app as unresponsive. Status changes do not affect
        # most filters, so patch only the 25-30 touched rows. The "unused"
        # filter is the one case where rows must disappear after status +1.
        if str(self.status_filter.currentData()) == "unused":
            self.reload_table()
        elif used_ids:
            self.run_task(
                lambda: self.repository.fetch_vocabulary_by_ids(used_ids),
                self._apply_podcast_row_updates,
                lambda message: self.statusBar().showMessage(
                    f"状态增量刷新失败：{message}", 4000
                ),
            )
        self.statusBar().showMessage(
            f"《{title}》已保存，采用词已完成 status +1",
            8000,
        )
        podcast_id = int(podcast.get("id", 0)) or None
        self.open_podcast_history(podcast_id, podcast)

    def _apply_podcast_row_updates(self, rows: list[dict[str, Any]]) -> None:
        self.table.blockSignals(True)
        try:
            for record in rows:
                row_index = self._visible_row_by_id.get(int(record["id"]))
                if row_index is None:
                    continue
                for column, field in ((8, "status"), (9, "last_used_date")):
                    item = self.table.item(row_index, column)
                    if item is not None:
                        value = record.get(field)
                        item.setText("" if value is None else str(value))
        finally:
            self.table.blockSignals(False)

    def _podcast_task_failed(self, message: str) -> None:
        if self._podcast_progress_dialog is not None:
            self._podcast_progress_dialog.mark_error(message)
        self.statusBar().showMessage("播客剧本生成失败", 5000)

    def _release_podcast_worker(self, worker: PodcastGenerationWorker) -> None:
        if self._podcast_worker is worker:
            self._podcast_worker = None
        worker.deleteLater()
        busy = self._busy_count > 0
        self.generate_podcast_button.setDisabled(busy)
        self.ai_parse_button.setDisabled(busy)
        self.import_button.setDisabled(busy)
        self.api_settings_button.setDisabled(False)

    def _clear_podcast_dialog(self, dialog: PodcastProgressDialog) -> None:
        if self._podcast_progress_dialog is dialog:
            self._podcast_progress_dialog = None

    def open_podcast_history(
        self,
        select_id: int | None = None,
        initial_story: dict[str, Any] | None = None,
    ) -> None:
        if isinstance(select_id, bool):
            select_id = None
        if self._history_dialog is None:
            self._history_dialog = PodcastHistoryDialog(
                self.repository,
                self.run_task,
                application_config_directory() / "audio_cache",
                self,
            )
            self._history_dialog.vocabulary_requested.connect(
                self._focus_vocabulary_from_history
            )
            self._history_dialog.finished.connect(self._history_closed)
        self._history_dialog.pages.setCurrentWidget(
            self._history_dialog.library_page
        )
        self._history_dialog.reload(select_id, initial_story)
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _history_closed(self, _: int) -> None:
        if self._history_dialog is not None:
            self._history_dialog.deleteLater()
            self._history_dialog = None

    def _focus_vocabulary_from_history(self, vocabulary_id: int, word: str) -> None:
        def loaded(record: dict[str, Any]) -> None:
            self._pending_focus_record = record
            self.search_edit.blockSignals(True)
            self.search_edit.setText(str(record["word"]))
            self.search_edit.blockSignals(False)
            self.status_filter.blockSignals(True)
            self.status_filter.setCurrentIndex(0)
            self.status_filter.blockSignals(False)
            self.reload_table()

        self.run_task(
            lambda: self.repository.fetch_vocabulary_record(vocabulary_id, word),
            loaded,
            lambda message: QMessageBox.warning(self, "无法定位生词", message),
        )

    def _show_vocabulary_detail(self, record: dict[str, Any]) -> None:
        if self._detail_dialog is not None:
            self._detail_dialog.close()
        self._detail_dialog = VocabularyDetailDialog(record, self)
        self._detail_dialog.finished.connect(lambda _: setattr(self, "_detail_dialog", None))
        self._detail_dialog.show()

    def choose_import_file(self) -> None:
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "选择词库文件",
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DocumentsLocation
            ) or str(Path.home()),
            "词库文件 (*.xlsx *.xls *.csv);;Excel (*.xlsx *.xls);;CSV (*.csv)",
        )
        if not file_path:
            return

        mode_dialog = ImportModeDialog(self)
        if mode_dialog.exec() != QDialog.DialogCode.Accepted:
            return
        mode = mode_dialog.selected_mode
        backup_path = ""

        if mode == "replace":
            warning = QMessageBox(self)
            warning.setIcon(QMessageBox.Icon.Warning)
            warning.setWindowTitle("覆盖前必须备份")
            warning.setText("清空将丢失所有历史进度，请先导出当前词库备份。")
            warning.setInformativeText(
                "只有备份文件成功写入后，系统才会清空旧词库并使用当前文件重建。"
            )
            proceed_button = warning.addButton(
                "先导出当前词库并覆盖",
                QMessageBox.ButtonRole.AcceptRole,
            )
            warning.addButton(QMessageBox.StandardButton.Cancel)
            warning.exec()
            if warning.clickedButton() is not proceed_button:
                return

            backup_path, _ = QFileDialog.getSaveFileName(
                self,
                "先保存当前词库备份",
                str(
                    Path(
                        QStandardPaths.writableLocation(
                            QStandardPaths.StandardLocation.DocumentsLocation
                        ) or str(Path.home())
                    )
                    / "linkcho_vocabulary_before_replace.csv"
                ),
                "CSV 文件 (*.csv)",
            )
            if not backup_path:
                return
            if not backup_path.lower().endswith(".csv"):
                backup_path += ".csv"
            source_resolved = str(Path(file_path).resolve()).casefold()
            backup_resolved = str(Path(backup_path).resolve()).casefold()
            if source_resolved == backup_resolved:
                QMessageBox.warning(
                    self,
                    "备份位置无效",
                    "备份文件不能覆盖本次导入的源文件，请选择其他位置。",
                )
                return

        busy_message = (
            "正在备份、清洗并完全覆盖词库…"
            if mode == "replace"
            else "正在后台读取、清洗并智能追加词库…"
        )
        self._set_busy(True, busy_message)

        def imported(summary: ImportSummary) -> None:
            self._set_busy(False, "导入完成")
            self.reload_table()
            duplicate_note = (
                f"\n另有 {summary.duplicate_count} 个重复单词已自动跳过。"
                if summary.duplicate_count
                else ""
            )
            backup_note = (
                f"\n原词库备份：{backup_path}"
                if mode == "replace"
                else ""
            )
            mode_text = "覆盖导入" if mode == "replace" else "智能追加"
            QMessageBox.information(
                self,
                f"{mode_text}完成",
                f"成功导入 {summary.inserted_count} 个有效单词，"
                f"拦截并隔离 {summary.quarantined_count} 个词组/句子。"
                f"{duplicate_note}{backup_note}",
            )

        def failed(message: str) -> None:
            self._set_busy(False, "导入失败")
            QMessageBox.critical(self, "导入失败", message)

        if mode == "replace":
            task = lambda: replace_from_file(
                self.repository,
                file_path,
                backup_path,
            )
        else:
            task = lambda: import_file(self.repository, file_path)
        self.run_task(task, imported, failed)

    def open_quarantine(self) -> None:
        dialog = QuarantineDialog(self.repository, self.run_task, self)
        dialog.exec()

    def add_word(self) -> None:
        dialog = AddWordDialog(self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        word = dialog.word_edit.text()
        notes = dialog.notes_edit.text()

        def added(_: int) -> None:
            self.reload_table()
            self.statusBar().showMessage("单词已新增", 2500)

        self.run_task(
            lambda: self.repository.create_word(word, notes),
            added,
            lambda message: QMessageBox.warning(self, "新增失败", message),
        )

    def _selected_vocabulary_ids(self) -> list[int]:
        checked_rows: list[int] = []
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.checkState() == Qt.CheckState.Checked:
                checked_rows.append(row)
        rows = checked_rows or [
            index.row() for index in self.table.selectionModel().selectedRows()
        ]
        return [int(self.table.item(row, 1).text()) for row in sorted(set(rows))]

    def delete_words(self) -> None:
        ids = self._selected_vocabulary_ids()
        if not ids:
            QMessageBox.information(self, "未选择", "请勾选或选中要删除的单词。")
            return
        if QMessageBox.question(
            self,
            "确认删除",
            f"确定永久删除选中的 {len(ids)} 个单词吗？",
        ) != QMessageBox.StandardButton.Yes:
            return

        def deleted(count: int) -> None:
            self.reload_table()
            self.statusBar().showMessage(f"已删除 {count} 个单词", 2500)

        self.run_task(lambda: self.repository.delete_words(ids), deleted)

    def choose_export_file(self) -> None:
        documents_directory = QStandardPaths.writableLocation(
            QStandardPaths.StandardLocation.DocumentsLocation
        ) or str(Path.home())
        default_path = Path(documents_directory) / "linkcho_vocabulary_backup.csv"
        file_path, _ = QFileDialog.getSaveFileName(
            self,
            "导出词库备份",
            str(default_path),
            "CSV 文件 (*.csv)",
        )
        if not file_path:
            return
        if not file_path.lower().endswith(".csv"):
            file_path += ".csv"
        self._set_busy(True, "正在后台导出词库…")

        def exported(count: int) -> None:
            self._set_busy(False, "导出完成")
            QMessageBox.information(
                self,
                "导出完成",
                f"已将 {count} 个单词导出到：\n{file_path}",
            )

        def failed(message: str) -> None:
            self._set_busy(False, "导出失败")
            QMessageBox.critical(self, "导出失败", message)

        self.run_task(lambda: export_csv(self.repository, file_path), exported, failed)

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API name
        if (
            self._history_dialog is not None
            and any(
                worker.isRunning()
                for worker in self._history_dialog._tts_workers.values()
            )
        ):
            QMessageBox.information(
                self,
                "音频任务运行中",
                "请等待当前 TTS 音频生成完成后再退出。",
            )
            event.ignore()
            return
        if self._podcast_worker is not None and self._podcast_worker.isRunning():
            QMessageBox.information(
                self,
                "播客任务运行中",
                "请先在生成进度窗口中终止任务，等待当前请求结束后再退出。",
            )
            event.ignore()
            return
        if self._ai_worker is not None and self._ai_worker.isRunning():
            QMessageBox.information(
                self,
                "AI 任务运行中",
                "请先在解析进度窗口中终止任务，等待当前请求结束后再退出。",
            )
            event.ignore()
            return
        if any(thread.isRunning() for thread in self._threads):
            QMessageBox.information(
                self,
                "后台任务运行中",
                "请等待当前后台任务完成后再退出。",
            )
            event.ignore()
            return
        event.accept()


def main() -> int:
    app = QApplication(sys.argv)
    # Keep the legacy internal name so QStandardPaths continues to resolve the
    # user's existing database/config directory after the Linkcho rebrand.
    app.setApplicationName("AI 生词自动播客生成器")
    app.setApplicationDisplayName("Linkcho 桌面版")
    app.setOrganizationName("Personal Learning Tools")

    repository = VocabularyRepository(application_database_path())
    try:
        # Initialization happens before the window is shown; all subsequent I/O
        # is dispatched through TaskThread.
        repository.initialize()
    except Exception as exc:
        QMessageBox.critical(None, "数据库初始化失败", str(exc))
        return 1

    config_directory = application_config_directory()
    window = MainWindow(
        repository,
        AIConfigStore(config_directory / "ai_config.json"),
        config_directory / "ai_parser.log",
    )
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
