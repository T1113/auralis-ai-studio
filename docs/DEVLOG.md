# Auralis 开发日志（DEVLOG）

> 约定：每次开发会话结束时在最上方追加一条记录。格式：日期 + 改动摘要 + 涉及文件 + 待办/风险。
> 提交代码前先更新本日志；上线动作（部署、迁移、配置变更）单独标注 **[上线]**。

---

## 2026-06-10（第三批）**[上线]**

部署：Version `11555636`，云端验证 cost=100 / initial=300。

### 积分经济模型修正
- `wrangler.jsonc`：`GENERATION_COST_CREDITS` 1→100，`INITIAL_CREDITS` 3→300（新账户仍是 3 次免费生成）。
- **[迁移]** 云端 D1 执行 `UPDATE users SET credits = credits * 100`（21 个账户），保持老用户购买力不变。

### 移动端适配
- `mobile/` 目录页面确认为孤儿页面（无任何入口、流程缺选风格/确认步骤），不启用；手机用户走主页面的响应式布局。
- 触屏下拉菜单：用户头像菜单原来只有 `:hover` 触发（手机点不开），app.js 增加全局点击切换 `is-open`，点外部自动关闭。
- Dashboard 移动端侧边栏：原来 ≤768px 直接 `display:none`，☰ 按钮无效，"资产/设置/隐私/客服"在手机上完全不可达。改为抽屉浮层（components.css `.sidebar.is-open` + app.js 全局拦截，4 个 dashboard 页一次修复）。
- 控制台过时文案"免费开放/试运行"改为实时积分余额（app.js `fillCreditBalance` 自动填充 `nav-credit-balance` / `main-credit-balance`）。

### 待办
- [ ] Resend 邮件配置（见下方说明）。

## 2026-06-10 **[上线]**

- `wrangler deploy` 部署成功：https://impeccable-site.tangzhengzhuo.workers.dev （Version `1cbed7e9`）。
- 云端验证通过：`/api/config` 已返回 `billing.packages` 与 `unlock.packages`；`/reset` 页面 200；`/api/auth/forgot` 正常（schema 自动迁移已生效）。
- ⚠️ **积分经济模型不一致**：当前 `GENERATION_COST_CREDITS=1`，但套餐积分按"每次生成 100 积分"设计（100/400/1200）。照现状 ¥39 的体验包能生成 100 次。**启用支付前必须把 `GENERATION_COST_CREDITS` 改为 `100`**（或同步调整套餐积分）。
- ⚠️ 忘记密码邮件：生产环境已注册邮箱发起重置会提示"邮件服务暂未开通"，直到配置 `RESEND_API_KEY` + `MAIL_FROM`。

## 2026-06-10

### 忘记密码（邮件重置）
- 新增 `POST /api/auth/forgot`：输入邮箱 → 生成 30 分钟有效的一次性重置 token（SHA-256 哈希入库，旧 token 自动作废），通过 Resend 发送重置邮件；账户不存在时返回相同文案，不泄露注册信息。
- 新增 `POST /api/auth/reset`：校验 token 后更新密码。
- 新增页面 `形象照/reset.html`（设置新密码）。
- `login.html` 忘记密码弹窗从"加客服微信"改为邮箱重置表单；本地联调（非生产）直接返回重置链接便于测试。
- 新增表 `password_resets`（由 `ensureBillingSchema` 自动迁移）。
- **[配置]** 生产环境需要设置 `RESEND_API_KEY` 和 `MAIL_FROM`（发件地址，需在 Resend 验证域名）；未配置时接口返回"邮件服务暂未开通，请联系客服"。

### 注册密码规则放宽
- 由"8 位 + 大写 + 小写 + 数字 + 特殊符号"放宽为"8 位 + 字母 + 数字"（worker `validatePassword` 与 login.html 前端规则同步修改），降低注册流失。

### Google OAuth 登录后回跳
- `/api/auth/google/login?next=<页面>`：next 经站内相对路径白名单校验后写入 `oauth_next` cookie（10 分钟），回调成功后跳回原页面而非固定 dashboard。
- `login.html` 自动把 URL `?next=` 或 sessionStorage 中的回跳地址附加到 Google 登录链接。

### 生成页真实进度
- `generating.html` 进度改为以后端已完成成片数为主信号（`results` 中可用图片数 / `resultCount`），时间估算只作为 80% 以内的平滑下限，不再假装跑到 88% 卡住。

---

## 2026-06-10（更早，付费链路审查与修复）

> 注意：微信支付商户凭证尚未办理，以下支付代码已就绪但**支付通道暂不启用**。

- **[严重]** 结果页"支付解锁"调用的 `/api/jobs/:id/checkout`、`/checkout/mock-confirm`、`/payment-status` 三个接口此前不存在，已在 worker 实现：订单挂任务（`payment_orders.job_id` 新列）、支付成功自动解锁高清下载。
- **[安全]** `prepay` 接口此前信任客户端传入金额/积分（可 ¥1 买 10 万积分），已改为只接受 `packageId`，金额由服务端 `BILLING_PACKAGES` 决定（¥39/1次、¥99/4次、¥249/12次，与落地页一致）。
- `/api/config` 补发 `billing.packages` 与 `unlock.packages`（此前 checkout 页套餐列表渲染为空）。
- checkout 页：充值面板此前永不显示已修复；402 积分不足自动定位充值面板；新增真实支付状态轮询（3 秒）；修复非法 CSS `-var(...)`；二维码加载失败兜底。
- 流程修复：登录后回跳原页面（app.js `requireAuth`）；上传页步骤导航不可跳级；生成页错误横幅自动恢复、失败时给出"重新创建/联系客服"出口。

### 待办 / 风险
- [ ] 微信支付商户号、API 证书未办理 → 支付通道未启用（代码已就绪，配置 `WECHATPAY_*` 后启用）。
- [ ] 支付二维码目前依赖 api.qrserver.com 渲染（境外服务），启用支付前换成本地 QR 生成。
- [ ] Resend 域名验证 + `RESEND_API_KEY` / `MAIL_FROM` 配置（忘记密码邮件依赖）。
- [ ] 部署后首个请求会自动跑 schema 迁移（`payment_orders.job_id`、`password_resets` 表），部署后验证一次。
