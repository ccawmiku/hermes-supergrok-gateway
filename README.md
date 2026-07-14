# supergrok-openai

把个人 SuperGrok / xAI OAuth 登录转换为本机 OpenAI 与 Claude/Anthropic 兼容 API，并提供完整网页控制面板。

项目只保留：

- xAI device-code OAuth 登录与 refresh-token 自动轮换
- 从现有 Hermes `auth.json` 导入 xAI OAuth 凭据
- 固定转发到 `https://api.x.ai/v1` 的 OpenAI 兼容接口
- 将 Anthropic Messages 请求、响应、SSE 和工具调用转换到 xAI Chat Completions
- 基于 xAI 实际 usage 的本地 Token 统计
- 带首次设密和登录验证的局域网网页控制面板

不包含 Hermes Agent、工具、记忆、网关、定时任务或其他 Provider。请求正文和 OAuth token 均不写日志。

## 一键启动

Windows 直接双击：

```text
start-dashboard.bat
```

第一次运行会自动创建本地 Python 环境并安装依赖，随后打开：

```text
http://127.0.0.1:8645/
```

首次进入会要求设置管理密码。密码至少 8 位，并同时包含大写字母、小写字母和数字；磁盘上只保存 Argon2id 哈希。设置完成后，其他局域网设备可以使用启动窗口显示的地址访问，例如：

```text
http://192.168.1.20:8645/
```

局域网首次设置采用“先到先得”，请在启动后立即完成。Windows 防火墙若询问是否允许 Python 访问网络，只允许“专用网络”，不要允许公用网络。

之后所有管理操作都在网页完成，包括：

- xAI / SuperGrok 登录
- 导入 Hermes 登录
- 查看和复制 Base URL、API key
- 重新生成本地 API key
- 测试 xAI 连接并读取模型列表
- 查看完整实时/回退模型目录
- 查看总 Token、输入、输出、请求数和按模型统计
- 退出登录与删除本地凭据
- 复制环境变量和 Python 客户端配置

关闭启动窗口即可停止本地服务。

## 手动安装与启动

需要 Python 3.10 或更高版本：

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -e .
.\.venv\Scripts\supergrok-openai serve --host 0.0.0.0 --allow-network
```

启动时会自动打开控制面板。使用 `serve --no-browser` 可以禁止自动打开。

## 网页登录方式

推荐点击“连接 xAI”。网页会申请 device code、打开 xAI 登录页并在后台等待授权。这使用与 Hermes 相同的登录流程，但会保存为本工具自己的独立 OAuth 会话，适合与 Hermes 同时使用。

“导入 Hermes”默认读取 `~/.hermes/auth.json` 或 `HERMES_HOME/auth.json`，也可以在网页填写其他路径。导入不会修改 Hermes 文件。

注意：xAI refresh token 会轮换。导入后如果 Hermes 与本工具长期并发使用同一登录会话，先刷新的一方可能让另一方的 refresh token 失效。需要同时运行两者时，请使用网页中的“连接 xAI”创建独立会话。

凭据默认写入 `~/.supergrok-openai/auth.json`，可用 `SUPERGROK_OPENAI_HOME` 修改位置。

控制面板密码哈希保存在同一目录的 `dashboard-auth.json`。退出 xAI 不会删除管理密码；“锁定面板”只会撤销当前浏览器会话。

## Docker

公开镜像只发布带语义版本号的标签，不提供 `latest`：

```powershell
docker run -d --name hermes-supergrok-gateway `
  -p 8645:8645 `
  -v supergrok-data:/data `
  ghcr.io/ccawmiku/hermes-supergrok-gateway:v1.0.1
```

也可以使用仓库内固定到 `v1.0.1` 的 Compose 配置：

```powershell
docker compose up -d
```

浏览器打开 `http://<Docker 主机局域网 IP>:8645/` 设置管理密码。OAuth 凭据、管理密码哈希和 Token 统计都保存在 `supergrok-data` 数据卷中。

## Windows 单文件版

无需安装 Python，直接运行仓库内的 `windows-exe/dist/SuperGrokGateway.exe`。该版本包含网页界面，不设置面板密码，其他登录、模型适配、OpenAI/Anthropic API 和 Token 统计功能与 Docker 版同步维护。只建议在可信的家庭或个人局域网中使用。

每个 `v*.*.*` 版本标签都会重新构建 Windows EXE，并把 `SuperGrokGateway.exe` 附加到对应的 GitHub Release；仓库中也保留当前版本的 EXE。

## OpenAI API

默认配置：

```text
Base URL: http://127.0.0.1:8645/v1
API Key:  网页控制面板中显示的 sg-local-... 密钥
```

支持的透传端点：

- `GET /v1/models`
- `POST /v1/chat/completions`
- `POST /v1/responses`
- `POST /v1/responses/compact`
- `POST /v1/completions`
- `POST /v1/embeddings`
- `POST /v1/messages`（Anthropic Messages 兼容层）

网关会把 CCSwitch、Codex 和 Claude Code 使用的 `gpt-*`、`codex-*`、`claude-*` 模型名自动映射到 Hermes 的 SuperGrok 默认模型 `grok-build-0.1`。同时会清理 xAI 不接受的 Codex Responses 字段，以及工具 JSON Schema 中的 `pattern`、`format` 和含 `/` 的枚举。流式 SSE 与工具调用响应仍由 xAI 上游生成。健康检查为 `GET /health`。

## Claude / Anthropic API

Claude Python SDK 示例：

```python
from anthropic import Anthropic

client = Anthropic(
    base_url="http://127.0.0.1:8645",
    api_key="网页显示的 sg-local-...",
)

message = client.messages.create(
    model="grok-4.5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "你好"}],
)
print(message.content[0].text)
```

支持普通 Messages 响应、Anthropic SSE 事件流、`tool_use` / `tool_result`、图片内容块，以及 `x-api-key` 认证。可以直接保留 Claude Code 的 `claude-*` 模型名，网关会自动映射到 `grok-build-0.1`；显式填写网页列出的 Grok 模型 ID 时则保持原模型不变。

## 模型目录和 Token 统计

网页优先读取 xAI `/v1/models` 的实时目录。部分 OAuth 会话会得到 HTTP 200 但 `data` 数组为空；这表示认证链路正常，只是该 OAuth 接口没有返回目录。此时网页自动显示从 Hermes 0.18.2 提取的精选回退模型并标注 `HERMES CURATED`，实际可用性以调用结果为准。

Token 面板只统计 xAI 响应实际返回的 usage：

- OpenAI JSON 与 SSE 响应
- Anthropic 普通与流式 Messages
- 输入、输出、缓存和推理 token
- 总请求数、usage 覆盖率和按模型汇总

若某个上游响应没有 usage，该请求只计入请求数，不估算 token。统计保存在 `~/.supergrok-openai/stats.json`，可在网页一键清零。

## 安全边界

- 整个服务只接受回环、私有或链路本地来源地址，不接受直接公网客户端。
- 控制面板密码使用 Argon2id 哈希保存；登录会话使用 12 小时 HttpOnly、SameSite=Strict Cookie。
- 同一来源地址 10 分钟内连续失败 5 次后会暂时限制登录。
- 网页管理请求还使用进程级随机会话令牌，阻止其他网站向本地端口提交管理操作。
- 控制面板带有 CSP、禁止 iframe 嵌入并关闭缓存。
- OAuth bearer 固定只发送到 HTTPS 的 `x.ai` / `*.x.ai` 域名。
- OIDC discovery 返回的 token endpoint 执行同样的域名校验。
- 客户端传入的本地 `Authorization`、cookie 和 API-key headers 不会转发给 xAI。
- refresh token 轮换后使用原子替换保存，避免写出不完整的凭据文件。
- 局域网模式使用 HTTP，管理密码和 API 请求不会被传输层加密；仅在可信专用网络使用，不要做公网端口映射。
- 该工具面向个人使用；请遵守 xAI 服务条款，不要共享订阅凭据。

## 来源

OAuth 常量、device-code 流程、短期令牌刷新窗口和代理端点边界基于 MIT 许可的 [NousResearch/hermes-agent](https://github.com/NousResearch/hermes-agent)，提取时上游提交为 `c7e09f2`（Hermes Agent 0.18.2）。本项目保留 Nous Research 的 MIT 版权声明。

具体提取映射见 [UPSTREAM.md](UPSTREAM.md)。
