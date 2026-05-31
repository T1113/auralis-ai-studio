# 本地网站运行代码说明

你打开的这个地址：

```text
http://127.0.0.1:5001/index.html
```

当前是由这个 Flask 项目在运行，不是根目录的 Cloudflare/Node 项目：

```text
形象照全栈/app.py
```

我已经把它整理到这个新文件夹：

```text
网站本地运行代码说明/
```

## 这些文件分别负责什么

```text
app.py
```

网站后端入口。它启动 Flask 服务、配置数据库、处理页面路由和接口。

主要页面路由：

```text
/                  -> templates/index.html
/index.html        -> templates/index.html
/dashboard.html    -> templates/dashboard.html
/admin.html        -> templates/admin.html
/*.html            -> templates/ 里面同名页面
```

主要接口：

```text
/api/auth/register       注册
/api/auth/login          登录
/api/auth/session        查询当前登录状态
/api/auth/logout         退出登录
/api/chat                客服聊天
/api/upload              上传照片
/api/generate            调用豆包生成形象照
/api/assets              用户图片资产
/api/credits             查询点数
/api/credits/topup       充值点数
/api/batches/<id>        查询生成批次
/api/batches/<id>/unlock 解锁生成结果
/api/account/delete      删除账号
```

```text
templates/
```

所有 HTML 页面。你看到的首页就是：

这里已经补齐 `形象照/` 静态前端版里的完整 UI 页面，包括登录、注册、支持页和 404 页。

```text
templates/index.html
```

```text
static/css/
```

页面样式文件：

```text
system.css      全局设计变量、颜色、字体、按钮基础
layout.css      布局、容器、网格、间距
components.css  卡片、表单、导航等组件样式
```

```text
static/js/
```

前端交互代码：

```text
uploader.js     上传照片、提交生成任务
camera.js       调用摄像头拍照
```

```text
static/assets/js/
```

静态前端版使用的通用交互脚本：

```text
app.js          登录状态、鉴权跳转、请求封装、退出登录
i18n.js         首页多语言切换
locales.json    多语言文案
uploader.js     静态页面引用的上传逻辑
camera.js       静态页面引用的拍照逻辑
```

```text
static/assets/
```

首页和风格选择页展示用图片。

```text
static/uploads/
```

用户上传照片保存目录。已经补齐原 Flask 项目里的历史上传图片。

```text
static/generated/
```

AI 生成结果保存目录。已经补齐原 Flask 项目里的历史生成图片。

```text
.env
```

豆包/火山 Ark 生图能力的本地配置文件。这里面包含 API Key，所以不要上传到 GitHub、网盘公开目录或发给别人。

```text
docs/ark-seedream-image-provider.md
```

豆包 Seedream 生图接口说明，包括本地开发、环境变量、公网图片 URL 和 Cloudflare 部署时的配置方式。

```text
docs/提示词_水印_生图能力说明.md
```

提示词、文字水印、豆包生图能力和对应代码位置说明。

```text
docs/original-requirements/
```

原始产品/UX/上传拍照需求说明。

```text
cloudflare-worker/
```

原项目的 Cloudflare Worker 线上部署版本，包含 `src/worker.js`、D1 migrations、Wrangler 配置、线上 Seedream 生图逻辑，以及 `形象照/` 静态资源目录。

Worker 本地联调配置在：

```text
cloudflare-worker/.dev.vars
```

当前本地联调模式会开启微信支付模拟确认，并给新用户 1000 初始积分。

## 怎么重新运行

进入这个文件夹：

```bash
cd /Users/chris/Desktop/AI形象照Codex5.5/网站本地运行代码说明
```

安装依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

启动：

```bash
.venv/bin/python app.py
```

然后打开：

```text
http://127.0.0.1:5001/index.html
```

说明：原始 `形象照全栈/app.py` 文件底部写的是 5000 端口，但你现在访问的是 5001。这个整理包里的 `app.py` 已经改成直接启动 5001，方便你对应当前页面理解。

如果要运行 Cloudflare Worker 版本：

```bash
cd /Users/chris/Desktop/AI形象照Codex5.5/网站本地运行代码说明/cloudflare-worker
npm install
npm run dev
```

然后打开：

```text
http://127.0.0.1:8787/
```

Worker 版线上部署前还需要把 `wrangler.jsonc` 里的 D1 `database_id` 替换成真实 Cloudflare D1 数据库 ID，并在 Cloudflare 环境里配置真实 R2 bucket 和生产 secrets。

## 豆包生图能力

生成图片功能已经跟着移动过来了，核心在：

```text
app.py
```

相关函数会读取同目录的 `.env`，然后调用豆包/火山 Ark 的图片生成接口：

```text
https://ark.cn-beijing.volces.com/api/v3/images/generations
```

已经复制过来的关键环境变量包括：

```text
DOUBAO_API_KEY
DOUBAO_MODEL
DOUBAO_IMAGE_SIZE
DOUBAO_RESPONSE_FORMAT
DOUBAO_INPUT_IMAGE_FORMAT
DOUBAO_MAX_IMAGES
DOUBAO_TIMEOUT_SECONDS
```

当前代码支持两种参考图输入方式：

```text
DOUBAO_INPUT_IMAGE_FORMAT=data_url
```

这种方式会把上传图片转成 base64/data URL，适合本地直接调试。当前整理包已经改成这个模式，不依赖旧 ngrok 地址。

```text
DOUBAO_INPUT_IMAGE_FORMAT=url
```

这种方式要求 `DOUBAO_PUBLIC_BASE_URL` 是公网 HTTPS 地址，豆包服务必须能访问到你本机上传的图片。如果这个地址是以前的 ngrok 地址，重新启动 ngrok 后需要更新 `.env`。
