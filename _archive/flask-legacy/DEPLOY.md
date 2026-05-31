# 上线部署手册

> 本手册假设你已经看过 `/Users/chris/.claude/plans/warm-riding-shore.md` 里的整体计划。
> 这里只写**怎么把代码跑到一台公网服务器上**的具体操作。

## 0. 准备清单（先把这些拿到手）

| 项 | 说明 |
|---|---|
| 域名 | 例如 `auralis.cn`，国内服务器需 ICP 备案（约 20 天） |
| 云服务器 | Ubuntu 22.04，2C4G，开放 80/443 端口 |
| 营业执照 | 个体工商户或公司，微信支付要 |
| 微信支付商户号 | pay.weixin.qq.com 申请，拿到 `apiclient_key.pem` |
| 火山方舟 API Key | volcengine.com 控制台开通豆包图像生成模型 |
| 阿里云 OSS / 腾讯云 COS | 存上传/生成图（P1，可后置） |

## 1. 服务器初始化

```bash
ssh root@your.server.ip
apt update && apt upgrade -y
apt install -y docker.io docker-compose-plugin nginx certbot python3-certbot-nginx git
systemctl enable --now docker
useradd -m -s /bin/bash app
mkdir -p /opt/impeccable && chown app:app /opt/impeccable
```

## 2. 拉代码 + 配置

```bash
su - app
cd /opt/impeccable
# git clone <your repo> .  或 scp 整个目录上来
cp .env.production.example .env.production
vim .env.production           # 填真实密钥
mkdir -p wechatpay_certs
# 上传 apiclient_key.pem 到 wechatpay_certs/，权限 600
chmod 600 wechatpay_certs/apiclient_key.pem
```

## 3. 启动应用（两种方式二选一）

### 方式 A：docker compose（推荐）

```bash
cd /opt/impeccable
docker compose build
docker compose up -d
docker compose logs -f app          # 看启动日志
```

### 方式 B：裸 systemd

```bash
cd /opt/impeccable
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
sudo cp deploy/impeccable.service /etc/systemd/system/
sudo cp .env.production /etc/impeccable.env
sudo chmod 600 /etc/impeccable.env
sudo systemctl daemon-reload
sudo systemctl enable --now impeccable
sudo systemctl status impeccable
```

## 4. Nginx + HTTPS

```bash
sudo cp deploy/nginx.conf /etc/nginx/sites-available/impeccable
sudo sed -i 's/your.domain/auralis.cn/g' /etc/nginx/sites-available/impeccable
sudo ln -sf /etc/nginx/sites-available/impeccable /etc/nginx/sites-enabled/
sudo nginx -t && sudo systemctl reload nginx

# 申请 Let's Encrypt 证书（会自动改写 nginx 配置加上 HTTPS）
sudo certbot --nginx -d auralis.cn -d www.auralis.cn
```

## 5. 数据库初始化

当前 app.py 仍走 SQLite，首次启动会在 `/opt/impeccable/impeccable.db` 自建库。

**要切到 Postgres（P0 计划项 #3，本次未自动改）**：

```bash
apt install -y postgresql
sudo -u postgres createuser impeccable -P
sudo -u postgres createdb -O impeccable impeccable
psql -U impeccable -d impeccable -f migrations/0001_initial_postgres.sql
# 然后需要改 app.py 把 sqlite3 换成 psycopg2 —— 这是单独一轮重构
```

## 6. 验证上线

```bash
curl -I https://auralis.cn/                              # 200 + HSTS
curl https://auralis.cn/api/auth/session                 # JSON
# 浏览器：注册 → 上传 → 生成 → 充值 10 元 → 扫码 → 看点数到账
```

## 7. 备份 + 监控

```bash
# 每天凌晨 3 点备份 sqlite + 私有图，30 天保留
echo '0 3 * * * app cd /opt/impeccable && tar czf /var/backups/impeccable-$(date +\%F).tgz impeccable.db private_media && find /var/backups -name "impeccable-*.tgz" -mtime +30 -delete' | sudo tee /etc/cron.d/impeccable-backup
```

接 Sentry：在 `.env.production` 填 `SENTRY_DSN`。
接 UptimeRobot：监控 `https://auralis.cn/api/auth/session` 5 分钟一次。

## 8. 上线开关切换清单

部署后、对外开放前，确认 `.env.production` 里：

- [ ] `FREE_TRIAL_MODE=false`
- [ ] `PAYMENT_MODE=wechatpay`
- [ ] `IMAGE_GENERATION_PROVIDER=doubao`
- [ ] `DOUBAO_ALLOW_MOCK_FALLBACK=false`
- [ ] `SESSION_COOKIE_SECURE=true`
- [ ] `FLASK_DEBUG=false`
- [ ] 自己用真钱跑通一笔 ¥1 微信支付

## 9. 还没做的（继续推进）

以下 P0/P1 项需要后续单独开任务完成，详见 `/Users/chris/.claude/plans/warm-riding-shore.md`：

1. **SQLite → Postgres 重构** —— app.py 里 ~2900 行裸 SQL 改 psycopg2 / SQLAlchemy
2. **OSS 对象存储抽象** —— 抽 `storage.py`，把 `UPLOAD_DIR/GENERATED_DIR` 切到 OSS
3. **Flask-Limiter 限流接入** —— 注册/登录/生图/充值四个端点
4. **Sentry 初始化** —— `app.py` 启动处加 `sentry_sdk.init(...)`
5. **首页底部备案号 + 合规链接** —— 改 `templates/index.html` footer
6. **微信支付 notify 幂等性审查** —— 复测 `wechat_pay_notify` 重入、签名异常分支
