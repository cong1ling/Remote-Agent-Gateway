import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const [base, writer, modulePath] = process.argv.slice(2);
assert.equal(new URL(base).hostname, "127.0.0.1");
const { chromium } = await import(pathToFileURL(modulePath));
const browser = await chromium.launch({ headless: true });

try {
  const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
  const errors = [];
  page.on("pageerror", (error) => errors.push(error.message));
  page.on("dialog", (dialog) => dialog.accept());
  await page.goto(base);
  await page.locator("#auth-email").fill("owner@example.test");
  await page.locator("#auth-password").fill("test-only passphrase 42");
  await page.locator("#auth-submit").click();

  await page.locator('[data-view="keys"]').click();
  await page.locator("#new-key-button").click();
  await page.locator('#form-dialog input[name="name"]').fill("Browser plan key");
  assert.equal(await page.locator('#form-dialog select[name="scope"]').inputValue(), "plan");
  await page.locator("#dialog-submit").click();
  await page.locator("#secret-close").click();
  await page.locator("#keys-list").getByText(/仅受控计划/).first().waitFor();

  await page.locator('[data-view="agents"]').click();
  await page.locator(`#agents-list [data-edit-agent="${writer}"]`).click();
  await page.locator('#form-dialog input[name="environment"]').fill("staging");
  await page.locator('#form-dialog input[name="tags"]').fill("canary, browser");
  await page.locator("#dialog-submit").click();
  await page.locator('#form-dialog').waitFor({ state: "hidden" });
  const directory = page.locator("#agent-directory-filters");
  await directory.locator('select[name="environment"]').selectOption("staging");
  await directory.locator('select[name="tag"]').selectOption("canary");
  await directory.locator('select[name="capability"]').selectOption("workspace.v1/read");
  assert.equal(await page.locator("#agents-list .agent-card").count(), 1);
  await page.locator(`#agents-list [data-agent-capabilities="${writer}"]`).click();
  await page.locator("#agent-capabilities-dialog").getByText("环境：staging · 标签：browser、canary").waitFor();
  await page.locator('#agent-capabilities-dialog [data-plan-policy]').click();
  await page.locator('#form-dialog textarea[name="l1_paths"]').fill("browser-l1.txt");
  await page.locator("#dialog-submit").click();
  await page.locator('#form-dialog').waitFor({ state: "hidden" });
  await page.locator(`#agents-list [data-agent-capabilities="${writer}"]`).click();
  await page.locator("#agent-capabilities-dialog").getByText("授权 1 个文件").waitFor();
  await page.locator("#agent-capabilities-done").click();

  await page.locator('[data-view="health"]').click();
  await page.locator("#new-health-button").click();
  await page.locator('#health-device-filters select[name="tag"]').selectOption("canary");
  assert.deepEqual(await page.locator('#health-options input:checked').evaluateAll(
    (items) => items.map((item) => item.value),
  ), [writer]);
  await page.locator("#dialog-submit").click();
  await page.locator("#health-tasks [data-health-detail]").first().waitFor();
  await page.locator("#new-monitor-button").click();
  await page.locator('#form-dialog select[name="agent_id"]').selectOption(writer);
  await page.locator("#dialog-submit").click();
  await page.locator("#health-monitors").getByText(/已授权 · 每 30 分钟/).waitFor();
  await page.locator(`#health-monitors [data-monitor-disable="${writer}"]`).click();
  await page.locator("#health-monitors").getByText("未授权定时巡检").waitFor();

  await page.locator('[data-view="controls"]').click();
  const accountRow = page.locator(".execution-control-row").filter({
    has: page.getByRole("heading", { name: "当前账号全部设备" }),
  });
  const account = accountRow.locator('[data-execution-stop="account"]');
  await account.click();
  await accountRow.getByText("拒绝新执行").waitFor();
  await accountRow.getByRole("button", { name: "恢复", exact: true }).waitFor();
  await account.click();
  await accountRow.getByRole("button", { name: "暂停", exact: true }).waitFor();

  await page.locator('[data-view="alerts"]').click();
  const risk = page.locator(".risk-alert-row").filter({ hasText: "已拦截" }).first();
  await risk.getByRole("button", { name: "查看告警" }).click();
  const riskDetail = page.locator("#profile-dialog");
  await riskDetail.getByRole("heading", { name: "触发依据" }).waitFor();
  await riskDetail.getByRole("button", { name: "确认告警" }).click();
  await riskDetail.getByText(/账号所有者 · 已确认/).waitFor();
  await page.locator("#profile-dialog-done").click();

  await page.locator('[data-view="plans"]').click();
  await page.locator("#new-plan-button").click();
  await page.locator('#form-dialog input[name="objective"]').fill("Browser approval smoke test");
  const localValue = (millis) => {
    const instant = new Date(millis);
    return new Date(millis - instant.getTimezoneOffset() * 60000).toISOString().slice(0, 19);
  };
  await page.locator('#form-dialog input[name="maintenance_start"]').fill(localValue(Date.now() - 60_000));
  await page.locator('#form-dialog input[name="maintenance_end"]').fill(localValue(Date.now() + 900_000));
  await page.locator('#form-dialog textarea[name="steps"]').fill(JSON.stringify([{
    key: "repair", agent_id: writer, operation: "workspace.create",
    params: { path: "browser.ini", content: "level=info\n" },
  }]));
  await page.locator("#dialog-submit").click();
  const detail = page.locator("#profile-dialog");
  await detail.getByRole("heading", { name: "维护窗口与批次" }).waitFor();
  await detail.getByText("L2 · 需人工审批 · 已创建").waitFor();
  assert.equal(await detail.locator('[data-plan-command="execute"]').count(), 0);
  await detail.getByRole("button", { name: "批准此版本" }).click();
  await detail.getByRole("button", { name: "执行此版本" }).click();
  await detail.getByRole("heading", { name: "已验证证据 · 1" }).waitFor();
  await detail.getByText("+level=info", { exact: false }).waitFor();
  await detail.getByText("action.verified", { exact: false }).waitFor();
  await detail.getByText("maintenance.batch_finished", { exact: false }).waitFor();
  assert.deepEqual(errors, []);
  console.log(`Chromium ${browser.version()}: login, scoped key, directory, filtered health, monitor consent, execution stops, alert history, device policy, L2 approval, maintenance and evidence rendered`);
} finally {
  await browser.close();
}
