# Windows 构建与发布

## 环境

- Windows 10/11 x64
- Python 3.11 x64
- PowerShell 5.1 或更高版本

## 本地构建

在项目根目录运行：

```powershell
.\build_windows.ps1
```

脚本会创建或复用 `.venv`，安装运行与构建依赖，执行阶段 3/4 冒烟测试，最后使用 `Linkcho.spec` 构建单文件窗口应用。

产物：

```text
dist\Linkcho Desktop.exe
```

## 发布前检查

1. 在一台没有 Python 的 Windows 机器上启动 EXE。
2. 验证词库导入、API 设置、播客生成、TTS 与音频下载。
3. 确认仓库中不包含 `ai_config.json`、数据库、日志或 MP3。
4. 将 EXE 压缩为 `Linkcho-Desktop-windows-x64.zip`。
5. 创建带版本号的 GitHub Release 并上传压缩包。

## 常见问题

- Edge-TTS 与大模型请求都需要网络。
- Windows SmartScreen 可能提示未知发布者；正式分发建议使用代码签名证书签署 EXE。
- 单文件程序首次启动需要释放依赖，启动速度会比源码模式稍慢。

