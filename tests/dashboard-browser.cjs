// Real layout, focus, selection and contrast checks using fake local data only.
const assert = require("node:assert/strict");
const http = require("node:http");
const fs = require("node:fs");
const path = require("node:path");
const { chromium } = require("playwright-core");
const axe = require("axe-core");
const { html, script, fixture, job } = require("./dashboard-fixture.cjs");

async function main() {
  const state = fixture(),
    errors = [],
    evidence = [];
  state.jobs[0].title =
    "审查前端交互与可访问性，检查标题完整换行以及任务描述独立展示。".repeat(2);
  state.descriptions[state.jobs[0].job_id].text =
    "完整任务描述\n" + "长路径/".repeat(75) + "\n描述尾部应完整可见。";
  const output = path.resolve("outputs/validation");
  fs.mkdirSync(output, { recursive: true });
  const app = http.createServer(async (request, response) => {
    response.setHeader("Cache-Control", "no-store");
    response.setHeader(
      "Content-Security-Policy",
      "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'",
    );
    if (request.url === "/fixture/") {
      response.setHeader("Content-Type", "text/html; charset=utf-8");
      response.end(html);
      return;
    }
    if (request.url === "/fixture/dashboard.js") {
      response.setHeader("Content-Type", "text/javascript; charset=utf-8");
      response.end(script);
      return;
    }
    response.setHeader("Content-Type", "application/json; charset=utf-8");
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    try {
      const payload = chunks.length
        ? JSON.parse(Buffer.concat(chunks).toString())
        : undefined;
      response.end(
        JSON.stringify(await state.api(request.url.split("/").pop(), payload)),
      );
    } catch (error) {
      response.statusCode = error.status || 400;
      response.end(JSON.stringify({ error: error.message }));
    }
  });
  await new Promise((resolve) => app.listen(0, "127.0.0.1", resolve));
  let browser;
  try {
    browser = await chromium.launch({
      headless: true,
      ...(process.env.BROWSER_EXECUTABLE
        ? { executablePath: process.env.BROWSER_EXECUTABLE }
        : { channel: "msedge" }),
    });
    const context = await browser.newContext({
      viewport: { width: 1024, height: 900 },
      permissions: ["clipboard-read", "clipboard-write"],
    });
    const page = await context.newPage();
    page.on("pageerror", (error) => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${app.address().port}/fixture/`);
    await page.waitForSelector(".result");
    await page.waitForFunction(
      () => document.querySelector(".result").textContent.length > 0,
    );
    await page.locator("#pause").click();
    await page.locator(".task-description summary").focus();
    await page.keyboard.press("Enter");
    await page.waitForFunction(() =>
      document
        .querySelector(".description-text")
        .textContent.endsWith("描述尾部应完整可见。"),
    );
    for (const width of [320, 375, 480, 760, 1024]) {
      await page.setViewportSize({ width, height: 900 });
      for (const tab of ["monitor", "task", "storage", "rules", "settings"]) {
        await page.locator(`#tab-${tab}`).click();
        if (tab === "rules" && width === 320) {
          await page.locator("#rules-catalog").click();
          await page.waitForFunction(() =>
            document
              .querySelector("#rules-catalog-status")
              .textContent.includes("2"),
          );
          await page.locator("#rules-add").click();
        }
        if (tab === "storage")
          await page.waitForFunction(() =>
            document
              .querySelector("#storage-note")
              .textContent.includes("共 2 个任务"),
          );
        await page.waitForTimeout(80);
        const dimensions = await page.evaluate(() => ({
          client: document.documentElement.clientWidth,
          scroll: document.documentElement.scrollWidth,
        }));
        assert.ok(
          dimensions.scroll <= dimensions.client + 1,
          `Overflow: ${width}px ${tab}`,
        );
        if (tab === "monitor") {
          const controls = await page.evaluate(() => {
            const segments = document
              .querySelector(".view-segments")
              .getBoundingClientRect();
            const select = document
              .querySelector("#job-status")
              .getBoundingClientRect();
            const button = document
              .querySelector("#view-current")
              .getBoundingClientRect();
            return {
              segments: segments.height,
              select: select.height,
              topDelta: select.top - segments.top,
              centerDelta:
                (segments.top + segments.bottom - button.top - button.bottom) /
                2,
            };
          });
          assert.equal(controls.segments, 34);
          assert.equal(controls.select, 34);
          assert.ok(Math.abs(controls.centerDelta) < 1);
          if (width >= 480) assert.ok(Math.abs(controls.topDelta) < 1);
          else assert.ok(controls.topDelta >= 0);
          assert.equal(
            await page.locator("#detail h2").textContent(),
            state.jobs[0].title,
          );
          assert.equal(
            await page.locator(".description-text").textContent(),
            state.descriptions[state.jobs[0].job_id].text,
          );
          const titleStyle = await page
            .locator("#detail h2")
            .evaluate((node) => ({
              clamp: getComputedStyle(node).webkitLineClamp,
              overflow: node.scrollHeight > node.clientHeight + 1,
            }));
          assert.equal(titleStyle.clamp, "none");
          assert.equal(titleStyle.overflow, false);
          const listTitle = await page
            .locator(`[data-job-id="${state.jobs[0].job_id}"] .job-title`)
            .first()
            .evaluate((node) => ({
              text: node.textContent,
              clamp: getComputedStyle(node).webkitLineClamp,
              overflow: node.scrollHeight > node.clientHeight + 1,
            }));
          assert.equal(listTitle.text, state.jobs[0].title);
          assert.equal(listTitle.clamp, "none");
          assert.equal(listTitle.overflow, false);
        }
        const a11y = await page.evaluate(async (source) => {
          // Playwright evaluation executes in the isolated test harness; no production CSP change.
          (0, eval)(source);
          const report = await axe.run(document, {
            runOnly: { type: "tag", values: ["wcag2a", "wcag2aa", "wcag21aa"] },
          });
          return report.violations.map((item) => ({
            id: item.id,
            impact: item.impact,
            nodes: item.nodes.map((node) => ({
              target: node.target,
              summary: node.failureSummary,
            })),
          }));
        }, axe.source);
        evidence.push({ width, tab, dimensions, violations: a11y });
        if (a11y.length)
          errors.push(`${width}px ${tab}: ${JSON.stringify(a11y)}`);
        if (width === 375)
          await page.screenshot({
            path: path.join(output, `${tab}-375.png`),
            fullPage: true,
          });
        if (tab === "monitor") {
          await page.locator("#archive-settings > summary").click();
          const expanded = await page.evaluate(async () => ({
            client: document.documentElement.clientWidth,
            scroll: document.documentElement.scrollWidth,
            violations: (
              await axe.run(document, {
                runOnly: {
                  type: "tag",
                  values: ["wcag2a", "wcag2aa", "wcag21aa"],
                },
              })
            ).violations.map((item) => ({
              id: item.id,
              nodes: item.nodes.map((node) => node.target),
            })),
          }));
          assert.ok(
            expanded.scroll <= expanded.client + 1,
            `Expanded archive overflow at ${width}px`,
          );
          assert.deepEqual(expanded.violations, []);
          evidence.push({ width, tab: "archive-rules", ...expanded });
          if ([375, 1024].includes(width))
            await page.screenshot({
              path: path.join(output, `archive-rules-${width}.png`),
              fullPage: true,
            });
          if (width === 1024)
            await page.screenshot({
              path: path.join(output, "archive-rules-overview.png"),
            });
          await page.locator("#archive-settings > summary").click();
        }
      }
    }
    await page.setViewportSize({ width: 1024, height: 900 });
    await page.locator("#tab-monitor").click();
    await page.evaluate(() => {
      const result = document.querySelector(".result");
      const copy = [...document.querySelectorAll("#detail button")].find(
        (node) => node.textContent === "复制完整结果",
      );
      copy.focus();
      result.scrollTop = 100;
      const selection = window.getSelection(),
        range = document.createRange();
      range.setStart(result.firstChild, 0);
      range.setEnd(result.firstChild, 4);
      selection.removeAllRanges();
      selection.addRange(range);
      window.retainedResult = result;
      window.retainedCopy = copy;
      document.querySelector("#refresh").click();
    });
    await page.waitForTimeout(100);
    assert.deepEqual(
      await page.evaluate(() => ({
        sameNode: retainedResult === document.querySelector(".result"),
        selected: window.getSelection().toString(),
        focused: document.activeElement === retainedCopy,
        scroll: retainedResult.scrollTop,
      })),
      { sameNode: true, selected: "完整结果", focused: true, scroll: 100 },
    );
    await page
      .getByRole("button", { name: "复制完整结果", exact: true })
      .click();
    await page.waitForFunction(() =>
      document.querySelector("#detail").textContent.includes("完整结果已复制"),
    );
    const copied = await page.evaluate(() => navigator.clipboard.readText());
    // Windows clipboard normalizes text line endings to CRLF.
    assert.ok(
      copied.replace(/\r\n/g, "\n") === state.results[state.jobs[0].job_id],
      "Clipboard must include the entire Unicode result",
    );
    const events = page.locator("#detail .events");
    await events.evaluate((node) => {
      node.scrollTop = 110;
    });
    await page.locator("#tab-settings").click();
    state.jobs[1].progress.tool_calls++;
    await page.evaluate(() => document.querySelector("#refresh").click());
    await page.waitForTimeout(100);
    await page.locator("#tab-monitor").click();
    assert.equal(await events.evaluate((node) => node.scrollTop), 110);
    await page.locator('.job[data-job-id="bbbbbbbbbbbbbbbb"]').click();
    await page.locator('.job[data-job-id="aaaaaaaaaaaaaaaa"]').click();
    assert.equal(await events.evaluate((node) => node.scrollTop), 110);
    await page.locator("#tab-monitor").focus();
    await page.keyboard.press("ArrowRight");
    assert.equal(
      await page.evaluate(() => document.activeElement.id),
      "tab-task",
    );
    await page.keyboard.press("End");
    assert.equal(
      await page.evaluate(() => document.activeElement.id),
      "tab-settings",
    );
    assert.equal(
      await page.locator("#task-model").getAttribute("aria-pressed"),
      null,
    );
    await page.locator("#tab-monitor").click();
    await page.locator("#search").fill("does-not-exist");
    await page
      .locator("#jobs")
      .getByText("没有匹配的任务", { exact: true })
      .waitFor();
    assert.equal(
      await page.getByText("没有匹配的任务", { exact: true }).count(),
      1,
    );
    await page.getByRole("button", { name: "清除筛选", exact: true }).click();
    await page.waitForSelector("#jobs .job");
    state.jobs.push(job("c", "completed", { conversation_id: "" }));
    await page.locator("#archive-settings > summary").focus();
    await page.keyboard.press("Enter");
    await page.locator("#archive-days").fill("14");
    await page.locator("#archive-policy-save").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#archive-rule-summary")
        .textContent.includes("14 天"),
    );
    await page.locator("#archive-preview").click();
    await page.locator("#archive-bulk:not([disabled])").waitFor();
    assert.equal(state.jobs[2].archived, false);
    await page.locator("#archive-bulk").click();
    await page.waitForFunction(() =>
      document
        .querySelector("#archive-bulk-alert")
        .textContent.includes("已归档 1 个"),
    );
    await page.locator("#view-archived").click();
    await page.locator('#jobs [data-job-id="cccccccccccccccc"]').click();
    await page.getByText(/旧任务批量归档 ·/).waitFor();
    await page.getByRole("button", { name: "恢复任务", exact: true }).click();
    await page.locator("#view-all").click();
    await page.locator('#jobs [data-job-id="cccccccccccccccc"]').click();
    await page.getByText(/此任务已恢复，免于自动归档/).waitFor();
    await page.locator("#archive-settings > summary").click();
    await page.setViewportSize({ width: 375, height: 900 });
    state.jobs = [];
    await page.locator("#refresh").click();
    await page
      .locator("#jobs")
      .getByText("此视图暂无任务", { exact: true })
      .waitFor();
    const emptyWidth = await page
      .locator("#jobs .empty")
      .evaluate((node) => node.getBoundingClientRect().width);
    const listWidth = await page
      .locator("#jobs")
      .evaluate((node) => node.getBoundingClientRect().width);
    assert.ok(
      Math.abs(emptyWidth - listWidth) < 1,
      "Empty state should span both grid columns",
    );
    assert.deepEqual(errors, []);
    console.log(
      JSON.stringify({
        browser: browser.version(),
        layouts: evidence.length,
        axeViolations: 0,
        interactions: 14,
        ok: true,
      }),
    );
  } finally {
    fs.writeFileSync(
      path.join(output, "browser-checks.json"),
      JSON.stringify({ evidence, errors }, null, 2),
    );
    if (browser) await browser.close();
    await new Promise((resolve) => app.close(resolve));
  }
}
main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
