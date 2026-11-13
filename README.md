# Linkcho Desktop

> 把想记住的单词，串成故事，听成播客。

Linkcho Desktop 是一款面向 Windows 的本地英语学习工具。它把零散生词整理成可维护的 SQLite 词库，通过兼容 OpenAI API 的大模型生成双语短故事，再使用 Edge-TTS 合成音频，形成可以跟读、回看和持续复习的个人播客库。

## 主要功能

- Excel / CSV 第一列导入、纯单词校验、重复过滤与隔离区管理
- 词库搜索、状态筛选、批量删除、CSV 备份以及表格内直接编辑
- 后台并发 AI 解析音标、中文释义、搭配、近反义词和例句
- 按学习状态抽取 50 个候选词并生成结构化双语故事
- 历史播客库、TTS 音频生成、下载和沉浸式播放
- 中英文句子随播放进度同步高亮与自动滚动
- 点击故事生词反向定位主词库；点击音标播放美式发音
- 数据、API Key 与生成音频保存在本机用户目录

## 获取与运行

### 使用 Windows 安装程序（推荐）

从 GitHub Releases 下载 `Linkcho-Desktop-Setup-v1.0.2-windows-x64.exe`，双击进入安装向导。安装程序默认使用当前用户目录，不要求管理员权限，并创建开始菜单快捷方式。

如需免安装版本，可下载 `Linkcho-Desktop-v1.0.2-windows-x64.zip`，解压后运行 `Linkcho Desktop.exe`。首次使用请在右上角“API 与模型设置”中填写兼容接口信息。

### 从源码运行

要求 Windows 10/11、Python 3.11 及网络连接（AI 与 Edge-TTS 功能需要）。

```powershell
git clone https://github.com/urlanfr/linkcho-desktop.git
cd linkcho-desktop
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
python main.py
```

日常启动不需要重复创建环境或安装依赖：

```powershell
.\.venv\Scripts\pythonw.exe main.py
```

也可以直接双击仓库中的 `run_linkcho.bat`。

## API 配置

Linkcho 支持 OpenAI / DeepSeek / OpenRouter 等兼容 Chat Completions 的服务，需要配置：

- API Base URL
- API Key
- Model Name

配置只保存在本机，不会提交到 Git。请不要在 Issue、截图或日志中公开 API Key。

## 数据位置

为兼容旧版本，程序继续使用原应用标识对应的 Windows 用户数据目录。数据库、配置、日志和音频缓存均位于当前 Windows 用户的 AppData 目录，而不是 EXE 临时解包目录。覆盖导入前仍建议先在应用内导出 CSV 备份。

## 项目结构

```text
main.py                 PyQt6 界面、线程调度、播放与交互
database.py             SQLite 表结构、CRUD、导入清洗与抽词算法
ai_parser.py            API 配置、批量解析和播客 Prompt / 校验
tts_service.py          Edge-TTS、MP3 时长与单词发音缓存
smoke_test_phase3.py    AI 故事链路离线冒烟测试
smoke_test_phase4.py    TTS、历史库和播放器离线冒烟测试
Linkcho.spec            PyInstaller Windows 打包配置
docs/                   用户与架构文档
```

## 测试

测试不需要真实 API Key：

```powershell
.\.venv\Scripts\python.exe -m py_compile main.py database.py ai_parser.py tts_service.py
.\.venv\Scripts\python.exe smoke_test_phase3.py
.\.venv\Scripts\python.exe smoke_test_phase4.py
```

## 构建 EXE

```powershell
.\build_windows.ps1
```

构建产物位于 `dist\Linkcho Desktop.exe`。详细说明见 [BUILDING.md](BUILDING.md)。

构建 Windows 安装程序：

```powershell
.\build_installer.ps1 -SkipAppBuild
```

## 文档

- [用户手册](docs/USER_GUIDE.md)
- [架构说明](docs/ARCHITECTURE.md)
- [构建与发布](BUILDING.md)
- [安全说明](SECURITY.md)
- [贡献指南](CONTRIBUTING.md)
- [更新日志](CHANGELOG.md)

## 许可

Copyright © 2026 urlanfr. All rights reserved. 详见 [LICENSE](LICENSE)。
