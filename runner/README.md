# 多 Chrome Profile 并发调度器

## 启动

macOS 双击项目根目录的 `start-profile-runner.command`，或在终端运行：

```bash
./start-profile-runner.command
```

WebUI 默认地址：`http://127.0.0.1:17374`。调度器会自动检查并尝试启动
`http://127.0.0.1:17373` 上的 Hotmail helper。

Chrome 150 已忽略旧的 `--load-extension` 启动参数。本调度器会为每个临时 Profile
开启独立的本机 CDP 端口，通过 `Extensions.loadUnpacked` 加载扩展，再打开该 worker
自己的任务页；CDP 只监听 `127.0.0.1`，任务令牌不会出现在 Chrome 命令行中。

## 导入格式

微软邮箱账户池支持：

```text
email----clientId----mailRefreshToken
email----password----clientId----mailRefreshToken
```

将文本粘贴到“待导入区”后点击“导入账户池”。成功后文本框会清空，账号进入本机调度器账户池；
WebUI 会显示总数、可用和已用，一键启动只随机领取可用账号。

代理池支持每行一种：

```text
http://username:password@host:port
socks5://username:password@host:port
host:port:username:password
host:port
```

代理池可以留空，此时所有 worker 使用本机直连。代理数少于并发数时，实际同时运行数受
可用代理数限制；同一代理不会同时租给多个 worker。代理凭据只写入对应 Profile 的扩展
存储，不会放进 Chrome 启动参数或 WebUI 状态响应。

## 本地配置

WebUI 配置、SMSBower Key、代理池和微软邮箱账户池会保存到：

```text
data/profile-runner/webui-config.json
```

macOS/Linux 下文件权限设为 `600`。该文件包含邮箱 refresh token、代理密码和 API Key，
不要上传或分享。接码价格单位为 USD，默认最低购买价 `$0.03`、价格上限 `$0.15`。

## 隔离与出口保护

- 每个账号任务使用新的 `user-data-dir`，任务之间不复用 Cookie、扩展 storage、标签页或 OAuth 状态。
- 不使用无痕模式。同一 Profile 的多个无痕窗口仍共享无痕会话，而且扩展默认不会在无痕窗口运行；一次一用的独立 Profile 隔离更完整。
- 同一时刻一条代理只租给一个 worker。
- worker 在打开 OpenAI 前探测实际出口 IP；已经被其他运行中 worker 占用的出口会被拦截。
- 出口探测阶段排除 OpenAI/ChatGPT 域名，出口唯一性通过后才开始 OAuth。
- 任务结束后默认清理临时 Profile；运行日志保留在 `data/profile-runner/<run-id>/`。
- “一键关闭所有 Worker”只终止当前调度器创建的 Chrome 进程组，不会关闭日常使用的 Chrome；启用清理时也会删除本批次临时 Profile。
- 调度器会复查成功 JSON 位于配置的输出目录，并包含 `type: codex`、`email`、
  `access_token`、`refresh_token` 和 `last_refresh`。

JSON 默认输出到项目同级的 `sub2api` 文件夹。
