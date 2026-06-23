const SESSION_DURATION_SECONDS = 60 * 60 * 24 * 30;
const PASSWORD_ITERATIONS = 100000;
// 仅用于"账户不存在"时执行一次等价的 PBKDF2，抹平登录响应时间差以防用户枚举。
const DUMMY_PASSWORD_SALT_HEX = "00000000000000000000000000000000";
const MAX_UPLOAD_SIZE_BYTES = 10 * 1024 * 1024;
const DEFAULT_INITIAL_CREDITS = 0;
const DEFAULT_LOCAL_INITIAL_CREDITS = 1000;
const DEFAULT_GENERATION_COST_CREDITS = 100;
const DEFAULT_FREE_REGENERATIONS = 2;
const DEFAULT_TOP_UP_CREDITS = 1000;
const DEFAULT_TOP_UP_AMOUNT_CENTS = 9900;
// 套餐定价是服务端唯一可信来源；前端只回传 packageId。
const BILLING_PACKAGES = [
  {
    id: "single",
    name: "体验包",
    tagline: "试一次，看效果",
    amountCents: 3900,
    credits: 100,
    generations: 1,
    popular: false,
    features: ["1 次生成 / 4 张成片", "2 次免费重绘", "全部风格可选"]
  },
  {
    id: "standard",
    name: "标准包",
    tagline: "最划算的求职套餐",
    amountCents: 9900,
    originalAmountCents: 15600,
    credits: 400,
    generations: 4,
    popular: true,
    features: ["4 次生成 / 16 张成片", "每次 2 次免费重绘", "单次成本低至 ¥24.75"]
  },
  {
    id: "pro",
    name: "团队包",
    tagline: "团队/多场景批量出片",
    amountCents: 24900,
    originalAmountCents: 46800,
    credits: 1200,
    generations: 12,
    popular: false,
    features: ["12 次生成 / 48 张成片", "每次 2 次免费重绘", "单次成本低至 ¥20.75"]
  }
];
const STALE_GENERATION_RETRY_AFTER_MS = 90 * 1000;
const IMAGE_GENERATION_TIMEOUT_MS = 110 * 1000;
const ARK_IMAGE_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3";
const ARK_IMAGE_GENERATIONS_PATH = "/images/generations";
const DEFAULT_ARK_SEEDREAM_IMAGE_MODEL = "doubao-seedream-5-0-260128";
const ARK_SEEDREAM_IMAGE_MODELS = new Set([
  "doubao-seedream-5-0-260128",
  "doubao-seedream-4-5-251128",
  "doubao-seedream-4-0-250828"
]);
const DEFAULT_RESULT_COUNT = 1;
let billingSchemaReady = false;
const ALLOWED_IMAGE_TYPES = new Set([
  "image/jpeg",
  "image/jpg",
  "image/png",
  "image/webp",
  "image/heic",
  "image/heif"
]);
const RESULT_LABELS = [
  "经典商务浅灰底",
  "领英专属蓝底",
  "自然光高级灰",
  "暖光极简白",
  "户外虚化自然光"
];
const CELEBRITY_HEADSHOT_LABEL = "明星级职业头像（1:1）";
const PUBLIC_SITEMAP_PATHS = [
  "/",
  "/gallery.html",
  "/enterprise.html",
  "/faq.html",
  "/privacy.html",
  "/terms.html",
  "/tutorial.html"
];
const PRIVATE_PATH_PREFIXES = [
  "/login",
  "/upload",
  "/style",
  "/checkout",
  "/generating",
  "/result",
  "/dashboard",
  "/support",
  "/admin"
];

export default {
  async fetch(request, env, ctx) {
    try {
      const url = new URL(request.url);

      if (request.method === "OPTIONS") {
        return withHeaders(new Response(null, { status: 204 }), request);
      }

      if (url.pathname === "/robots.txt") {
        return withHeaders(buildRobotsResponse(url), request);
      }

      if (url.pathname === "/sitemap.xml") {
        return withHeaders(buildSitemapResponse(url), request);
      }

      if (url.pathname.startsWith("/api/")) {
        await ensureBillingSchema(env);
        return withHeaders(await handleApiRequest(request, env, url, ctx), request);
      }

      if (url.pathname.startsWith("/media/uploads/")) {
        return withHeaders(await handleUploadMediaRequest(request, env, url), request);
      }

      return withHeaders(await handleAssetRequest(request, env, url), request);
    } catch (error) {
      console.error("Unhandled worker error", error);
      return withHeaders(
        jsonResponse(
          {
            error: "服务暂时不可用，请稍后再试。"
          },
          500
        ),
        request
      );
    }
  },

  // 定时清理过期的限流计数与失效的重置 token，防止表无限增长。
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(cleanupExpiredRecords(env));
  }
};

// 清理 24 小时前的限流窗口，以及已使用或已过期 24 小时的重置 token。
async function cleanupExpiredRecords(env) {
  if (!env.DB) {
    return;
  }
  const now = Date.now();
  try {
    await env.DB.prepare("DELETE FROM rate_limits WHERE window_start < ?")
      .bind(now - 24 * 60 * 60 * 1000)
      .run();
    await env.DB.prepare("DELETE FROM password_resets WHERE expires_at < ? OR used_at IS NOT NULL")
      .bind(now - 24 * 60 * 60 * 1000)
      .run();
  } catch (error) {
    console.error("cleanup expired records failed", error);
  }
}

async function handleAssetRequest(request, env, url) {
  let response = await env.ASSETS.fetch(request);

  if (response.status === 404 && !hasFileExtension(url.pathname)) {
    const htmlUrl = new URL(url.toString());
    htmlUrl.pathname = `${trimTrailingSlash(htmlUrl.pathname)}.html`;
    response = await env.ASSETS.fetch(new Request(htmlUrl.toString(), request));
  }

  return response;
}

async function handleUploadMediaRequest(request, env, url) {
  if (request.method !== "GET") {
    return methodNotAllowed(["GET"]);
  }

  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const uploadId = url.pathname.split("/").pop();
  if (!uploadId) {
    return jsonResponse({ error: "图片不存在。" }, 404);
  }

  const upload = await env.DB.prepare(
    `SELECT id, user_id, object_key, mime_type, file_name
     FROM uploads
     WHERE id = ?`
  )
    .bind(uploadId)
    .first();

  if (!upload || upload.user_id !== session.user.id) {
    return jsonResponse({ error: "无法访问该图片。" }, 404);
  }

  const object = await env.UPLOADS.get(upload.object_key);
  if (!object) {
    return jsonResponse({ error: "图片不存在。" }, 404);
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Content-Type", upload.mime_type || headers.get("Content-Type") || "application/octet-stream");
  headers.set("Content-Disposition", `inline; filename="${encodeRFC5987(upload.file_name)}"`);
  headers.set("Cache-Control", "private, max-age=300");

  return new Response(object.body, {
    status: 200,
    headers
  });
}

async function handleApiRequest(request, env, url, ctx) {
  const { pathname } = url;

  if (pathname === "/api/auth/register" && request.method === "POST") {
    return handleRegister(request, env);
  }

  if (pathname === "/api/auth/login" && request.method === "POST") {
    return handleLogin(request, env);
  }

  if (pathname === "/api/auth/forgot" && request.method === "POST") {
    return handleForgotPassword(request, env);
  }

  if (pathname === "/api/auth/reset" && request.method === "POST") {
    return handleResetPassword(request, env);
  }

  if (pathname === "/api/auth/logout" && request.method === "POST") {
    return handleLogout(request, env);
  }

  if (pathname === "/api/auth/session" && request.method === "GET") {
    return handleSession(request, env);
  }

  if ((pathname === "/api/auth/google/login" || pathname === "/api/auth/google") && request.method === "GET") {
    return handleGoogleLogin(request, env, url);
  }

  if (pathname === "/api/auth/google/callback" && request.method === "GET") {
    return handleGoogleCallback(request, env, url);
  }

  if (pathname === "/api/config" && request.method === "GET") {
    return handlePublicConfig(env);
  }

  if (pathname === "/api/account/password" && request.method === "PATCH") {
    return handlePasswordUpdate(request, env);
  }

  if (pathname === "/api/account/data" && request.method === "DELETE") {
    return handleAccountDataErase(request, env);
  }

  if (pathname === "/api/payments/wechat/prepay" && request.method === "POST") {
    return handleWeChatPrepay(request, env);
  }

  if (pathname === "/api/payments/wechat/mock-confirm" && request.method === "POST") {
    return handleMockPaymentConfirm(request, env);
  }

  if (pathname === "/api/payments/wechat/notify" && request.method === "POST") {
    return handleWeChatPaymentNotify(request, env);
  }

  if (pathname.startsWith("/api/payments/") && request.method === "GET") {
    return handlePaymentStatus(request, env, pathname);
  }

  if (pathname === "/api/leads/contact" && request.method === "POST") {
    return handleLeadSubmission(request, env);
  }

  if (pathname === "/api/uploads" && request.method === "POST") {
    return handleUploadSubmission(request, env);
  }

  if (pathname === "/api/jobs" && request.method === "POST") {
    return handleJobCreation(request, env, ctx);
  }

  if (pathname === "/api/jobs" && request.method === "GET") {
    return handleJobList(request, env);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.endsWith("/unlock") && request.method === "POST") {
    return handleUnlockJob(request, env, pathname);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.endsWith("/checkout") && request.method === "POST") {
    return handleJobCheckout(request, env, pathname);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.endsWith("/checkout/mock-confirm") && request.method === "POST") {
    return handleJobCheckoutMockConfirm(request, env, pathname);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.endsWith("/payment-status") && request.method === "GET") {
    return handleJobPaymentStatus(request, env, pathname, url);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.endsWith("/regenerate") && request.method === "POST") {
    return handleRegenerateJob(request, env, pathname, ctx);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.includes("/results/") && pathname.endsWith("/download") && request.method === "GET") {
    return handleResultDownload(request, env, pathname, url);
  }

  if (pathname.startsWith("/api/jobs/") && pathname.endsWith("/download") && request.method === "GET") {
    return handleJobArchiveDownload(request, env, pathname, url);
  }

  if (pathname.startsWith("/api/jobs/") && request.method === "GET") {
    return handleJobDetail(request, env, pathname, ctx);
  }

  if (pathname === "/api/chat" && request.method === "POST") {
    return handleChatMessageSend(request, env);
  }

  if (pathname === "/api/chat" && request.method === "GET") {
    return handleChatMessages(request, env, url);
  }

  if (pathname === "/api/chat/users" && request.method === "GET") {
    return handleChatUsers(request, env);
  }

  if (pathname === "/api/admin/summary" && request.method === "GET") {
    return handleAdminSummary(request, env);
  }

  return jsonResponse({ error: "接口不存在。" }, 404);
}

async function handleRegister(request, env) {
  const payload = await readJson(request);
  const name = sanitizeText(payload?.name, 40);
  const email = normaliseEmail(payload?.email);
  const password = String(payload?.password || "");

  if (!name) {
    return jsonResponse({ error: "请输入用户名或昵称。" }, 400);
  }

  if (!isValidEmail(email)) {
    return jsonResponse({ error: "请输入正确的邮箱地址。" }, 400);
  }

  const passwordError = validatePassword(password);
  if (passwordError) {
    return jsonResponse({ error: passwordError }, 400);
  }

  const existingUser = await env.DB.prepare("SELECT id FROM users WHERE email = ?")
    .bind(email)
    .first();
  if (existingUser) {
    return jsonResponse({ error: "该邮箱已注册，请直接登录。" }, 409);
  }

  const userId = randomId("usr");
  const saltHex = randomHex(16);
  const passwordHash = await derivePasswordHash(password, saltHex);
  const now = Date.now();
  const role = matchAdminEmail(email, env.ADMIN_EMAILS) ? "admin" : "user";

  const initialCredits = getInitialCredits(env);

  await env.DB.prepare(
    `INSERT INTO users (id, email, name, password_hash, password_salt, role, credits, created_at, last_login_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(userId, email, name, passwordHash, saltHex, role, initialCredits, now, now)
    .run();

  return createSessionResponse(env, request, {
    id: userId,
    email,
    name,
    role,
    credits: initialCredits
  });
}

async function handleLogin(request, env) {
  const payload = await readJson(request);
  const email = normaliseEmail(payload?.email);
  const password = String(payload?.password || "");

  if (!isValidEmail(email) || !password) {
    return jsonResponse({ error: "请填写正确的邮箱与密码。" }, 400);
  }

  // 同一 IP / 同一邮箱分别限流，挡住撞库与针对单账号的爆破。
  const ip = getClientIp(request);
  const ipLimit = await enforceRateLimit(env, `login:ip:${ip}`, 20, 10 * 60 * 1000);
  if (!ipLimit.allowed) {
    return rateLimitedResponse(ipLimit.retryAfter);
  }
  const emailLimit = await enforceRateLimit(env, `login:email:${email}`, 10, 10 * 60 * 1000);
  if (!emailLimit.allowed) {
    return rateLimitedResponse(emailLimit.retryAfter);
  }

  const user = await env.DB.prepare(
    `SELECT id, email, name, role, credits, password_hash, password_salt
     FROM users
     WHERE email = ?`
  )
    .bind(email)
    .first();

  // 即使账户不存在也执行一次 PBKDF2，让"无此账号"与"密码错误"耗时相近，避免用户枚举。
  const saltForHash = user ? user.password_salt : DUMMY_PASSWORD_SALT_HEX;
  const passwordHash = await derivePasswordHash(password, saltForHash);
  if (!user || passwordHash !== user.password_hash) {
    return jsonResponse({ error: "账号或密码错误。" }, 401);
  }

  await env.DB.prepare("UPDATE users SET last_login_at = ? WHERE id = ?")
    .bind(Date.now(), user.id)
    .run();

  return createSessionResponse(env, request, {
    id: user.id,
    email: user.email,
    name: user.name,
    role: user.role,
    credits: Number(user.credits || 0)
  });
}

async function handleLogout(request, env) {
  const sessionCookieName = getSessionCookieName(env);
  const cookies = parseCookies(request.headers.get("Cookie"));
  const token = cookies[sessionCookieName];

  if (token) {
    await env.DB.prepare("DELETE FROM sessions WHERE token_hash = ?")
      .bind(await sha256Hex(token))
      .run();
  }

  return jsonResponse(
    { success: true },
    200,
    {
      "Set-Cookie": buildExpiredSessionCookie(sessionCookieName, request)
    }
  );
}

async function handleSession(request, env) {
  const session = await getSessionFromRequest(request, env);
  if (!session) {
    return jsonResponse({ authenticated: false, user: null });
  }

  return jsonResponse({
    authenticated: true,
    user: {
      id: session.user.id,
      name: session.user.name,
      email: session.user.email,
      role: session.user.role,
      credits: session.user.credits
    }
  });
}

const PASSWORD_RESET_TTL_MS = 30 * 60 * 1000;

function isMailConfigured(env) {
  return Boolean(env.RESEND_API_KEY && env.MAIL_FROM);
}

async function sendPasswordResetEmail(env, email, resetUrl) {
  const siteName = env.PUBLIC_SITE_NAME || env.APP_NAME || "Auralis";
  const response = await fetch("https://api.resend.com/emails", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${env.RESEND_API_KEY}`,
      "Content-Type": "application/json"
    },
    body: JSON.stringify({
      from: env.MAIL_FROM,
      to: [email],
      subject: `${siteName} 密码重置`,
      html: `
        <div style="font-family: -apple-system, 'Segoe UI', sans-serif; max-width: 480px; margin: 0 auto; padding: 24px;">
          <h2 style="margin: 0 0 16px;">${siteName} 密码重置</h2>
          <p style="color: #444; line-height: 1.7;">我们收到了你的密码重置请求。点击下面的按钮设置新密码，链接 30 分钟内有效：</p>
          <p style="margin: 24px 0;">
            <a href="${resetUrl}" style="display: inline-block; background: #111; color: #fff; padding: 12px 24px; border-radius: 8px; text-decoration: none;">设置新密码</a>
          </p>
          <p style="color: #888; font-size: 13px; line-height: 1.7;">如果按钮无法点击，请复制以下链接到浏览器打开：<br>${resetUrl}</p>
          <p style="color: #888; font-size: 13px;">如果这不是你的操作，请忽略本邮件，你的密码不会被更改。</p>
        </div>`
    })
  });

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    throw new Error(`邮件发送失败（${response.status}）：${detail.slice(0, 200)}`);
  }
}

async function handleForgotPassword(request, env) {
  const payload = await readJson(request);
  const email = normaliseEmail(payload?.email);
  if (!email) {
    return jsonResponse({ error: "请填写邮箱地址。" }, 400);
  }

  // 无论账户是否存在都返回相同文案，避免暴露注册信息。
  const genericResponse = { success: true, message: "如果该邮箱已注册，我们已发送密码重置邮件，请查收（注意垃圾箱）。" };

  // 限流：同一 IP 与同一目标邮箱都设上限，防止刷邮件 / 骚扰他人收件箱。
  const ip = getClientIp(request);
  const ipLimit = await enforceRateLimit(env, `forgot:ip:${ip}`, 10, 60 * 60 * 1000);
  if (!ipLimit.allowed) {
    return rateLimitedResponse(ipLimit.retryAfter);
  }
  const emailLimit = await enforceRateLimit(env, `forgot:email:${email}`, 3, 60 * 60 * 1000);
  if (!emailLimit.allowed) {
    // 返回与正常路径一致的文案，避免通过限流差异判断邮箱是否注册。
    return jsonResponse(genericResponse);
  }

  const user = await env.DB.prepare("SELECT id, email FROM users WHERE email = ?")
    .bind(email)
    .first();
  if (!user) {
    return jsonResponse(genericResponse);
  }

  const token = randomHex(32);
  const tokenHash = await sha256Hex(token);
  const now = Date.now();
  // 同一用户的旧 token 全部作废，永远只有最新一封邮件有效。
  await env.DB.prepare("UPDATE password_resets SET used_at = ? WHERE user_id = ? AND used_at IS NULL")
    .bind(now, user.id)
    .run();
  await env.DB.prepare(
    `INSERT INTO password_resets (id, user_id, token_hash, expires_at, created_at)
     VALUES (?, ?, ?, ?, ?)`
  )
    .bind(randomId("rst"), user.id, tokenHash, now + PASSWORD_RESET_TTL_MS, now)
    .run();

  const resetUrl = `${new URL(request.url).origin}/reset.html?token=${token}`;

  if (isMailConfigured(env)) {
    try {
      await sendPasswordResetEmail(env, user.email, resetUrl);
    } catch (error) {
      console.error("password reset mail failed", error);
      return jsonResponse({ error: "邮件发送失败，请稍后再试或联系客服。" }, 502);
    }
    return jsonResponse(genericResponse);
  }

  if (!isProductionRuntime(env)) {
    // 本地联调没有邮件服务时直接返回链接，方便测试完整流程。
    return jsonResponse({ ...genericResponse, devResetUrl: resetUrl });
  }

  return jsonResponse({ error: "邮件服务暂未开通，请联系客服重置密码。" }, 503);
}

async function handleResetPassword(request, env) {
  const payload = await readJson(request);
  const token = sanitizeText(payload?.token, 128);
  const newPassword = String(payload?.password || "");

  if (!token) {
    return jsonResponse({ error: "重置链接无效，请重新发起找回密码。" }, 400);
  }
  const passwordError = validatePassword(newPassword);
  if (passwordError) {
    return jsonResponse({ error: passwordError }, 400);
  }

  const tokenHash = await sha256Hex(token);
  const record = await env.DB.prepare(
    `SELECT id, user_id, expires_at, used_at FROM password_resets WHERE token_hash = ?`
  )
    .bind(tokenHash)
    .first();

  if (!record || record.used_at || Number(record.expires_at) < Date.now()) {
    return jsonResponse({ error: "重置链接已失效，请重新发起找回密码。" }, 400);
  }

  const saltHex = randomHex(16);
  const passwordHash = await derivePasswordHash(newPassword, saltHex);
  const now = Date.now();
  await env.DB.prepare("UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?")
    .bind(passwordHash, saltHex, record.user_id)
    .run();
  await env.DB.prepare("UPDATE password_resets SET used_at = ? WHERE id = ?")
    .bind(now, record.id)
    .run();
  // 重置密码后吊销该用户所有既有会话，防止被盗会话在改密后继续存活。
  await env.DB.prepare("DELETE FROM sessions WHERE user_id = ?")
    .bind(record.user_id)
    .run();

  return jsonResponse({ success: true, message: "密码已重置，请使用新密码登录。" });
}

async function handlePublicConfig(env) {
  const payment = resolvePaymentMode(env);
  const generation = resolveGenerationMode(env);

  return jsonResponse({
    environment: {
      production: isProductionRuntime(env),
      freeTrialMode: isFreeTrialMode(env)
    },
    billing: {
      generationCostCredits: getGenerationCostCredits(env),
      initialCredits: getInitialCredits(env),
      topUpCredits: DEFAULT_TOP_UP_CREDITS,
      topUpAmountCents: DEFAULT_TOP_UP_AMOUNT_CENTS,
      packages: BILLING_PACKAGES,
      enabled: !isFreeTrialMode(env)
    },
    payment: {
      provider: payment.provider,
      ready: payment.ready,
      mockEnabled: payment.mockEnabled,
      requiredConfig: payment.ready || payment.mockEnabled ? [] : payment.requiredConfig
    },
    unlock: {
      mode: payment.ready ? "wechat_native" : "mock",
      packages: BILLING_PACKAGES.map((pkg) => ({
        id: pkg.id,
        label: pkg.name,
        subtitle: `${pkg.generations} 次生成 / ${pkg.generations * 4} 张成片`,
        amountCents: pkg.amountCents
      }))
    },
    generation: {
      provider: generation.provider,
      model: resolveSeedreamImageModel(env),
      ready: generation.ready,
      requiredConfig: generation.ready ? [] : generation.requiredConfig
    }
  });
}

async function handlePasswordUpdate(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const payload = await readJson(request);
  const currentPassword = String(payload?.currentPassword || "");
  const newPassword = String(payload?.newPassword || "");

  if (!currentPassword || !newPassword) {
    return jsonResponse({ error: "请填写当前密码与新密码。" }, 400);
  }

  const passwordError = validatePassword(newPassword);
  if (passwordError) {
    return jsonResponse({ error: passwordError }, 400);
  }

  const user = await env.DB.prepare(
    "SELECT password_hash, password_salt FROM users WHERE id = ?"
  )
    .bind(session.user.id)
    .first();

  if (!user) {
    return jsonResponse({ error: "账户不存在，请重新登录。" }, 404);
  }

  const currentHash = await derivePasswordHash(currentPassword, user.password_salt);
  if (currentHash !== user.password_hash) {
    return jsonResponse({ error: "当前密码不正确。" }, 401);
  }

  const saltHex = randomHex(16);
  const passwordHash = await derivePasswordHash(newPassword, saltHex);
  await env.DB.prepare(
    "UPDATE users SET password_hash = ?, password_salt = ? WHERE id = ?"
  )
    .bind(passwordHash, saltHex, session.user.id)
    .run();

  return jsonResponse({ success: true });
}

async function handleAccountDataErase(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const userId = session.user.id;
  const [uploadsResponse, resultsResponse] = await Promise.all([
    env.DB.prepare("SELECT object_key FROM uploads WHERE user_id = ?").bind(userId).all(),
    env.DB.prepare(
      `SELECT job_results.object_key
       FROM job_results
       JOIN jobs ON jobs.id = job_results.job_id
       WHERE jobs.user_id = ?
         AND job_results.object_key IS NOT NULL`
    ).bind(userId).all()
  ]);

  const objectKeys = [
    ...(uploadsResponse.results || []).map((row) => row.object_key),
    ...(resultsResponse.results || []).map((row) => row.object_key)
  ].filter(Boolean);

  for (const key of objectKeys) {
    await env.UPLOADS.delete(key);
  }

  await env.DB.prepare(
    `DELETE FROM job_results
     WHERE job_id IN (SELECT id FROM jobs WHERE user_id = ?)`
  )
    .bind(userId)
    .run();
  await env.DB.prepare(
    `DELETE FROM job_uploads
     WHERE job_id IN (SELECT id FROM jobs WHERE user_id = ?)`
  )
    .bind(userId)
    .run();
  await env.DB.prepare("DELETE FROM jobs WHERE user_id = ?")
    .bind(userId)
    .run();
  await env.DB.prepare("DELETE FROM uploads WHERE user_id = ?")
    .bind(userId)
    .run();

  return jsonResponse({
    success: true,
    deletedObjects: objectKeys.length
  });
}

async function handleLeadSubmission(request, env) {
  const payload = await readJson(request);
  const source = ["index", "enterprise"].includes(payload?.source) ? payload.source : "index";
  const name = sanitizeText(payload?.name, 80);
  const company = sanitizeText(payload?.company, 120);
  const contact = sanitizeText(payload?.contact, 120);
  const note = sanitizeText(payload?.note, 300);

  if (!name && !company && !contact && !note) {
    return jsonResponse({ error: "请至少留下一个联系方式或备注。" }, 400);
  }

  await env.DB.prepare(
    `INSERT INTO sales_leads (id, source, name, company, contact, note, created_at)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(randomId("lead"), source, name, company, contact, note, Date.now())
    .run();

  return jsonResponse({ success: true });
}

async function handleUploadSubmission(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  let formData;
  try {
    formData = await request.formData();
  } catch {
    return jsonResponse({ error: "上传请求格式不正确。" }, 400);
  }

  const fileEntries = formData.getAll("files").filter((entry) => entry instanceof File);
  if (fileEntries.length === 0) {
    return jsonResponse({ error: "请至少选择 1 张照片。" }, 400);
  }

  if (fileEntries.length > 5) {
    return jsonResponse({ error: "单次最多上传 5 张照片。" }, 400);
  }

  const uploads = [];
  for (const file of fileEntries) {
    const validationError = validateUploadFile(file);
    if (validationError) {
      return jsonResponse({ error: validationError }, 400);
    }

    const uploadId = randomId("upl");
    const objectKey = `${session.user.id}/${uploadId}/${sanitizeFileName(file.name)}`;

    await env.UPLOADS.put(objectKey, file.stream(), {
      httpMetadata: {
        contentType: file.type || "application/octet-stream"
      }
    });

    await env.DB.prepare(
      `INSERT INTO uploads (id, user_id, object_key, file_name, mime_type, size_bytes, created_at, status)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(uploadId, session.user.id, objectKey, file.name, file.type || "application/octet-stream", file.size, Date.now(), "uploaded")
      .run();

    uploads.push({
      id: uploadId,
      name: file.name,
      size: file.size,
      mimeType: file.type,
      status: "success",
      previewUrl: `/media/uploads/${uploadId}`
    });
  }

  return jsonResponse({
    success: true,
    uploads
  });
}

async function handleJobCreation(request, env, ctx) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const payload = await readJson(request);
  const uploadIds = Array.isArray(payload?.uploadIds)
    ? payload.uploadIds.map((value) => String(value || "").trim()).filter(Boolean)
    : [];
  const styleSummary = sanitizeText(payload?.styleSummary, 240) || "求职简历场景 / 亲和专业 / 智能背景";
  const resultLabels = resolveRequestedResultLabels(payload?.styleConfig);
  const packageTier = sanitizeText(payload?.packageTier, 40) || "基础版";
  const amountCents = Number(payload?.amountCents || 990);
  const generationCostCredits = getGenerationCostCredits(env);
  const generationMode = resolveGenerationMode(env);

  if (uploadIds.length < 1 || uploadIds.length > 5) {
    return jsonResponse({ error: "请先上传 1 到 5 张照片。" }, 400);
  }

  if (generationMode.requiresExternalProvider && !generationMode.ready) {
    return jsonResponse(
      {
        error: "图像生成服务尚未配置，暂时不能创建正式生成任务。",
        requiredConfig: generationMode.requiredConfig,
        nextStep: "请配置图像生成服务 API Key 与模型参数后重试。"
      },
      503
    );
  }

  const placeholders = uploadIds.map(() => "?").join(", ");
  const statement = env.DB.prepare(
    `SELECT id, user_id FROM uploads
     WHERE user_id = ? AND id IN (${placeholders})`
  ).bind(session.user.id, ...uploadIds);
  const { results } = await statement.all();

  if ((results || []).length !== uploadIds.length) {
    return jsonResponse({ error: "上传文件无效，请重新上传后再试。" }, 400);
  }

  if (generationCostCredits > 0) {
    const debitResult = await env.DB.prepare(
      "UPDATE users SET credits = credits - ? WHERE id = ? AND credits >= ?"
    )
      .bind(generationCostCredits, session.user.id, generationCostCredits)
      .run();

    if (!debitResult.meta || debitResult.meta.changes !== 1) {
      const user = await env.DB.prepare("SELECT credits FROM users WHERE id = ?")
        .bind(session.user.id)
        .first();
      return jsonResponse(
        {
          error: `积分不足。本次生成需要 ${generationCostCredits} 积分，当前剩余 ${Number(user?.credits || 0)} 积分。`
        },
        402
      );
    }
  }

  const now = Date.now();
  const previewReadyAt = now + 18000;
  const jobId = randomId("job");

  try {
    await env.DB.prepare(
      `INSERT INTO jobs (
        id, user_id, style_summary, package_tier, amount_cents, status,
        result_count, created_at, updated_at, preview_ready_at, unlocked_at,
        charged_at, generation_cost_credits, free_regenerations_remaining
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(
        jobId,
        session.user.id,
        styleSummary,
        packageTier,
        amountCents,
        "processing",
        resultLabels.length,
        now,
        now,
        previewReadyAt,
        null,
        now,
        generationCostCredits,
        DEFAULT_FREE_REGENERATIONS
      )
      .run();

    let order = 0;
    for (const uploadId of uploadIds) {
      await env.DB.prepare(
        `INSERT INTO job_uploads (job_id, upload_id, sort_order)
         VALUES (?, ?, ?)`
      )
        .bind(jobId, uploadId, order)
        .run();
      order += 1;
    }

    await createInitialJobResults(env, {
      jobId,
      uploadIds,
      mode: generationMode,
      labels: resultLabels
    });

    if (generationMode.ready) {
      const task = processJobGeneration(env, jobId);
      if (ctx?.waitUntil) {
        ctx.waitUntil(task);
      } else {
        await task;
      }
    } else {
      await env.DB.prepare("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?")
        .bind("completed", Date.now(), jobId)
        .run();
    }
  } catch (error) {
      if (generationCostCredits > 0) {
        await env.DB.prepare("UPDATE users SET credits = credits + ? WHERE id = ?")
          .bind(generationCostCredits, session.user.id)
          .run();
      }
    throw error;
  }

  return jsonResponse({
    success: true,
    jobId,
    chargedCredits: generationCostCredits
  });
}

async function handleJobList(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const { results } = await env.DB.prepare(
    `SELECT
        jobs.id,
        jobs.status,
        jobs.style_summary,
        jobs.package_tier,
        jobs.amount_cents,
        jobs.result_count,
        jobs.created_at,
        jobs.updated_at,
        jobs.preview_ready_at,
        jobs.unlocked_at,
        jobs.charged_at,
        jobs.generation_cost_credits,
        jobs.free_regenerations_remaining,
        uploads.id AS cover_upload_id
      FROM jobs
      LEFT JOIN job_uploads
        ON job_uploads.job_id = jobs.id
       AND job_uploads.sort_order = 0
      LEFT JOIN uploads
        ON uploads.id = job_uploads.upload_id
      WHERE jobs.user_id = ?
      ORDER BY jobs.created_at DESC`
  )
    .bind(session.user.id)
    .all();

  const jobs = (results || []).map((job) => ({
    id: job.id,
    styleSummary: job.style_summary,
    packageTier: job.package_tier,
    amountCents: job.amount_cents,
    resultCount: job.result_count,
    createdAt: job.created_at,
    updatedAt: job.updated_at,
    chargedAt: job.charged_at,
    generationCostCredits: Number(job.generation_cost_credits || DEFAULT_GENERATION_COST_CREDITS),
    freeRegenerationsRemaining: Number(job.free_regenerations_remaining || 0),
    status: deriveJobStatus(job),
    coverImageUrl: job.cover_upload_id ? `/media/uploads/${job.cover_upload_id}` : null
  }));

  return jsonResponse({ jobs });
}

async function handleJobDetail(request, env, pathname, ctx) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const jobId = pathname.replace("/api/jobs/", "");
  const job = await env.DB.prepare(
    `SELECT id, user_id, style_summary, package_tier, amount_cents, result_count,
            status, created_at, updated_at, preview_ready_at, unlocked_at, charged_at,
            generation_cost_credits, free_regenerations_remaining
     FROM jobs
     WHERE id = ?`
  )
    .bind(jobId)
    .first();

  if (!job || job.user_id !== session.user.id) {
    return jsonResponse({ error: "任务不存在。" }, 404);
  }

  const { results } = await env.DB.prepare(
    `SELECT uploads.id, uploads.file_name, uploads.mime_type, uploads.created_at
     FROM job_uploads
     JOIN uploads ON uploads.id = job_uploads.upload_id
     WHERE job_uploads.job_id = ?
     ORDER BY job_uploads.sort_order ASC`
  )
    .bind(jobId)
    .all();

  const uploads = (results || []).map((upload) => ({
    id: upload.id,
    fileName: upload.file_name,
    mimeType: upload.mime_type,
    previewUrl: `/media/uploads/${upload.id}`
  }));
  await recoverStaleGenerationIfNeeded(env, job, ctx);
  const resultItems = await loadJobResultItems(env, job.id, uploads);

  return jsonResponse({
    job: {
      id: job.id,
      styleSummary: job.style_summary,
      packageTier: job.package_tier,
      amountCents: job.amount_cents,
      resultCount: job.result_count,
      createdAt: job.created_at,
      updatedAt: job.updated_at,
      previewReadyAt: job.preview_ready_at,
      chargedAt: job.charged_at,
      generationCostCredits: Number(job.generation_cost_credits || DEFAULT_GENERATION_COST_CREDITS),
      freeRegenerationsRemaining: Number(job.free_regenerations_remaining || 0),
      secondsRemaining: Math.max(0, Math.ceil((job.preview_ready_at - Date.now()) / 1000)),
      status: deriveJobStatus(job),
      unlocked: Boolean(job.unlocked_at),
      uploads,
      generation: buildGenerationDisclosure(env),
      results: resultItems
    }
  });
}

async function recoverStaleGenerationIfNeeded(env, job, ctx) {
  if (!["processing", "generating"].includes(String(job.status || ""))) {
    return;
  }

  const row = await env.DB.prepare(
    `SELECT MAX(updated_at) AS last_updated,
            SUM(CASE WHEN object_key IS NOT NULL THEN 1 ELSE 0 END) AS ready_count,
            SUM(CASE WHEN status IN ('queued', 'generating') THEN 1 ELSE 0 END) AS pending_count
     FROM job_results
     WHERE job_id = ?`
  )
    .bind(job.id)
    .first();

  if (Number(row?.ready_count || 0) > 0 || Number(row?.pending_count || 0) === 0) {
    return;
  }

  const lastUpdated = Number(row?.last_updated || job.updated_at || job.created_at || 0);
  if (Date.now() - lastUpdated < STALE_GENERATION_RETRY_AFTER_MS) {
    return;
  }

  await env.DB.prepare("UPDATE jobs SET updated_at = ?, preview_ready_at = ? WHERE id = ? AND status IN ('processing', 'generating')")
    .bind(Date.now(), Date.now() + 30000, job.id)
    .run();
  await env.DB.prepare("UPDATE job_results SET status = ?, updated_at = ?, diagnostic_message = ? WHERE job_id = ? AND status IN ('queued', 'generating')")
    .bind("queued", Date.now(), "生成任务等待时间较长，系统正在自动重试。", job.id)
    .run();

  const task = processJobGeneration(env, job.id);
  if (ctx?.waitUntil) {
    ctx.waitUntil(task);
  } else {
    await task;
  }
}

async function handleUnlockJob(request, env, pathname) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const jobId = pathname.replace("/api/jobs/", "").replace("/unlock", "");
  const job = await env.DB.prepare("SELECT id, user_id FROM jobs WHERE id = ?")
    .bind(jobId)
    .first();

  if (!job || job.user_id !== session.user.id) {
    return jsonResponse({ error: "任务不存在。" }, 404);
  }

  await env.DB.prepare("UPDATE jobs SET unlocked_at = ?, updated_at = ? WHERE id = ?")
    .bind(Date.now(), Date.now(), jobId)
    .run();

  return jsonResponse({ success: true });
}

async function requireOwnedJob(env, session, jobId) {
  const job = await env.DB.prepare("SELECT id, user_id, unlocked_at FROM jobs WHERE id = ?")
    .bind(jobId)
    .first();
  if (!job || (job.user_id !== session.user.id && session.user.role !== "admin")) {
    return null;
  }
  return job;
}

// 结果页"支付解锁"下单：金额由服务端套餐表决定，订单挂在任务上，支付成功即解锁。
async function handleJobCheckout(request, env, pathname) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const jobId = pathname.replace("/api/jobs/", "").replace("/checkout", "");
  const job = await requireOwnedJob(env, session, jobId);
  if (!job) {
    return jsonResponse({ error: "任务不存在。" }, 404);
  }
  if (job.unlocked_at) {
    return jsonResponse({ alreadyUnlocked: true });
  }

  const payload = await readJson(request);
  const pkg = BILLING_PACKAGES.find((item) => item.id === String(payload?.packageId || ""));
  if (!pkg) {
    return jsonResponse({ error: "套餐不存在，请刷新页面后重新选择。" }, 400);
  }

  const paymentMode = resolvePaymentMode(env);
  if (!paymentMode.ready && !paymentMode.mockEnabled) {
    return jsonResponse(
      { error: "支付通道暂未开放，请稍后再试或联系客服。", requiredConfig: paymentMode.requiredConfig },
      503
    );
  }

  // 复用同一任务、同一套餐、同一金额的待支付订单，避免重复点击生成多个付款码导致重复扣款。
  const existing = await env.DB.prepare(
    `SELECT id, out_trade_no, code_url, amount_cents, credits
     FROM payment_orders
     WHERE job_id = ? AND user_id = ? AND status = 'pending' AND amount_cents = ?
     ORDER BY created_at DESC LIMIT 1`
  )
    .bind(jobId, session.user.id, pkg.amountCents)
    .first();
  if (existing && (!paymentMode.ready || existing.code_url)) {
    return jsonResponse({
      orderId: existing.id,
      outTradeNo: existing.out_trade_no,
      mode: paymentMode.ready ? "wechat_native" : "mock",
      amountCents: Number(existing.amount_cents),
      credits: Number(existing.credits),
      codeUrl: existing.code_url || "",
      alreadyUnlocked: false,
      reused: true
    });
  }

  const orderId = randomId("pay");
  const outTradeNo = `${orderId}_${Date.now()}`;
  const now = Date.now();

  await env.DB.prepare(
    `INSERT INTO payment_orders
       (id, user_id, provider, status, amount_cents, credits, out_trade_no, code_url, job_id, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(orderId, session.user.id, "wechat_native", "pending", pkg.amountCents, pkg.credits, outTradeNo, "", jobId, now, now)
    .run();

  let codeUrl = "";
  if (paymentMode.ready) {
    const payment = await createWeChatNativePrepay(env, {
      outTradeNo,
      amountCents: pkg.amountCents,
      description: `${env.PUBLIC_SITE_NAME || env.APP_NAME || "Auralis"} ${pkg.name}（解锁高清下载）`,
      notifyUrl: env.WECHATPAY_NOTIFY_URL || `${new URL(request.url).origin}/api/payments/wechat/notify`
    });
    codeUrl = payment.codeUrl || "";
    await env.DB.prepare("UPDATE payment_orders SET code_url = ?, raw_response = ?, updated_at = ? WHERE id = ?")
      .bind(codeUrl, JSON.stringify(payment.raw || {}), Date.now(), orderId)
      .run();
  }

  return jsonResponse({
    orderId,
    outTradeNo,
    mode: paymentMode.ready ? "wechat_native" : "mock",
    amountCents: pkg.amountCents,
    credits: pkg.credits,
    codeUrl,
    alreadyUnlocked: false
  });
}

async function handleJobCheckoutMockConfirm(request, env, pathname) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  if (isProductionRuntime(env)) {
    return jsonResponse({ error: "生产环境不能使用模拟支付确认。" }, 403);
  }
  if (isWeChatPayConfigured(env) && env.ENABLE_PAYMENT_MOCK !== "true") {
    return jsonResponse({ error: "支付配置已启用，不能使用模拟支付确认。" }, 403);
  }

  const jobId = pathname.replace("/api/jobs/", "").replace("/checkout/mock-confirm", "");
  const job = await requireOwnedJob(env, session, jobId);
  if (!job) {
    return jsonResponse({ error: "任务不存在。" }, 404);
  }

  const payload = await readJson(request);
  const outTradeNo = sanitizeText(payload?.outTradeNo, 120);
  const order = await env.DB.prepare(
    "SELECT id FROM payment_orders WHERE out_trade_no = ? AND user_id = ? AND job_id = ?"
  )
    .bind(outTradeNo, session.user.id, jobId)
    .first();
  if (!order) {
    return jsonResponse({ error: "支付订单不存在。" }, 404);
  }

  const result = await markPaymentOrderPaid(env, order.id, session.user.id);
  if (!result.ok) {
    return jsonResponse({ error: result.error }, result.status || 400);
  }

  const unlocked = await env.DB.prepare("SELECT unlocked_at FROM jobs WHERE id = ?")
    .bind(jobId)
    .first();

  return jsonResponse({ success: true, unlockedAt: unlocked ? unlocked.unlocked_at : null, user: result.user });
}

async function handleJobPaymentStatus(request, env, pathname, url) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const jobId = pathname.replace("/api/jobs/", "").replace("/payment-status", "");
  const job = await requireOwnedJob(env, session, jobId);
  if (!job) {
    return jsonResponse({ error: "任务不存在。" }, 404);
  }

  const outTradeNo = sanitizeText(url.searchParams.get("outTradeNo"), 120);
  let orderStatus = null;
  if (outTradeNo) {
    const order = await env.DB.prepare(
      "SELECT status FROM payment_orders WHERE out_trade_no = ? AND user_id = ?"
    )
      .bind(outTradeNo, job.user_id)
      .first();
    orderStatus = order ? order.status : null;
  }

  const fresh = await env.DB.prepare("SELECT unlocked_at FROM jobs WHERE id = ?")
    .bind(jobId)
    .first();

  return jsonResponse({
    unlockedAt: fresh ? fresh.unlocked_at : null,
    orderStatus
  });
}

async function handleRegenerateJob(request, env, pathname, ctx) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const jobId = pathname.replace("/api/jobs/", "").replace("/regenerate", "");
  const job = await env.DB.prepare(
    "SELECT id, user_id, free_regenerations_remaining FROM jobs WHERE id = ?"
  )
    .bind(jobId)
    .first();

  if (!job || job.user_id !== session.user.id) {
    return jsonResponse({ error: "任务不存在。" }, 404);
  }

  if (Number(job.free_regenerations_remaining || 0) <= 0) {
    return jsonResponse({ error: "免费重绘次数已用完。请重新创建生成任务并扣除积分。" }, 402);
  }

  const now = Date.now();
  const decrement = await env.DB.prepare(
    `UPDATE jobs
     SET unlocked_at = NULL,
         free_regenerations_remaining = free_regenerations_remaining - 1,
         updated_at = ?,
         preview_ready_at = ?
     WHERE id = ? AND free_regenerations_remaining > 0`
  )
    .bind(now, now + 18000, jobId)
    .run();

  if (!decrement.meta || decrement.meta.changes !== 1) {
    return jsonResponse({ error: "免费重绘次数已用完。请重新创建生成任务并扣除积分。" }, 402);
  }

  await env.DB.prepare("DELETE FROM job_results WHERE job_id = ?")
    .bind(jobId)
    .run();

  const { results } = await env.DB.prepare(
    `SELECT upload_id
     FROM job_uploads
     WHERE job_id = ?
     ORDER BY sort_order ASC`
  )
    .bind(jobId)
    .all();

  const generationMode = resolveGenerationMode(env);
  if (generationMode.requiresExternalProvider && !generationMode.ready) {
    return jsonResponse(
      {
        error: "图像生成服务尚未配置，暂时不能重新生成。",
        requiredConfig: generationMode.requiredConfig
      },
      503
    );
  }

  await createInitialJobResults(env, {
    jobId,
    uploadIds: (results || []).map((row) => row.upload_id),
    mode: generationMode
  });

  if (generationMode.ready) {
    const task = processJobGeneration(env, jobId);
    if (ctx?.waitUntil) {
      ctx.waitUntil(task);
    } else {
      await task;
    }
  } else {
    await env.DB.prepare("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?")
      .bind("completed", Date.now(), jobId)
      .run();
  }

  return jsonResponse({
    success: true,
    freeRegenerationsRemaining: Number(job.free_regenerations_remaining || 0) - 1
  });
}

async function handleResultDownload(request, env, pathname, url) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const match = pathname.match(/^\/api\/jobs\/([^/]+)\/results\/([^/]+)\/download$/);
  if (!match) {
    return jsonResponse({ error: "下载地址不正确。" }, 400);
  }

  const [, jobId, resultId] = match;
  const variant = sanitizeText(url.searchParams.get("variant") || "jpg", 40);
  if (!["jpg", "original"].includes(variant)) {
    return jsonResponse(
      {
        error: "该导出格式尚未接入。",
        code: "EXPORT_PROVIDER_REQUIRED",
        requiredConfig: variant === "background" ? ["BACKGROUND_REMOVAL_PROVIDER"] : ["MOTION_EXPORT_PROVIDER"],
        nextStep: "请接入对应的图像处理服务后启用此格式。"
      },
      501
    );
  }

  const result = await env.DB.prepare(
    `SELECT
       job_results.id,
       job_results.job_id,
       job_results.label,
       job_results.object_key,
       job_results.mime_type,
       job_results.file_name,
       job_results.source_upload_id,
       jobs.user_id
     FROM job_results
     JOIN jobs ON jobs.id = job_results.job_id
     WHERE job_results.id = ?
       AND job_results.job_id = ?`
  )
    .bind(resultId, jobId)
    .first();

  if (!result || (result.user_id !== session.user.id && session.user.role !== "admin")) {
    return jsonResponse({ error: "结果不存在或无权下载。" }, 404);
  }

  let object = null;
  let fileName = result.file_name || `${result.label || "auralis-result"}.jpg`;
  let mimeType = result.mime_type || "image/jpeg";

  if (result.object_key) {
    object = await env.UPLOADS.get(result.object_key);
  } else if (result.source_upload_id) {
    const source = await env.DB.prepare(
      `SELECT object_key, file_name, mime_type
       FROM uploads
       WHERE id = ?`
    )
      .bind(result.source_upload_id)
      .first();

    if (source) {
      object = await env.UPLOADS.get(source.object_key);
      fileName = source.file_name || fileName;
      mimeType = source.mime_type || mimeType;
    }
  }

  if (!object) {
    return jsonResponse({ error: "结果文件不存在，请重新生成或联系人工支持。" }, 404);
  }

  const headers = new Headers();
  object.writeHttpMetadata(headers);
  headers.set("Content-Type", mimeType || headers.get("Content-Type") || "application/octet-stream");
  headers.set("Content-Disposition", `attachment; filename="${encodeRFC5987(fileName)}"`);
  headers.set("Cache-Control", "private, no-store, max-age=0");

  return new Response(object.body, {
    status: 200,
    headers
  });
}

async function handleJobArchiveDownload(request, env, pathname, url) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const jobId = pathname.replace("/api/jobs/", "").replace("/download", "");
  const format = sanitizeText(url.searchParams.get("format") || "zip", 20);
  const job = await env.DB.prepare("SELECT id, user_id FROM jobs WHERE id = ?")
    .bind(jobId)
    .first();

  if (!job || (job.user_id !== session.user.id && session.user.role !== "admin")) {
    return jsonResponse({ error: "任务不存在或无权下载。" }, 404);
  }

  if (format !== "zip") {
    return jsonResponse({ error: "暂不支持该打包格式。" }, 400);
  }

  return jsonResponse(
    {
      error: "批量 ZIP 打包服务尚未接入。",
      code: "ZIP_EXPORT_PROVIDER_REQUIRED",
      requiredConfig: ["ZIP_EXPORT_WORKER_OR_QUEUE"],
      nextStep: "需要接入打包 Worker、队列或对象存储临时文件后启用。"
    },
    501
  );
}

async function handleWeChatPrepay(request, env) {
  if (isFreeTrialMode(env)) {
    return jsonResponse({ error: "当前为免费试运行模式，暂不开放充值。" }, 403);
  }
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const payload = await readJson(request);
  // 金额与积分只能由服务端套餐表决定，绝不信任客户端传入的数值。
  const requestedPackage = BILLING_PACKAGES.find((pkg) => pkg.id === String(payload?.packageId || ""));
  if (!requestedPackage) {
    return jsonResponse({ error: "套餐不存在，请刷新页面后重新选择。" }, 400);
  }
  const credits = requestedPackage.credits;
  const amountCents = requestedPackage.amountCents;
  const paymentMode = resolvePaymentMode(env);
  if (!paymentMode.ready && !paymentMode.mockEnabled) {
    return jsonResponse(
      {
        error: "微信支付生产参数尚未配置，不能创建支付订单。",
        requiredConfig: paymentMode.requiredConfig,
        nextStep: "请配置微信商户参数后再启用线上充值。"
      },
      503
    );
  }

  const orderId = randomId("pay");
  const outTradeNo = `${orderId}_${Date.now()}`;
  const now = Date.now();

  await env.DB.prepare(
    `INSERT INTO payment_orders
       (id, user_id, provider, status, amount_cents, credits, out_trade_no, code_url, created_at, updated_at)
     VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(orderId, session.user.id, "wechat_native", "pending", amountCents, credits, outTradeNo, "", now, now)
    .run();

  let payment = null;
  if (paymentMode.ready) {
    payment = await createWeChatNativePrepay(env, {
      outTradeNo,
      amountCents,
      description: `${env.PUBLIC_SITE_NAME || env.APP_NAME || "Auralis"} ${credits}积分充值`,
      notifyUrl: env.WECHATPAY_NOTIFY_URL || `${new URL(request.url).origin}/api/payments/wechat/notify`
    });

    await env.DB.prepare("UPDATE payment_orders SET code_url = ?, raw_response = ?, updated_at = ? WHERE id = ?")
      .bind(payment.codeUrl || "", JSON.stringify(payment.raw || {}), Date.now(), orderId)
      .run();
  } else {
    payment = {
      codeUrl: `weixin://wxpay/mock/${encodeURIComponent(orderId)}`,
      raw: { mode: "mock", reason: "WECHATPAY_* env vars are not configured" }
    };

    await env.DB.prepare("UPDATE payment_orders SET code_url = ?, raw_response = ?, updated_at = ? WHERE id = ?")
      .bind(payment.codeUrl, JSON.stringify(payment.raw), Date.now(), orderId)
      .run();
  }

  return jsonResponse({
    orderId,
    provider: "wechat_native",
    status: "pending",
    amountCents,
    credits,
    codeUrl: payment.codeUrl,
    mock: !paymentMode.ready,
    message: paymentMode.ready
      ? "微信支付订单已创建，请扫码支付。"
      : "本地未配置微信支付商户密钥，当前使用模拟支付完成产品流程。"
  });
}

async function handlePaymentStatus(request, env, pathname) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  const orderId = pathname.replace("/api/payments/", "");
  const order = await env.DB.prepare(
    `SELECT id, user_id, status, amount_cents, credits, code_url, paid_at, created_at
     FROM payment_orders
     WHERE id = ?`
  )
    .bind(orderId)
    .first();

  if (!order || (order.user_id !== session.user.id && session.user.role !== "admin")) {
    return jsonResponse({ error: "支付订单不存在。" }, 404);
  }

  return jsonResponse({
    order: {
      id: order.id,
      status: order.status,
      amountCents: Number(order.amount_cents || 0),
      credits: Number(order.credits || 0),
      codeUrl: order.code_url,
      paidAt: order.paid_at,
      createdAt: order.created_at
    }
  });
}

async function handleMockPaymentConfirm(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  if (isProductionRuntime(env)) {
    return jsonResponse({ error: "生产环境不能使用模拟支付确认。" }, 403);
  }

  if (isWeChatPayConfigured(env) && env.ENABLE_PAYMENT_MOCK !== "true") {
    return jsonResponse({ error: "支付配置已启用，不能使用模拟支付确认。" }, 403);
  }

  const payload = await readJson(request);
  const orderId = sanitizeText(payload?.orderId, 80);
  if (!orderId) {
    return jsonResponse({ error: "缺少支付订单号。" }, 400);
  }

  const result = await markPaymentOrderPaid(env, orderId, session.user.id);
  if (!result.ok) {
    return jsonResponse({ error: result.error }, result.status || 400);
  }

  return jsonResponse({
    success: true,
    order: result.order,
    user: result.user
  });
}

async function handleWeChatPaymentNotify(request, env) {
  if (!isWeChatPayConfigured(env)) {
    return jsonResponse({ code: "FAIL", message: "wechat pay is not configured" }, 503);
  }

  // 读取原始报文用于验签（解析成 JSON 会丢失字节级一致性）。
  const rawBody = await request.text();

  // 验证微信平台证书签名：配置了公钥才校验，防止伪造的支付成功回调。
  const signatureOk = await verifyWeChatNotifySignature(env, request, rawBody);
  if (!signatureOk) {
    console.error("wechat notify signature verification failed");
    return jsonResponse({ code: "FAIL", message: "signature verification failed" }, 401);
  }

  let payload = {};
  try {
    payload = rawBody ? JSON.parse(rawBody) : {};
  } catch (_error) {
    return jsonResponse({ code: "FAIL", message: "invalid body" }, 400);
  }

  const transaction = await decryptWeChatNotifyResource(env, payload?.resource);
  const outTradeNo = sanitizeText(transaction?.out_trade_no, 120);
  const tradeState = sanitizeText(transaction?.trade_state, 40);

  if (!outTradeNo || tradeState !== "SUCCESS") {
    return jsonResponse({ code: "SUCCESS", message: "notify accepted" });
  }

  const order = await env.DB.prepare("SELECT id, amount_cents FROM payment_orders WHERE out_trade_no = ?")
    .bind(outTradeNo)
    .first();
  if (order) {
    // 金额必须与下单时服务端写入的金额一致，杜绝被篡改的回调。
    const notifiedTotal = Number(transaction?.amount?.total);
    if (Number.isFinite(notifiedTotal) && notifiedTotal !== Number(order.amount_cents)) {
      console.error("wechat notify amount mismatch", outTradeNo, notifiedTotal, order.amount_cents);
      return jsonResponse({ code: "FAIL", message: "amount mismatch" }, 400);
    }
    await markPaymentOrderPaid(env, order.id);
  }

  return jsonResponse({ code: "SUCCESS", message: "成功" });
}

// 验证微信支付 v3 回调签名。需配置 WECHATPAY_PLATFORM_PUBLIC_KEY（平台证书公钥 PEM）。
// 未配置公钥时返回 false（拒绝），避免在缺少验签的情况下放行伪造回调。
async function verifyWeChatNotifySignature(env, request, rawBody) {
  const publicKeyPem = env.WECHATPAY_PLATFORM_PUBLIC_KEY;
  if (!publicKeyPem) {
    return false;
  }
  const timestamp = request.headers.get("Wechatpay-Timestamp") || "";
  const nonce = request.headers.get("Wechatpay-Nonce") || "";
  const signature = request.headers.get("Wechatpay-Signature") || "";
  if (!timestamp || !nonce || !signature) {
    return false;
  }
  // 防重放：拒绝时间戳偏差超过 5 分钟的回调。
  const skewSeconds = Math.abs(Date.now() / 1000 - Number(timestamp));
  if (!Number.isFinite(skewSeconds) || skewSeconds > 300) {
    return false;
  }
  try {
    const key = await crypto.subtle.importKey(
      "spki",
      pemPublicKeyToArrayBuffer(publicKeyPem),
      { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
      false,
      ["verify"]
    );
    const message = `${timestamp}\n${nonce}\n${rawBody}\n`;
    return await crypto.subtle.verify(
      "RSASSA-PKCS1-v1_5",
      key,
      base64ToArrayBuffer(signature),
      encoder.encode(message)
    );
  } catch (error) {
    console.error("wechat notify signature verify error", error);
    return false;
  }
}

async function handleChatMessageSend(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }
  const payload = await readJson(request);
  const content = sanitizeText(payload?.content, 500);
  const incomingUserId = sanitizeText(payload?.userId, 120);
  const isAdmin = payload?.isAdmin ? 1 : 0;

  if (!content) {
    return jsonResponse({ error: "消息不能为空。" }, 400);
  }

  const userId = session.user.role === "admin" && incomingUserId ? incomingUserId : session.user.id;
  const effectiveAdminFlag = session.user.role === "admin" && isAdmin ? 1 : 0;

  await env.DB.prepare(
    `INSERT INTO chat_messages (id, user_id, is_admin, content, created_at)
     VALUES (?, ?, ?, ?, ?)`
  )
    .bind(randomId("msg"), userId, effectiveAdminFlag, content, Date.now())
    .run();

  return jsonResponse({ success: true });
}

async function handleChatMessages(request, env, url) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }
  const requestedUserId = sanitizeText(url.searchParams.get("userId"), 120);
  const userId =
    session.user.role === "admin"
      ? requestedUserId || session.user.id
      : session.user.id;

  if (!userId) {
    return jsonResponse({ messages: [] });
  }

  const { results } = await env.DB.prepare(
    `SELECT id, user_id, is_admin, content, created_at
     FROM chat_messages
     WHERE user_id = ?
     ORDER BY created_at ASC`
  )
    .bind(userId)
    .all();

  return jsonResponse({
    messages: (results || []).map((row) => ({
      id: row.id,
      userId: row.user_id,
      isAdmin: Boolean(row.is_admin),
      content: row.content,
      timestamp: row.created_at
    }))
  });
}

async function handleChatUsers(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  if (session.user.role !== "admin") {
    return jsonResponse({ error: "无权访问。" }, 403);
  }

  const { results } = await env.DB.prepare(
    `SELECT
       chat_messages.user_id,
       users.name,
       users.email,
       MAX(chat_messages.created_at) AS last_message_at,
       SUM(CASE WHEN chat_messages.is_admin = 0 THEN 1 ELSE 0 END) AS customer_message_count
     FROM chat_messages
     LEFT JOIN users ON users.id = chat_messages.user_id
     GROUP BY chat_messages.user_id, users.name, users.email
     ORDER BY last_message_at DESC`
  ).all();

  return jsonResponse({
    users: (results || []).map((row) => ({
      id: row.user_id,
      name: row.name || "访客用户",
      email: row.email || "",
      lastMessageAt: row.last_message_at,
      customerMessageCount: Number(row.customer_message_count || 0)
    }))
  });
}

async function handleAdminSummary(request, env) {
  const session = await requireSession(request, env);
  if (!session.ok) {
    return session.response;
  }

  if (session.user.role !== "admin") {
    return jsonResponse({ error: "无权访问。" }, 403);
  }

  const [usersResponse, jobsResponse] = await Promise.all([
    env.DB.prepare(
      `SELECT id, name, email, created_at
       FROM users
       ORDER BY created_at DESC
       LIMIT 200`
    ).all(),
    env.DB.prepare(
      `SELECT id, created_at, unlocked_at, result_count, package_tier
       FROM jobs
       ORDER BY created_at DESC
       LIMIT 200`
    ).all()
  ]);

  const users = usersResponse.results || [];
  const jobs = jobsResponse.results || [];
  const unlockedJobs = jobs.filter((job) => job.unlocked_at);

  return jsonResponse({
    metrics: {
      users: users.length,
      images: jobs.reduce((sum, job) => sum + Number(job.result_count || 0), 0),
      revenue: (unlockedJobs.length * 9.9).toFixed(1)
    },
    users: users.map((user) => ({
      id: user.id,
      name: user.name,
      email: user.email,
      createdAt: user.created_at
    })),
    jobs: jobs.map((job) => ({
      id: job.id,
      createdAt: job.created_at,
      resultCount: job.result_count,
      packageTier: job.package_tier,
      unlocked: Boolean(job.unlocked_at)
    }))
  });
}

async function getSessionFromRequest(request, env) {
  const cookies = parseCookies(request.headers.get("Cookie"));
  const token = cookies[getSessionCookieName(env)];

  if (!token) {
    return null;
  }

  const tokenHash = await sha256Hex(token);
  const now = Date.now();
  const session = await env.DB.prepare(
    `SELECT
        sessions.id AS session_id,
        sessions.expires_at,
        users.id,
        users.name,
        users.email,
        users.role,
        users.credits
      FROM sessions
      JOIN users ON users.id = sessions.user_id
      WHERE sessions.token_hash = ?
        AND sessions.expires_at > ?`
  )
    .bind(tokenHash, now)
    .first();

  if (!session) {
    return null;
  }

  return {
    id: session.session_id,
    user: {
      id: session.id,
      name: session.name,
      email: session.email,
      role: session.role,
      credits: Number(session.credits || 0)
    }
  };
}

function getClientIp(request) {
  return sanitizeText(request.headers.get("CF-Connecting-IP"), 120) || "unknown";
}

// 滑动固定窗口限流：同一 bucket 在 windowMs 内最多 limit 次。
// 失败开放（DB 不可用时不拦截），避免限流层把正常登录也挡死。
async function enforceRateLimit(env, bucket, limit, windowMs) {
  if (!env.DB) {
    return { allowed: true };
  }
  const now = Date.now();
  try {
    const row = await env.DB.prepare("SELECT count, window_start FROM rate_limits WHERE bucket = ?")
      .bind(bucket)
      .first();
    if (!row || now - Number(row.window_start) > windowMs) {
      await env.DB.prepare(
        `INSERT INTO rate_limits (bucket, count, window_start) VALUES (?, 1, ?)
         ON CONFLICT(bucket) DO UPDATE SET count = 1, window_start = excluded.window_start`
      )
        .bind(bucket, now)
        .run();
      return { allowed: true };
    }
    if (Number(row.count) >= limit) {
      const retryAfter = Math.max(1, Math.ceil((windowMs - (now - Number(row.window_start))) / 1000));
      return { allowed: false, retryAfter };
    }
    await env.DB.prepare("UPDATE rate_limits SET count = count + 1 WHERE bucket = ?").bind(bucket).run();
    return { allowed: true };
  } catch (error) {
    console.error("rate limit check failed", error);
    return { allowed: true };
  }
}

function rateLimitedResponse(retryAfter) {
  return jsonResponse(
    { error: "操作过于频繁，请稍后再试。" },
    429,
    { "Retry-After": String(retryAfter || 60) }
  );
}

async function requireSession(request, env) {
  const session = await getSessionFromRequest(request, env);
  if (!session) {
    return {
      ok: false,
      response: jsonResponse({ error: "请先登录后继续操作。" }, 401)
    };
  }

  return {
    ok: true,
    user: session.user
  };
}

async function createSessionResponse(env, request, user) {
  const sessionId = randomId("ses");
  const token = randomHex(32);
  const tokenHash = await sha256Hex(token);
  const now = Date.now();
  const expiresAt = now + SESSION_DURATION_SECONDS * 1000;

  await env.DB.prepare(
    `INSERT INTO sessions (id, user_id, token_hash, created_at, expires_at, user_agent, ip_address)
     VALUES (?, ?, ?, ?, ?, ?, ?)`
  )
    .bind(
      sessionId,
      user.id,
      tokenHash,
      now,
      expiresAt,
      sanitizeText(request.headers.get("User-Agent"), 200),
      sanitizeText(request.headers.get("CF-Connecting-IP"), 120)
    )
    .run();

  return jsonResponse(
    {
      success: true,
      authenticated: true,
      user: {
        id: user.id,
        email: user.email,
        name: user.name,
        role: user.role,
        credits: Number(user.credits || 0)
      }
    },
    200,
    {
      "Set-Cookie": buildSessionCookie(getSessionCookieName(env), token, request)
    }
  );
}

async function ensureBillingSchema(env) {
  if (billingSchemaReady || !env.DB) {
    return;
  }

  const statements = [
    `ALTER TABLE users ADD COLUMN credits INTEGER NOT NULL DEFAULT ${DEFAULT_INITIAL_CREDITS}`,
    "ALTER TABLE jobs ADD COLUMN charged_at INTEGER",
    `ALTER TABLE jobs ADD COLUMN generation_cost_credits INTEGER NOT NULL DEFAULT ${DEFAULT_GENERATION_COST_CREDITS}`,
    `ALTER TABLE jobs ADD COLUMN free_regenerations_remaining INTEGER NOT NULL DEFAULT ${DEFAULT_FREE_REGENERATIONS}`,
    `CREATE TABLE IF NOT EXISTS payment_orders (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      provider TEXT NOT NULL,
      status TEXT NOT NULL,
      amount_cents INTEGER NOT NULL,
      credits INTEGER NOT NULL,
      out_trade_no TEXT NOT NULL UNIQUE,
      code_url TEXT,
      transaction_id TEXT,
      raw_response TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      paid_at INTEGER,
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )`,
    "ALTER TABLE payment_orders ADD COLUMN job_id TEXT",
    `CREATE TABLE IF NOT EXISTS password_resets (
      id TEXT PRIMARY KEY,
      user_id TEXT NOT NULL,
      token_hash TEXT NOT NULL,
      expires_at INTEGER NOT NULL,
      used_at INTEGER,
      created_at INTEGER NOT NULL,
      FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
    )`,
    "CREATE INDEX IF NOT EXISTS idx_password_resets_token ON password_resets(token_hash)",
    `CREATE TABLE IF NOT EXISTS rate_limits (
      bucket TEXT PRIMARY KEY,
      count INTEGER NOT NULL,
      window_start INTEGER NOT NULL
    )`,
    "CREATE INDEX IF NOT EXISTS idx_payment_orders_user_id ON payment_orders(user_id, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_payment_orders_out_trade_no ON payment_orders(out_trade_no)",
    `CREATE TABLE IF NOT EXISTS job_results (
      id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      label TEXT NOT NULL,
      provider TEXT NOT NULL,
      status TEXT NOT NULL,
      object_key TEXT,
      source_upload_id TEXT,
      image_url TEXT,
      mime_type TEXT,
      file_name TEXT,
      quality_score INTEGER,
      diagnostic_message TEXT,
      created_at INTEGER NOT NULL,
      updated_at INTEGER NOT NULL,
      FOREIGN KEY (job_id) REFERENCES jobs(id) ON DELETE CASCADE,
      FOREIGN KEY (source_upload_id) REFERENCES uploads(id) ON DELETE SET NULL
    )`,
    "CREATE INDEX IF NOT EXISTS idx_job_results_job_id ON job_results(job_id, created_at)"
  ];

  for (const sql of statements) {
    try {
      await env.DB.prepare(sql).run();
    } catch (error) {
      const message = String(error?.message || error);
      if (!message.includes("duplicate column name")) {
        throw error;
      }
    }
  }

  await env.DB.prepare(
    `UPDATE jobs
     SET charged_at = COALESCE(charged_at, created_at),
         generation_cost_credits = COALESCE(generation_cost_credits, ?),
         free_regenerations_remaining = COALESCE(free_regenerations_remaining, ?)
     WHERE charged_at IS NULL
        OR generation_cost_credits IS NULL
        OR free_regenerations_remaining IS NULL`
  )
    .bind(DEFAULT_GENERATION_COST_CREDITS, DEFAULT_FREE_REGENERATIONS)
    .run();

  billingSchemaReady = true;
}

function getGenerationCostCredits(env) {
  const configuredValue = Number(env.GENERATION_COST_CREDITS);
  return Number.isFinite(configuredValue) && configuredValue >= 0
    ? Math.floor(configuredValue)
    : DEFAULT_GENERATION_COST_CREDITS;
}

function getInitialCredits(env) {
  const configuredValue = Number(env.INITIAL_CREDITS);
  if (Number.isFinite(configuredValue) && configuredValue >= 0) {
    return Math.floor(configuredValue);
  }
  return isProductionRuntime(env) ? DEFAULT_INITIAL_CREDITS : DEFAULT_LOCAL_INITIAL_CREDITS;
}

function isFreeTrialMode(env) {
  return String(env.FREE_TRIAL_MODE || "").toLowerCase() === "true" || getGenerationCostCredits(env) === 0;
}

function normalisePositiveInteger(value, fallback, min, max) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return fallback;
  }
  return Math.max(min, Math.min(max, Math.floor(number)));
}

function isWeChatPayConfigured(env) {
  return Boolean(
    env.WECHATPAY_APP_ID &&
      env.WECHATPAY_MCH_ID &&
      env.WECHATPAY_API_V3_KEY &&
      env.WECHATPAY_PRIVATE_KEY &&
      env.WECHATPAY_CERT_SERIAL_NO
  );
}

function resolvePaymentMode(env) {
  const ready = isWeChatPayConfigured(env);
  const production = isProductionRuntime(env);
  return {
    provider: "wechat_native",
    ready,
    mockEnabled: !production && !ready,
    requiredConfig: [
      "WECHATPAY_APP_ID",
      "WECHATPAY_MCH_ID",
      "WECHATPAY_API_V3_KEY",
      "WECHATPAY_PRIVATE_KEY",
      "WECHATPAY_CERT_SERIAL_NO",
      "WECHATPAY_NOTIFY_URL"
    ]
  };
}

async function decryptWeChatNotifyResource(env, resource) {
  if (!resource?.ciphertext || !resource?.nonce) {
    return null;
  }

  const key = await crypto.subtle.importKey(
    "raw",
    encoder.encode(env.WECHATPAY_API_V3_KEY),
    "AES-GCM",
    false,
    ["decrypt"]
  );
  const additionalData = resource.associated_data ? encoder.encode(resource.associated_data) : undefined;
  const decrypted = await crypto.subtle.decrypt(
    {
      name: "AES-GCM",
      iv: encoder.encode(resource.nonce),
      additionalData
    },
    key,
    base64ToArrayBuffer(resource.ciphertext)
  );
  return JSON.parse(new TextDecoder().decode(decrypted));
}

async function createWeChatNativePrepay(env, order) {
  const body = {
    appid: env.WECHATPAY_APP_ID,
    mchid: env.WECHATPAY_MCH_ID,
    description: order.description,
    out_trade_no: order.outTradeNo,
    notify_url: order.notifyUrl,
    amount: {
      total: order.amountCents,
      currency: "CNY"
    }
  };
  const url = "https://api.mch.weixin.qq.com/v3/pay/transactions/native";
  const bodyText = JSON.stringify(body);
  const authorization = await buildWeChatAuthorizationHeader(env, "POST", "/v3/pay/transactions/native", bodyText);
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Authorization": authorization,
      "Accept": "application/json",
      "Content-Type": "application/json",
      "User-Agent": env.APP_NAME || "Auralis"
    },
    body: bodyText
  });
  const responseText = await response.text();
  let payload = {};
  try {
    payload = responseText ? JSON.parse(responseText) : {};
  } catch (_error) {
    payload = { raw: responseText };
  }

  if (!response.ok || !payload.code_url) {
    console.error("WeChat Pay prepay failed", response.status, payload);
    throw new Error("微信支付下单失败，请检查商户参数与证书配置。");
  }

  return {
    codeUrl: payload.code_url,
    raw: payload
  };
}

async function buildWeChatAuthorizationHeader(env, method, canonicalUrl, bodyText) {
  const timestamp = String(Math.floor(Date.now() / 1000));
  const nonce = randomHex(16);
  const message = `${method}\n${canonicalUrl}\n${timestamp}\n${nonce}\n${bodyText}\n`;
  const signature = await signWechatMessage(env.WECHATPAY_PRIVATE_KEY, message);
  const params = [
    `mchid="${env.WECHATPAY_MCH_ID}"`,
    `nonce_str="${nonce}"`,
    `timestamp="${timestamp}"`,
    `serial_no="${env.WECHATPAY_CERT_SERIAL_NO}"`,
    `signature="${signature}"`
  ];
  return `WECHATPAY2-SHA256-RSA2048 ${params.join(",")}`;
}

async function signWechatMessage(privateKeyPem, message) {
  const key = await crypto.subtle.importKey(
    "pkcs8",
    pemToArrayBuffer(privateKeyPem),
    { name: "RSASSA-PKCS1-v1_5", hash: "SHA-256" },
    false,
    ["sign"]
  );
  const signature = await crypto.subtle.sign("RSASSA-PKCS1-v1_5", key, encoder.encode(message));
  return arrayBufferToBase64(signature);
}

function pemToArrayBuffer(pem) {
  const base64 = String(pem)
    .replace(/-----BEGIN PRIVATE KEY-----/g, "")
    .replace(/-----END PRIVATE KEY-----/g, "")
    .replace(/\\n/g, "")
    .replace(/\s+/g, "");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function pemPublicKeyToArrayBuffer(pem) {
  const base64 = String(pem)
    .replace(/-----BEGIN PUBLIC KEY-----/g, "")
    .replace(/-----END PUBLIC KEY-----/g, "")
    .replace(/\\n/g, "")
    .replace(/\s+/g, "");
  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function arrayBufferToBase64(buffer) {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) {
    binary += String.fromCharCode(byte);
  }
  return btoa(binary);
}

function base64ToArrayBuffer(base64) {
  const binary = atob(String(base64 || ""));
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
}

function base64ToUint8Array(base64) {
  return new Uint8Array(base64ToArrayBuffer(base64));
}

async function markPaymentOrderPaid(env, orderId, expectedUserId) {
  const order = await env.DB.prepare(
    `SELECT id, user_id, status, amount_cents, credits, job_id
     FROM payment_orders
     WHERE id = ?`
  )
    .bind(orderId)
    .first();

  if (!order) {
    return { ok: false, status: 404, error: "支付订单不存在。" };
  }

  if (expectedUserId && order.user_id !== expectedUserId) {
    return { ok: false, status: 403, error: "无权确认该支付订单。" };
  }

  if (order.status !== "paid") {
    const now = Date.now();
    const upd = await env.DB.prepare(
      "UPDATE payment_orders SET status = 'paid', paid_at = ?, updated_at = ? WHERE id = ? AND status != 'paid'"
    )
      .bind(now, now, order.id)
      .run();
    if (upd.meta && upd.meta.changes === 1) {
      await env.DB.prepare("UPDATE users SET credits = credits + ? WHERE id = ?")
        .bind(Number(order.credits || 0), order.user_id)
        .run();
      // 结果页解锁订单：支付成功后同步解锁对应任务的高清下载。
      if (order.job_id) {
        await env.DB.prepare("UPDATE jobs SET unlocked_at = ?, updated_at = ? WHERE id = ? AND unlocked_at IS NULL")
          .bind(now, now, order.job_id)
          .run();
      }
    }
  }

  const user = await env.DB.prepare("SELECT id, name, email, role, credits FROM users WHERE id = ?")
    .bind(order.user_id)
    .first();

  return {
    ok: true,
    order: {
      id: order.id,
      status: "paid",
      amountCents: Number(order.amount_cents || 0),
      credits: Number(order.credits || 0)
    },
    user: user
      ? {
          id: user.id,
          name: user.name,
          email: user.email,
          role: user.role,
          credits: Number(user.credits || 0)
        }
      : null
  };
}

function buildRobotsResponse(url) {
  const lines = [
    "User-agent: *",
    "Allow: /",
    "Disallow: /login.html",
    "Disallow: /upload.html",
    "Disallow: /style.html",
    "Disallow: /checkout.html",
    "Disallow: /generating.html",
    "Disallow: /result.html",
    "Disallow: /dashboard.html",
    "Disallow: /dashboard_assets.html",
    "Disallow: /dashboard_settings.html",
    "Disallow: /dashboard_privacy.html",
    "Disallow: /admin.html",
    `Sitemap: ${url.origin}/sitemap.xml`
  ];

  return new Response(lines.join("\n"), {
    headers: {
      "Content-Type": "text/plain; charset=utf-8"
    }
  });
}

function buildSitemapResponse(url) {
  const now = new Date().toISOString();
  const urls = PUBLIC_SITEMAP_PATHS.map((path) => {
    const loc = `${url.origin}${path === "/" ? "" : path}`;
    return `<url><loc>${escapeXml(loc)}</loc><lastmod>${now}</lastmod></url>`;
  }).join("");

  return new Response(
    `<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">${urls}</urlset>`,
    {
      headers: {
        "Content-Type": "application/xml; charset=utf-8"
      }
    }
  );
}

function withHeaders(response, request) {
  const headers = new Headers(response.headers);
  const contentType = headers.get("Content-Type") || "";
  const url = new URL(request.url);

  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  headers.set("Permissions-Policy", "camera=(self), microphone=(), geolocation=()");
  headers.set("X-Frame-Options", "DENY");
  const cspDirectives = [
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com",
    "img-src 'self' data: blob: https:",
    "font-src 'self' https://fonts.gstatic.com",
    "connect-src 'self'",
    "media-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "object-src 'none'"
  ];
  if (url.protocol === "https:") {
    cspDirectives.push("upgrade-insecure-requests");
  }
  headers.set(
    "Content-Security-Policy",
    cspDirectives.join("; ")
  );

  if (contentType.includes("text/html")) {
    headers.set("Link", `<${url.toString()}>; rel="canonical"`);
    headers.set("X-Robots-Tag", isPrivatePage(url.pathname) ? "noindex, nofollow" : "index, follow");
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers
  });
}

function resolveRequestedResultLabels(styleConfig) {
  const requestedStyles = Array.isArray(styleConfig?.generationStyles)
    ? styleConfig.generationStyles.map((value) => String(value || "").trim())
    : [];
  const singleStyle = String(styleConfig?.singleStyle || "").trim();
  if (singleStyle) {
    requestedStyles.push(singleStyle);
  }
  if (requestedStyles.includes("celebrity_headshot_square")) {
    return [CELEBRITY_HEADSHOT_LABEL];
  }
  if (requestedStyles.includes("studio_business_portrait_3x4")) {
    return [String(styleConfig?.templateName || "工作室职业肖像照").trim() || "工作室职业肖像照"];
  }
  return [String(styleConfig?.templateName || "正式商务档案头像").trim() || "正式商务档案头像"];
}

async function createInitialJobResults(env, { jobId, uploadIds, mode, labels }) {
  const now = Date.now();
  const sourceUploadIds = uploadIds.length > 0 ? uploadIds : [null];
  const provider = mode.ready ? mode.provider : "local_reference";
  const status = mode.ready ? "queued" : "reference_only";
  const resultLabels = Array.isArray(labels) && labels.length ? labels : RESULT_LABELS.slice(0, DEFAULT_RESULT_COUNT);
  const diagnosticMessage = mode.ready
    ? "已进入图像生成队列，等待生成服务回写结果。"
    : "当前环境未配置图像生成服务，先展示可下载的原始上传参考图。";

  for (let index = 0; index < resultLabels.length; index += 1) {
    const sourceUploadId = sourceUploadIds[index % sourceUploadIds.length];
    await env.DB.prepare(
      `INSERT INTO job_results (
        id, job_id, label, provider, status, object_key, source_upload_id,
        image_url, mime_type, file_name, quality_score, diagnostic_message,
        created_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`
    )
      .bind(
        `${jobId}_res_${index + 1}`,
        jobId,
        resultLabels[index % resultLabels.length],
        provider,
        status,
        null,
        sourceUploadId,
        null,
        "image/jpeg",
        `${jobId}_${index + 1}.jpg`,
        mode.ready ? null : 0,
        diagnosticMessage,
        now,
        now
      )
      .run();
  }
}

async function loadJobResultItems(env, jobId, uploads) {
  const { results } = await env.DB.prepare(
    `SELECT id, label, provider, status, object_key, source_upload_id, image_url,
            mime_type, file_name, quality_score, diagnostic_message, created_at, updated_at
     FROM job_results
     WHERE job_id = ?
     ORDER BY created_at ASC, id ASC`
  )
    .bind(jobId)
    .all();

  if (!results || results.length === 0) {
    return buildLegacyResultItems(jobId, uploads);
  }

  const uploadMap = new Map(uploads.map((upload) => [upload.id, upload]));
  return results.map((result) => {
    const sourceUpload = uploadMap.get(result.source_upload_id);
    const isReferenceOnly = result.status === "reference_only";
    const resultVersion = encodeURIComponent(String(result.updated_at || result.created_at || Date.now()));
    const generatedUrl = result.object_key ? `/api/jobs/${jobId}/results/${result.id}/download?v=${resultVersion}` : null;
    const imageUrl = result.image_url || generatedUrl || (isReferenceOnly ? sourceUpload?.previewUrl || null : null);
    const canDownload = Boolean(result.object_key || (isReferenceOnly && result.source_upload_id));
    const downloadBaseUrl = result.object_key
      ? `/api/jobs/${jobId}/results/${result.id}/download?v=${resultVersion}`
      : isReferenceOnly
        ? `/api/jobs/${jobId}/results/${result.id}/download?variant=jpg&v=${resultVersion}`
        : null;

    return {
      id: result.id,
      label: result.label,
      provider: result.provider,
      status: result.status,
      imageUrl,
      sourceImageUrl: sourceUpload?.previewUrl || null,
      qualityScore: result.quality_score,
      message: result.diagnostic_message,
      referenceOnly: isReferenceOnly,
      canDownload,
      downloadUrl: downloadBaseUrl,
      exportUrls: {
        jpg: downloadBaseUrl,
        background: result.object_key ? `/api/jobs/${jobId}/results/${result.id}/download?variant=background&v=${resultVersion}` : null,
        motion: result.object_key ? `/api/jobs/${jobId}/results/${result.id}/download?variant=motion&v=${resultVersion}` : null
      }
    };
  });
}

async function processJobGeneration(env, jobId) {
  const job = await env.DB.prepare(
    `SELECT id, user_id, style_summary, generation_cost_credits, charged_at
     FROM jobs
     WHERE id = ?`
  )
    .bind(jobId)
    .first();

  if (!job) {
    return;
  }

  const { results: uploads } = await env.DB.prepare(
    `SELECT uploads.id, uploads.object_key, uploads.mime_type, uploads.file_name
     FROM job_uploads
     JOIN uploads ON uploads.id = job_uploads.upload_id
     WHERE job_uploads.job_id = ?
     ORDER BY job_uploads.sort_order ASC`
  )
    .bind(jobId)
    .all();

  const { results } = await env.DB.prepare(
    `SELECT id, label, source_upload_id
     FROM job_results
     WHERE job_id = ?
     ORDER BY created_at ASC, id ASC`
  )
    .bind(jobId)
    .all();

  if (!uploads?.length || !results?.length) {
    return;
  }

  const mode = resolveGenerationMode(env);

  try {
    await env.DB.prepare("UPDATE job_results SET status = ?, updated_at = ?, diagnostic_message = ? WHERE job_id = ?")
      .bind("generating", Date.now(), "正在调用 Seedream 图生一组图服务。", jobId)
      .run();

    const source = uploads[0];
    const sourceObject = await env.UPLOADS.get(source.object_key);
    if (!sourceObject) {
      throw new Error("源照片文件不存在。");
    }

    const generatedImages = await generateImageSetWithProvider(env, {
      mode,
      sourceBytes: await sourceObject.arrayBuffer(),
      sourceMimeType: source.mime_type || "image/jpeg",
      labels: results.map((result) => result.label),
      styleSummary: job.style_summary
    });

    for (let index = 0; index < results.length; index += 1) {
      const result = results[index];
      const generated = generatedImages[index];
      if (!generated) {
        await env.DB.prepare("UPDATE job_results SET status = ?, diagnostic_message = ?, updated_at = ? WHERE id = ?")
          .bind("failed", "图像生成服务返回图片数量不足，请重试。", Date.now(), result.id)
          .run();
        continue;
      }

      const objectKey = `${job.user_id}/${job.id}/${result.id}.${generated.extension}`;
      await env.UPLOADS.put(objectKey, generated.bytes, {
        httpMetadata: {
          contentType: generated.mimeType
        }
      });

      await env.DB.prepare(
        `UPDATE job_results
         SET status = ?, object_key = ?, mime_type = ?, file_name = ?,
             provider = ?, quality_score = ?, diagnostic_message = ?, updated_at = ?
         WHERE id = ?`
      )
        .bind(
          "ready",
          objectKey,
          generated.mimeType,
          `${result.label}.${generated.extension}`,
          mode.provider,
          generated.qualityScore,
          "Seedream 图生一组图完成，已写入私有对象存储。",
          Date.now(),
          result.id
        )
        .run();
    }

    await env.DB.prepare("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?")
      .bind("completed", Date.now(), jobId)
      .run();
  } catch (error) {
    console.error("Image generation failed", jobId, error);
    await env.DB.prepare("UPDATE job_results SET status = ?, diagnostic_message = ?, updated_at = ? WHERE job_id = ?")
      .bind("failed", sanitizeText(error?.message || "图像生成失败。", 300), Date.now(), jobId)
      .run();
    await env.DB.prepare("UPDATE jobs SET status = ?, updated_at = ? WHERE id = ?")
      .bind("failed", Date.now(), jobId)
      .run();

    // Refund the credits charged for this job, exactly once. We use charged_at as
    // the "refunded" sentinel: clearing it atomically prevents double refunds from
    // concurrent stale-generation retries.
    const refundCredits = Number(job.generation_cost_credits || 0);
    if (refundCredits > 0 && job.charged_at) {
      const cleared = await env.DB.prepare(
        "UPDATE jobs SET charged_at = NULL, updated_at = ? WHERE id = ? AND charged_at IS NOT NULL"
      )
        .bind(Date.now(), jobId)
        .run();
      if (cleared.meta && cleared.meta.changes === 1) {
        await env.DB.prepare("UPDATE users SET credits = credits + ? WHERE id = ?")
          .bind(refundCredits, job.user_id)
          .run();
      }
    }
  }
}

async function generateImageSetWithProvider(env, input) {
  if (input.mode.provider !== "doubao") {
    throw new Error("当前仅内置豆包/火山方舟图像生成适配器。");
  }

  return generateSeedreamImageSet(env, input);
}

async function generateSeedreamImageSet(env, input) {
  const apiKey = env.DOUBAO_API_KEY || env.ARK_API_KEY;
  const apiUrl = env.DOUBAO_API_URL || `${ARK_IMAGE_BASE_URL}${ARK_IMAGE_GENERATIONS_PATH}`;
  const model = resolveSeedreamImageModel(env);
  const imageBase64 = arrayBufferToBase64(input.sourceBytes);
  const imageValue = env.DOUBAO_INPUT_IMAGE_FORMAT === "raw_base64"
    ? imageBase64
    : `data:${input.sourceMimeType};base64,${imageBase64}`;

  const body = {
    model,
    prompt: buildHeadshotSetPrompt(input.labels, input.styleSummary),
    size: "2K",
    response_format: "b64_json",
    stream: true,
    image: imageValue,
    watermark: false,
    sequential_image_generation: "auto",
    sequential_image_generation_options: {
      max_images: Math.min(DEFAULT_RESULT_COUNT, Math.max(1, input.labels.length))
    }
  };

  const response = await fetch(apiUrl, {
    method: "POST",
    headers: {
      "Authorization": `Bearer ${apiKey}`,
      "Content-Type": "application/json",
      "Accept": "application/json"
    },
    signal: AbortSignal.timeout(IMAGE_GENERATION_TIMEOUT_MS),
    body: JSON.stringify(body)
  }).catch((error) => {
    if (error?.name === "TimeoutError" || error?.name === "AbortError") {
      throw new Error("图像生成服务响应超时，请返回后重新生成。");
    }
    throw error;
  });

  const responseText = await response.text();
  if (!response.ok) {
    console.error("Seedream generation failed", response.status, responseText.slice(0, 1000));
    throw new Error("图像生成服务返回错误，请检查模型、额度或参数配置。");
  }

  const items = collectGeneratedImageItems(responseText);
  if (items.length === 0) {
    throw new Error("图像生成服务没有返回图片。");
  }

  return Promise.all(items.slice(0, DEFAULT_RESULT_COUNT).map((item) => generatedImageItemToAsset(item)));
}

function resolveSeedreamImageModel(env) {
  const requested = String(env.DOUBAO_MODEL || env.ARK_SEEDREAM_IMAGE_MODEL || DEFAULT_ARK_SEEDREAM_IMAGE_MODEL).trim();
  return ARK_SEEDREAM_IMAGE_MODELS.has(requested) ? requested : DEFAULT_ARK_SEEDREAM_IMAGE_MODEL;
}

async function generatedImageItemToAsset(item) {
  if (item.b64_json || item.base64) {
    return {
      bytes: base64ToUint8Array(stripDataUrl(item.b64_json || item.base64)),
      mimeType: "image/png",
      extension: "png",
      qualityScore: 90
    };
  }

  const imageUrl = item.url || item.image_url;
  if (!imageUrl) {
    throw new Error("图像生成服务返回格式缺少图片数据。");
  }

  const imageResponse = await fetch(imageUrl);
  if (!imageResponse.ok) {
    throw new Error("生成图下载失败。");
  }

  const contentType = imageResponse.headers.get("Content-Type") || "image/jpeg";
  return {
    bytes: await imageResponse.arrayBuffer(),
    mimeType: contentType,
    extension: contentType.includes("png") ? "png" : contentType.includes("webp") ? "webp" : "jpg",
    qualityScore: 88
  };
}

function buildHeadshotSetPrompt(labels, styleSummary) {
  if ((labels || []).some((label) => label === CELEBRITY_HEADSHOT_LABEL) || String(styleSummary || "").includes(CELEBRITY_HEADSHOT_LABEL)) {
    return [
      `将用户上传的人像照片生成 ${labels.length} 张 1:1 明星级职业头像，风格为好莱坞经纪公司标准宣传照。`,
      "保持人物面部身份、五官特征、表情、头部角度和整体气质一致。",
      "人物应具备高级审美、商业价值和强个人品牌吸引力。",
      "背景替换为高级灰影棚背景，干净、极简、有层次，带轻微渐变和暗角，突出人物主体；不能出现原始杂乱环境，背景不能抢人物主体。",
      `用户选择与服装/文字摘要：${styleSummary || "服装为黑色西装或高级礼服，质感高级，剪裁利落，适合明星宣传照"}。`,
      "如果摘要中包含外套颜色、外套款式、内衬颜色、内衬款式、领型或扣子状态，必须按用户选择动态替换服装；否则使用黑色西装或高级礼服，剪裁利落。",
      "使用专业棚拍灯光，增强面部立体感和脸部轮廓，进行专业人像修复和色彩分级，使肤色自然、干净、均匀、细腻、有高级质感，同时保持人物真实自然、可识别。",
      "整体画面高级、克制、有明星感，适合作为个人品牌头像。输出必须为正方形头像构图。",
      "不要在图片中生成任何文字、字母、数字、Logo、水印、签名或排版元素；文字水印必须由程序后期叠加。"
    ].join(" ");
  }
  return [
    `基于用户上传的真实自拍，一次生成 ${labels.length} 张职业形象照，分别对应：${labels.join("、")}。`,
    `用户选择与场景摘要：${styleSummary || "求职简历场景 / 亲和专业 / 智能背景"}。`,
    "必须保持人物身份、五官比例、脸型、发际线、发型和年龄观感一致，不换脸，不改变性别，不生成多人。",
    "每张图需要有明显不同的背景、构图、光线和职场氛围，但人物本人必须一致。",
    "摄影标准：真实商业棚拍质感，85mm 人像镜头效果，自然皮肤纹理，眼神清晰，发丝边缘干净，服装合身，背景简洁高级。",
    "输出质量：高清、无水印、无文字、无 Logo、无畸变、无塑料皮肤、不过度磨皮、不过度锐化。"
  ].join(" ");
}

function collectGeneratedImageItems(responseText) {
  const text = String(responseText || "").trim();
  const items = [];

  if (text.includes("data:")) {
    for (const line of text.split(/\r?\n/)) {
      const trimmed = line.trim();
      if (!trimmed.startsWith("data:")) {
        continue;
      }
      const data = trimmed.slice(5).trim();
      if (!data || data === "[DONE]") {
        continue;
      }
      try {
        const event = JSON.parse(data);
        if (event?.type === "image_generation.partial_succeeded" && (event.b64_json || event.base64 || event.url || event.image_url)) {
          items.push(event);
        } else {
          items.push(...pickGeneratedImageItems(event));
        }
      } catch (_error) {
        // Ignore malformed keepalive/event lines.
      }
    }
    return items;
  }

  try {
    return pickGeneratedImageItems(JSON.parse(text));
  } catch (_error) {
    return [];
  }
}

function pickGeneratedImageItems(payload) {
  if (Array.isArray(payload?.data) && payload.data.length > 0) {
    return payload.data;
  }
  if (Array.isArray(payload?.images) && payload.images.length > 0) {
    return payload.images;
  }
  if (Array.isArray(payload?.result?.data) && payload.result.data.length > 0) {
    return payload.result.data;
  }
  if (payload?.b64_json || payload?.base64 || payload?.url || payload?.image_url) {
    return [payload];
  }
  return [];
}

function stripDataUrl(value) {
  const text = String(value || "");
  return text.startsWith("data:") && text.includes(",") ? text.split(",", 2)[1] : text;
}

function buildLegacyResultItems(jobId, uploads) {
  const sourceUploads = uploads.length > 0 ? uploads : [{ id: null, previewUrl: null }];
  return Array.from({ length: DEFAULT_RESULT_COUNT }).map((_, index) => {
    const upload = sourceUploads[index % sourceUploads.length];
    return {
      id: `${jobId}_${index + 1}`,
      label: RESULT_LABELS[index % RESULT_LABELS.length],
      provider: "legacy",
      status: "reference_only",
      imageUrl: upload.previewUrl,
      sourceImageUrl: upload.previewUrl,
      qualityScore: 0,
      message: "旧任务没有结果记录，当前按原始上传图做兼容预览。",
      referenceOnly: true,
      canDownload: false,
      downloadUrl: null,
      exportUrls: {}
    };
  });
}

function buildGenerationDisclosure(env) {
  const mode = resolveGenerationMode(env);
  return {
    provider: mode.provider,
    model: resolveSeedreamImageModel(env),
    ready: mode.ready,
    referenceOnly: !mode.ready,
    message: mode.ready
      ? `Seedream 图生一组图已配置，使用 ${resolveSeedreamImageModel(env)}。`
      : "当前环境未配置图像生成服务，页面展示的是上传参考图，不会伪装成 AI 成片。",
    requiredConfig: mode.ready ? [] : mode.requiredConfig
  };
}

function resolveGenerationMode(env) {
  const provider = sanitizeText(env.IMAGE_GENERATION_PROVIDER || env.GENERATION_PROVIDER || "doubao", 40).toLowerCase();
  const isDoubaoReady = Boolean(env.DOUBAO_API_KEY || env.ARK_API_KEY);
  const ready = provider === "doubao" ? isDoubaoReady : Boolean(env.IMAGE_GENERATION_API_KEY && env.IMAGE_GENERATION_ENDPOINT);
  const production = isProductionRuntime(env);

  return {
    provider,
    ready,
    requiresExternalProvider: production,
    requiredConfig:
      provider === "doubao"
        ? ["ARK_API_KEY 或 DOUBAO_API_KEY", `可选模型：${DEFAULT_ARK_SEEDREAM_IMAGE_MODEL}`, `固定默认地址：${ARK_IMAGE_BASE_URL}`]
        : ["IMAGE_GENERATION_PROVIDER", "IMAGE_GENERATION_API_KEY", "IMAGE_GENERATION_ENDPOINT", "IMAGE_GENERATION_MODEL"]
  };
}

function isProductionRuntime(env) {
  return ["production", "prod"].includes(String(env.ENVIRONMENT || env.NODE_ENV || "").toLowerCase());
}

function deriveJobStatus(job) {
  if (job.status === "failed") {
    return "failed";
  }
  if (job.status === "processing" || job.status === "generating") {
    return "processing";
  }
  if (job.status === "completed") {
    return job.unlocked_at ? "unlocked" : "preview";
  }
  if (job.unlocked_at) {
    return "unlocked";
  }
  return "processing";
}

function validateUploadFile(file) {
  if (!ALLOWED_IMAGE_TYPES.has(file.type)) {
    return "仅支持 JPG、PNG、WebP、HEIC 等常见图片格式。";
  }

  if (file.size > MAX_UPLOAD_SIZE_BYTES) {
    return "单张照片不能超过 10MB。";
  }

  return null;
}

function validatePassword(password) {
  // 消费级产品只要求长度 + 字母数字组合，过严的复杂度规则会赶走注册用户。
  if (password.length < 8) {
    return "密码至少需要 8 位。";
  }
  if (!/[A-Za-z]/.test(password) || !/[0-9]/.test(password)) {
    return "密码需要同时包含字母和数字。";
  }
  return null;
}

async function derivePasswordHash(password, saltHex) {
  const key = await crypto.subtle.importKey("raw", encoder.encode(password), "PBKDF2", false, ["deriveBits"]);
  const bits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      hash: "SHA-256",
      salt: hexToUint8Array(saltHex),
      iterations: PASSWORD_ITERATIONS
    },
    key,
    256
  );

  return uint8ArrayToHex(new Uint8Array(bits));
}

async function sha256Hex(value) {
  const digest = await crypto.subtle.digest("SHA-256", encoder.encode(value));
  return uint8ArrayToHex(new Uint8Array(digest));
}

function buildSessionCookie(name, token, request) {
  const parts = [
    `${name}=${token}`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    `Max-Age=${SESSION_DURATION_SECONDS}`
  ];

  if (shouldUseSecureCookie(request)) {
    parts.splice(3, 0, "Secure");
  }

  return parts.join("; ");
}

function buildExpiredSessionCookie(name, request) {
  const parts = [
    `${name}=`,
    "Path=/",
    "HttpOnly",
    "SameSite=Lax",
    "Max-Age=0"
  ];

  if (shouldUseSecureCookie(request)) {
    parts.splice(3, 0, "Secure");
  }

  return parts.join("; ");
}

function shouldUseSecureCookie(request) {
  try {
    return new URL(request.url).protocol === "https:";
  } catch {
    return true;
  }
}

function getSessionCookieName(env) {
  return env.SESSION_COOKIE_NAME || "impeccable_session";
}

async function readJson(request) {
  try {
    return await request.json();
  } catch {
    return {};
  }
}

function jsonResponse(payload, status = 200, extraHeaders = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...extraHeaders
    }
  });
}

function methodNotAllowed(allow) {
  return jsonResponse(
    { error: "请求方法不被允许。" },
    405,
    {
      Allow: allow.join(", ")
    }
  );
}

function parseCookies(cookieHeader) {
  return String(cookieHeader || "")
    .split(";")
    .map((part) => part.trim())
    .filter(Boolean)
    .reduce((acc, part) => {
      const separatorIndex = part.indexOf("=");
      if (separatorIndex === -1) {
        return acc;
      }
      const key = part.slice(0, separatorIndex).trim();
      const value = part.slice(separatorIndex + 1).trim();
      acc[key] = value;
      return acc;
    }, {});
}

function normaliseEmail(value) {
  return String(value || "").trim().toLowerCase();
}

function isValidEmail(value) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value);
}

function sanitizeText(value, maxLength = 255) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, maxLength);
}

function sanitizeFileName(fileName) {
  const cleaned = String(fileName || "upload")
    .replace(/[^\w.\-()\u4e00-\u9fa5]/g, "-")
    .replace(/-+/g, "-")
    .slice(-120);
  return cleaned || "upload";
}

function randomId(prefix) {
  return `${prefix}_${randomHex(12)}`;
}

function randomHex(byteLength) {
  const bytes = new Uint8Array(byteLength);
  crypto.getRandomValues(bytes);
  return uint8ArrayToHex(bytes);
}

function uint8ArrayToHex(bytes) {
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
}

function hexToUint8Array(hex) {
  const pairs = hex.match(/.{1,2}/g) || [];
  return new Uint8Array(pairs.map((pair) => Number.parseInt(pair, 16)));
}

function trimTrailingSlash(pathname) {
  return pathname.length > 1 ? pathname.replace(/\/$/, "") : pathname;
}

function hasFileExtension(pathname) {
  return /\.[a-zA-Z0-9]+$/.test(pathname);
}

function escapeXml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&apos;");
}

function encodeRFC5987(value) {
  return encodeURIComponent(value).replace(/['()*]/g, (character) => `%${character.charCodeAt(0).toString(16)}`);
}

function matchAdminEmail(email, adminList) {
  if (!adminList) {
    return false;
  }

  const emails = String(adminList)
    .split(",")
    .map((item) => item.trim().toLowerCase())
    .filter(Boolean);

  return emails.includes(email);
}

function isPrivatePage(pathname) {
  return PRIVATE_PATH_PREFIXES.some((prefix) => pathname === `${prefix}.html` || pathname.startsWith(`${prefix}_`) || pathname === prefix);
}

const encoder = new TextEncoder();

async function handleGoogleLogin(request, env, url) {
  const clientId = env.GOOGLE_CLIENT_ID;
  if (!clientId) {
    return jsonResponse({ error: "Google Login is not configured." }, 503);
  }

  const redirectUri = `${url.origin}/api/auth/google/callback`;
  const state = randomHex(16);
  const googleUrl = new URL("https://accounts.google.com/o/oauth2/v2/auth");
  googleUrl.searchParams.set("client_id", clientId);
  googleUrl.searchParams.set("redirect_uri", redirectUri);
  googleUrl.searchParams.set("response_type", "code");
  googleUrl.searchParams.set("scope", "openid email profile");
  googleUrl.searchParams.set("access_type", "online");
  googleUrl.searchParams.set("prompt", "select_account");
  googleUrl.searchParams.set("state", state);

  const headers = new Headers({ Location: googleUrl.toString() });
  headers.append("Set-Cookie", buildOAuthStateCookie(state, request));
  const next = sanitizeOAuthNext(url.searchParams.get("next"));
  if (next) {
    headers.append("Set-Cookie", buildOAuthNextCookie(next, request));
  }

  return new Response(null, { status: 302, headers });
}

// 只允许站内相对页面，防止开放重定向。
// 使用白名单而非黑名单：反斜杠会被浏览器规范化成斜杠（/\evil.com → //evil.com），
// 黑名单很容易被绕过，所以只接受形如 "upload.html" / "upload.html?x=1" 的站内页面。
const OAUTH_NEXT_PATTERN = /^[a-z0-9._-]+\.html(\?[\w=&%.-]*)?$/i;

function sanitizeOAuthNext(value) {
  const next = String(value || "").trim().replace(/^\/+/, "").slice(0, 200);
  if (!next || next.includes("\\") || next.includes(":") || next.includes("//") || next.includes("..")) {
    return "";
  }
  return OAUTH_NEXT_PATTERN.test(next) ? next : "";
}

function buildOAuthNextCookie(next, request) {
  const parts = [`oauth_next=${encodeURIComponent(next)}`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=600"];
  if (shouldUseSecureCookie(request)) {
    parts.splice(3, 0, "Secure");
  }
  return parts.join("; ");
}

function readOAuthNextCookie(request) {
  const cookies = request.headers.get("Cookie") || "";
  const match = cookies.match(/(?:^|;\s*)oauth_next=([^;]+)/);
  return match ? sanitizeOAuthNext(decodeURIComponent(match[1])) : "";
}

function buildOAuthStateCookie(state, request) {
  const parts = [`oauth_state=${state}`, "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=600"];
  if (shouldUseSecureCookie(request)) {
    parts.splice(3, 0, "Secure");
  }
  return parts.join("; ");
}

function buildExpiredOAuthStateCookie(request) {
  const parts = ["oauth_state=", "Path=/", "HttpOnly", "SameSite=Lax", "Max-Age=0"];
  if (shouldUseSecureCookie(request)) {
    parts.splice(3, 0, "Secure");
  }
  return parts.join("; ");
}

async function handleGoogleCallback(request, env, url) {
  try {
    const code = url.searchParams.get("code");
    if (!code) {
      return jsonResponse({ error: "Missing authorization code from Google." }, 400);
    }

    const returnedState = url.searchParams.get("state");
    const expectedState = parseCookies(request.headers.get("Cookie")).oauth_state;
    if (!returnedState || !expectedState || returnedState !== expectedState) {
      return jsonResponse({ error: "Invalid OAuth state. Please retry login." }, 400);
    }

    const clientId = env.GOOGLE_CLIENT_ID;
    const clientSecret = env.GOOGLE_CLIENT_SECRET;

    if (!clientId || !clientSecret) {
      console.error("Google OAuth secrets not configured");
      return jsonResponse({ error: "OAuth not configured." }, 500);
    }

    const redirectUri = `${url.origin}/api/auth/google/callback`;

    // Exchange code for tokens
    let tokenResponse;
    try {
      tokenResponse = await fetch("https://oauth2.googleapis.com/token", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: new URLSearchParams({
          code,
          client_id: clientId,
          client_secret: clientSecret,
          redirect_uri: redirectUri,
          grant_type: "authorization_code",
        }),
      });
    } catch (fetchErr) {
      console.error("Network error calling Google token endpoint", fetchErr);
      return jsonResponse({ error: "Network error contacting Google." }, 502);
    }

    if (!tokenResponse.ok) {
      const errorText = await tokenResponse.text();
      console.error("Google token exchange failed", tokenResponse.status, errorText);
      return jsonResponse({ error: "Google token exchange failed.", detail: errorText }, 500);
    }

    const tokens = await tokenResponse.json();

    // Get user info
    let userResponse;
    try {
      userResponse = await fetch("https://www.googleapis.com/oauth2/v3/userinfo", {
        headers: { Authorization: `Bearer ${tokens.access_token}` },
      });
    } catch (fetchErr) {
      console.error("Network error calling Google userinfo endpoint", fetchErr);
      return jsonResponse({ error: "Network error fetching user info." }, 502);
    }

    if (!userResponse.ok) {
      const errorText = await userResponse.text();
      console.error("Google userinfo failed", userResponse.status, errorText);
      return jsonResponse({ error: "Failed to get user info from Google." }, 500);
    }

    const googleUser = await userResponse.json();
    const email = normaliseEmail(googleUser.email);
    const name = googleUser.name || "Google User";

    // Find or create user in D1
    let user = await env.DB.prepare("SELECT id, email, name, role, credits FROM users WHERE email = ?")
      .bind(email)
      .first();

    if (!user) {
      const userId = randomId("usr");
      const now = Date.now();
      const role = matchAdminEmail(email, env.ADMIN_EMAILS) ? "admin" : "user";
      const initialCredits = getInitialCredits(env);

      await env.DB.prepare(
        `INSERT INTO users (id, email, name, role, credits, created_at, last_login_at)
         VALUES (?, ?, ?, ?, ?, ?, ?)`
      )
        .bind(userId, email, name, role, initialCredits, now, now)
        .run();

      user = { id: userId, email, name, role, credits: initialCredits };
    } else {
      await env.DB.prepare("UPDATE users SET last_login_at = ? WHERE id = ?")
        .bind(Date.now(), user.id)
        .run();
    }

    // Create session and redirect to dashboard
    const sessionResponse = await createSessionResponse(env, request, user);
    const responseHeaders = new Headers(sessionResponse.headers);
    const next = readOAuthNextCookie(request);
    responseHeaders.set("Location", next ? `/${next}` : "/dashboard.html");
    responseHeaders.append("Set-Cookie", buildExpiredOAuthStateCookie(request));
    responseHeaders.append("Set-Cookie", "oauth_next=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0");

    return new Response(null, {
      status: 302,
      headers: responseHeaders,
    });
  } catch (error) {
    console.error("Unexpected error in handleGoogleCallback", error);
    return jsonResponse({ error: "Login failed. Please try again.", detail: String(error) }, 500);
  }
}
