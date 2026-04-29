// @ts-check
//
// Playwright E2E scaffold for ANILA Functions v1 (spec §8.4).
//
// These tests are not wired to a CI runner yet — they're the
// deployment-readiness checklist. Run via `npx playwright test e2e/`
// after `docker compose up` brings up the worker stack and the
// Sprint 2.5 prototype gate has passed.
//
// Each test is a black-box assertion on the surfaces this feature
// added. Auth uses pre-seeded developer / admin / user accounts so
// RBAC paths can be exercised cleanly.

import { expect, test } from "@playwright/test";


const ANILA_UI_URL = process.env.ANILA_UI_URL || "http://localhost:3001";


// ── Happy path: dev creates fill-text Function, button appears ─────────


test("developer creates fill-text Function, click in chat fills composer", async ({ page }) => {
  await page.goto(`${ANILA_UI_URL}/login`);
  await loginAs(page, "dev1");

  await page.goto(`${ANILA_UI_URL}/admin/functions`);
  await page.click("text=+ New Function");

  await page.fill('[placeholder="slug"]', "fill-text-test");
  await page.fill('[placeholder="title"]', "Fill Text Test");
  await page.fill('textarea[aria-label="function code"]', `
"""title: Fill Text Test"""
class Action:
    actions = [{"id": "fill", "name": "Fill", "icon_url": None}]
    async def action(self, body, __event_emitter__=None, **kw):
        await __event_emitter__({
            "type": "host_command",
            "verb": "composer.set_text",
            "args": {"text": "Hello from E2E test"}
        })
`);

  await page.selectOption("select", "enabled");
  await page.click("text=Save");

  // Trigger a chat message so the assistant toolbar appears
  await page.goto(`${ANILA_UI_URL}/`);
  await sendChatMessage(page, "ping");
  await waitForAssistantReply(page);

  // Click the new Function button under the assistant message
  await page.click('button[title="Fill"]');

  // Composer should now contain the injected text
  const composerValue = await page.inputValue("textarea[placeholder*='Composer'], div[contenteditable=true]");
  expect(composerValue).toContain("Hello from E2E test");
});


// ── RBAC: user role sees button but no admin CTAs ──────────────────────


test("user role sees Library tab but no New Function CTA", async ({ page }) => {
  await page.goto(`${ANILA_UI_URL}/login`);
  await loginAs(page, "user1");
  await page.goto(`${ANILA_UI_URL}/admin/functions`);

  await expect(page.locator("text=Library")).toBeVisible();
  await expect(page.locator("text=+ New Function")).toHaveCount(0);
});


// ── Verb whitelist: injected unknown verb is rejected ──────────────────


test("frontend rejects host_command verb outside whitelist", async ({ page }) => {
  await page.goto(`${ANILA_UI_URL}/login`);
  await loginAs(page, "dev1");

  // Inject a Function that emits a non-whitelisted verb. The frontend
  // dispatcher must reject it without executing — only an error toast.
  await page.goto(`${ANILA_UI_URL}/admin/functions`);
  await createFunctionWithCode(page, "verb-injection-test", `
"""title: Verb Injection"""
class Action:
    actions = [{"id": "x", "name": "Inject", "icon_url": None}]
    async def action(self, body, __event_emitter__=None, **kw):
        await __event_emitter__({
            "type": "host_command",
            "verb": "filesystem.delete_all",
            "args": {}
        })
`);

  await page.goto(`${ANILA_UI_URL}/`);
  await sendChatMessage(page, "ping");
  await waitForAssistantReply(page);
  await page.click('button[title="Inject"]');

  // Rejection surfaces as console warn + error toast (not as a real action)
  await expect(page.locator("text=/Unknown host command/")).toBeVisible();
});


// ── Ownership: cross-conversation /run is rejected ─────────────────────


test("/run for another user's message returns 403", async ({ page, request }) => {
  await page.goto(`${ANILA_UI_URL}/login`);
  await loginAs(page, "dev1");
  // Get a CSRF cookie value from the page's domain
  const csrf = await page.evaluate(() => {
    const m = document.cookie.match(/anila_csrf=([^;]+)/);
    return m?.[1];
  });

  // Pre-seeded conversation owned by user2 with a known assistant message
  const resp = await request.post(`${ANILA_UI_URL}/api/functions/seeded-test/run`, {
    headers: { "Content-Type": "application/json", "X-CSRF-Token": csrf || "" },
    data: {
      action_id: "x",
      context: { conversation_id: 99999, message_id: 99999 },
      test_mode: false,
    },
  });
  expect(resp.status()).toBe(403);
});


// ── Helpers ────────────────────────────────────────────────────────────


async function loginAs(page, username) {
  await page.fill('input[name="username"]', username);
  await page.fill('input[name="password"]', "dev-password");
  await page.click('button[type="submit"]');
  await page.waitForURL((url) => !url.pathname.includes("/login"));
}


async function createFunctionWithCode(page, slug, code) {
  await page.click("text=+ New Function");
  await page.fill('[placeholder="slug"]', slug);
  await page.fill('[placeholder="title"]', slug);
  await page.fill('textarea[aria-label="function code"]', code);
  await page.selectOption("select", "enabled");
  await page.click("text=Save");
}


async function sendChatMessage(page, text) {
  await page.fill("textarea[placeholder*='訊息'], textarea[placeholder*='ask']", text);
  await page.keyboard.press("Enter");
}


async function waitForAssistantReply(page) {
  // Wait for the assistant message bubble + its toolbar to render.
  // ANILA UI marks assistant rows with a stable role attribute.
  await page.waitForSelector('[data-role="assistant"]', { timeout: 30_000 });
}
