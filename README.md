# Glados自动签到

## 食用方式：

### 注册一个GLaDOS的账号([注册地址](https://glados.space/landing/0A58E-NV28S-6U3QV-33VMG))

#### 我的邀请码：([0A58E-NV28S-6U3QV-33VMG](https://0a58e-nv28s-6u3qv-33vmg.glados.space))

### **Fork**本仓库

![图片加载失败](imgs/1.png)

### 添加**secret**

1. 跳转至自己的仓库的`Settings`->`Secrets and variables`->`Action`

2. 添加1个`repository secret`，命名为`GLADOS_COOKIES`，其值对应GLaDOS账号的cookie值中的有效部分（获取方式如下）

- 打开 https://glados.cloud，退出后重新登录，再进入签到页面按 `F12`。

- 切换到`Network`页面下，刷新

![图片加载失败](imgs/2.png)

- 找到成功的 `/api/user/status` 请求，在 `Request Headers` 下复制完整 `Cookie` 值；同时复制该请求的 `User-Agent`，保存到仓库 Secret `GLADOS_USER_AGENT`（也支持同名 Repository Variable，Secret 优先）。不要只复制旧的 `koa:sess`。

  > 参考格式：gld:sess=你的会话值; gld:sess.sig=你的签名值;
  > 必须同时包含这两项。完整 Cookie 中的其他项可以保留；也支持带 `Cookie:` 前缀的值。
  > Cookie 和 User-Agent 必须来自同一次登录的浏览器。不要把真实 Cookie 发到聊天、Issue 或日志中。

![图片加载失败](imgs/3.png)

- 多账号请在 `GLADOS_COOKIES` 中添加多个 Cookie，中间使用 `&` 连接。（例如：`c1&c2&c3...`）。当前所有账号共用 `GLADOS_USER_AGENT`，请用同一种浏览器版本重新登录获取会话。

3. 配置积分兑换策略（非必须）

- 添加1个`repository secret`，命名为`GLADOS_EXCHANGE_PLAN`，配置自动兑换积分策略：

| 值 | 积分要求 | 兑换天数 |
|---|---------|---------|
| `plan100` | 100 积分 | 10 天 |
| `plan200` | 200 积分 | 30 天 |
| `plan500` | 500 积分 | 100 天 (默认) |

> 不配置时默认为 `plan500`，即积分达到 500 时自动兑换 100 天

4. 手机推送（非必须）

- 默认使用 **Server酱³ 安卓/iOS App** 接收通知：
  1. 在手机打开 [官方下载页](https://sc3.ft07.com/client) 安装 App。
  2. 在 [SendKey 页面](https://sc3.ft07.com/sendkey) 登录并获取新 SendKey，按 App 提示绑定同一个 Key。
  3. 在 GitHub `Settings → Secrets and variables → Actions` 新增 Secret `SERVERCHAN_SENDKEY`，值填完整的新 SendKey（格式为 `sctp<数字>t<令牌>`）。不要把真实 Key 提交到仓库或发送到聊天。
  4. 允许 App 通知；若手机不支持官网列出的厂商通道，按官网说明维持 App 后台运行。
  5. 在 Actions 手动运行一次，并同时核对手机 App 消息列表与通知提醒。

Server酱³ 与 Server酱Turbo 的账号、Key 不通用；旧 `SENDKEY` 不会自动复用。费用与额度请以官网和账号页面为准，签到摘要不调用 AI 功能。

未配置 `SERVERCHAN_SENDKEY` 时，签到仍运行，日志明确提示跳过手机通知；配置后，HTTP 错误、API 拒绝或解析错误都会让工作流失败。服务端接受消息不等于手机已经收到，最终以 App 为准。

需要保留 PushDeer 的其他用户，可设置 Repository Variable `GLADOS_PUSH_PROVIDER=pushdeer`，并配置 Secret `PUSHDEER_SENDKEY`。官方 Android PushDeer 已停用，安卓用户应使用默认的 Server酱³。来源：[PushDeer 官方说明](https://github.com/easychen/pushdeer)、[Server酱³ 官方 FAQ](https://sc3.ft07.com/doc)。

### **star**自己的仓库

![图片加载失败](imgs/4.png)

## 文件结构

```shell
│  checkin.py	# 签到脚本
│
├─.github
│  └─workflows
│          gladosCheck.yml	# Actions 配置文件
```

## 更新日志

- **2026-10**：适配新版 `gld:sess` 签名会话、可配置的 User-Agent 和新版签到结果；认证失败立即停止该账户的后续请求；查询失败不再当作零积分；失败返回非零退出码；请求增加超时并避免输出原始响应或异常中的敏感信息。
- **2026-10**：手机通知默认切换到 Server酱³ App；校验服务端返回值；PushDeer 返回 `False` 时不再误报成功；新推送请求有超时且不自动重试。
- **2026-01**: 重构代码，添加log输出方便定位，支持新版网址，支持配置积分兑换策略。

## “没有权限”排查

GitHub 仓库访问权限与 GLaDOS 登录权限是两回事。接口返回“没有权限”通常需要更新登录会话，修改代码无法恢复已失效的 Cookie。

1. 按上述步骤重新登录 `glados.cloud`，复制包含 `gld:sess` 和 `gld:sess.sig` 的完整 Cookie 与对应 User-Agent。
2. 在仓库 `Settings → Secrets and variables → Actions` 更新 `GLADOS_COOKIES`，添加或更新 `GLADOS_USER_AGENT`；不要修改 `PUSHDEER_SENDKEY` 或兑换计划来解决认证问题。
3. 在 `Actions → auto check → Run workflow` 选择 `master` 手动运行一次。成功签到或重复签到均为正常结果。
4. 如果提示 `device-mismatch`，重新登录并同步更新这两项；不要重复使用旧会话重跑。若同步更新后仍被拒绝，需要在官网处理登录/设备校验，脚本不能保证通过服务端认证策略。

新版会话变化及维护者的实际验证见 [2026-09 会话与设备校验修复记录](https://github.com/lankerr/2026-glados-checkin/pull/18)。当前官方前端也处理 `code=4`、`reason=device-mismatch` 的响应。

## 本地验证

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install requests pypushdeer
.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用模拟接口响应，不签到、不兑换、不发送推送。实际运行 `checkin.py` 会使用环境变量中的凭据签到，并按原配置自动兑换、推送。

## 声明

本项目不保证稳定运行与更新, 因GitHub相关规定可能会删库, 请注意备份
