# 架构说明

## 分层

```text
PyQt6 UI / Signals
        │
Workers and TaskThread
        │
AI / TTS Services
        │
VocabularyRepository
        │
SQLite + local cache
```

## 模块职责

- `main.py`：窗口、页面导航、表格交互、线程生命周期和播放器。
- `database.py`：连接管理、迁移、CRUD、导入清洗、候选词抽取、播客持久化与状态更新。
- `ai_parser.py`：兼容 API 客户端、配置、JSON 恢复、校验、批处理和故事生成 Prompt。
- `tts_service.py`：Edge-TTS、Markdown 清洗、音频时长和单词发音缓存。

## 并发原则

文件解析、数据库批处理、网络 API 和 TTS 均在 `QThread` 或后台任务中执行。后台线程通过 Qt 信号回传小粒度结果，主线程只负责绘制和轻量状态更新。

## 数据模型

- `vocabulary`：单词、解析字段、学习状态、最近使用日期和备注。
- `quarantine`：导入过程中被拦截的词组或句子。
- `podcasts`：标题、中英文正文、使用词汇、日期和音频元数据。

数据库迁移在 `VocabularyRepository.initialize()` 中以增量方式完成，避免破坏旧数据。

## 本地存储

运行数据使用 `QStandardPaths` 定位到当前用户的 Windows AppData / Config 目录。仓库目录只保存程序文件，因此源码运行和打包运行共享同一套用户数据。

