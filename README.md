# GuJumpgate

GuJumpgate 是一个 Chrome 浏览器扩展，用于把你已经拥有 Plus 资格的 Outlook / Hotmail 账号，通过 OpenAI OAuth 登录流程导出为 SUB2API 可用的本地 JSON 文件。

当前版本只保留“已有 Plus OAuth JSON”流程，不包含注册账号、Plus 支付、PayPal、代理购买或自动导入 SUB2API 等功能。

## 主要用途

适合已经有一批 Plus 账号，并且希望批量生成本地 SUB2API / Codex 兼容认证 JSON 的场景。

流程大致为：

1. 从“微软邮箱账户池”选择一个未使用账号。
2. 打开 OpenAI OAuth 登录页。
3. 使用邮箱验证码登录。
4. 如页面要求手机号验证，则调用 SMSBower 接码并提交验证码。
5. 完成 OAuth 授权。
6. 交换 `access_token` 和 `refresh_token`。
7. 保存本地 JSON 到项目根目录的 `sub2api/` 文件夹。

导出的 JSON 会包含：

```json
{
  "type": "codex",
  "email": "example@outlook.com",
  "access_token": "...",
  "refresh_token": "...",
  "last_refresh": "..."
}
```

## 账号格式

在侧边栏的“微软邮箱账户池”中导入账号，每行一个：

```text
email----password----clientId----mailRefreshToken
```

其中：

- `email`：Outlook / Hotmail 邮箱。
- `password`：邮箱密码，仅用于记录和展示。
- `clientId`：微软 OAuth client id。
- `mailRefreshToken`：用于本地助手读取邮箱验证码。

## 本地助手

邮箱收码和本地 JSON 写入依赖 Hotmail 本地助手。

macOS：

```bash
./start-hotmail-helper.command
```

Windows：

```bat
start-hotmail-helper.bat
```

默认监听地址：

```text
http://127.0.0.1:17373
```

健康检查地址：

```text
http://127.0.0.1:17373/health
```

如果扩展提示无法连接本地助手，请先确认助手窗口已经启动、端口没有被占用，并且浏览器可以访问上面的健康检查地址。

## 接码配置

当前主流程使用 SMSBower 接收 OpenAI 手机验证码。

在扩展侧边栏中配置：

- SMS provider：选择 `SMSBower`
- API Key：填写你的 SMSBower API Key
- 国家：按你的需求选择，默认会使用内置国家顺序
- 最低购买价：默认 `0.03` 美元
- 最高购买价：默认 `0.15` 美元

如果账号已经满足手机号要求，页面直接进入 OAuth 授权页，则不会新增接码。

## 输出目录

导出的 JSON 固定保存到项目根目录：

```text
sub2api/
```

文件名格式：

```text
sub2api-{email}.json
```

如果同名文件已经存在，会自动追加时间戳，避免覆盖。

注意：`sub2api/*.json` 已加入 `.gitignore`，这些文件包含 refresh token，不应该提交到 Git。

## 安装扩展

1. 打开 Chrome：

   ```text
   chrome://extensions/
   ```

2. 开启“开发者模式”。

3. 点击“加载已解压的扩展程序”。

4. 选择本项目目录。

5. 修改代码后，需要在扩展管理页点击“重新加载”。

## 使用步骤

1. 启动 Hotmail 本地助手。
2. 打开扩展侧边栏。
3. 在“微软邮箱账户池”导入 Outlook / Hotmail 账号。
4. 配置 SMSBower API Key 和接码参数。
5. 设置目标成功数量。
6. 点击启动。
7. 成功导出的 JSON 到 `sub2api/` 文件夹中查看。

## 状态处理

扩展会为账号记录状态：

- `pending`：待使用
- `running`：运行中
- `success`：已成功导出 JSON
- `failed`：失败
- `used`：已使用或不可再用

常见失败原因：

- `ACCOUNT_DEACTIVATED`：OpenAI 提示账号被删除或停用，会标记为已用并切换下一个账号。
- `refresh_token_invalid`：微软邮箱 refresh token 失效，无法继续读取邮箱验证码。
- 手机号已被使用：SMSBower 取到的号码无法通过 OpenAI 验证，会换号重试，超过次数后失败。
- 请求次数过多：触发限流后会等待一段时间再继续。

## 安全提醒

- 请只处理你有权使用的账号。
- `mailRefreshToken`、SMSBower API Key、导出的 `refresh_token` 都是敏感信息。
- 不要把 `sub2api/*.json`、本地日志、配置备份上传到公开仓库。
- 使用本项目时请自行遵守目标平台服务条款和所在地法律法规。

## 开发说明

核心文件：

- `background.js`：主流程编排、账号选择、OAuth JSON 保存。
- `background/steps/oauth-login.js`：OAuth 邮箱登录步骤。
- `background/steps/fetch-login-code.js`：邮箱验证码获取。
- `content/signup-page.js`：页面状态识别和表单操作。
- `scripts/hotmail_helper.py`：本地 Hotmail 收码助手和本地 JSON 写入。
- `sidepanel/sidepanel.js`：扩展侧边栏交互。

语法检查：

```bash
node --check background.js
node --check sidepanel/sidepanel.js
PYTHONPYCACHEPREFIX=/tmp/gujumpgate-pycache python3 -m py_compile scripts/hotmail_helper.py
```

## 版权与来源

本项目基于 GuJumpgate / FlowPilot 相关代码修改而来。原项目及其相关开源部分采用 MIT License 发布。分发或修改本项目时，请保留仓库中的 `LICENSE` 及相关来源说明。
