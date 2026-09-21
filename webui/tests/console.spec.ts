import { readFile } from "node:fs/promises";
import { extname, resolve } from "node:path";
import { expect, test as base, type Page } from "@playwright/test";
import type { Account, AccountLimits, ProxyNode } from "../src/types";

const limits: AccountLimits = {
  suspended: false, degraded: false, unmetered: false, fetched_epoch: 1790000000,
  windows: ["5h", "7d", "7d_claude", "7d_fable"].map((name) => ({
    name, label: name, length: name === "5h" ? 18000 : 604800,
    used: 30, budget: 100, reset_at: 1790012345,
    models: name === "7d_fable" ? ["claude-fable-5", "claude-fable-5-1"].map((model) => ({
      model, requests: 2, input_tokens: 100, output_tokens: 20,
      cache_read_tokens: 300, cache_write_tokens: 40, total_tokens: 460,
    })) : [],
  })),
};

function account(alias: string, extra: Partial<Account> = {}): Account {
  return {
    alias, email: `${alias}@example.com`, plan: "pro", quota: {}, last_usage: {},
    healthy: true, health: {}, disabled: false, limits: structuredClone(limits),
    profile: { name: "Remote holder", plan_expires_epoch: 1800001234 },
    ...extra,
  };
}

type Call = { path: string; method: string; body: any; headers: Record<string, string> };
type State = {
  accounts: Account[];
  nodes: ProxyNode[];
  calls: Call[];
  failures: Map<string, number>;
  modelShape: "standard" | "namespaced";
  chat: "success" | "http-error" | "stream-error" | "synthetic";
  ceiling: number;
  unexpected: string[];
};

const test = base.extend<{ state: State }>({
  state: async ({ page }, use) => {
    const state: State = {
      accounts: [
        account("work"),
        account("error", { healthy: false, health: { state: "error", status: 401, message: "invalid token" } }),
        account("banned", { healthy: false, health: { state: "suspended", status: 403, message: "contact support" } }),
        account("off", { disabled: true }),
        account("cool", { shared_quota_cooldown: 120 }),
      ],
      nodes: Array.from({ length: 35 }, (_, i) => ({
        id: `p${i}`, name: i === 1 ? "" : `出口 ${i}`, scheme: "socks5",
        host: `proxy${i}.example.com`, port: 1080, username: "proxy-user",
        password: "mock-secret", active: true, assigned: 0,
        failure_count: 0, last_error: null, last_checked: null,
      })),
      calls: [], failures: new Map(), modelShape: "standard", chat: "success",
      ceiling: 0.9, unexpected: [],
    };
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    await page.addInitScript(() => {
      localStorage.setItem("mf_proxy_key", "mock-admin-key");
      localStorage.setItem("mf_page", "accounts");
      localStorage.setItem("mf_theme", "dark");
    });
    // Every request is fulfilled locally: no relay, Vite proxy, Google, or model network.
    await page.route("**/*", async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const path = url.pathname;
      if (url.origin !== "http://console.test") {
        state.unexpected.push(request.url());
        return route.abort();
      }
      const json = (value: unknown, status = 200) =>
        route.fulfill({ status, contentType: "application/json", body: JSON.stringify(value) });
      if (/^\/(accounts|health|proxies|api|v1)(\/|$)/.test(path)) {
        const method = request.method();
        const body = request.postData() ? request.postDataJSON() : null;
        state.calls.push({ path: path + url.search, method, body, headers: request.headers() });
        const failure = state.failures.get(`${method} ${path}`);
        if (failure) {
          const alias = path.match(/^\/accounts\/([^/]+)\//)?.[1] ?? "work";
          const a = state.accounts.find((a) => a.alias === alias);
          if (a) {
            a.healthy = false;
            a.health = { state: "error", status: failure, message: "mock refusal" };
          }
          return json({ error: { message: "mock refusal" } }, failure);
        }
        if (path === "/health") return json({ ok: true, version: "test", accounts: state.accounts.length });
        if (path === "/accounts") return json({ accounts: state.accounts });
        if (path === "/proxies") return json({
          configured: true, active: state.nodes.length, total: state.nodes.length,
          assigned: 0, nodes: state.nodes,
        });
        if (path === "/api/usage") return json({
          hours: 24, totals: { requests: 0, input_tokens: 0, output_tokens: 0 }, buckets: [],
        });
        if (path === "/api/schedule") {
          if (method === "POST") state.ceiling = body.max_utilization;
          return json({ max_utilization: state.ceiling, limits_ttl: 600, policy: "reset_first_fable" });
        }
        if (path === "/api/limits/refresh" && method === "POST") {
          state.accounts[0].limits!.windows[0].used = 45;
          return json({ accounts: state.accounts.map((a) => ({ alias: a.alias, ok: true, limits: a.limits })) });
        }
        const singleRead = path.match(/^\/accounts\/([^/]+)\/(status|limits)$/);
        if (singleRead) {
          const a = state.accounts.find((a) => a.alias === singleRead[1])!;
          return json(singleRead[2] === "limits" ? a.limits : a);
        }
        const accountEdit = path.match(/^\/api\/accounts\/([^/]+)(\/enabled)?$/);
        if (accountEdit) {
          const a = state.accounts.find((a) => a.alias === accountEdit[1])!;
          if (method === "DELETE") state.accounts = state.accounts.filter((item) => item !== a);
          if (method === "PATCH") {
            a.display_name = body.display_name;
            a.proxy = state.nodes.find((p) => p.id === body.proxy_id) ?? null;
          }
          if (accountEdit[2]) a.disabled = !body.enabled;
          return json(a);
        }
        if (path === "/api/login/start") return json({ sent: true, alias: body.alias });
        if (path === "/api/login/finish") return json({ alias: body.alias, plan: "pro" });
        if (path === "/api/proxies/import") return json({ added: 2, failed: [] });
        if (path === "/api/proxies" && method === "POST") {
          const node = { ...state.nodes[0], ...body, id: "new-proxy" };
          state.nodes.push(node);
          return json(node);
        }
        const proxyEdit = path.match(/^\/api\/proxies\/([^/]+)(\/test)?$/);
        if (proxyEdit) {
          const node = state.nodes.find((p) => p.id === proxyEdit[1])!;
          if (proxyEdit[2]) return json({ ok: true, latency_ms: 32 });
          if (method === "PATCH") Object.assign(node, body);
          if (method === "DELETE") state.nodes = state.nodes.filter((p) => p !== node);
          return json({ ok: true });
        }
        if (path === "/v1/models") {
          return json(state.modelShape === "namespaced"
            ? { mirofish_model_ids: ["claude-fable-5-1"], default_model: "claude-fable-5-1" }
            : { data: [{ id: "claude-fable-5" }], default_model: "claude-fable-5" });
        }
        if (path === "/v1/chat/completions") {
          const a = state.accounts.find((a) => a.alias === request.headers()["x-mirofish-account"])!;
          if (state.chat === "synthetic") {
            return route.fulfill({
              contentType: "text/event-stream", headers: { "X-Mirofish-Synthetic": "true" },
              body: 'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\ndata: [DONE]\n\n',
            });
          }
          if (state.chat === "http-error") {
            a.healthy = false;
            a.health = { state: "error", status: 401, message: "login required" };
            return json({ error: { message: "login required" } }, 401);
          }
          if (state.chat === "stream-error") {
            a.healthy = false;
            a.health = { state: "error", status: 503, message: "capacity" };
            return route.fulfill({ contentType: "text/event-stream",
              body: 'data: {"error":{"message":"capacity","code":503}}\n\n' });
          }
          a.healthy = true;
          a.health = {};
          return route.fulfill({ contentType: "text/event-stream", body:
            'data: {"choices":[{"delta":{"content":"真实对话 mock"}}]}\n\n'
            + 'data: {"usage":{"prompt_tokens":20,"completion_tokens":5,"total_tokens":25}}\n\n'
            + 'data: [DONE]\n\n' });
        }
        state.unexpected.push(`${method} ${path}`);
        return json({ error: { message: "unexpected API" } }, 500);
      }
      const file = resolve("dist", path === "/" ? "index.html" : path.slice(1));
      const contentType = ({ ".html": "text/html", ".js": "text/javascript", ".css": "text/css" })[extname(file)];
      return route.fulfill({ contentType, body: await readFile(file) });
    });
    await page.goto("/");
    await expect(page.locator(".shell")).toBeVisible();
    await use(state);
    expect(errors).toEqual([]);
    expect(state.unexpected).toEqual([]);
  },
});

async function go(page: Page, label: string) {
  await page.locator(".nav-item").filter({ hasText: label }).click();
}

test("all pages only hydrate cached local reads; no limits/models/probes", async ({ page, state }) => {
  for (const label of ["总览", "用量", "网络", "调度", "测试", "设置", "账号"]) {
    await go(page, label);
    await page.waitForTimeout(30);
  }
  expect(state.calls.every((call) => call.method === "GET")).toBe(true);
  expect(state.calls.every((call) =>
    ["/health", "/accounts", "/proxies", "/api/usage?hours=24", "/api/schedule"].includes(call.path))).toBe(true);
});

test("account identity/status/plan stay separate and four cached windows share a row", async ({ page, state }) => {
  const rows = page.locator("table.data tbody tr");
  await expect(rows).toHaveCount(5);
  expect(await page.locator("th").allTextContents()).not.toContain("邮箱");
  const identity = rows.first().locator("td.identity");
  await expect(identity).toHaveText("workwork@example.com");
  await expect(identity).not.toContainText("Remote holder");
  for (const [index, label] of ["正常", "异常", "封停", "disabled", "窗口冷却"].entries()) {
    await expect(rows.nth(index).locator("td").nth(1)).toHaveText(label);
    await expect(rows.nth(index).locator("td").nth(3)).toHaveText("pro");
    await expect(rows.nth(index).getByRole("button")).toHaveCount(2);
  }
  await expect(rows.nth(1).locator("td").nth(1).locator(".chip")).toHaveAttribute("title", "invalid token");
  const boxes = await rows.first().locator(".account-windows > div").evaluateAll((nodes) =>
    nodes.map((node) => node.getBoundingClientRect().top));
  expect(boxes.length).toBe(4);
  expect(new Set(boxes).size).toBe(1);
  expect(state.calls.some((call) => call.path.includes("limits"))).toBe(false);
});

test("drawer saves display_name and explicit direct proxy without changing alias", async ({ page, state }) => {
  await page.locator("tbody tr").first().getByRole("button", { name: "修改", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "修改账号" });
  await expect(drawer).toContainText("2027-01-15 08:20:34");
  await expect(drawer.getByRole("button", { name: "选择固定代理" })).toHaveText("无代理▾");
  await drawer.getByLabel("账号显示名称").fill("本地显示名");
  await drawer.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => state.calls.filter((c) => c.method === "PATCH").length).toBe(1);
  expect(state.calls.find((c) => c.method === "PATCH")).toMatchObject({
    path: "/api/accounts/work", body: { display_name: "本地显示名", proxy_id: "" },
  });
  await expect(drawer.getByRole("heading", { name: "本地显示名" })).toBeVisible();
  await drawer.getByRole("button", { name: "关闭", exact: true }).click();
  await expect(page.locator("td.identity").first()).toHaveText("本地显示名work@example.com");
  expect(state.accounts[0].alias).toBe("work");
});

test("proxy selector is bounded, searchable, named-first and uses URI for unnamed nodes", async ({ page, state }) => {
  await page.locator("tbody tr").first().getByRole("button", { name: "修改", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "修改账号" });
  await drawer.getByRole("button", { name: "选择固定代理" }).click();
  const list = drawer.getByRole("list", { name: "代理节点" });
  expect(await list.evaluate((el) => el.clientHeight)).toBeLessThanOrEqual(200);
  expect(await list.evaluate((el) => el.scrollHeight > el.clientHeight)).toBe(true);
  await expect(list.getByRole("button", { name: "出口 0", exact: true })).toBeVisible();
  await drawer.getByLabel("搜索代理").fill("proxy1.example.com");
  await expect(list.getByRole("button")).toHaveCount(2);
  await list.getByRole("button", { name: "socks5://proxy1.example.com:1080", exact: true }).click();
  await drawer.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => state.accounts[0].proxy?.id).toBe("p1");
});

test("profile refresh failure still reloads health and never refreshes limits", async ({ page, state }) => {
  state.failures.set("GET /accounts/work/status", 503);
  await page.locator("tbody tr").first().getByRole("button", { name: "修改", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "修改账号" });
  const start = state.calls.length;
  await drawer.getByRole("button", { name: "刷新资料" }).click();
  await expect(drawer.locator(".chip.danger")).toHaveText("异常");
  await expect(drawer.getByRole("button", { name: "刷新资料" })).toBeEnabled();
  expect(state.calls.slice(start).filter((c) => c.path.startsWith("/accounts")).map((c) => c.path))
    .toEqual(["/accounts/work/status", "/accounts"]);
});

test("login pins selected/direct proxy at start; finish only sends alias and code", async ({ page, state }) => {
  await page.getByRole("button", { name: "添加", exact: true }).click();
  await page.getByLabel("本地别名（alias）").fill("new");
  await page.getByLabel("登录邮箱").fill("new@example.com");
  const picker = page.getByRole("button", { name: "选择固定代理" });
  await expect(picker).toHaveText("无代理▾");
  await page.getByRole("button", { name: "发送验证码" }).click();
  await expect(page.getByLabel("验证码", { exact: true })).toBeVisible();
  expect(state.calls.find((c) => c.path === "/api/login/start")?.body)
    .toEqual({ alias: "new", email: "new@example.com", proxy_id: "" });
  await expect(picker).toBeDisabled();
  await page.getByRole("button", { name: "重来" }).click();
  await picker.click();
  await page.getByRole("button", { name: "出口 0", exact: true }).click();
  await page.getByRole("button", { name: "发送验证码" }).click();
  await expect(page.getByLabel("验证码", { exact: true })).toBeVisible();
  expect(state.calls.filter((c) => c.path === "/api/login/start").at(-1)?.body.proxy_id).toBe("p0");
  state.failures.set("POST /api/login/finish", 403);
  const start = state.calls.length;
  await page.getByLabel("验证码", { exact: true }).fill("123456");
  await page.getByRole("button", { name: "完成登录" }).click();
  await expect(page.getByRole("button", { name: "完成登录" })).toBeEnabled();
  await expect.poll(() => state.calls.slice(start).some((c) => c.path === "/accounts")).toBe(true);
  expect(state.calls.find((c) => c.path === "/api/login/finish")?.body).toEqual({ alias: "new", code: "123456" });
});

test("manual single/batch limits refresh is independent and reloads health on both failures", async ({ page, state }) => {
  await go(page, "用量");
  const windows = page.locator(".limits-windows").first();
  await expect(windows.locator(":scope > div")).toHaveCount(4);
  expect(new Set(await windows.locator(":scope > div").evaluateAll((nodes) =>
    nodes.map((node) => node.getBoundingClientRect().top))).size).toBe(1);
  await expect(windows).toContainText("7d_claude");
  await expect(windows).toContainText("fable-5-1");
  await expect(windows).toContainText("缓存读 300 / 写 40");
  state.failures.set("GET /accounts/work/limits", 503);
  const start = state.calls.length;
  await page.getByRole("button", { name: "刷新额度", exact: true }).first().click();
  await expect(page.getByRole("button", { name: "刷新额度", exact: true }).first()).toBeEnabled();
  await expect(page.locator(".chip.danger").first()).toHaveText("异常");
  expect(state.calls.slice(start).map((c) => c.path)).toEqual(["/accounts/work/limits", "/accounts"]);
  await page.getByRole("button", { name: "批量刷新额度" }).click();
  await expect(windows).toContainText("45%");
  state.failures.set("POST /api/limits/refresh", 503);
  const before = state.calls.length;
  await page.getByRole("button", { name: "批量刷新额度" }).click();
  await expect(page.getByRole("button", { name: "批量刷新额度" })).toBeEnabled();
  await expect.poll(() => state.calls.slice(before).map((c) => `${c.method} ${c.path}`))
    .toEqual(["POST /api/limits/refresh", "GET /accounts"]);
  expect(state.calls.some((c) => c.path.includes("/status") || c.path === "/api/limits")).toBe(false);
});

test("schedule exposes only a 10–100% hard threshold, POST contains no mode", async ({ page, state }) => {
  await go(page, "调度");
  const slider = page.getByRole("slider");
  await expect(slider).toHaveValue("0.9");
  await expect(slider).toHaveAttribute("min", "0.1");
  await expect(slider).toHaveAttribute("max", "1");
  await expect(page.getByRole("radio")).toHaveCount(0);
  await slider.fill("0.8");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => state.ceiling).toBe(0.8);
  expect(state.calls.find((c) => c.path === "/api/schedule" && c.method === "POST")?.body)
    .toEqual({ max_utilization: 0.8 });
  expect(state.calls.some((c) => c.path.includes("limits") || c.path.includes("models"))).toBe(false);
});

test("manual proxy CRUD/import/Google204 and password masking use legacy endpoints", async ({ page, state }) => {
  await go(page, "网络");
  const first = page.locator("tbody tr").first();
  await expect(first).not.toContainText("mock-secret");
  await page.getByRole("button", { name: "显示", exact: true }).click();
  await expect(first).toContainText("mock-secret");
  await expect(page.getByRole("switch")).toHaveCount(0);
  await first.getByRole("button", { name: "测试 Google 204" }).click();
  await expect.poll(() => state.calls.some((c) => c.path === "/api/proxies/p0/test" && c.method === "POST")).toBe(true);
  await first.getByRole("button", { name: "修改", exact: true }).click();
  await page.getByLabel("名称", { exact: true }).fill("新的出口");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect(first).toContainText("新的出口");
  expect(state.calls.find((c) => c.path === "/api/proxies/p0" && c.method === "PATCH")?.body).toMatchObject({
    name: "新的出口", scheme: "socks5", host: "proxy0.example.com", port: 1080,
    username: "proxy-user", password: "mock-secret",
  });
  await page.getByRole("button", { name: "添加代理", exact: true }).click();
  await page.getByLabel("协议").selectOption("https");
  await page.getByLabel("主机", { exact: true }).fill("manual.example.com");
  await page.getByLabel("端口", { exact: true }).fill("8443");
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => state.nodes.length).toBe(36);
  expect(state.calls.find((c) => c.path === "/api/proxies" && c.method === "POST")?.body).toMatchObject({
    scheme: "https", host: "manual.example.com", port: 8443,
  });
  await page.getByRole("button", { name: "批量导入", exact: true }).click();
  const text = "http://first.example.com:80\nsocks5://second.example.com:1080";
  await page.getByLabel("代理列表（每行一条）").fill(text);
  await page.getByRole("button", { name: "保存", exact: true }).click();
  await expect.poll(() => state.calls.some((c) => c.path === "/api/proxies/import")).toBe(true);
  expect(state.calls.find((c) => c.path === "/api/proxies/import")?.body).toEqual({ text });
  await first.getByRole("button", { name: "删除", exact: true }).click();
  await page.getByRole("button", { name: "确认删除" }).click();
  await expect.poll(() => state.nodes.some((n) => n.id === "p0")).toBe(false);
});

test("manual models accepts OpenAI/namespaced lists; actual chat reloads health on success and failures", async ({ page, state }) => {
  await go(page, "测试");
  expect(state.calls.some((c) => c.path === "/v1/models")).toBe(false);
  await page.getByLabel("账号（留空按固定策略选择）").selectOption("error");
  await page.getByRole("button", { name: "读取列表" }).click();
  await expect(page.getByLabel("模型", { exact: true })).toHaveValue("claude-fable-5");
  expect(state.calls.find((c) => c.path === "/v1/models")?.headers["x-mirofish-account"]).toBe("error");
  state.modelShape = "namespaced";
  await page.getByRole("button", { name: "读取列表" }).click();
  await expect(page.getByLabel("模型", { exact: true })).toHaveValue("claude-fable-5-1");
  await page.getByLabel("max_tokens").fill("1");
  await expect(page.getByRole("button", { name: "发送", exact: true })).toBeDisabled();
  expect(state.calls.some((c) => c.path === "/v1/chat/completions")).toBe(false);
  await page.getByLabel("max_tokens").fill("256");
  for (const outcome of ["success", "http-error", "stream-error", "synthetic"] as const) {
    state.chat = outcome;
    const before = state.calls.length;
    await page.getByRole("button", { name: "发送", exact: true }).click();
    await expect(page.getByRole("button", { name: "发送", exact: true })).toBeEnabled();
    await expect.poll(() => state.calls.slice(before).some((c) => c.path === "/accounts")).toBe(true);
    const chat = state.calls.slice(before).find((c) => c.path === "/v1/chat/completions");
    expect(chat?.body).toMatchObject({
      model: "claude-fable-5-1", max_tokens: 256, stream: true,
      messages: [{ role: "user", content: "你好，请用一句话介绍你自己。" }],
    });
    expect(chat?.headers["x-mirofish-account"]).toBe("error");
    if (outcome === "synthetic") {
      await expect(page.locator("pre.output")).toContainText("未执行真实模型对话");
      expect(state.calls.slice(before).some((c) => c.path.startsWith("/api/usage"))).toBe(false);
    }
  }
  await go(page, "账号");
  await expect(page.locator("tbody tr").nth(1).locator(".chip.danger")).toHaveText("异常");
});

test("proxy diagnostics do not confuse valid configuration with tested connectivity", async ({ page, state }) => {
  state.nodes[0].status = "error";
  state.nodes[0].last_error = "proxy timeout";
  state.nodes[1].status = "untested";
  state.accounts[0].proxy = { id: null, active: false, status: "unbound" };
  await go(page, "网络");
  await expect(page.locator("tbody tr").first().locator("td").first()).toHaveText("不可用");
  await expect(page.locator("tbody tr").nth(1).locator("td").first()).toHaveText("未测试");
  await go(page, "账号");
  await expect(page.locator("tbody tr").first()).toContainText("未绑定");
  await page.locator("tbody tr").first().getByRole("button", { name: "修改", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "修改账号" })).toContainText("保存「无代理」才会显式使用直连");
  await page.getByRole("button", { name: "保存修改" }).click();
  await expect.poll(() => state.calls.some((c) => c.method === "PATCH")).toBe(true);
  expect(state.calls.find((c) => c.method === "PATCH")?.body).toEqual({ display_name: "", proxy_id: "" });
});

test("account switch/delete use alias endpoints and themed confirmation", async ({ page, state }) => {
  await page.getByRole("switch", { name: "work 启用" }).click();
  await expect(page.locator("tbody tr").first()).toContainText("disabled");
  expect(state.calls.find((c) => c.path === "/api/accounts/work/enabled")?.body).toEqual({ enabled: false });
  await page.locator("tbody tr").first().getByRole("button", { name: "删除", exact: true }).click();
  const dialog = page.getByRole("dialog", { name: "删除账号" });
  await expect(dialog).toBeVisible();
  expect(await dialog.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe("rgb(17, 17, 19)");
  await dialog.getByRole("button", { name: "确认删除" }).click();
  await expect(page.locator("tbody tr")).toHaveCount(4);
  expect(state.calls.some((c) => c.path === "/api/accounts/work" && c.method === "DELETE")).toBe(true);
});

test("drawers/picker/confirmation inherit light and dark themes", async ({ page, state }) => {
  expect(state.accounts.length).toBe(5);
  for (const theme of ["light", "dark"]) {
    await page.evaluate((theme) => document.documentElement.dataset.theme = theme, theme);
    const color = theme === "light" ? "rgb(255, 255, 255)" : "rgb(17, 17, 19)";
    await page.locator("tbody tr").first().getByRole("button", { name: "修改", exact: true }).click();
    const drawer = page.getByRole("dialog", { name: "修改账号" });
    expect(await drawer.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(color);
    await drawer.getByRole("button", { name: "选择固定代理" }).click();
    expect(await page.locator(".picker-panel").evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(color);
    await drawer.getByRole("button", { name: "关闭", exact: true }).click();
    await page.locator("tbody tr").first().getByRole("button", { name: "删除", exact: true }).click();
    const dialog = page.getByRole("dialog", { name: "删除账号" });
    expect(await dialog.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe(color);
    await dialog.getByRole("button", { name: "取消" }).click();
  }
  await go(page, "网络");
  await page.getByRole("button", { name: "添加代理", exact: true }).click();
  const drawer = page.getByRole("dialog", { name: "代理编辑" });
  expect(await drawer.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe("rgb(17, 17, 19)");
  await page.evaluate(() => document.documentElement.dataset.theme = "light");
  expect(await drawer.evaluate((el) => getComputedStyle(el).backgroundColor)).toBe("rgb(255, 255, 255)");
});
