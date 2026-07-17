# AdsPower PPBoom 架构设计

## 目标

把 `PP爆破模式` 中与 `步骤 6：创建 Plus Checkout / PayPal 支付链路` 相关的浏览器执行部分迁移到 `AdsPower` 独立浏览器进程中运行，同时保留当前主扩展作为总控，不在 AdsPower 内再维护第二套主扩展状态。

## 结论

推荐方案：

- 主浏览器扩展继续负责总流程、节点状态、邮箱/短信、OAuth 回调、日志聚合。
- 本地 helper 新增 `AdsPower Bridge` 能力，调用 `AdsPower Local API` 启动指定 profile。
- helper 连接 AdsPower 启动后的浏览器调试接口，在 AdsPower 中独立执行 `步骤 6`。
- 执行结果回传主扩展，主扩展继续后续流程。

不推荐方案：

- 在 AdsPower 里再装一份完整扩展作为主链路。

原因：

- 双扩展会导致配置、节点状态、日志、OAuth 回调归属都需要同步，复杂度明显高于单扩展 + 本地 bridge。

## 官方能力边界

AdsPower 官方 Local API 已支持：

- 本地 HTTP API 启动 / 关闭 / 查询浏览器 profile
- 启动 profile 后返回 Selenium / Puppeteer 调试接口
- 返回 `debug_port` 与 `webdriver` 路径，供自动化程序接管浏览器

因此，“由本地程序启动 AdsPower profile，再由自动化程序接管该浏览器执行步骤 6” 是官方支持的能力范围内实现。

## 总体架构

### 1. 主扩展

职责：

- 维护总流程状态
- 维护 OAuth / localhost 回调
- 维护邮箱、手机号、接码池、节点重试
- 负责触发 `PPBoom AdsPower` 任务
- 接收 helper 返回结果并继续节点推进

不做的事：

- 不直接操控 AdsPower 浏览器

### 2. 本地 helper

建议直接在现有 `services/ppboom` 基础上扩展，不单独起第三套服务。

新增模块：

- `services/ppboom/adspower_client.py`
- `services/ppboom/adspower_worker.py`

职责：

- 调用 AdsPower Local API
- 启动指定 profile
- 获取 `debug_port` / `ws.puppeteer` / `webdriver`
- 用自动化框架连接 AdsPower 浏览器
- 在 AdsPower 浏览器中执行步骤 6
- 回传运行结果与诊断日志

### 3. AdsPower 浏览器进程

职责：

- 独立执行步骤 6
- 与主浏览器完全隔离 cookie / storage / 指纹 / 代理环境

默认不要求在 AdsPower 中安装第二份扩展。

## 为什么不装第二份扩展

如果在 AdsPower 里再装一份主扩展，会立即引入这些问题：

- 两边 `chrome.storage` 状态要同步
- 两边节点状态要同步
- 两边 PPBoom 配置要同步
- OAuth 由哪边负责监听和消费要重新划分
- 用户排障时需要同时看两份日志

因此首选方案是：

- AdsPower 中不装第二份扩展
- AdsPower 只作为被 helper 接管的独立浏览器 worker

## 实现目标拆分

### Phase 1：最小跑通版

目标：

- helper 能启动 AdsPower profile
- helper 能连接 AdsPower 浏览器
- helper 能确认 AdsPower 内已登录 ChatGPT
- helper 能从 AdsPower 会话中拿到 accessToken
- helper 继续复用现有 PPBoom HTTP 创建逻辑，返回 `stripeRedirectUrl`

结果：

- 主扩展得到 `stripeRedirectUrl`
- 后续仍可先在主浏览器内打开，验证控制链路

这是最小可验证版本，但不能完全达到“步骤 6 完全在 AdsPower 独立执行”的目标。

### Phase 2：步骤 6 全链路 AdsPower 化

目标：

- helper 在 AdsPower 中直接打开 `stripeRedirectUrl`
- 在 AdsPower 中继续执行 PayPal / OpenAI 完成页判断
- helper 在 AdsPower 中完成 `already paid` / `completion page` / `plus activated` 判断
- 只把最终结果回给主扩展

结果：

- AdsPower 真正独立完成步骤 6
- 主扩展只接收最终状态

### Phase 3：恢复链路与运营化

目标：

- AdsPower profile 启动失败自动报错回传
- profile 已打开时复用现有实例
- genericError / approval branch 恢复逻辑迁入 AdsPower worker
- 统一结构化日志

## 推荐接口设计

### 主扩展 -> helper

新增接口：

- `POST /api/ppboom/adspower/jobs`

请求体建议：

```json
{
  "profileId": "adspower-profile-id",
  "adsPowerApiBase": "http://local.adspower.net:50325",
  "accessTokenMode": "profile_session",
  "paymentLocale": "en",
  "defaultProxy": "http://...",
  "providerProxy": "http://...",
  "maxAttempts": 10,
  "checkoutRebuildMaxAttempts": 3,
  "step6Mode": "full",
  "expectPlusActivation": true
}
```

字段说明：

- `profileId`: AdsPower profile 唯一标识
- `adsPowerApiBase`: AdsPower Local API 地址
- `accessTokenMode`: 先固定为 `profile_session`
- `step6Mode`: `create_only` 或 `full`
  - `create_only`: 只创建 checkout 并返回 redirect
  - `full`: 在 AdsPower 中完整执行步骤 6

### helper -> 主扩展

任务状态字段建议：

```json
{
  "jobId": "xxx",
  "status": "succeeded",
  "phase": "plus_activated",
  "stripeRedirectUrl": "https://pay.openai.com/...",
  "providerRedirectUrl": "https://www.paypal.com/...",
  "alreadyPaid": false,
  "plusActivated": true,
  "completionPageDetected": true,
  "logs": [
    {"level": "info", "message": "AdsPower profile started"},
    {"level": "info", "message": "accessToken captured"}
  ]
}
```

## helper 内部模块设计

### adspower_client.py

职责：

- `GET /status`
- `POST /api/v2/browser-profile/start`
- `POST /api/v2/browser-profile/stop`
- `GET /api/v2/browser-profile/active`

建议封装：

- `start_profile(profile_id)`
- `stop_profile(profile_id)`
- `get_profile_status(profile_id)`

### adspower_worker.py

职责：

- 启动 profile
- 连接浏览器
- 读取 ChatGPT 会话
- 执行 PPBoom checkout
- 执行 PayPal / 完成页判断

建议拆成：

- `connect_browser(debug_port, webdriver_path)`
- `open_chatgpt_home(driver)`
- `read_chatgpt_session(driver)`
- `run_ppboom_create_from_profile_session(...)`
- `run_ppboom_full_step6_in_adspower(...)`

## 自动化框架选择

推荐优先：

- `Selenium`

原因：

- AdsPower 启动接口直接返回 `webdriver` 路径
- 官方文档明确把 Selenium 作为一等支持
- 与 Python helper 集成最直接

备选：

- `Puppeteer`

不作为首选：

- `Playwright`

原因：

- 虽然理论上可通过 CDP 连接，但官方文档直接举例的是 Selenium / Puppeteer
- 当前项目 helper 侧本来就是 Python，更适合先走 Selenium

## 与当前项目的耦合点

### 需要沿用的现有逻辑

- `services/ppboom/app.py` 的 HTTP 创建 checkout / Stripe / approve 逻辑
- `background/steps/create-plus-checkout.js` 对 `already paid`、`completion page`、`plus activated` 的状态定义

### 需要新增的前端设置

侧边栏新增一组 PPBoom 浏览器配置：

- `PPBoom Browser Backend`
  - `local`
  - `adspower`
  - `roxybrowser`
- `AdsPower API Base`
- `AdsPower 窗口ID`
- `RoxyBrowser API Base`
- `RoxyBrowser API Key`
- `RoxyBrowser 窗口ID`
- `AdsPower Step 6 Mode`
  - `create_only`
  - `full`

### 需要新增的状态字段

- `ppBoomBrowserBackend`
- `ppBoomAdsPowerApiBase`
- `ppBoomAdsPowerProfileId`
- `ppBoomRoxyBrowserApiBase`
- `ppBoomRoxyBrowserApiKey`
- `ppBoomRoxyBrowserProfileId`
- `ppBoomAdsPowerStep6Mode`

## 关键执行链路

### Full 模式

1. 主扩展进入步骤 6
2. 判断 `ppBoomBrowserBackend === adspower`
3. 向 helper 创建 AdsPower 任务
4. helper 启动指定 AdsPower profile
5. helper 连接 AdsPower 浏览器
6. helper 读取 ChatGPT 会话 / accessToken
7. helper 复用现有 PPBoom HTTP 逻辑创建 checkout
8. helper 在 AdsPower 浏览器中打开 `stripeRedirectUrl`
9. helper 在 AdsPower 中完成 PayPal / 完成页判断
10. helper 返回：
   - `alreadyPaid`
   - `plusActivated`
   - `completionPageDetected`
   - `stripeRedirectUrl`
11. 主扩展根据返回状态推进后续节点

## 风险点

### 1. AdsPower profile 未登录

处理：

- helper 启动后先访问 `https://chatgpt.com/`
- 调 `fetch('/api/auth/session')`
- 如果没有有效 session，直接失败回传 `profile_not_logged_in`

### 2. AdsPower 浏览器与主浏览器账号不一致

处理：

- Full 模式要求 AdsPower profile 内已登录目标 ChatGPT 账号
- 不尝试由主扩展把当前浏览器 session 直接同步到 AdsPower

### 3. PayPal 流程强依赖页面结构

处理：

- 第一阶段优先复用 PPBoom 当前 HTTP checkout 与完成页判断
- PayPal 页面自动化尽量封装成 helper 内独立模块

### 4. profile 生命周期

处理：

- 默认只启动 profile，不强制关闭
- 增加 `closeProfileOnFinish` 开关后再决定是否自动 stop

## 推荐实施顺序

1. 先做 AdsPower Local API client
2. 再做 `start profile -> connect -> read session`
3. 再做 `create_only` 模式
4. 验证能拿到稳定 `stripeRedirectUrl`
5. 再做 `full` 模式
6. 最后把主扩展的侧边栏配置接上

## 当前最优实现建议

如果目标是“尽快跑通”，先做：

- `主扩展 + PPBoom helper + AdsPower create_only`

如果目标是“最终彻底隔离步骤 6”，再做：

- `主扩展 + PPBoom helper + AdsPower full`

这样风险最小，也最符合当前项目的演进路径。
