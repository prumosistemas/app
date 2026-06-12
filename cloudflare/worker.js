// worker.js
// Cloudflare Worker + D1 API auth + empresas + colaboradores + roles + master + logs + proxy FastAPI
// Binding D1 esperado: env.db
//
// Variáveis esperadas:
// SETUP_TOKEN
// ADMIN_EMAIL
// ADMIN_PASSWORD
//
// Para proxy Python:
// PYTHON_API_URL
// ISS_INTERNAL_SECRET
//
// Opcionais:
// COOKIE_MODE                     -> dev-remote | local | prod
// FRONTEND_ORIGINS                -> origens permitidas separadas por virgula
// COMPANY_NAME                    -> padrão: "Conta Master"
// SESSION_IDLE_TTL_SECONDS        -> padrão: 1800
// SESSION_ABSOLUTE_TTL_SECONDS    -> padrão: 43200
// PBKDF2_ITERATIONS               -> padrão: 100000

const SESSION_COOKIE = "session";

const DEFAULT_LOGIN_IP_LIMIT = 10;
const DEFAULT_LOGIN_EMAIL_LIMIT = 5;
const DEFAULT_LOGIN_WINDOW_SECONDS = 300;

const MAX_MEMBERS_PER_COMPANY = 30;
const MAX_SESSIONS_PER_USER = 5;

// Limites separados:
// - rotas normais continuam protegidas
// - rotas /py/* aceitam datasets, xlsx e payloads maiores
const MAX_POST_BYTES = 128 * 1024; // 128 KB
const MAX_PY_POST_BYTES = 10 * 1024 * 1024; // 10 MB

const MAX_EMAIL_LENGTH = 254;
const MAX_PASSWORD_LENGTH = 200;
const MAX_COMPANY_NAME_LENGTH = 120;
const MAX_LOGS_ON_SCREEN = 80;

const LOG_RETENTION_SECONDS = 90 * 24 * 60 * 60;
const SESSION_PASSWORD_CHANGE_TOLERANCE_SECONDS = 2;

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    try {
      if (request.method === "OPTIONS") {
        return optionsResponse(request, env);
      }

      if (!env.db) {
        return jsonResponse(request, env, { ok: false, error: "Binding D1 'db' não configurado." }, 500);
      }

      await migrate(env.db);

      if (hasRequestBody(request.method)) {
        const maxBytes = getMaxPostBytes(url.pathname);

        if (isBodyTooLarge(request, maxBytes)) {
          return jsonResponse(request, env, {
            ok: false,
            error: "Requisição muito grande.",
            max_bytes: maxBytes,
          }, 413);
        }
      }

      if (url.pathname === "/api/setup" && request.method === "GET") {
        return handleSetup(request, env);
      }

      if (url.pathname === "/api/login" && request.method === "POST") {
        return handleLoginPost(request, env);
      }

      if (url.pathname === "/api/logout" && request.method === "POST") {
        return handleLogout(request, env);
      }

      if (url.pathname === "/api/me" && request.method === "GET") {
        return handleMe(request, env);
      }

      if (url.pathname === "/api/change-password" && request.method === "POST") {
        return handleChangePassword(request, env);
      }

      if (url.pathname === "/api/master/companies" && request.method === "GET") {
        return handleMasterListCompanies(request, env);
      }

      if (url.pathname.startsWith("/api/master/companies/") && request.method === "GET") {
        return handleMasterGetCompany(request, env);
      }

      if (url.pathname === "/api/master/companies" && request.method === "POST") {
        return handleMasterCreateCompany(request, env);
      }

      if (url.pathname === "/api/master/companies/toggle" && request.method === "POST") {
        return handleMasterToggleCompany(request, env);
      }

      if (url.pathname === "/api/master/companies/delete" && request.method === "POST") {
        return handleMasterDeleteCompany(request, env);
      }

      if (url.pathname === "/api/master/owners/reset-password" && request.method === "POST") {
        return handleMasterResetOwnerPassword(request, env);
      }

      if (url.pathname === "/api/master/logs" && request.method === "GET") {
        return handleMasterLogs(request, env);
      }

      if (url.pathname === "/api/master/metrics" && request.method === "GET") {
        return handleMasterMetrics(request, env);
      }

      if (url.pathname === "/api/billing" && request.method === "GET") {
        return handleBilling(request, env);
      }

      if (url.pathname === "/api/master/billing" && request.method === "GET") {
        return handleMasterBilling(request, env);
      }

      if (url.pathname === "/api/master/billing/settings" && request.method === "POST") {
        return handleMasterBillingSettings(request, env);
      }

      if (url.pathname === "/api/master/billing/payments" && request.method === "POST") {
        return handleMasterBillingPaymentCreate(request, env);
      }

      if (url.pathname === "/api/users" && request.method === "GET") {
        return handleListUsers(request, env);
      }

      if (url.pathname === "/api/users" && request.method === "POST") {
        return handleCreateUser(request, env);
      }

      if (url.pathname === "/api/users/delete" && request.method === "POST") {
        return handleDeleteUser(request, env);
      }

      if (url.pathname === "/api/users/toggle" && request.method === "POST") {
        return handleToggleUser(request, env);
      }

      if (url.pathname === "/api/users/reset-password" && request.method === "POST") {
        return handleResetUserPassword(request, env);
      }

      if (url.pathname.startsWith("/py/")) {
        return handlePythonProxy(request, env);
      }

      return jsonResponse(request, env, { ok: false, error: "Rota não encontrada." }, 404);
    } catch (err) {
      console.error("Worker error:", err);
      return jsonResponse(request, env, { ok: false, error: "Erro interno no Worker." }, 500);
    }
  },

  async scheduled(_event, env, ctx) {
    ctx.waitUntil(reconcileDeletionJobs(env));
  },
};

/* =========================
   SETUP
========================= */

async function handleSetup(request, env) {
  const url = new URL(request.url);

  await migrate(env.db);

  const masterExists = await env.db.prepare(`
    SELECT id
    FROM users
    WHERE role = 'master'
    LIMIT 1
  `).first();

  if (masterExists) {
    return jsonResponse(request, env, { ok: false, error: "Setup indisponível." }, 404);
  }

  const authHeader = request.headers.get("Authorization") || "";
  const bearer = authHeader.startsWith("Bearer ")
    ? authHeader.slice("Bearer ".length)
    : "";

  const token = bearer || url.searchParams.get("token") || "";

  if (!env.SETUP_TOKEN || token !== env.SETUP_TOKEN) {
    return jsonResponse(request, env, { ok: false, error: "Setup indisponível." }, 404);
  }

  const masterEmail = normalizeEmail(env.ADMIN_EMAIL || "");
  const masterPassword = String(env.ADMIN_PASSWORD || "");
  const companyName = String(env.COMPANY_NAME || "Conta Master").trim();

  if (!isValidEmailInput(masterEmail)) {
    return jsonResponse(request, env, { ok: false, error: "ADMIN_EMAIL inválido." }, 400);
  }

  if (!isValidPasswordInput(masterPassword)) {
    return jsonResponse(request, env, { ok: false, error: "ADMIN_PASSWORD precisa ter entre 8 e 200 caracteres." }, 400);
  }

  if (companyName.length < 1 || companyName.length > MAX_COMPANY_NAME_LENGTH) {
    return jsonResponse(request, env, { ok: false, error: `COMPANY_NAME precisa ter entre 1 e ${MAX_COMPANY_NAME_LENGTH} caracteres.` }, 400);
  }

  const existing = await env.db.prepare(`
    SELECT id
    FROM users
    WHERE email = ?
    LIMIT 1
  `).bind(masterEmail).first();

  if (existing) {
    return jsonResponse(request, env, { ok: false, error: "Já existe usuário com esse email." }, 400);
  }

  const companyId = randomId();
  const userId = randomId();
  const passwordHash = await hashPassword(masterPassword, env);
  const ts = now();

  await env.db.batch([
    env.db.prepare(`
      INSERT INTO companies (id, name, created_at, disabled)
      VALUES (?, ?, ?, 0)
    `).bind(companyId, companyName, ts),

    env.db.prepare(`
      INSERT INTO users (
        id,
        company_id,
        email,
        password_hash,
        role,
        must_change_password,
        created_at,
        disabled,
        password_changed_at,
        last_login_at,
        current_login_at
      )
      VALUES (?, ?, ?, ?, 'master', 0, ?, 0, ?, NULL, NULL)
    `).bind(userId, companyId, masterEmail, passwordHash, ts, ts),
  ]);

  await logEvent(env, request, {
    actor: null,
    companyId,
    action: "setup_master_created",
    targetType: "user",
    targetId: userId,
    message: `Conta master criada: ${masterEmail}`,
  });

  return jsonResponse(request, env, {
    ok: true,
    setup: "done",
    master_created: true,
    master_email: masterEmail,
    company_name: companyName,
    important: "Depois do setup, remova SETUP_TOKEN. A rota /api/setup bloqueia se já existir master.",
  });
}

/* =========================
   AUTH ROUTES
========================= */

async function handleLoginPost(request, env) {
  await cleanupTemporaryData(env.db);

  const body = await readBody(request);

  const email = normalizeEmail(body.email || "");
  const password = String(body.password || "");

  if (!isValidEmailInput(email) || !isValidPasswordInput(password)) {
    await fakePasswordDelay(env);
    return jsonResponse(request, env, { ok: false, error: "Email ou senha inválidos." }, 400);
  }

  const ip = getClientIp(request);

  const ipLimit = await checkRateLimit(
    env.db,
    `login:ip:${ip}`,
    getIntEnv(env, "LOGIN_IP_LIMIT", DEFAULT_LOGIN_IP_LIMIT, 1, 1000),
    getIntEnv(env, "LOGIN_WINDOW_SECONDS", DEFAULT_LOGIN_WINDOW_SECONDS, 30, 86400)
  );

  if (!ipLimit.ok) {
    await fakePasswordDelay(env);
    return jsonResponse(request, env, {
      ok: false,
      error: "Muitas tentativas.",
      retry_after: ipLimit.retryAfter,
    }, 429);
  }

  const emailHash = await sha256Hex(email);

  const emailLimit = await checkRateLimit(
    env.db,
    `login:email:${emailHash}`,
    getIntEnv(env, "LOGIN_EMAIL_LIMIT", DEFAULT_LOGIN_EMAIL_LIMIT, 1, 1000),
    getIntEnv(env, "LOGIN_WINDOW_SECONDS", DEFAULT_LOGIN_WINDOW_SECONDS, 30, 86400)
  );

  if (!emailLimit.ok) {
    await fakePasswordDelay(env);
    return jsonResponse(request, env, {
      ok: false,
      error: "Muitas tentativas para este email.",
      retry_after: emailLimit.retryAfter,
    }, 429);
  }

  const user = await env.db.prepare(`
    SELECT
      u.id,
      u.email,
      u.password_hash,
      u.disabled,
      u.company_id,
      u.role,
      u.must_change_password,
      c.disabled AS company_disabled
    FROM users u
    JOIN companies c ON c.id = u.company_id
    WHERE u.email = ?
    LIMIT 1
  `).bind(email).first();

  if (
    !user ||
    Number(user.disabled) === 1 ||
    (Number(user.company_disabled) === 1 && user.role !== "owner")
  ) {
    await fakePasswordDelay(env);
    return jsonResponse(request, env, { ok: false, error: "Email ou senha inválidos." }, 401);
  }

  const passwordOk = await verifyPassword(password, user.password_hash);

  if (!passwordOk) {
    return jsonResponse(request, env, { ok: false, error: "Email ou senha inválidos." }, 401);
  }

  const ts = now();

  await env.db.prepare(`
    UPDATE users
    SET last_login_at = current_login_at,
        current_login_at = ?
    WHERE id = ?
  `).bind(ts, user.id).run();

  await logEvent(env, request, {
    actor: {
      id: user.id,
      email: user.email,
      role: user.role,
      company_id: user.company_id,
    },
    companyId: user.company_id,
    action: "login_success",
    targetType: "user",
    targetId: user.id,
    message: `Login realizado: ${user.email}`,
  });

  const session = await createSession(request, env, user.id);
  const cfg = getConfig(env);
  const cookie = makeSessionCookie(env, session.rawToken, cfg.absoluteTtlSeconds);

  const csrf = await issueCsrfForSession(env, session.sessionHash);

  return jsonResponse(
    request,
    env,
    {
      ok: true,
      must_change_password: Number(user.must_change_password) === 1,
      company_disabled: Number(user.company_disabled) === 1,
      csrf,
      session_token: session.rawToken,
      user: {
        id: user.id,
        email: user.email,
        role: user.role,
        company_id: user.company_id,
      },
    },
    200,
    cookie
  );
}

async function handleLogout(request, env) {
  const auth = await requireAuth(request, env);
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);

  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  await revokeSession(env.db, auth.session.session_hash);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "logout",
    targetType: "user",
    targetId: auth.user.id,
    message: `Logout: ${auth.user.email}`,
  });

  return jsonResponse(request, env, { ok: true }, 200, clearSessionCookie(env));
}

async function handleMe(request, env) {
  const auth = await getAuth(request, env);

  if (!auth.ok) {
    return jsonResponse(
      request,
      env,
      { authenticated: false },
      401,
      auth.clearCookie ? clearSessionCookie(env) : null
    );
  }

  const csrf = await issueCsrfForSession(env, auth.session.session_hash);

  return jsonResponse(request, env, {
    authenticated: true,
    csrf,
    user: {
      id: auth.user.id,
      email: auth.user.email,
      role: auth.user.role,
      company_id: auth.user.company_id,
      company_name: auth.user.company_name,
      must_change_password: Boolean(auth.user.must_change_password),
      company_disabled: Boolean(auth.user.company_disabled),
      last_login_at: auth.user.last_login_at,
      current_login_at: auth.user.current_login_at,
    },
    session: {
      created_at: auth.session.created_at,
      last_seen_at: auth.session.last_seen_at,
      absolute_expires_at: auth.session.absolute_expires_at,
    },
  });
}

async function handleChangePassword(request, env) {
  const auth = await requireAuth(request, env);
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);

  const currentPassword = String(body.current_password || "");
  const newPassword = String(body.new_password || "");
  const confirmPassword = String(body.confirm_password || "");

  if (!isValidPasswordInput(currentPassword)) {
    return jsonResponse(request, env, { ok: false, error: "Senha atual inválida." }, 400);
  }

  if (!isValidPasswordInput(newPassword)) {
    return jsonResponse(request, env, { ok: false, error: "A nova senha precisa ter entre 8 e 200 caracteres." }, 400);
  }

  if (newPassword !== confirmPassword) {
    return jsonResponse(request, env, { ok: false, error: "As senhas novas não conferem." }, 400);
  }

  const userRow = await env.db.prepare(`
    SELECT password_hash
    FROM users
    WHERE id = ?
    LIMIT 1
  `).bind(auth.user.id).first();

  if (!userRow) {
    return jsonResponse(request, env, { ok: false, error: "Usuário não encontrado." }, 401, clearSessionCookie(env));
  }

  const currentOk = await verifyPassword(currentPassword, userRow.password_hash);

  if (!currentOk) {
    return jsonResponse(request, env, { ok: false, error: "Senha atual incorreta." }, 401);
  }

  const ts = now();
  const newHash = await hashPassword(newPassword, env);

  await env.db.batch([
    env.db.prepare(`
      UPDATE users
      SET password_hash = ?,
          must_change_password = 0,
          password_changed_at = ?
      WHERE id = ?
    `).bind(newHash, ts, auth.user.id),

    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id = ?
        AND session_hash != ?
    `).bind(ts, auth.user.id, auth.session.session_hash),

    env.db.prepare(`
      UPDATE sessions
      SET created_at = ?,
          last_seen_at = ?
      WHERE session_hash = ?
    `).bind(ts + SESSION_PASSWORD_CHANGE_TOLERANCE_SECONDS, ts, auth.session.session_hash),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "password_changed",
    targetType: "user",
    targetId: auth.user.id,
    message: `Senha alterada: ${auth.user.email}`,
  });

  const csrf = await issueCsrfForSession(env, auth.session.session_hash);

  return jsonResponse(request, env, { ok: true, csrf });
}

/* =========================
   MASTER ROUTES
========================= */

async function handleMasterListCompanies(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const companies = await getCompaniesForMaster(env.db);
  const usersByCompany = await getUsersGroupedByCompany(env.db);
  const deletionJobs = await getActiveDeletionJobs(env.db);
  const enriched = [];

  for (const company of companies) {
    const users = (usersByCompany.get(company.id) || []).map((user) => ({
      ...user,
      deletion_status: deletionJobs.get(`member:${user.id}`)?.status || null,
    }));
    const stats = await fetchPythonCompanyStats(env, auth.user, company.id);
    enriched.push({
      ...company,
      deletion_status: deletionJobs.get(`company:${company.id}`)?.status || null,
      is_master_company: users.some((user) => user.role === "master"),
      users,
      member_count: users.filter((user) => user.role === "member").length,
      active_member_count: users.filter((user) => user.role === "member" && Number(user.disabled) === 0).length,
      disabled_member_count: users.filter((user) => user.role === "member" && Number(user.disabled) === 1).length,
      stats,
    });
  }

  return jsonResponse(request, env, { ok: true, companies: enriched });
}

async function handleMasterGetCompany(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const url = new URL(request.url);
  const companyId = decodeURIComponent(url.pathname.slice("/api/master/companies/".length));
  if (!isSafeId(companyId)) {
    return jsonResponse(request, env, { ok: false, error: "Empresa inválida." }, 400);
  }

  const companies = await getCompaniesForMaster(env.db);
  const company = companies.find((item) => item.id === companyId);
  if (!company) {
    return jsonResponse(request, env, { ok: false, error: "Empresa não encontrada." }, 404);
  }

  const usersByCompany = await getUsersGroupedByCompany(env.db);
  const deletionJobs = await getActiveDeletionJobs(env.db);
  const users = (usersByCompany.get(company.id) || []).map((user) => ({
    ...user,
    deletion_status: deletionJobs.get(`member:${user.id}`)?.status || null,
  }));
  const detail = await fetchPythonCompanyDetail(env, auth.user, company.id);

  return jsonResponse(request, env, {
    ok: true,
    company: {
      ...company,
      deletion_status: deletionJobs.get(`company:${company.id}`)?.status || null,
      is_master_company: users.some((user) => user.role === "master"),
      users,
      member_count: users.filter((user) => user.role === "member").length,
      active_member_count: users.filter((user) => user.role === "member" && Number(user.disabled) === 0).length,
      disabled_member_count: users.filter((user) => user.role === "member" && Number(user.disabled) === 1).length,
      stats: detail,
    },
  });
}

async function handleMasterLogs(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const logs = await getRecentLogs(env.db);

  return jsonResponse(request, env, { ok: true, logs });
}

async function handleMasterMetrics(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;
  const url = new URL(request.url);
  const requestedRange = url.searchParams.get("range") || "";
  const range = ["1h", "6h", "24h", "5d"].includes(requestedRange) ? requestedRange : "6h";
  const metrics = await fetchPythonSystemMetrics(env, auth.user, range);
  if (!metrics.ok) {
    return jsonResponse(request, env, metrics, 502);
  }
  return jsonResponse(request, env, metrics);
}

async function handleBilling(request, env) {
  const auth = await requireRole(request, env, "owner");
  if (auth.response) return auth.response;

  const settings = await getBillingSettings(env.db);
  const payments = await getBillingPayments(env.db, { companyId: auth.user.company_id, limit: 80 });
  return jsonResponse(request, env, {
    ok: true,
    settings,
    summary: summarizeBilling(payments),
    payments,
  });
}

async function handleMasterBilling(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const url = new URL(request.url);
  const companyId = String(url.searchParams.get("company_id") || "").trim();
  const settings = await getBillingSettings(env.db);
  const payments = await getBillingPayments(env.db, { companyId: companyId || null, limit: 200 });

  return jsonResponse(request, env, {
    ok: true,
    settings,
    summary: summarizeBilling(payments),
    payments,
  });
}

async function handleMasterBillingSettings(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);

  const body = await readBody(request);
  const settings = {
    pix_key: String(body.pix_key || "").trim().slice(0, 500),
    pix_copy_paste: String(body.pix_copy_paste || "").trim().slice(0, 6000),
    qr_code_url: String(body.qr_code_url || "").trim().slice(0, 6000),
    monthly_amount: String(body.monthly_amount || "").trim().slice(0, 80),
  };
  const ts = now();

  await env.db.batch(Object.entries(settings).map(([key, value]) => (
    env.db.prepare(`
      INSERT INTO billing_settings (key, value, updated_at, updated_by_user_id)
      VALUES (?, ?, ?, ?)
      ON CONFLICT(key) DO UPDATE SET
        value = excluded.value,
        updated_at = excluded.updated_at,
        updated_by_user_id = excluded.updated_by_user_id
    `).bind(key, value, ts, auth.user.id)
  )));

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "billing_settings_updated",
    targetType: "billing",
    targetId: "settings",
    message: "Configurações PIX atualizadas.",
  });

  return jsonResponse(request, env, { ok: true, settings: await getBillingSettings(env.db) });
}

async function handleMasterBillingPaymentCreate(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);

  const body = await readBody(request);
  const companyId = String(body.company_id || "").trim();
  const userId = String(body.user_id || "").trim();
  const userEmail = normalizeEmail(body.user_email || "");
  const periodStart = normalizeBillingMonth(body.period_start || body.month || "");
  const months = Math.max(1, Math.min(36, Number.parseInt(String(body.months || "1"), 10) || 1));

  if (!isSafeId(companyId)) return jsonResponse(request, env, { ok: false, error: "Empresa inválida." }, 400);
  if (!periodStart) return jsonResponse(request, env, { ok: false, error: "Mês inicial inválido. Use AAAA-MM." }, 400);

  const company = await env.db.prepare("SELECT id, name FROM companies WHERE id = ? LIMIT 1").bind(companyId).first();
  if (!company) return jsonResponse(request, env, { ok: false, error: "Empresa não encontrada." }, 404);

  let finalUserEmail = userEmail;
  if (userId) {
    const user = await env.db.prepare("SELECT id, email FROM users WHERE id = ? AND company_id = ? LIMIT 1").bind(userId, companyId).first();
    if (!user) return jsonResponse(request, env, { ok: false, error: "Usuário não pertence à empresa." }, 400);
    finalUserEmail = user.email;
  }

  const activeUntil = normalizeBillingActiveUntil(body.active_until || "", periodStart, months);
  const amountCents = parseAmountCents(body.amount || body.amount_cents || "");
  const payment = {
    id: randomId(),
    company_id: companyId,
    user_id: userId || null,
    user_email: finalUserEmail || null,
    period_start: periodStart,
    months,
    active_until: activeUntil,
    amount_cents: amountCents,
    description: String(body.description || "").trim().slice(0, 300),
    note: String(body.note || "").trim().slice(0, 1000),
    status: "paid",
    paid_at: parseUnixDate(body.paid_at || "") || now(),
    created_at: now(),
    created_by_user_id: auth.user.id,
  };

  await env.db.prepare(`
    INSERT INTO payments (
      id, company_id, user_id, user_email, period_start, months, active_until,
      amount_cents, description, note, status, paid_at, created_at, created_by_user_id
    )
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
  `).bind(
    payment.id, payment.company_id, payment.user_id, payment.user_email, payment.period_start,
    payment.months, payment.active_until, payment.amount_cents, payment.description, payment.note,
    payment.status, payment.paid_at, payment.created_at, payment.created_by_user_id
  ).run();

  await logEvent(env, request, {
    actor: auth.user,
    companyId,
    action: "billing_payment_marked",
    targetType: "payment",
    targetId: payment.id,
    message: `Pagamento marcado: ${company.name} ${periodStart} (${months} mês(es)).`,
  });

  return jsonResponse(request, env, { ok: true, payment, summary: summarizeBilling(await getBillingPayments(env.db, { companyId, limit: 200 })) });
}

async function handleMasterCreateCompany(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);

  const companyName = String(body.company_name || "").trim();
  const ownerEmail = normalizeEmail(body.owner_email || "");

  if (companyName.length < 1 || companyName.length > MAX_COMPANY_NAME_LENGTH) {
    return jsonResponse(request, env, { ok: false, error: "Nome da empresa inválido." }, 400);
  }

  if (!isValidEmailInput(ownerEmail)) {
    return jsonResponse(request, env, { ok: false, error: "Email do admin inválido." }, 400);
  }

  const existing = await env.db.prepare(`
    SELECT id
    FROM users
    WHERE email = ?
    LIMIT 1
  `).bind(ownerEmail).first();

  if (existing) {
    return jsonResponse(request, env, { ok: false, error: "Já existe um usuário com esse email." }, 400);
  }

  const ts = now();
  const companyId = randomId();
  const ownerId = randomId();
  const ownerPassword = generateTemporaryPassword();
  const ownerHash = await hashPassword(ownerPassword, env);

  try {
    await env.db.batch([
      env.db.prepare(`
        INSERT INTO companies (id, name, created_at, disabled)
        VALUES (?, ?, ?, 0)
      `).bind(companyId, companyName, ts),

      env.db.prepare(`
        INSERT INTO users (
          id,
          company_id,
          email,
          password_hash,
          role,
          must_change_password,
          created_at,
          disabled,
          password_changed_at,
          last_login_at,
          current_login_at
        )
        VALUES (?, ?, ?, ?, 'owner', 1, ?, 0, ?, NULL, NULL)
      `).bind(ownerId, companyId, ownerEmail, ownerHash, ts, ts),
    ]);
  } catch (err) {
    console.error("Master create company error:", err);
    return jsonResponse(request, env, { ok: false, error: "Não foi possível criar a empresa/admin." }, 400);
  }

  await logEvent(env, request, {
    actor: auth.user,
    companyId,
    action: "company_created",
    targetType: "company",
    targetId: companyId,
    message: `Empresa criada: ${companyName}; admin: ${ownerEmail}`,
  });

  return jsonResponse(request, env, {
    ok: true,
    company: {
      id: companyId,
      name: companyName,
    },
    owner: {
      id: ownerId,
      email: ownerEmail,
      temporary_password: ownerPassword,
    },
  });
}

async function handleMasterToggleCompany(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);

  const companyId = String(body.company_id || "");
  const intent = String(body.intent || "");
  const confirm = String(body.confirm || "");

  if (!isSafeId(companyId) || !["disable", "enable"].includes(intent)) {
    return jsonResponse(request, env, { ok: false, error: "Empresa inválida." }, 400);
  }

  if (confirm !== "yes") {
    return jsonResponse(request, env, { ok: false, error: "Confirmação exigida.", requires_confirmation: true }, 400);
  }

  const company = await env.db.prepare(`
    SELECT id, name, disabled
    FROM companies
    WHERE id = ?
    LIMIT 1
  `).bind(companyId).first();

  if (!company) {
    return jsonResponse(request, env, { ok: false, error: "Empresa não encontrada." }, 404);
  }

  const masterRow = await env.db.prepare(`
    SELECT id
    FROM users
    WHERE company_id = ?
      AND role = 'master'
    LIMIT 1
  `).bind(companyId).first();

  if (masterRow) {
    return jsonResponse(request, env, { ok: false, error: "A empresa da conta master não pode ser desativada." }, 400);
  }

  const disabled = intent === "disable" ? 1 : 0;
  const ts = now();

  if (intent === "disable") {
    await stopPythonRunsForCompany(env, auth.user, companyId);
  }

  await env.db.batch([
    env.db.prepare(`
      UPDATE companies
      SET disabled = ?
      WHERE id = ?
    `).bind(disabled, companyId),

    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id IN (
        SELECT id
        FROM users
        WHERE company_id = ?
          AND role = 'member'
      )
    `).bind(ts, companyId),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId,
    action: intent === "disable" ? "company_disabled" : "company_enabled",
    targetType: "company",
    targetId: companyId,
    message: `${intent === "disable" ? "Empresa desativada" : "Empresa reativada"}: ${company.name}`,
  });

  return jsonResponse(request, env, { ok: true });
}

async function handleMasterDeleteCompany(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);
  const companyId = String(body.company_id || "");
  const confirm = String(body.confirm || "");

  if (!isSafeId(companyId)) {
    return jsonResponse(request, env, { ok: false, error: "Empresa inválida." }, 400);
  }

  if (confirm !== "DELETE") {
    return jsonResponse(request, env, { ok: false, error: "Confirmação exigida.", requires_confirmation: true }, 400);
  }

  const company = await env.db.prepare(`
    SELECT id, name
    FROM companies
    WHERE id = ?
    LIMIT 1
  `).bind(companyId).first();

  if (!company) {
    return jsonResponse(request, env, { ok: false, error: "Empresa não encontrada." }, 404);
  }

  const masterRow = await env.db.prepare(`
    SELECT id
    FROM users
    WHERE company_id = ?
      AND role = 'master'
    LIMIT 1
  `).bind(companyId).first();

  if (masterRow) {
    return jsonResponse(request, env, { ok: false, error: "A empresa da conta master não pode ser apagada." }, 400);
  }

  const ts = now();
  await env.db.batch([
    env.db.prepare(`UPDATE users SET disabled = 1 WHERE company_id = ?`).bind(companyId),
    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id IN (SELECT id FROM users WHERE company_id = ?)
    `).bind(ts, companyId),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "company_delete_requested",
    targetType: "company",
    targetId: companyId,
    message: `Exclusão sincronizada solicitada para empresa: ${company.name}`,
  });

  const job = await enqueueDeletionJob(env, auth.user, {
    targetType: "company",
    targetId: companyId,
    companyId,
    targetEmail: company.name,
  });
  const reconciliation = await reconcileDeletionJob(env, job);
  return jsonResponse(
    request,
    env,
    { ok: true, deletion_status: reconciliation.status, job_id: job.id },
    reconciliation.status === "completed" ? 200 : 202
  );
}

async function handleMasterResetOwnerPassword(request, env) {
  const auth = await requireRole(request, env, "master");
  if (auth.response) return auth.response;

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);

  const ownerId = String(body.owner_id || "");
  const confirm = String(body.confirm || "");

  if (!isSafeId(ownerId)) {
    return jsonResponse(request, env, { ok: false, error: "Admin inválido." }, 400);
  }

  if (confirm !== "yes") {
    return jsonResponse(request, env, { ok: false, error: "Confirmação exigida.", requires_confirmation: true }, 400);
  }

  const owner = await env.db.prepare(`
    SELECT
      u.id,
      u.email,
      u.company_id,
      u.role,
      u.disabled,
      c.name AS company_name
    FROM users u
    JOIN companies c ON c.id = u.company_id
    WHERE u.id = ?
      AND u.role = 'owner'
    LIMIT 1
  `).bind(ownerId).first();

  if (!owner) {
    return jsonResponse(request, env, { ok: false, error: "Admin não encontrado." }, 404);
  }

  if (Number(owner.disabled) === 1) {
    return jsonResponse(request, env, { ok: false, error: "Admin está desativado." }, 400);
  }

  const tempPassword = generateTemporaryPassword();
  const passwordHash = await hashPassword(tempPassword, env);
  const ts = now();

  await env.db.batch([
    env.db.prepare(`
      UPDATE users
      SET password_hash = ?,
          must_change_password = 1,
          password_changed_at = ?
      WHERE id = ?
        AND role = 'owner'
    `).bind(passwordHash, ts, ownerId),

    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id = ?
    `).bind(ts, ownerId),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: owner.company_id,
    action: "owner_password_reset",
    targetType: "user",
    targetId: ownerId,
    message: `Senha do admin resetada: ${owner.email} (${owner.company_name})`,
  });

  return jsonResponse(request, env, {
    ok: true,
    owner: {
      id: ownerId,
      email: owner.email,
      temporary_password: tempPassword,
    },
  });
}

/* =========================
   OWNER / COLABORADORES
========================= */

async function handleListUsers(request, env) {
  const auth = await requireRole(request, env, "owner");
  if (auth.response) return auth.response;

  if (auth.user.must_change_password) {
    return jsonResponse(request, env, { ok: false, error: "Troca de senha obrigatória." }, 403);
  }

  const users = await getCompanyUsers(env.db, auth.user.company_id);
  const stats = await fetchPythonCompanyStats(env, auth.user, auth.user.company_id);
  const deletionJobs = await getActiveDeletionJobs(env.db);
  const statsByUser = new Map();

  if (stats && stats.users) {
    for (const item of stats.users) {
      statsByUser.set(item.user_id, item);
    }
  }

  return jsonResponse(request, env, {
    ok: true,
    company_disabled: Boolean(auth.user.company_disabled),
    users: users.map((user) => ({
      ...user,
      deletion_status: deletionJobs.get(`member:${user.id}`)?.status || null,
      blocked_by_company: Boolean(auth.user.company_disabled && user.role === "member"),
      stats: statsByUser.get(user.id) || null,
    })),
    stats,
  });
}

async function handleCreateUser(request, env) {
  const auth = await requireRole(request, env, "owner");
  if (auth.response) return auth.response;
  const readonlyResponse = requireWritableOwnerCompany(request, env, auth);
  if (readonlyResponse) return readonlyResponse;

  if (auth.user.must_change_password) {
    return jsonResponse(request, env, { ok: false, error: "Troca de senha obrigatória." }, 403);
  }

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);
  const email = normalizeEmail(body.email || "");

  if (!isValidEmailInput(email)) {
    return jsonResponse(request, env, { ok: false, error: "Email inválido." }, 400);
  }

  if (email === auth.user.email) {
    return jsonResponse(request, env, { ok: false, error: "Você não pode criar colaborador com seu próprio email." }, 400);
  }

  const countRow = await env.db.prepare(`
    SELECT COUNT(*) AS total
    FROM users
    WHERE company_id = ?
      AND role = 'member'
      AND disabled = 0
  `).bind(auth.user.company_id).first();

  const totalMembers = Number(countRow?.total || 0);

  if (totalMembers >= MAX_MEMBERS_PER_COMPANY) {
    return jsonResponse(request, env, { ok: false, error: `Limite de ${MAX_MEMBERS_PER_COMPANY} colaboradores atingido.` }, 400);
  }

  const existing = await env.db.prepare(`
    SELECT id
    FROM users
    WHERE email = ?
    LIMIT 1
  `).bind(email).first();

  if (existing) {
    return jsonResponse(request, env, { ok: false, error: "Já existe um usuário com esse email." }, 400);
  }

  const tempPassword = generateTemporaryPassword();
  const passwordHash = await hashPassword(tempPassword, env);

  const ts = now();
  const userId = randomId();

  try {
    await env.db.prepare(`
      INSERT INTO users (
        id,
        company_id,
        email,
        password_hash,
        role,
        must_change_password,
        created_at,
        disabled,
        password_changed_at,
        last_login_at,
        current_login_at
      )
      VALUES (?, ?, ?, ?, 'member', 1, ?, 0, ?, NULL, NULL)
    `).bind(userId, auth.user.company_id, email, passwordHash, ts, ts).run();
  } catch (err) {
    console.error("Create user error:", err);
    return jsonResponse(request, env, { ok: false, error: "Não foi possível criar o usuário." }, 400);
  }

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "user_created",
    targetType: "user",
    targetId: userId,
    message: `Colaborador criado: ${email}`,
  });

  return jsonResponse(request, env, {
    ok: true,
    user: {
      id: userId,
      email,
      temporary_password: tempPassword,
    },
  });
}

async function handleDeleteUser(request, env) {
  const auth = await requireRole(request, env, "owner");
  if (auth.response) return auth.response;
  const readonlyResponse = requireWritableOwnerCompany(request, env, auth);
  if (readonlyResponse) return readonlyResponse;

  if (auth.user.must_change_password) {
    return jsonResponse(request, env, { ok: false, error: "Troca de senha obrigatória." }, 403);
  }

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);

  const userId = String(body.user_id || "");
  const confirm = String(body.confirm || "");

  if (!isSafeId(userId)) {
    return jsonResponse(request, env, { ok: false, error: "Usuário inválido." }, 400);
  }

  if (confirm !== "yes") {
    return jsonResponse(request, env, { ok: false, error: "Confirmação exigida.", requires_confirmation: true }, 400);
  }

  if (userId === auth.user.id) {
    return jsonResponse(request, env, { ok: false, error: "Você não pode remover a si mesmo." }, 400);
  }

  const target = await env.db.prepare(`
    SELECT id, email, role, company_id
    FROM users
    WHERE id = ?
    LIMIT 1
  `).bind(userId).first();

  if (!target || target.company_id !== auth.user.company_id) {
    return jsonResponse(request, env, { ok: false, error: "Usuário não encontrado nesta empresa." }, 404);
  }

  if (target.role !== "member") {
    return jsonResponse(request, env, { ok: false, error: "Você só pode remover colaboradores comuns." }, 400);
  }

  const ts = now();

  await env.db.batch([
    env.db.prepare(`
      UPDATE users
      SET disabled = 1
      WHERE id = ?
        AND company_id = ?
        AND role = 'member'
    `).bind(userId, auth.user.company_id),

    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id = ?
    `).bind(ts, userId),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "user_delete_requested",
    targetType: "user",
    targetId: userId,
    message: `Exclusão sincronizada solicitada para colaborador: ${target.email}`,
  });

  const job = await enqueueDeletionJob(env, auth.user, {
    targetType: "member",
    targetId: userId,
    companyId: auth.user.company_id,
    targetEmail: target.email,
  });
  const reconciliation = await reconcileDeletionJob(env, job);
  return jsonResponse(
    request,
    env,
    { ok: true, deletion_status: reconciliation.status, job_id: job.id },
    reconciliation.status === "completed" ? 200 : 202
  );
}

async function handleToggleUser(request, env) {
  const auth = await requireRole(request, env, "owner");
  if (auth.response) return auth.response;
  const readonlyResponse = requireWritableOwnerCompany(request, env, auth);
  if (readonlyResponse) return readonlyResponse;

  if (auth.user.must_change_password) {
    return jsonResponse(request, env, { ok: false, error: "Troca de senha obrigatória." }, 403);
  }

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);
  const userId = String(body.user_id || "");
  const intent = String(body.intent || "");
  const confirm = String(body.confirm || "");

  if (!isSafeId(userId) || !["disable", "enable"].includes(intent)) {
    return jsonResponse(request, env, { ok: false, error: "Usuário inválido." }, 400);
  }

  if (confirm !== "yes") {
    return jsonResponse(request, env, { ok: false, error: "Confirmação exigida.", requires_confirmation: true }, 400);
  }

  if (userId === auth.user.id) {
    return jsonResponse(request, env, { ok: false, error: "Você não pode alterar a si mesmo." }, 400);
  }

  const target = await env.db.prepare(`
    SELECT id, email, role, company_id
    FROM users
    WHERE id = ?
    LIMIT 1
  `).bind(userId).first();

  if (!target || target.company_id !== auth.user.company_id) {
    return jsonResponse(request, env, { ok: false, error: "Usuário não encontrado nesta empresa." }, 404);
  }

  if (target.role !== "member") {
    return jsonResponse(request, env, { ok: false, error: "Você só pode alterar colaboradores comuns." }, 400);
  }

  const disabled = intent === "disable" ? 1 : 0;
  const ts = now();

  if (intent === "disable") {
    const stopResult = await stopPythonRunsForUser(env, {
      id: target.id,
      email: target.email,
      role: "member",
      company_id: auth.user.company_id,
      company_name: auth.user.company_name || "",
    });

    if (!stopResult.ok) {
      return jsonResponse(request, env, {
        ok: false,
        error: "Não foi possível parar as runs ativas do colaborador. Tente novamente antes de desativar.",
        detail: stopResult.error,
      }, 502);
    }
  }

  await env.db.batch([
    env.db.prepare(`
      UPDATE users
      SET disabled = ?
      WHERE id = ?
        AND company_id = ?
        AND role = 'member'
    `).bind(disabled, userId, auth.user.company_id),

    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id = ?
    `).bind(ts, userId),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: intent === "disable" ? "user_disabled" : "user_enabled",
    targetType: "user",
    targetId: userId,
    message: `${intent === "disable" ? "Colaborador desativado" : "Colaborador reativado"}: ${target.email}`,
  });

  return jsonResponse(request, env, { ok: true });
}

async function handleResetUserPassword(request, env) {
  const auth = await requireRole(request, env, "owner");
  if (auth.response) return auth.response;
  const readonlyResponse = requireWritableOwnerCompany(request, env, auth);
  if (readonlyResponse) return readonlyResponse;

  if (auth.user.must_change_password) {
    return jsonResponse(request, env, { ok: false, error: "Troca de senha obrigatória." }, 403);
  }

  const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
  if (!csrfOk) {
    return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
  }

  const body = await readBody(request);

  const userId = String(body.user_id || "");
  const confirm = String(body.confirm || "");

  if (!isSafeId(userId)) {
    return jsonResponse(request, env, { ok: false, error: "Usuário inválido." }, 400);
  }

  if (confirm !== "yes") {
    return jsonResponse(request, env, { ok: false, error: "Confirmação exigida.", requires_confirmation: true }, 400);
  }

  const target = await env.db.prepare(`
    SELECT id, email, role, company_id, disabled
    FROM users
    WHERE id = ?
    LIMIT 1
  `).bind(userId).first();

  if (!target || target.company_id !== auth.user.company_id) {
    return jsonResponse(request, env, { ok: false, error: "Usuário não encontrado nesta empresa." }, 404);
  }

  if (target.role !== "member") {
    return jsonResponse(request, env, { ok: false, error: "Você só pode resetar senha de colaboradores comuns." }, 400);
  }

  if (Number(target.disabled) === 1) {
    return jsonResponse(request, env, { ok: false, error: "Usuário está desativado." }, 400);
  }

  const tempPassword = generateTemporaryPassword();
  const passwordHash = await hashPassword(tempPassword, env);
  const ts = now();

  await env.db.batch([
    env.db.prepare(`
      UPDATE users
      SET password_hash = ?,
          must_change_password = 1,
          password_changed_at = ?
      WHERE id = ?
        AND company_id = ?
        AND role = 'member'
    `).bind(passwordHash, ts, userId, auth.user.company_id),

    env.db.prepare(`
      UPDATE sessions
      SET revoked_at = ?
      WHERE user_id = ?
    `).bind(ts, userId),
  ]);

  await logEvent(env, request, {
    actor: auth.user,
    companyId: auth.user.company_id,
    action: "user_password_reset",
    targetType: "user",
    targetId: userId,
    message: `Senha resetada: ${target.email}`,
  });

  return jsonResponse(request, env, {
    ok: true,
    user: {
      id: userId,
      email: target.email,
      temporary_password: tempPassword,
    },
  });
}

/* =========================
   PROXY FASTAPI
========================= */

async function handlePythonProxy(request, env) {
  const auth = await requireAuth(request, env);

  if (auth.response) {
    return auth.response;
  }

  if (hasRequestBody(request.method)) {
    const csrfOk = await verifyRequestCsrf(request, auth.session.csrf_hash);
    if (!csrfOk) {
      return jsonResponse(request, env, { ok: false, error: "CSRF inválido." }, 403);
    }
  }

  if (auth.user.role !== "member") {
    return jsonResponse(request, env, {
      ok: false,
      error: "A área ISS Fortaleza é exclusiva para colaboradores.",
    }, 403);
  }

  if (auth.user.must_change_password) {
    return jsonResponse(request, env, {
      ok: false,
      error: "Troca de senha obrigatória.",
    }, 403);
  }

  if (!env.PYTHON_API_URL) {
    return jsonResponse(request, env, {
      ok: false,
      error: "PYTHON_API_URL não configurado.",
    }, 500);
  }

  const url = new URL(request.url);

  const path = url.pathname.slice("/py".length) || "/";
  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  const targetUrl = `${base}${path}${url.search}`;

  const headers = new Headers();

  const contentType = request.headers.get("Content-Type");
  if (contentType) {
    headers.set("Content-Type", contentType);
  }

  const accept = request.headers.get("Accept");
  if (accept) {
    headers.set("Accept", accept);
  }

  headers.set("X-Internal-Secret", env.ISS_INTERNAL_SECRET || "");

  headers.set("X-Company-Id", auth.user.company_id);
  headers.set("X-Company-Name", auth.user.company_name || "");

  headers.set("X-User-Id", auth.user.id);
  headers.set("X-User-Email", auth.user.email);
  headers.set("X-User-Role", auth.user.role);

  const init = {
    method: request.method,
    headers,
  };

  if (!["GET", "HEAD"].includes(request.method)) {
    init.body = await request.arrayBuffer();
  }

  let upstreamResponse;

  try {
    upstreamResponse = await fetch(targetUrl, init);
  } catch (err) {
    console.error("PY PROXY ERROR:", err);

    return jsonResponse(request, env, {
      ok: false,
      error: String(err?.message || err),
      targetUrl,
    }, 500);
  }

  const responseHeaders = apiHeaders(request, env, {});

  const upstreamContentType = upstreamResponse.headers.get("Content-Type");
  if (upstreamContentType) {
    responseHeaders.set("Content-Type", upstreamContentType);
  }

  const contentDisposition = upstreamResponse.headers.get("Content-Disposition");
  if (contentDisposition) {
    responseHeaders.set("Content-Disposition", contentDisposition);
  }

  return new Response(upstreamResponse.body, {
    status: upstreamResponse.status,
    headers: responseHeaders,
  });
}

function pythonHeaders(env, user) {
  const headers = new Headers();
  headers.set("X-Internal-Secret", env.ISS_INTERNAL_SECRET || "");
  headers.set("X-Company-Id", user.company_id || "");
  headers.set("X-Company-Name", user.company_name || "");
  headers.set("X-User-Id", user.id || "");
  headers.set("X-User-Email", user.email || "");
  headers.set("X-User-Role", user.role || "member");
  return headers;
}

async function stopPythonRunsForUser(env, user) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return { ok: false, error: "PYTHON_API_URL ou ISS_INTERNAL_SECRET não configurado." };
  }

  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  const headers = pythonHeaders(env, user);

  try {
    const res = await fetch(`${base}/api/runs/stop-all`, {
      method: "POST",
      headers,
    });

    if (!res.ok) {
      const text = await res.text();
      return { ok: false, error: text || `HTTP ${res.status}` };
    }

    return { ok: true };
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function stopPythonRunsForCompany(env, actor, companyId) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return { ok: false, error: "PYTHON_API_URL ou ISS_INTERNAL_SECRET não configurado." };
  }

  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  const headers = pythonHeaders(env, actor);

  try {
    const res = await fetch(`${base}/api/admin/company-runs/stop?company_id=${encodeURIComponent(companyId)}`, {
      method: "POST",
      headers,
    });
    if (!res.ok) return { ok: false, error: await res.text() };
    return { ok: true, ...(await res.json()) };
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function enqueueDeletionJob(env, actor, { targetType, targetId, companyId, targetEmail = "" }) {
  const existing = await env.db.prepare(`
    SELECT *
    FROM deletion_jobs
    WHERE target_type = ?
      AND target_id = ?
    LIMIT 1
  `).bind(targetType, targetId).first();

  if (existing && existing.status !== "completed") {
    return existing;
  }

  const ts = now();
  const id = randomId();
  await env.db.prepare(`
    INSERT INTO deletion_jobs (
      id, target_type, target_id, company_id, target_email, status,
      attempts, last_error, requested_by_user_id, requested_by_email,
      created_at, updated_at, completed_at
    )
    VALUES (?, ?, ?, ?, ?, 'pending', 0, NULL, ?, ?, ?, ?, NULL)
    ON CONFLICT(target_type, target_id) DO UPDATE SET
      id = excluded.id,
      company_id = excluded.company_id,
      target_email = excluded.target_email,
      status = 'pending',
      attempts = 0,
      last_error = NULL,
      requested_by_user_id = excluded.requested_by_user_id,
      requested_by_email = excluded.requested_by_email,
      created_at = excluded.created_at,
      updated_at = excluded.updated_at,
      completed_at = NULL
  `).bind(
    id,
    targetType,
    targetId,
    companyId,
    targetEmail,
    actor.id || null,
    actor.email || null,
    ts,
    ts
  ).run();

  return env.db.prepare(`SELECT * FROM deletion_jobs WHERE id = ? LIMIT 1`).bind(id).first();
}

async function reconcileDeletionJobs(env) {
  await migrate(env.db);
  const ts = now();
  await env.db.prepare(`
    DELETE FROM deletion_jobs
    WHERE status = 'completed'
      AND completed_at < ?
  `).bind(ts - 30 * 24 * 60 * 60).run();

  const result = await env.db.prepare(`
    SELECT *
    FROM deletion_jobs
    WHERE status IN ('pending', 'stopping', 'failed')
    ORDER BY updated_at ASC
    LIMIT 25
  `).all();

  for (const job of result.results || []) {
    await reconcileDeletionJob(env, job);
  }
}

async function reconcileDeletionJob(env, job) {
  const actor = {
    id: job.requested_by_user_id || "system",
    email: job.requested_by_email || "system@prumosistemas",
    role: "master",
    company_id: job.company_id,
    company_name: job.company_id,
  };
  const ts = now();

  await env.db.prepare(`
    UPDATE deletion_jobs
    SET status = 'stopping',
        attempts = attempts + 1,
        updated_at = ?,
        last_error = NULL
    WHERE id = ?
  `).bind(ts, job.id).run();

  const python = job.target_type === "member"
    ? await deletePythonMemberData(env, actor, job.company_id, job.target_id)
    : await deletePythonCompanyData(env, actor, job.company_id);

  if (!python.ok) {
    await env.db.prepare(`
      UPDATE deletion_jobs
      SET status = 'failed',
          last_error = ?,
          updated_at = ?
      WHERE id = ?
    `).bind(String(python.error || "Falha desconhecida."), now(), job.id).run();
    return { ok: false, status: "failed", error: python.error };
  }

  if (!python.completed) {
    await env.db.prepare(`
      UPDATE deletion_jobs
      SET status = 'stopping',
          updated_at = ?
      WHERE id = ?
    `).bind(now(), job.id).run();
    return { ok: true, status: "stopping", python };
  }

  if (job.target_type === "member") {
    await env.db.batch([
      env.db.prepare(`DELETE FROM sessions WHERE user_id = ?`).bind(job.target_id),
      env.db.prepare(`DELETE FROM users WHERE id = ? AND company_id = ? AND role = 'member'`).bind(job.target_id, job.company_id),
    ]);
  } else {
    await env.db.batch([
      env.db.prepare(`DELETE FROM logs WHERE company_id = ?`).bind(job.company_id),
      env.db.prepare(`
        DELETE FROM sessions
        WHERE user_id IN (SELECT id FROM users WHERE company_id = ?)
      `).bind(job.company_id),
      env.db.prepare(`DELETE FROM users WHERE company_id = ?`).bind(job.company_id),
      env.db.prepare(`DELETE FROM companies WHERE id = ?`).bind(job.company_id),
    ]);
  }

  await env.db.prepare(`
    UPDATE deletion_jobs
    SET status = 'completed',
        completed_at = ?,
        updated_at = ?,
        last_error = NULL
    WHERE id = ?
  `).bind(now(), now(), job.id).run();

  await logSystemEvent(env, {
    action: job.target_type === "member" ? "user_delete_completed" : "company_delete_completed",
    targetType: job.target_type,
    targetId: job.target_id,
    message: `${job.target_type === "member" ? "Colaborador" : "Empresa"} removido com sincronização concluída: ${job.target_email || job.target_id}`,
  });

  return { ok: true, status: "completed", python };
}

/* =========================
   CLEANUP
========================= */

async function cleanupTemporaryData(db) {
  if (Math.random() > 0.1) return;

  const ts = now();

  await db.batch([
    db.prepare(`
      DELETE FROM rate_limits
      WHERE reset_at < ?
    `).bind(ts),

    db.prepare(`
      DELETE FROM sessions
      WHERE absolute_expires_at < ?
         OR (revoked_at IS NOT NULL AND revoked_at < ?)
    `).bind(ts, ts - 3600),

    db.prepare(`
      DELETE FROM logs
      WHERE created_at < ?
    `).bind(ts - LOG_RETENTION_SECONDS),

    db.prepare(`
      DELETE FROM deletion_jobs
      WHERE status = 'completed'
        AND completed_at < ?
    `).bind(ts - 30 * 24 * 60 * 60),
  ]);
}

/* =========================
   RATE LIMIT
========================= */

async function checkRateLimit(db, key, limit, windowSeconds) {
  const nowTs = now();

  const row = await db.prepare(`
    SELECT count, reset_at
    FROM rate_limits
    WHERE key = ?
    LIMIT 1
  `).bind(key).first();

  if (!row) {
    await db.prepare(`
      INSERT INTO rate_limits (key, count, reset_at)
      VALUES (?, 1, ?)
    `).bind(key, nowTs + windowSeconds).run();

    return { ok: true };
  }

  if (nowTs >= Number(row.reset_at)) {
    await db.prepare(`
      UPDATE rate_limits
      SET count = 1, reset_at = ?
      WHERE key = ?
    `).bind(nowTs + windowSeconds, key).run();

    return { ok: true };
  }

  if (Number(row.count) >= limit) {
    return {
      ok: false,
      retryAfter: Math.max(1, Number(row.reset_at) - nowTs),
    };
  }

  await db.prepare(`
    UPDATE rate_limits
    SET count = count + 1
    WHERE key = ?
  `).bind(key).run();

  return { ok: true };
}

/* =========================
   AUTH / SESSION
========================= */

async function requireAuth(request, env) {
  const auth = await getAuth(request, env);

  if (!auth.ok) {
    return {
      response: jsonResponse(
        request,
        env,
        { ok: false, authenticated: false, error: "Não autenticado." },
        401,
        auth.clearCookie ? clearSessionCookie(env) : null
      ),
    };
  }

  return auth;
}

async function requireRole(request, env, role) {
  const auth = await requireAuth(request, env);

  if (auth.response) return auth;

  if (auth.user.role !== role) {
    return {
      response: jsonResponse(request, env, { ok: false, error: "Permissão negada." }, 403),
    };
  }

  return auth;
}

function requireWritableOwnerCompany(request, env, auth) {
  if (auth.user.role === "owner" && auth.user.company_disabled) {
    return jsonResponse(request, env, {
      ok: false,
      error: "Empresa desativada. O administrador possui acesso somente leitura.",
      company_disabled: true,
    }, 423);
  }
  return null;
}

async function createSession(request, env, userId) {
  const cfg = getConfig(env);

  await pruneOldSessionsForUser(env.db, userId, MAX_SESSIONS_PER_USER - 1);

  const rawToken = randomBase64Url(32);
  const sessionHash = await sha256Hex(rawToken);

  const csrfToken = randomBase64Url(32);
  const csrfHash = await sha256Hex(csrfToken);

  const userAgent = request.headers.get("User-Agent") || "";
  const userAgentHash = await sha256Hex(userAgent);

  const ts = now();
  const absoluteExpiresAt = ts + cfg.absoluteTtlSeconds;

  await env.db.prepare(`
    INSERT INTO sessions (
      session_hash,
      user_id,
      created_at,
      last_seen_at,
      absolute_expires_at,
      revoked_at,
      user_agent_hash,
      csrf_hash
    )
    VALUES (?, ?, ?, ?, ?, NULL, ?, ?)
  `).bind(
    sessionHash,
    userId,
    ts,
    ts,
    absoluteExpiresAt,
    userAgentHash,
    csrfHash
  ).run();

  return {
    rawToken,
    sessionHash,
    csrfToken,
  };
}

async function pruneOldSessionsForUser(db, userId, keepCount) {
  await db.prepare(`
    DELETE FROM sessions
    WHERE user_id = ?
      AND session_hash NOT IN (
        SELECT session_hash
        FROM sessions
        WHERE user_id = ?
        ORDER BY created_at DESC
        LIMIT ?
      )
  `).bind(userId, userId, keepCount).run();
}

async function getAuth(request, env) {
  const cfg = getConfig(env);

  const cookieHeader = request.headers.get("Cookie") || "";
  const cookies = parseCookies(cookieHeader);
  const authHeader = request.headers.get("Authorization") || "";
  const bearerMatch = authHeader.match(/^Bearer\s+(.+)$/i);
  const rawToken = cookies[SESSION_COOKIE] || (bearerMatch ? bearerMatch[1].trim() : "");

  if (!rawToken) {
    return { ok: false, clearCookie: false };
  }

  const sessionHash = await sha256Hex(rawToken);
  const ts = now();

  const row = await env.db.prepare(`
    SELECT
      s.session_hash AS session_hash,
      s.user_id AS user_id,
      s.created_at AS created_at,
      s.last_seen_at AS last_seen_at,
      s.absolute_expires_at AS absolute_expires_at,
      s.revoked_at AS revoked_at,
      s.user_agent_hash AS user_agent_hash,
      s.csrf_hash AS csrf_hash,

      u.id AS uid,
      u.company_id AS company_id,
      u.email AS email,
      u.role AS role,
      u.must_change_password AS must_change_password,
      u.disabled AS disabled,
      u.password_changed_at AS password_changed_at,
      u.last_login_at AS last_login_at,
      u.current_login_at AS current_login_at,

      c.name AS company_name,
      c.disabled AS company_disabled
    FROM sessions s
    JOIN users u ON u.id = s.user_id
    JOIN companies c ON c.id = u.company_id
    WHERE s.session_hash = ?
    LIMIT 1
  `).bind(sessionHash).first();

  if (!row) {
    return { ok: false, clearCookie: true };
  }

  const currentUserAgentHash = await sha256Hex(
    request.headers.get("User-Agent") || ""
  );

  const userAgentChanged =
    row.user_agent_hash &&
    row.user_agent_hash !== currentUserAgentHash;

  const revoked = row.revoked_at !== null && row.revoked_at !== undefined;
  const disabled = Number(row.disabled) === 1;
  const companyDisabled = Number(row.company_disabled) === 1;
  const absoluteExpired = ts >= Number(row.absolute_expires_at);
  const idleExpired = ts - Number(row.last_seen_at) > cfg.idleTtlSeconds;
  const passwordChangedAfterLogin =
    Number(row.password_changed_at || 0) >
    Number(row.created_at || 0) + SESSION_PASSWORD_CHANGE_TOLERANCE_SECONDS;

  if (
    revoked ||
    disabled ||
    (companyDisabled && row.role !== "owner") ||
    absoluteExpired ||
    idleExpired ||
    passwordChangedAfterLogin ||
    userAgentChanged
  ) {
    await revokeSession(env.db, row.session_hash);
    return { ok: false, clearCookie: true };
  }

  if (ts - Number(row.last_seen_at) > 60) {
    await env.db.prepare(`
      UPDATE sessions
      SET last_seen_at = ?
      WHERE session_hash = ?
    `).bind(ts, row.session_hash).run();

    row.last_seen_at = ts;
  }

  return {
    ok: true,
    clearCookie: false,
    user: {
      id: row.uid,
      company_id: row.company_id,
      company_name: row.company_name,
      email: row.email,
      role: row.role,
      must_change_password: Number(row.must_change_password) === 1,
      company_disabled: companyDisabled,
      last_login_at: row.last_login_at === null || row.last_login_at === undefined
        ? null
        : Number(row.last_login_at),
      current_login_at: row.current_login_at === null || row.current_login_at === undefined
        ? null
        : Number(row.current_login_at),
    },
    session: {
      session_hash: row.session_hash,
      user_id: row.user_id,
      created_at: Number(row.created_at),
      last_seen_at: Number(row.last_seen_at),
      absolute_expires_at: Number(row.absolute_expires_at),
      csrf_hash: row.csrf_hash,
    },
  };
}

async function revokeSession(db, sessionHash) {
  await db.prepare(`
    UPDATE sessions
    SET revoked_at = ?
    WHERE session_hash = ?
  `).bind(now(), sessionHash).run();
}

async function issueCsrfForSession(env, sessionHash) {
  const csrfToken = randomBase64Url(32);
  const csrfHash = await sha256Hex(csrfToken);

  await env.db.prepare(`
    UPDATE sessions
    SET csrf_hash = ?
    WHERE session_hash = ?
  `).bind(csrfHash, sessionHash).run();

  return csrfToken;
}

async function verifyRequestCsrf(request, expectedHash) {
  const token = request.headers.get("X-CSRF-Token") || "";
  return verifyCsrf(token, expectedHash);
}

async function verifyCsrf(csrfToken, expectedHash) {
  if (!csrfToken || !expectedHash) return false;

  const actualHash = await sha256Hex(csrfToken);
  return timingSafeEqualString(actualHash, expectedHash);
}

/* =========================
   LOGS / LISTS
========================= */

async function logEvent(env, request, data) {
  try {
    const actor = data.actor || {};
    const ts = now();

    await env.db.prepare(`
      INSERT INTO logs (
        id,
        actor_user_id,
        actor_email,
        actor_role,
        company_id,
        action,
        target_type,
        target_id,
        message,
        ip,
        created_at
      )
      VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    `).bind(
      randomId(),
      actor.id || null,
      actor.email || null,
      actor.role || null,
      data.companyId || actor.company_id || null,
      String(data.action || "unknown"),
      String(data.targetType || ""),
      String(data.targetId || ""),
      String(data.message || ""),
      getClientIp(request),
      ts
    ).run();
  } catch (err) {
    console.error("logEvent failed:", err);
  }
}

async function logSystemEvent(env, data) {
  try {
    const master = await env.db.prepare(`
      SELECT id, company_id, email
      FROM users
      WHERE role = 'master'
      LIMIT 1
    `).first();
    await env.db.prepare(`
      INSERT INTO logs (
        id, actor_user_id, actor_email, actor_role, company_id,
        action, target_type, target_id, message, ip, created_at
      )
      VALUES (?, ?, ?, 'master', ?, ?, ?, ?, ?, 'cron', ?)
    `).bind(
      randomId(),
      master?.id || null,
      master?.email || "system@prumosistemas",
      master?.company_id || null,
      String(data.action || "system"),
      String(data.targetType || ""),
      String(data.targetId || ""),
      String(data.message || ""),
      now()
    ).run();
  } catch (err) {
    console.error("logSystemEvent failed:", err);
  }
}

async function getRecentLogs(db) {
  const result = await db.prepare(`
    SELECT
      l.created_at,
      l.actor_email,
      l.actor_role,
      l.action,
      l.message,
      l.ip,
      c.name AS company_name
    FROM logs l
    LEFT JOIN companies c ON c.id = l.company_id
    ORDER BY l.created_at DESC
    LIMIT ?
  `).bind(MAX_LOGS_ON_SCREEN).all();

  return result.results || [];
}

async function getCompaniesForMaster(db) {
  const result = await db.prepare(`
    SELECT
      c.id,
      c.name,
      c.created_at,
      c.disabled,
      u.id AS owner_id,
      u.email AS owner_email,
      u.last_login_at AS owner_last_login_at,
      u.current_login_at AS owner_current_login_at,
      u.disabled AS owner_disabled
    FROM companies c
    LEFT JOIN users u ON u.company_id = c.id AND u.role = 'owner'
    WHERE NOT EXISTS (
      SELECT 1
      FROM users master_user
      WHERE master_user.company_id = c.id
        AND master_user.role = 'master'
    )
    ORDER BY c.created_at DESC
  `).all();

  return result.results || [];
}

async function getUsersGroupedByCompany(db) {
  const result = await db.prepare(`
    SELECT
      id,
      company_id,
      email,
      role,
      disabled,
      created_at,
      last_login_at,
      current_login_at
    FROM users
    ORDER BY created_at ASC
  `).all();

  const map = new Map();
  for (const user of result.results || []) {
    const list = map.get(user.company_id) || [];
    list.push({
      id: user.id,
      email: user.email,
      role: user.role,
      disabled: user.disabled,
      created_at: user.created_at,
      last_login_at: user.last_login_at,
      current_login_at: user.current_login_at,
    });
    map.set(user.company_id, list);
  }
  return map;
}

async function getActiveDeletionJobs(db) {
  const result = await db.prepare(`
    SELECT id, target_type, target_id, company_id, status, last_error, updated_at
    FROM deletion_jobs
    WHERE status != 'completed'
  `).all();
  const jobs = new Map();
  for (const job of result.results || []) {
    jobs.set(`${job.target_type}:${job.target_id}`, job);
  }
  return jobs;
}

async function getBillingSettings(db) {
  const defaults = {
    pix_key: "",
    pix_copy_paste: "",
    qr_code_url: "",
    monthly_amount: "",
  };
  const result = await db.prepare("SELECT key, value, updated_at FROM billing_settings").all();
  for (const row of result.results || []) {
    if (Object.prototype.hasOwnProperty.call(defaults, row.key)) {
      defaults[row.key] = row.value || "";
      defaults[`${row.key}_updated_at`] = row.updated_at || null;
    }
  }
  return defaults;
}

async function getBillingPayments(db, { companyId = null, limit = 100 } = {}) {
  const safeLimit = Math.max(1, Math.min(300, Number(limit) || 100));
  const sql = `
    SELECT
      p.*,
      c.name AS company_name,
      u.email AS current_user_email
    FROM payments p
    LEFT JOIN companies c ON c.id = p.company_id
    LEFT JOIN users u ON u.id = p.user_id
    ${companyId ? "WHERE p.company_id = ?" : ""}
    ORDER BY p.active_until DESC, p.paid_at DESC, p.created_at DESC
    LIMIT ?
  `;
  const stmt = db.prepare(sql);
  const result = companyId
    ? await stmt.bind(companyId, safeLimit).all()
    : await stmt.bind(safeLimit).all();
  return (result.results || []).map((row) => ({
    ...row,
    user_email: row.user_email || row.current_user_email || "",
  }));
}

function summarizeBilling(payments) {
  const paid = (payments || []).filter((item) => item.status === "paid");
  const activeUntil = paid.reduce((max, item) => Math.max(max, Number(item.active_until || 0)), 0);
  return {
    active_until: activeUntil || null,
    active: activeUntil ? activeUntil >= now() : false,
    payments_count: paid.length,
    latest_payment: paid[0] || null,
  };
}

function normalizeBillingMonth(value) {
  const text = String(value || "").trim();
  const match = text.match(/^(\d{4})-(\d{2})$/);
  if (!match) return "";
  const month = Number(match[2]);
  if (month < 1 || month > 12) return "";
  return `${match[1]}-${match[2]}`;
}

function normalizeBillingActiveUntil(value, periodStart, months) {
  const explicit = parseUnixDate(value);
  if (explicit) return explicit;
  const [year, month] = periodStart.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1 + months, 0, 23, 59, 59));
  return Math.floor(date.getTime() / 1000);
}

function parseUnixDate(value) {
  const text = String(value || "").trim();
  if (!text) return 0;
  if (/^\d+$/.test(text)) {
    const n = Number(text);
    return n > 10_000_000_000 ? Math.floor(n / 1000) : n;
  }
  const parsed = Date.parse(text.length === 10 ? `${text}T23:59:59Z` : text);
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : 0;
}

function parseAmountCents(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.max(0, Math.round(value > 1000 ? value : value * 100));
  }
  const text = String(value || "").trim().replace(/\./g, "").replace(",", ".");
  const number = Number(text);
  return Number.isFinite(number) ? Math.max(0, Math.round(number * 100)) : 0;
}

async function fetchPythonCompanyStats(env, actor, companyId) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return null;
  }

  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  const headers = pythonHeaders(env, actor);

  try {
    const res = await fetch(`${base}/api/admin/company-summary?company_id=${encodeURIComponent(companyId)}`, {
      method: "GET",
      headers,
    });

    if (!res.ok) {
      return { ok: false, error: await res.text() };
    }

    const data = await res.json();
    return data.summary || null;
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function fetchPythonCompanyDetail(env, actor, companyId) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return null;
  }
  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  try {
    const res = await fetch(`${base}/api/admin/company-detail?company_id=${encodeURIComponent(companyId)}`, {
      method: "GET",
      headers: pythonHeaders(env, actor),
    });
    if (!res.ok) return { ok: false, error: await res.text() };
    const data = await res.json();
    return data.detail || null;
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function fetchPythonSystemMetrics(env, actor, range) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return { ok: false, error: "PYTHON_API_URL ou ISS_INTERNAL_SECRET não configurado." };
  }
  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  try {
    const res = await fetch(`${base}/api/admin/system-metrics?range=${encodeURIComponent(range)}`, {
      method: "GET",
      headers: pythonHeaders(env, actor),
    });
    if (!res.ok) return { ok: false, error: await res.text() };
    return await res.json();
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function deletePythonMemberData(env, actor, companyId, userId) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return { ok: false, error: "PYTHON_API_URL ou ISS_INTERNAL_SECRET não configurado." };
  }
  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  try {
    const res = await fetch(`${base}/api/admin/member-data/delete?company_id=${encodeURIComponent(companyId)}&user_id=${encodeURIComponent(userId)}`, {
      method: "POST",
      headers: pythonHeaders(env, actor),
    });
    if (![200, 202].includes(res.status)) return { ok: false, error: await res.text() };
    return { ok: true, ...(await res.json()) };
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function deletePythonCompanyData(env, actor, companyId) {
  if (!env.PYTHON_API_URL || !env.ISS_INTERNAL_SECRET) {
    return { ok: false, error: "PYTHON_API_URL ou ISS_INTERNAL_SECRET não configurado." };
  }

  const base = String(env.PYTHON_API_URL).replace(/\/+$/, "");
  const headers = pythonHeaders(env, actor);

  try {
    const res = await fetch(`${base}/api/admin/company-data/delete?company_id=${encodeURIComponent(companyId)}`, {
      method: "POST",
      headers,
    });

    if (![200, 202].includes(res.status)) {
      return { ok: false, error: await res.text() };
    }

    const data = await res.json();
    return { ok: true, ...data };
  } catch (err) {
    return { ok: false, error: String(err?.message || err) };
  }
}

async function getCompanyUsers(db, companyId) {
  const result = await db.prepare(`
    SELECT
      id,
      email,
      role,
      disabled,
      created_at,
      last_login_at,
      current_login_at
    FROM users
    WHERE company_id = ?
    ORDER BY
      CASE role
        WHEN 'owner' THEN 0
        WHEN 'member' THEN 1
        ELSE 2
      END,
      created_at ASC
  `).bind(companyId).all();

  return result.results || [];
}

/* =========================
   PASSWORD HASHING
========================= */

async function hashPassword(password, env) {
  const cfg = getConfig(env);

  const salt = randomBytes(16);
  const hash = await pbkdf2Sha256(password, salt, cfg.pbkdf2Iterations);

  return [
    "pbkdf2_sha256",
    String(cfg.pbkdf2Iterations),
    bytesToBase64Url(salt),
    bytesToBase64Url(hash),
  ].join("$");
}

async function verifyPassword(password, stored) {
  try {
    const parts = String(stored || "").split("$");

    if (parts.length !== 4) return false;

    const [algo, iterStr, saltB64, hashB64] = parts;

    if (algo !== "pbkdf2_sha256") return false;

    const iterations = Number(iterStr);

    if (!Number.isFinite(iterations) || iterations < 100000 || iterations > 100000) {
      return false;
    }

    const salt = base64UrlToBytes(saltB64);
    const expected = base64UrlToBytes(hashB64);
    const actual = await pbkdf2Sha256(password, salt, iterations);

    return timingSafeEqualBytes(actual, expected);
  } catch {
    return false;
  }
}

async function fakePasswordDelay(env) {
  await hashPassword("fake-password-" + randomBase64Url(8), env);
}

async function pbkdf2Sha256(password, salt, iterations) {
  const safeIterations = Math.min(Number(iterations) || 100000, 100000);

  const keyMaterial = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(String(password)),
    "PBKDF2",
    false,
    ["deriveBits"]
  );

  const bits = await crypto.subtle.deriveBits(
    {
      name: "PBKDF2",
      hash: "SHA-256",
      salt,
      iterations: safeIterations,
    },
    keyMaterial,
    256
  );

  return new Uint8Array(bits);
}

/* =========================
   D1 SETUP
========================= */

async function migrate(db) {
  await db.batch([
    db.prepare(`
      CREATE TABLE IF NOT EXISTS companies (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0
      )
    `),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK(role IN ('master', 'owner', 'member')),
        must_change_password INTEGER NOT NULL DEFAULT 0,
        created_at INTEGER NOT NULL,
        disabled INTEGER NOT NULL DEFAULT 0,
        password_changed_at INTEGER NOT NULL DEFAULT 0,
        last_login_at INTEGER,
        current_login_at INTEGER,
        FOREIGN KEY (company_id) REFERENCES companies(id)
      )
    `),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS sessions (
        session_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        last_seen_at INTEGER NOT NULL,
        absolute_expires_at INTEGER NOT NULL,
        revoked_at INTEGER,
        user_agent_hash TEXT,
        csrf_hash TEXT NOT NULL,
        FOREIGN KEY (user_id) REFERENCES users(id)
      )
    `),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS rate_limits (
        key TEXT PRIMARY KEY,
        count INTEGER NOT NULL,
        reset_at INTEGER NOT NULL
      )
    `),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS logs (
        id TEXT PRIMARY KEY,
        actor_user_id TEXT,
        actor_email TEXT,
        actor_role TEXT,
        company_id TEXT,
        action TEXT NOT NULL,
        target_type TEXT,
        target_id TEXT,
        message TEXT,
        ip TEXT,
        created_at INTEGER NOT NULL
      )
    `),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS deletion_jobs (
        id TEXT PRIMARY KEY,
        target_type TEXT NOT NULL CHECK(target_type IN ('member', 'company')),
        target_id TEXT NOT NULL,
        company_id TEXT NOT NULL,
        target_email TEXT,
        status TEXT NOT NULL CHECK(status IN ('pending', 'stopping', 'failed', 'completed')),
        attempts INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        requested_by_user_id TEXT,
        requested_by_email TEXT,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        completed_at INTEGER,
        UNIQUE(target_type, target_id)
      )
    `),

    db.prepare(`CREATE INDEX IF NOT EXISTS idx_users_email ON users(email)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_users_company_id ON users(company_id)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_sessions_user_id ON sessions(user_id)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_sessions_absolute_expires_at ON sessions(absolute_expires_at)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_sessions_revoked_at ON sessions(revoked_at)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_rate_limits_reset_at ON rate_limits(reset_at)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_logs_created_at ON logs(created_at)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_logs_company_id ON logs(company_id)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_deletion_jobs_status ON deletion_jobs(status)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_deletion_jobs_company_id ON deletion_jobs(company_id)`),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS billing_settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_at INTEGER NOT NULL,
        updated_by_user_id TEXT
      )
    `),

    db.prepare(`
      CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        company_id TEXT NOT NULL,
        user_id TEXT,
        user_email TEXT,
        period_start TEXT NOT NULL,
        months INTEGER NOT NULL DEFAULT 1,
        active_until INTEGER NOT NULL,
        amount_cents INTEGER NOT NULL DEFAULT 0,
        description TEXT,
        note TEXT,
        status TEXT NOT NULL DEFAULT 'paid',
        paid_at INTEGER NOT NULL,
        created_at INTEGER NOT NULL,
        created_by_user_id TEXT,
        FOREIGN KEY (company_id) REFERENCES companies(id),
        FOREIGN KEY (user_id) REFERENCES users(id)
      )
    `),

    db.prepare(`CREATE INDEX IF NOT EXISTS idx_payments_company_id ON payments(company_id)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_payments_user_id ON payments(user_id)`),
    db.prepare(`CREATE INDEX IF NOT EXISTS idx_payments_active_until ON payments(active_until)`),
  ]);
}

/* =========================
   RESPONSES / CORS / BODY
========================= */

async function readBody(request) {
  const contentType = request.headers.get("Content-Type") || "";

  if (contentType.includes("application/json")) {
    return await request.json();
  }

  const form = await request.formData();
  const out = {};

  for (const [key, value] of form.entries()) {
    out[key] = value;
  }

  return out;
}

function jsonResponse(request, env, data, status = 200, setCookie = null) {
  const headers = apiHeaders(request, env, {
    "Content-Type": "application/json; charset=utf-8",
  });

  if (setCookie) {
    headers.append("Set-Cookie", setCookie);
  }

  return new Response(JSON.stringify(data), {
    status,
    headers,
  });
}

function optionsResponse(request, env) {
  const origin = request.headers.get("Origin");

  if (origin && !isAllowedOrigin(request, env, origin)) {
    return new Response(null, {
      status: 403,
      headers: baseSecurityHeaders({
        "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-CSRF-Token, Authorization",
      }),
    });
  }

  return new Response(null, {
    status: 204,
    headers: apiHeaders(request, env, {
      "Access-Control-Allow-Methods": "GET,POST,PUT,PATCH,DELETE,OPTIONS",
      "Access-Control-Allow-Headers": "Content-Type, X-CSRF-Token, Authorization",
      "Access-Control-Max-Age": "86400",
    }),
  });
}

function apiHeaders(request, env, extra = {}) {
  const headers = baseSecurityHeaders(extra);

  const origin = request.headers.get("Origin");

  if (origin && isAllowedOrigin(request, env, origin)) {
    headers.set("Access-Control-Allow-Origin", origin);
    headers.set("Access-Control-Allow-Credentials", "true");
    headers.set("Vary", "Origin");
  }

  return headers;
}

function baseSecurityHeaders(extra = {}) {
  const headers = new Headers(extra);

  headers.set("X-Content-Type-Options", "nosniff");
  headers.set("Referrer-Policy", "no-referrer");
  headers.set("X-Frame-Options", "DENY");
  headers.set("Cache-Control", "no-store");
  headers.set("Access-Control-Expose-Headers", "Content-Disposition");

  return headers;
}

function isAllowedOrigin(request, env, origin) {
  let parsed;

  try {
    parsed = new URL(origin);
  } catch (_) {
    return false;
  }

  if (!["https:", "http:"].includes(parsed.protocol)) {
    return false;
  }

  const requestOrigin = new URL(request.url).origin;
  if (origin === requestOrigin) {
    return true;
  }

  for (const allowed of getAllowedOrigins(env)) {
    if (origin === allowed) {
      return true;
    }
  }

  return false;
}

function getAllowedOrigins(env) {
  const configured = String(env.FRONTEND_ORIGINS || "")
    .split(",")
    .map((item) => item.trim().replace(/\/+$/, ""))
    .filter(Boolean);

  return [...new Set(configured)];
}

function getIntEnv(env, name, fallback, min, max) {
  const parsed = Number.parseInt(String(env[name] ?? ""), 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(min, Math.min(max, parsed));
}

/* =========================
   COOKIES
========================= */

function getCookieMode(env) {
  return String(env.COOKIE_MODE || "dev-remote").trim().toLowerCase();
}

function makeSessionCookie(env, rawToken, maxAgeSeconds) {
  const mode = getCookieMode(env);

  const parts = [
    `${SESSION_COOKIE}=${rawToken}`,
    "Path=/",
    "HttpOnly",
    `Max-Age=${Number(maxAgeSeconds)}`,
  ];

  if (mode === "local") {
    parts.push("SameSite=Lax");
  } else {
    parts.push("SameSite=None");
    parts.push("Secure");
  }

  return parts.join("; ");
}

function clearSessionCookie(env) {
  const mode = getCookieMode(env);

  const parts = [
    `${SESSION_COOKIE}=`,
    "Path=/",
    "HttpOnly",
    "Max-Age=0",
  ];

  if (mode === "local") {
    parts.push("SameSite=Lax");
  } else {
    parts.push("SameSite=None");
    parts.push("Secure");
  }

  return parts.join("; ");
}

function parseCookies(cookieHeader) {
  const out = {};

  for (const part of String(cookieHeader).split(";")) {
    const trimmed = part.trim();
    if (!trimmed) continue;

    const eq = trimmed.indexOf("=");
    if (eq === -1) continue;

    const key = trimmed.slice(0, eq);
    const value = trimmed.slice(eq + 1);

    out[key] = value;
  }

  return out;
}

/* =========================
   CRYPTO
========================= */

function randomBytes(length) {
  const bytes = new Uint8Array(length);
  crypto.getRandomValues(bytes);
  return bytes;
}

function randomBase64Url(length) {
  return bytesToBase64Url(randomBytes(length));
}

function randomId() {
  return randomBase64Url(16);
}

function generateTemporaryPassword() {
  return `Tmp-${randomBase64Url(12)}!`;
}

async function sha256Hex(input) {
  const data =
    input instanceof Uint8Array
      ? input
      : new TextEncoder().encode(String(input));

  const digest = await crypto.subtle.digest("SHA-256", data);
  return bytesToHex(new Uint8Array(digest));
}

function bytesToHex(bytes) {
  return [...bytes]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

function bytesToBase64Url(bytes) {
  let binary = "";

  for (const b of bytes) {
    binary += String.fromCharCode(b);
  }

  return btoa(binary)
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/g, "");
}

function base64UrlToBytes(str) {
  const normalized = String(str)
    .replace(/-/g, "+")
    .replace(/_/g, "/");

  const padded = normalized.padEnd(
    Math.ceil(normalized.length / 4) * 4,
    "="
  );

  const binary = atob(padded);
  const bytes = new Uint8Array(binary.length);

  for (let i = 0; i < binary.length; i++) {
    bytes[i] = binary.charCodeAt(i);
  }

  return bytes;
}

function timingSafeEqualBytes(a, b) {
  if (!(a instanceof Uint8Array) || !(b instanceof Uint8Array)) return false;

  const maxLen = Math.max(a.length, b.length);
  let diff = a.length ^ b.length;

  for (let i = 0; i < maxLen; i++) {
    const av = i < a.length ? a[i] : 0;
    const bv = i < b.length ? b[i] : 0;
    diff |= av ^ bv;
  }

  return diff === 0;
}

async function timingSafeEqualString(a, b) {
  const aa = new TextEncoder().encode(String(a));
  const bb = new TextEncoder().encode(String(b));
  return timingSafeEqualBytes(aa, bb);
}

/* =========================
   UTILS
========================= */

function getConfig(env) {
  const idleTtlSeconds = Number(env.SESSION_IDLE_TTL_SECONDS || 1800);
  const absoluteTtlSeconds = Number(env.SESSION_ABSOLUTE_TTL_SECONDS || 43200);

  const pbkdf2IterationsRaw = Number(env.PBKDF2_ITERATIONS || 100000);
  const pbkdf2Iterations = Math.min(
    Number.isFinite(pbkdf2IterationsRaw) ? pbkdf2IterationsRaw : 100000,
    100000
  );

  return {
    idleTtlSeconds: Number.isFinite(idleTtlSeconds) ? idleTtlSeconds : 1800,
    absoluteTtlSeconds: Number.isFinite(absoluteTtlSeconds) ? absoluteTtlSeconds : 43200,
    pbkdf2Iterations,
  };
}

function now() {
  return Math.floor(Date.now() / 1000);
}

function normalizeEmail(email) {
  return String(email).trim().toLowerCase();
}

function isValidEmailInput(email) {
  if (typeof email !== "string") return false;
  if (email.length < 3 || email.length > MAX_EMAIL_LENGTH) return false;
  if (!email.includes("@")) return false;
  if (email.includes(" ")) return false;

  const basic = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  return basic.test(email);
}

function isValidPasswordInput(password) {
  if (typeof password !== "string") return false;
  return password.length >= 8 && password.length <= MAX_PASSWORD_LENGTH;
}

function isSafeId(id) {
  if (typeof id !== "string") return false;
  if (id.length < 10 || id.length > 80) return false;
  return /^[A-Za-z0-9_-]+$/.test(id);
}

function hasRequestBody(method) {
  return ["POST", "PUT", "PATCH", "DELETE"].includes(String(method || "").toUpperCase());
}

function getMaxPostBytes(pathname) {
  const path = String(pathname || "");

  if (path.startsWith("/py/")) {
    return MAX_PY_POST_BYTES;
  }

  return MAX_POST_BYTES;
}

function isBodyTooLarge(request, maxBytes) {
  const raw = request.headers.get("Content-Length");

  if (!raw) {
    return false;
  }

  const length = Number(raw);

  if (!Number.isFinite(length)) {
    return true;
  }

  return length > maxBytes;
}

function getClientIp(request) {
  return request.headers.get("CF-Connecting-IP") || "unknown";
}
