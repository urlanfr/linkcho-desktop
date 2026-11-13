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

脚本会创建或复用 `.venv`，安装运行与构建依赖，执行阶段 3/4 冒烟测试，使用 `Linkcho.spec` 构建单文件窗口应用，并在干净 Windows `PATH` 下执行真实 EXE 启动检查。

产物：

```text
dist\Linkcho Desktop.exe
```

## 构建安装程序

安装 Inno Setup 6 后运行：

```powershell
.\build_installer.ps1 -SkipAppBuild
```

产物：

```text
dist\Linkcho-Desktop-Setup-v1.0.2-windows-x64.exe
```

安装器默认进行当前用户安装，不需要管理员权限；它会创建开始菜单入口，并支持可选桌面快捷方式和标准卸载。构建脚本完成后会自动执行隔离目录中的静默安装、应用启动和卸载验证；若当前用户已经安装 Linkcho，则为保护现有安装而中止这项测试。

## 发布前检查

1. 在一台没有 Python 的 Windows 机器上启动 EXE。
2. 验证词库导入、API 设置、播客生成、TTS 与音频下载。
3. 确认仓库中不包含 `ai_config.json`、数据库、日志或 MP3。
4. 构建安装器并完成静默安装、启动、卸载测试。
5. 将便携版 EXE 压缩为版本化 ZIP。
6. 创建带版本号的 GitHub Release，上传安装器和便携版压缩包。

## 常见问题

- Edge-TTS 与大模型请求都需要网络。
- Windows SmartScreen 可能提示未知发布者；正式分发建议使用代码签名证书签署 EXE。
- 单文件程序首次启动需要释放依赖，启动速度会比源码模式稍慢。
