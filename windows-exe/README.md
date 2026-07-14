# SuperGrok Gateway Windows Portable

这是不使用 Docker、不设置网页密码的 Windows 单文件版本。双击 `dist/SuperGrokGateway.exe` 后会启动局域网服务并自动打开网页控制面板；网页 HTML、CSS、JavaScript 和 Python 服务均包含在 EXE 内，不需要旁边保留源码或资源文件。

保留功能：

- xAI / SuperGrok OAuth 登录与自动刷新
- 导入 Hermes xAI OAuth 凭据
- OpenAI 兼容 `/v1` API
- Claude / Anthropic Messages `/v1/messages`
- CCSwitch、Codex 与 Claude Code 模型名自动映射、Codex `custom` 工具双向转换及 xAI 请求兼容处理
- 实时或 Hermes 回退模型列表
- Token 与请求统计
- 局域网访问

## 直接运行

双击：

```text
dist\SuperGrokGateway.exe
```

程序会显示本机及局域网地址并打开 `http://127.0.0.1:8645/`。关闭黑色控制台窗口即可停止服务。

如需换端口：

```powershell
.\dist\SuperGrokGateway.exe --port 9000
```

OAuth 凭据和统计仍保存在 `%USERPROFILE%\.supergrok-openai\`，不会写入 EXE。

## 安全提醒

此变体按要求没有网页密码。任何能够访问该端口的局域网设备都可以打开控制面板、查看本地 API Key、操作 xAI 登录或清空统计。程序只接受回环、私有及链路本地来源地址，但仍应只在可信的 Windows 专用网络使用：

- 不要把端口映射到公网。
- Windows 防火墙只允许“专用网络”，不要允许“公用网络”。
- 不要在公共 Wi-Fi 上运行。

## 重新构建

安装 Python 3.10 或更高版本后双击 `build-exe.bat`。脚本使用固定依赖创建本地构建环境，产物仍在 `dist\SuperGrokGateway.exe`。
