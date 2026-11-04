const { test } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const {
  html,
  script,
  markdownScript,
  markdownResult,
  fixture,
  job,
  clone,
  deferred,
} = require("./dashboard-fixture.cjs");
const tick = () => new Promise((resolve) => setImmediate(resolve));
const change = (h, id, value) => {
  if (value !== undefined) h.$(id).value = value;
  h.$(id).dispatchEvent(new h.window.Event("change", { bubbles: true }));
};
async function until(predicate) {
  for (let i = 0; i < 80; i++) {
    if (predicate()) return;
    await tick();
  }
  assert.ok(predicate(), "Expected UI state did not arrive");
}

test("settings contains independent keyboard sub-tabs and preserves drafts between pages", async (t) => {
  const h = await harness(t);
  const ids = [...h.window.document.querySelectorAll("[id]")].map((node) => node.id);
  assert.equal(new Set(ids).size, ids.length, "Every tab, field and diagnostic must have a unique ID");
  assert.deepEqual([...h.window.document.querySelectorAll('header [role="tab"]')].map((node) => node.id),
    ["tab-monitor", "tab-task", "tab-settings"]);
  h.$("tab-settings").click();
  await tick();
  h.input("model", "connection-draft");
  h.$("tab-runtime").click();
  await tick();
  h.input("pref-max_concurrent_tasks", "3");
  h.$("tab-runtime").dispatchEvent(new h.window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  assert.equal(h.window.document.activeElement.id, "tab-interface");
  assert.equal(h.$("interface").hidden, false);
  assert.equal(h.$("runtime").hidden, true);
  assert.equal(h.$("runtime").tagName, "SECTION");
  h.input("pref-jobs_page_size", "20");
  h.$("tab-connections").click();
  await tick();
  assert.equal(h.$("model").value, "connection-draft");
  h.$("tab-runtime").click();
  await tick();
  assert.equal(h.$("pref-max_concurrent_tasks").value, "3");
  assert.equal(h.$("pref-jobs_page_size").value, "20");
  h.$("runtime-save").click();
  await until(() => h.$("runtime-alert").textContent.includes("设置已保存"));
  assert.equal(h.state.config.app_settings.values.max_concurrent_tasks, 3);
  assert.equal(h.state.config.app_settings.values.jobs_page_size, 50);
  h.$("tab-interface").click();
  await tick();
  assert.equal(h.$("interface-save").disabled, false);
  h.$("interface-save").click();
  await until(() => h.$("interface-alert").textContent.includes("设置已保存"));
  assert.equal(h.state.config.app_settings.values.jobs_page_size, 20);
});

test("preferences conflict keeps local values and reload only clears its own page", async (t) => {
  const h = await harness(t);
  h.$("tab-runtime").click();
  await tick();
  h.input("pref-max_concurrent_tasks", "4");
  h.input("pref-jobs_page_size", "20");
  h.state.config.app_settings.revision = "other-window";
  h.state.config.app_settings.values.max_concurrent_tasks = 5;
  h.state.config.app_settings.saved_values.max_concurrent_tasks = 5;
  h.$("runtime-save").click();
  await until(() => h.$("runtime-alert").textContent.includes("重新加载"));
  assert.equal(h.$("pref-max_concurrent_tasks").value, "4");
  assert.equal(h.$("runtime-save").disabled, true);
  h.$("runtime-reload").click();
  await until(() => h.$("runtime-alert").textContent.includes("已重新加载"));
  assert.equal(h.$("pref-max_concurrent_tasks").value, "5");
  assert.equal(h.$("pref-jobs_page_size").value, "20");
});

test("defaults are a draft, bounds reject invalid numbers and environment overrides remain explicit", async (t) => {
  const state = fixture();
  state.config.app_settings.values.max_concurrent_tasks = 3;
  state.config.app_settings.overrides.max_concurrent_tasks = { environment: "AGENT_HUB_MAX_CONCURRENT_TASKS", value: 3 };
  state.config.app_settings.values.task_timeout_seconds = 900;
  state.config.app_settings.saved_values.task_timeout_seconds = 900;
  const h = await harness(t, state);
  h.$("tab-runtime").click();
  await tick();
  assert.equal(h.$("pref-max_concurrent_tasks").disabled, true);
  assert.equal(h.$("pref-max_concurrent_tasks").value, "3");
  assert.match(h.$("pref-max_concurrent_tasks-hint").textContent, /环境变量|AGENT_HUB_MAX_CONCURRENT_TASKS/);
  h.input("pref-task_timeout_seconds", "0");
  h.$("runtime-save").click();
  assert.equal(state.calls.some((call) => call.action === "app-settings"), false);
  h.$("runtime-reset").click();
  assert.equal(h.$("pref-task_timeout_seconds").value, "600");
  assert.equal(state.config.app_settings.values.task_timeout_seconds, 900);
  h.$("runtime-save").click();
  await until(() => h.$("runtime-alert").textContent.includes("设置已保存"));
  const saved = state.calls.find((call) => call.action === "app-settings").payload;
  assert.equal(saved.values.max_concurrent_tasks, 2);
});

test("saved interface preferences drive paging, polling and default result mode", async (t) => {
  const state = fixture();
  const preferences = { jobs_page_size: 10, result_page_chars: 2500, poll_interval_ms: 4000, default_result_view: "raw", task_timeout_seconds: 1200 };
  Object.assign(state.config.app_settings.values, preferences);
  Object.assign(state.config.app_settings.saved_values, preferences);
  const h = await harness(t, state);
  assert.equal(state.calls.find((call) => call.action === "jobs").payload.limit, 10);
  assert.equal(state.calls.find((call) => call.action === "job" && call.payload.include_description === false).payload.max_chars, 2500);
  assert.ok(h.$("detail").querySelector(".result-raw"));
  assert.equal(h.$("task-timeout").value, "1200");
  h.input("task-timeout", "123");
  h.$("tab-settings").click();
  await tick();
  assert.equal(h.$("task-timeout").value, "123");
  h.$("pause").click();
  await until(() => [...h.timers.values()].some((timer) => timer.ms === 4000));
});

test("late configuration reads cannot overwrite saved preferences or an in-flight save", async (t) => {
  const h = await harness(t);
  h.$("tab-runtime").click();
  await tick();
  h.input("pref-max_concurrent_tasks", "4");
  const old = clone(h.state.config), read = deferred(), save = deferred();
  h.state.handlers.config = () => read.promise;
  h.state.handlers["app-settings"] = () => save.promise;
  h.$("tab-runtime").click();
  h.$("runtime-save").click();
  read.resolve(old);
  await tick();
  assert.equal(h.$("pref-max_concurrent_tasks").value, "4");
  assert.equal(h.$("pref-max_concurrent_tasks").disabled, true);
  const updated = clone(old.app_settings);
  updated.saved_values.max_concurrent_tasks = updated.values.max_concurrent_tasks = 4;
  updated.revision = "new-version";
  save.resolve(updated);
  await until(() => h.$("runtime-alert").textContent.includes("设置已保存"));
  assert.equal(h.$("pref-max_concurrent_tasks").value, "4");
  assert.equal(h.$("runtime-save").disabled, true);
});

test("local import is opt-in, stays per-agent and preserves connection drafts", async (t) => {
  const h = await harness(t);
  assert.equal(
    h.state.calls.some((c) => c.action === "local-detect"),
    false,
  );
  h.$("tab-settings").click();
  await tick();
  assert.equal(h.$("connection-manual").hidden, false);
  assert.equal(h.$("connection-local").hidden, true);
  h.input("model", "unsaved-choice");
  h.$("tab-connection-manual").dispatchEvent(new h.window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }));
  assert.equal(h.window.document.activeElement.id, "tab-connection-local");
  assert.equal(h.$("connection-local").hidden, false);
  assert.equal(h.$("connection-manual").hidden, true);
  assert.equal(h.state.calls.some((c) => ["local-detect", "local-sync", "configure"].includes(c.action)), false);
  change(h, "local-mode", "follow");
  h.$("local-detect").click();
  await until(() => h.$("local-alert").textContent.includes("检测完成"));
  assert.match(h.$("local-source-info").textContent, /不回显/);
  h.$("tab-connection-local").dispatchEvent(new h.window.KeyboardEvent("keydown", { key: "Home", bubbles: true }));
  assert.equal(h.window.document.activeElement.id, "tab-connection-manual");
  assert.equal(h.$("model").value, "unsaved-choice");
  h.$("tab-connection-local").click();
  assert.equal(h.$("local-mode").value, "follow");
  assert.equal(h.$("local-source").value, "detect-token");
  h.$("local-apply").click();
  assert.match(h.$("local-alert").textContent, /先保存/);
  assert.equal(
    h.state.calls.some((c) => c.action === "local-sync"),
    false,
  );
  h.$("reload-config").click();
  await until(() => !h.$("local-apply").disabled);
  h.$("local-apply").click();
  await until(() => h.$("local-alert").textContent.includes("已应用"));
  const call = h.state.calls.find((c) => c.action === "local-sync");
  assert.equal(call.payload.mode, "follow");
  assert.equal(call.payload.token, "detect-token");
  assert.equal(call.payload.sync_revision, "sync-r1");
  assert.equal("key" in call.payload, false);
});

test("rule drafts support order, exact candidate pools and stale-save conflicts", async (t) => {
  const h = await harness(t);
  h.$("tab-rules").click();
  h.$("rules-catalog").click();
  await until(() => h.$("rules-catalog-status").textContent.includes("2"));
  h.$("rules-add").click();
  const card = h.$("rules-list").firstChild;
  const name = card.querySelector('input[id$="-name"]');
  h.input(name.id, "复杂任务");
  const pool = card.querySelector('select[id$="-candidates"]');
  pool.options[1].selected = true;
  pool.focus();
  change(h, pool.id);
  assert.equal(h.window.document.activeElement.id, pool.id);
  h.$("rules-add").click();
  const second = h.$("rules-list").lastChild;
  [...second.querySelectorAll("button")]
    .find((b) => b.textContent === "上移")
    .click();
  const enabled = h.$("rules-enabled");
  enabled.checked = true;
  change(h, "rules-enabled");
  h.$("rules-save").click();
  await until(
    () =>
      h.$("rules-alert").textContent.startsWith("规则已保存") &&
      !h.$("rules-save").disabled,
  );
  assert.equal(h.state.config.routing_policy.rules[1].name, "复杂任务");
  assert.equal(
    h.state.config.routing_policy.rules[1].candidates[0],
    "deepseek:" + "b".repeat(24),
  );
  const edit = h.$("rules-list").querySelector('input[id$="-name"]');
  h.input(edit.id, "保留草稿");
  h.state.config.routing_policy.revision = "other-window";
  h.$("rules-save").click();
  await until(() => h.$("rules-alert").textContent.includes("重新加载"));
  assert.equal(h.$(edit.id).value, "保留草稿");
});

test("manual rule classification previews without execution and binds the submitted decision", async (t) => {
  const state = fixture();
  state.config.routing_policy.enabled = true;
  state.config.routing_policy.rules = [
    {
      id: "intent",
      name: "审查",
      intent: "审查代码",
      enabled: true,
      candidates: ["claude:" + "a".repeat(24)],
      conditions: {},
      effort: "",
      allow_fallback: true,
    },
  ];
  const h = await harness(t, state);
  h.$("tab-task").click();
  await tick();
  assert.equal(h.$("task-selection-mode").value, "rules");
  assert.equal(h.$("task-backend").disabled, true);
  h.input("task-prompt", "Review this code");
  h.$("route-context").click();
  await until(() => !h.$("route-preview").disabled);
  const intent = h.$("route-intents").querySelector("input");
  intent.checked = true;
  intent.dispatchEvent(new h.window.Event("change", { bubbles: true }));
  h.$("route-preview").click();
  await until(() => !h.$("route-candidate").disabled);
  assert.equal(
    state.calls.some((c) => c.action === "tasks"),
    false,
  );
  h.input("route-reason", "模型适合本次只读审查");
  h.$("task-form").dispatchEvent(
    new h.window.Event("submit", { bubbles: true, cancelable: true }),
  );
  await until(() => state.calls.some((c) => c.action === "tasks"));
  const payload = state.calls.find((c) => c.action === "tasks").payload;
  assert.deepEqual(payload.routing_decision.matched_intents, ["intent"]);
  assert.equal(
    payload.routing_decision.candidate_id,
    "claude:" + "a".repeat(24),
  );
  assert.equal(payload.model, "");
  assert.equal(payload.task_complexity, "standard");
  assert.equal(payload.model_override_reason, undefined);
});

test("late rule contexts cannot authorize a changed task or connection", async (t) => {
  const state = fixture();
  state.config.routing_policy.enabled = true;
  const h = await harness(t, state),
    pending = deferred();
  state.handlers["routing-context"] = () => pending.promise;
  h.input("task-prompt", "Original task");
  h.$("route-context").click();
  h.input("task-prompt", "Different task");
  pending.resolve({ enabled: true, token: "late", rules: [] });
  await until(() => !h.$("route-context").disabled);
  assert.equal(h.$("route-preview").disabled, true);
  delete state.handlers["routing-context"];
  h.$("route-context").click();
  await until(() => !h.$("route-preview").disabled);
  h.$("route-preview").click();
  await until(() => !h.$("route-candidate").disabled);
  state.config.profiles.claude.revision = "changed";
  h.hide(true);
  h.hide(false);
  await until(() => h.$("route-candidate").disabled);
  h.$("task-form").dispatchEvent(
    new h.window.Event("submit", { bubbles: true, cancelable: true }),
  );
  await until(() => h.$("task-alert").textContent.includes("先为当前任务"));
  assert.equal(
    state.calls.some((c) => c.action === "tasks"),
    false,
  );
});

test("archive rules preserve drafts across polls, validate input and surface revision conflicts", async (t) => {
  const h = await harness(t);
  await until(() => !h.$("archive-policy-save").disabled);
  assert.match(h.$("archive-rule-summary").textContent, /7 天/);
  h.input("archive-days", "14");
  await h.refresh();
  assert.equal(h.$("archive-days").value, "14");
  h.state.archivePolicy = {
    enabled: false,
    after_days: 30,
    revision: "other-panel",
  };
  h.$("archive-policy-save").click();
  await until(() =>
    h.$("archive-policy-alert").textContent.includes("重新加载"),
  );
  assert.equal(h.$("archive-days").value, "14");
  h.$("archive-policy-reload").click();
  await until(() => h.$("archive-days").value === "30");
  assert.equal(h.$("archive-enabled").checked, false);
  h.input("archive-days", "0");
  h.$("archive-policy-save").click();
  assert.match(h.$("archive-policy-alert").textContent, /1 到 3650/);
  h.input("archive-days", "14");
  h.$("archive-enabled").checked = true;
  h.$("archive-policy-save").click();
  await until(() => h.$("archive-policy-alert").textContent.includes("已保存"));
  assert.equal(h.state.archivePolicy.after_days, 14);
  assert.match(h.$("archive-rule-summary").textContent, /14 天/);
});

test("archive policy reads serialize with saves and leave operation feedback separate from polling", async (t) => {
  const h = await harness(t),
    pending = deferred();
  await until(() => !h.$("archive-policy-save").disabled);
  h.state.handlers["archive-policy"] = () => pending.promise;
  h.$("archive-policy-reload").click();
  assert.equal(h.$("archive-policy-save").disabled, true);
  h.$("archive-policy-save").click();
  pending.reject(new Error("policy unavailable"));
  await until(() =>
    h.$("archive-policy-alert").textContent.includes("unavailable"),
  );
  const data = await h.state.api("jobs", {});
  h.state.handlers.jobs = () => ({
    ...data,
    archive_error: "自动归档暂不可用",
  });
  await h.refresh();
  assert.match(
    h.$("archive-maintenance-alert").textContent,
    /自动归档暂不可用/,
  );
  assert.match(h.$("archive-policy-alert").textContent, /policy unavailable/);
  assert.equal(
    h.state.calls.filter(
      (call) => call.action === "archive-policy" && call.payload.revision,
    ).length,
    0,
  );
});

test("legacy preview is read-only and bulk double clicks consume one token even after failure", async (t) => {
  const state = fixture();
  state.jobs.push(job("c", "completed", { conversation_id: "" }));
  const h = await harness(t, state),
    pending = deferred();
  h.$("archive-preview").click();
  await until(() => !h.$("archive-bulk").disabled);
  assert.equal(state.jobs[2].archived, false);
  assert.match(h.$("archive-preview-list").textContent, /Review project c/);
  state.handlers["archive-bulk"] = () => pending.promise;
  h.$("archive-bulk").click();
  h.$("archive-bulk").click();
  assert.equal(
    state.calls.filter((call) => call.action === "archive-bulk").length,
    1,
  );
  pending.reject(new Error("response lost"));
  await until(() =>
    h.$("archive-bulk-alert").textContent.includes("response lost"),
  );
  assert.equal(h.$("archive-bulk").disabled, true);
  assert.equal(h.$("archive-preview").disabled, false);
  assert.match(h.$("archive-bulk-alert").textContent, /重新预览/);
});

test("expired archive previews cannot be confirmed", async (t) => {
  const state = fixture();
  state.jobs.push(job("c", "completed", { conversation_id: "" }));
  const h = await harness(t, state);
  h.$("archive-preview").click();
  await until(() => !h.$("archive-bulk").disabled);
  [...h.timers.values()].find((timer) => timer.ms === 300000).callback();
  assert.equal(h.$("archive-bulk").disabled, true);
  assert.match(h.$("archive-preview-note").textContent, /过期/);
  h.$("archive-bulk").click();
  assert.equal(
    state.calls.filter((call) => call.action === "archive-bulk").length,
    0,
  );
});

test("manual archive invalidates pending legacy previews and ignores late responses", async (t) => {
  const h = await harness(t),
    pending = deferred();
  h.state.handlers["archive-preview"] = () => pending.promise;
  h.$("archive-preview").click();
  h.findButton("归档任务").click();
  await until(() => h.$("history-alert").textContent.includes("已归档"));
  pending.resolve({
    token: "stale",
    job_count: 1,
    expires_in: 300,
    jobs: [h.state.jobs[0]],
  });
  await until(() => !h.$("archive-preview").disabled);
  assert.equal(h.$("archive-bulk").disabled, true);
  assert.match(h.$("archive-preview-note").textContent, /状态已变化/);
  assert.equal(h.$("archive-preview-list").children.length, 0);
});

test("bulk archive reasons and restored exemptions appear in task details", async (t) => {
  const state = fixture();
  state.jobs.push(job("c", "completed", { conversation_id: "" }));
  const h = await harness(t, state);
  h.$("archive-preview").click();
  await until(() => !h.$("archive-bulk").disabled);
  h.$("archive-bulk").click();
  await until(() =>
    h.$("archive-bulk-alert").textContent.includes("已归档 1 个"),
  );
  h.$("view-archived").click();
  await until(() =>
    h.$("jobs").querySelector('[data-job-id="cccccccccccccccc"]'),
  );
  h.select("c");
  assert.match(
    h.$("detail").querySelector(".archive-note").textContent,
    /旧任务批量归档/,
  );
  h.findButton("恢复任务").click();
  await until(() => state.jobs[2].auto_archive_exempt);
  h.$("view-all").click();
  await until(() =>
    h.$("jobs").querySelector('[data-job-id="cccccccccccccccc"]'),
  );
  h.select("c");
  assert.match(
    h.$("detail").querySelector(".archive-note").textContent,
    /免于自动归档/,
  );
});
async function harness(t, state = fixture()) {
  const dom = new JSDOM(html, {
    url: "http://127.0.0.1/token/",
    runScripts: "outside-only",
    pretendToBeVisual: true,
  });
  t.after(async () => {
    for (let i = 0; i < 12; i++) await tick();
    dom.window.close();
  });
  const window = dom.window,
    $ = (id) => window.document.getElementById(id);
  const timers = new Map(),
    intervals = [];
  let next = 0,
    hidden = false;
  window.setTimeout = (callback, ms) => {
    const id = ++next;
    timers.set(id, { callback, ms });
    return id;
  };
  window.clearTimeout = (id) => timers.delete(id);
  window.setInterval = (callback) => {
    intervals.push(callback);
    return ++next;
  };
  Object.defineProperty(window.document, "hidden", { get: () => hidden });
  Object.defineProperty(window.navigator, "clipboard", {
    value: {
      writeText: async (value) => {
        state.clipboard = value;
      },
    },
  });
  window.fetch = async (url, options = {}) => {
    const action = new URL(url).pathname.split("/").pop(),
      payload = options.body ? JSON.parse(options.body) : undefined;
    try {
      const value = await state.api(action, payload);
      return { ok: true, status: 200, json: async () => value };
    } catch (error) {
      return {
        ok: false,
        status: error.status || 400,
        json: async () => ({ error: error.message }),
      };
    }
  };
  window.eval(markdownScript);
  window.eval(script);
  await until(() => $("sync").textContent.includes("更新于"));
  await until(
    () =>
      $("detail").querySelector(".result")?.textContent.length > 0 ||
      !state.jobs[0]?.result_chars,
  );
  $("pause").click();
  const refresh = async () => {
    const before = state.calls.filter((item) => item.action === "jobs").length;
    $("refresh").click();
    await until(
      () =>
        state.calls.filter((item) => item.action === "jobs").length > before,
    );
    for (let i = 0; i < 5; i++) await tick();
  };
  const input = (id, value) => {
    $(id).value = value;
    $(id).dispatchEvent(new window.Event("input", { bubbles: true }));
  };
  const select = (id) =>
    $("jobs")
      .querySelector(`[data-job-id="${id.repeat(16)}"]`)
      .click();
  const findButton = (label) =>
    [...$("detail").querySelectorAll("button")].find((node) =>
      node.textContent.includes(label),
    );
  return {
    state,
    window,
    $,
    refresh,
    input,
    select,
    findButton,
    timers,
    intervals,
    hide(value) {
      hidden = value;
      window.document.dispatchEvent(new window.Event("visibilitychange"));
    },
  };
}

test("polling another job retains result selection, focus, scroll and pending copy", async (t) => {
  const h = await harness(t),
    pre = h.$("detail").querySelector(".result"),
    copy = h.findButton("复制完整结果");
  copy.focus();
  const range = h.window.document.createRange();
  range.setStart(pre.querySelector("p").firstChild, 0);
  range.setEnd(pre.querySelector("p").firstChild, 4);
  h.window.getSelection().removeAllRanges();
  h.window.getSelection().addRange(range);
  pre.scrollTop = 70;
  assert.equal(h.window.getSelection().toString(), "完整结果");
  h.state.jobs[1].progress.tool_calls++;
  await h.refresh();
  assert.equal(h.$("detail").querySelector(".result"), pre);
  assert.equal(h.window.getSelection().toString(), "完整结果");
  assert.equal(h.window.document.activeElement, copy);
  assert.equal(pre.scrollTop, 70);
  const pending = deferred();
  h.state.handlers.job = () => pending.promise;
  copy.click();
  assert.equal(copy.disabled, true);
  await h.refresh();
  assert.equal(h.findButton("复制中"), copy);
  assert.equal(copy.disabled, true);
  pending.resolve({
    result: "tail",
    offset: 20000,
    total_chars: 20004,
    next_offset: null,
  });
  await until(() => h.state.clipboard.endsWith("tail"));
});

test("submission queues a forced refresh behind an older request even while paused", async (t) => {
  const h = await harness(t),
    old = clone(h.state.jobs),
    pending = deferred();
  let count = 0;
  h.state.handlers.jobs = async () =>
    ++count === 1
      ? pending.promise
      : { jobs: clone(h.state.jobs), updated_at: h.state.updated };
  h.$("refresh").click();
  await until(() => count === 1);
  h.$("tab-task").click();
  h.input("task-prompt", "new task");
  h.$("task-form").dispatchEvent(
    new h.window.Event("submit", { cancelable: true }),
  );
  await until(() => h.state.jobs[0].job_id === "c".repeat(16));
  for (let i = 0; i < 3; i++) await tick();
  pending.resolve({ jobs: old, updated_at: h.state.updated });
  await until(
    () => count === 2 && h.$("detail").textContent.includes("Review project c"),
  );
  assert.match(h.$("sync").textContent, /自动刷新已暂停 · 更新于/);
});

test("removed selections clear old details and action buttons", async (t) => {
  const h = await harness(t);
  h.select("b");
  assert.ok(h.findButton("终止任务"));
  h.state.jobs = [h.state.jobs[0]];
  await h.refresh();
  assert.match(h.$("detail").textContent, /所选任务不在当前列表/);
  assert.equal(h.$("detail").querySelector("button"), null);
});

test("cancellation errors remain attached to the originating job", async (t) => {
  const h = await harness(t),
    pending = deferred();
  h.state.handlers.cancel = () => pending.promise;
  h.select("b");
  h.findButton("终止任务").click();
  h.select("a");
  pending.reject(new Error("cancel b failed"));
  for (let i = 0; i < 8; i++) await tick();
  assert.doesNotMatch(h.$("detail").textContent, /cancel b failed/);
  h.select("b");
  assert.match(h.$("detail").textContent, /cancel b failed/);
  await h.refresh();
  assert.match(h.$("detail").textContent, /cancel b failed/);
});

test("provider drafts survive switches and stale saves show conflicts", async (t) => {
  const h = await harness(t);
  h.$("tab-settings").click();
  for (let i = 0; i < 4; i++) await tick();
  h.$("providers").querySelector('[data-backend="claude"]').click();
  h.input("base-url", "https://draft.example.com");
  h.input("api-key", "draft-fixture");
  h.input("model", "draft-model");
  h.$("providers").querySelector('[data-backend="gemini"]').click();
  h.$("providers").querySelector('[data-backend="claude"]').click();
  assert.equal(h.$("model").value, "draft-model");
  assert.equal(h.$("api-key").value, "draft-fixture");
  Object.assign(h.state.config.profiles.claude, {
    revision: "claude-r2",
    model: "external-model",
  });
  h.$("tab-monitor").click();
  h.$("tab-settings").click();
  await until(() => !h.$("settings-conflict").hidden);
  assert.equal(h.$("model").value, "draft-model");
  h.$("save").click();
  await until(() => h.$("settings-alert").textContent.includes("配置已更新"));
  assert.equal(h.state.config.profiles.claude.model, "external-model");
  assert.equal(h.window.localStorage.length, 0);
  assert.equal(h.window.sessionStorage.length, 0);
  await until(() => !h.$("reload-config").disabled);
  h.$("reload-config").click();
  await until(() => h.$("model").value === "external-model");
  assert.equal(h.$("api-key").value, "");
});

test("late model responses from old credential revisions cannot refill the task cache", async (t) => {
  const h = await harness(t),
    pending = deferred();
  let count = 0;
  h.state.handlers.models = async (payload) =>
    ++count === 1
      ? pending.promise
      : { revision: payload.revision, models: ["new-credential-model"] };
  h.$("tab-task").click();
  await until(() => count === 1);
  h.state.config.profiles.claude.revision = "claude-r2";
  h.$("tab-settings").click();
  h.$("tab-task").click();
  await until(() =>
    h.$("task-model").textContent.includes("new-credential-model"),
  );
  pending.resolve({ revision: "claude-r1", models: ["old-credential-model"] });
  for (let i = 0; i < 8; i++) await tick();
  assert.doesNotMatch(h.$("task-model").textContent, /old-credential-model/);
  assert.match(h.$("task-model").textContent, /new-credential-model/);
});

test("changing draft credentials invalidates settings model catalogs", async (t) => {
  const h = await harness(t);
  h.$("tab-settings").click();
  h.$("providers").querySelector('[data-backend="claude"]').click();
  h.$("fetch-models").click();
  await until(() => !h.$("model-browser").hidden);
  await until(() => !h.$("api-key").disabled);
  h.input("api-key", "different-fixture");
  assert.equal(h.$("model-browser").hidden, true);
  assert.equal(h.$("models").children.length, 0);
});

test("complete copy follows server offsets and includes astral Unicode past the preview", async (t) => {
  const h = await harness(t),
    full = h.state.results[h.state.jobs[0].job_id];
  assert.notEqual(h.$("detail").querySelector(".result").textContent, full);
  h.findButton("复制完整结果").click();
  await until(() => h.state.clipboard === full);
  assert.deepEqual(
    h.state.calls
      .filter((item) => item.action === "job")
      .map((item) => item.payload.offset),
    [0, 20000, 40000, 60000],
  );
  assert.match(h.$("detail").textContent, /完整结果已复制/);
});

test("results preview Markdown, retain per-task mode and copy the exact paginated source", async (t) => {
  const state = fixture();
  const full = markdownResult + "分页内容🙂\r\n".repeat(7000) + "\r\n**最后一页**\r\n";
  state.results[state.jobs[0].job_id] = full;
  state.jobs[0].result_chars = Array.from(full).length;
  const h = await harness(t, state),
    result = h.$("detail").querySelector(".result");
  assert.equal(result.querySelector("h1").textContent, "审查结果");
  assert.equal(result.querySelector("h2").textContent, "已完成");
  assert.equal(result.querySelector("strong").textContent, "重点");
  assert.equal(result.querySelector("em").textContent, "说明");
  assert.equal(result.querySelectorAll("ul li").length, 2);
  assert.equal(result.querySelectorAll("ol li").length, 2);
  assert.equal(result.querySelectorAll("table tbody tr").length, 1);
  assert.match(result.querySelector("pre code").textContent, /<script>literal<\/script>/);
  assert.match(result.querySelector("blockquote").textContent, /引用说明/);
  assert.equal(result.querySelector("a").target, "_blank");
  assert.equal(result.querySelector("a").rel, "noopener noreferrer");
  h.findButton("查看原文").click();
  assert.equal(result.querySelector("h1"), null);
  assert.equal(result.textContent, Array.from(full).slice(0, 20000).join(""));
  h.select("b");
  h.select("a");
  assert.ok(h.findButton("Markdown 预览"));
  h.findButton("Markdown 预览").click();
  h.findButton("复制完整结果").click();
  await until(() => state.clipboard === full);
  assert.equal(result.querySelectorAll("strong")[1].textContent, "最后一页");
  h.findButton("查看原文").click();
  assert.equal(result.textContent, full);
  state.clipboard = "";
  h.findButton("复制完整结果").click();
  await until(() => h.$("detail").textContent.includes("完整结果已复制"));
  assert.equal(state.clipboard, full);
});

test("Markdown renders HTML and unsafe links as inert text and never loads images", async (t) => {
  const state = fixture();
  const full = [
    '<script>window.previewAttack = true</script>',
    '<img src=x onerror="window.previewAttack = true">',
    '<svg onload="window.previewAttack = true"></svg>',
    '[unsafe](javascript:alert(1))',
    '[encoded](jav&#x61;script:alert(1))',
    '[file](file:///tmp/private)',
    '[data](data:text/html;base64,PHNjcmlwdD4=)',
    '![remote <img>](https://example.com/tracker.png)',
    '![inline](data:image/png;base64,aGVsbG8=)',
    '[safe](https://example.com/ "quoted & title")',
  ].join("\n\n");
  state.results[state.jobs[0].job_id] = full;
  state.jobs[0].result_chars = Array.from(full).length;
  const h = await harness(t, state),
    result = h.$("detail").querySelector(".result");
  assert.equal(result.querySelectorAll("script, img, svg, iframe, object, style").length, 0);
  assert.equal(result.querySelectorAll("a").length, 1);
  assert.equal(result.querySelector("a").href, "https://example.com/");
  assert.match(result.textContent, /图片：remote <img>/);
  assert.match(result.textContent, /<script>window.previewAttack/);
  assert.equal(h.window.previewAttack, undefined);
  h.findButton("复制完整结果").click();
  await until(() => state.clipboard === full);
});

test("plain text mode removes preview markup even when the text content is identical", async (t) => {
  const state = fixture();
  state.results[state.jobs[0].job_id] = "Hello\n";
  state.jobs[0].result_chars = 6;
  const h = await harness(t, state),
    result = h.$("detail").querySelector(".result");
  assert.equal(result.textContent, "Hello\n");
  assert.ok(result.querySelector("p"));
  h.findButton("查看原文").click();
  assert.equal(result.textContent, "Hello\n");
  assert.equal(result.children.length, 0);
  await h.refresh();
  assert.equal(result.children.length, 0);
});

test("Markdown reference links resolve when the next page arrives without duplicate source", async (t) => {
  const state = fixture(),
    first = "[结果链接][ref]\n\n";
  const full = first + " ".repeat(20000 - Array.from(first).length) +
    "\n\n[ref]: https://example.com/result\n";
  state.results[state.jobs[0].job_id] = full;
  state.jobs[0].result_chars = Array.from(full).length;
  const h = await harness(t, state),
    result = h.$("detail").querySelector(".result");
  assert.equal(result.querySelector("a"), null);
  h.findButton("加载更多").click();
  await until(() => result.querySelector("a"));
  assert.equal(result.querySelector("a").href, "https://example.com/result");
  h.findButton("查看原文").click();
  assert.equal(result.textContent, full);
});

test("pagination failure keeps loaded text and retries the failed page", async (t) => {
  const h = await harness(t),
    original = h.$("detail").querySelector(".result").textContent;
  h.state.handlers.job = () => {
    throw new Error("page unavailable");
  };
  h.findButton("加载更多").click();
  await until(() => h.findButton("重试读取结果"));
  assert.equal(h.$("detail").querySelector(".result").textContent, original);
  delete h.state.handlers.job;
  h.findButton("重试读取结果").click();
  await until(
    () =>
      h.$("detail").querySelector(".result").textContent.length >
      original.length,
  );
  assert.deepEqual(
    h.state.calls
      .filter((item) => item.action === "job")
      .map((item) => item.payload.offset),
    [0, 20000, 20000],
  );
});

test("running diff previews refresh once after completion and retain scroll on unrelated polls", async (t) => {
  const h = await harness(t);
  h.select("b");
  h.findButton("查看变更").click();
  await until(() => h.$("detail").querySelector(".diff").textContent.length);
  const pre = h.$("detail").querySelector(".diff");
  pre.scrollTop = 90;
  await h.refresh();
  assert.equal(h.$("detail").querySelector(".diff"), pre);
  assert.equal(pre.scrollTop, 90);
  assert.match(h.$("detail").textContent, /预览可能已过期/);
  h.state.handlers.changes = () => ({
    diff: "final diff",
    untracked_files: ["first file", "second file"],
  });
  h.state.jobs[1].status = "completed";
  h.state.jobs[1].finished_at = "2026-11-03T00:12:00Z";
  await h.refresh();
  await until(() => pre.textContent === "final diff");
  await h.refresh();
  assert.equal(
    h.state.calls.filter((item) => item.action === "changes").length,
    2,
  );
  assert.equal(h.$("detail").querySelectorAll("ul li").length, 2);
});

test("hidden tabs retain activity nodes and document visibility suspends polling", async (t) => {
  const h = await harness(t),
    events = h.$("detail").querySelector(".events");
  events.scrollTop = 110;
  h.$("tab-settings").click();
  h.state.jobs[0].progress.events.push({
    timestamp: "2026-11-03T00:01:00Z",
    label: "new",
  });
  await h.refresh();
  assert.equal(events.scrollTop, 110);
  assert.equal(events.children.length, 25);
  h.$("tab-monitor").click();
  assert.equal(h.$("detail").querySelector(".events"), events);
  assert.equal(events.children.length, 26);
  h.$("pause").click();
  await tick();
  h.hide(true);
  for (let i = 0; i < 4; i++) await tick();
  assert.equal(
    [...h.timers.values()].some((item) => item.ms === 1500),
    false,
  );
  const count = h.state.calls.filter((item) => item.action === "jobs").length;
  h.hide(false);
  await until(
    () => h.state.calls.filter((item) => item.action === "jobs").length > count,
  );
});

test("poll failures back off and successful refresh resets the interval", async (t) => {
  const h = await harness(t);
  h.state.handlers.jobs = () => {
    throw new Error("offline");
  };
  h.$("pause").click();
  await until(() => [...h.timers.values()].some((item) => item.ms === 3000));
  await h.refresh();
  assert.ok([...h.timers.values()].some((item) => item.ms === 6000));
  delete h.state.handlers.jobs;
  await h.refresh();
  assert.ok([...h.timers.values()].some((item) => item.ms === 1500));
});

test("keyboard tabs, task selection, provider ARIA and terminal duration are consistent", async (t) => {
  const h = await harness(t);
  assert.equal(h.$("task-backend").value, "claude");
  h.$("tab-monitor").dispatchEvent(
    new h.window.KeyboardEvent("keydown", { key: "ArrowRight", bubbles: true }),
  );
  assert.equal(h.window.document.activeElement, h.$("tab-task"));
  assert.equal(h.$("tab-task").tabIndex, 0);
  assert.equal(h.$("tab-monitor").tabIndex, -1);
  h.$("tab-task").dispatchEvent(
    new h.window.KeyboardEvent("keydown", { key: "End" }),
  );
  h.$("providers").querySelector('[data-backend="claude"]').click();
  assert.equal(h.$("task-model").hasAttribute("aria-pressed"), false);
  assert.ok(h.window.document.querySelector("fieldset legend"));
  h.$("tab-monitor").click();
  h.state.jobs[0].status = "interrupted";
  h.state.jobs[0].finished_at = null;
  await h.refresh();
  h.intervals.forEach((callback) => callback());
  assert.equal(h.$("detail").querySelector("[data-duration]").textContent, "—");
  assert.equal(h.$("detail").hasAttribute("aria-live"), false);
});

test("project search distinguishes an empty filter from empty task history", async (t) => {
  const h = await harness(t);
  h.input("search", "C:/Projects/project-b");
  await until(() => h.$("jobs").querySelectorAll(".job").length === 1);
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 1);
  h.input("search", "no-match");
  await until(() => h.$("jobs").textContent.includes("没有匹配的任务"));
  assert.match(h.$("jobs").textContent, /没有匹配的任务/);
  h.$("jobs").querySelector("button").click();
  await until(() => h.$("jobs").querySelectorAll(".job").length === 2);
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 2);
  h.state.jobs = [];
  await h.refresh();
  assert.match(h.$("jobs").textContent, /此视图暂无任务/);
});

test("conversation views isolate legacy tasks, show other running work and restore archives", async (t) => {
  const state = fixture();
  state.jobs.push(
    job("d", "running", { conversation_id: "other-chat" }),
    job("e", "completed", { conversation_id: "" }),
  );
  const h = await harness(t, state);
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 2);
  assert.match(h.$("other-running").textContent, /1 个任务运行中/);
  assert.equal(h.$("view-current").getAttribute("aria-pressed"), "true");
  h.findButton("归档任务").click();
  await until(() => h.$("history-alert").textContent.includes("已归档"));
  await until(() => h.$("jobs").querySelectorAll(".job").length === 1);
  h.$("view-archived").click();
  await until(() => h.findButton("恢复任务"));
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 1);
  h.findButton("恢复任务").click();
  await until(() => !state.jobs[0].archived);
  h.$("view-all").click();
  await until(() => h.$("jobs").querySelectorAll(".job").length === 4);
  assert.match(h.$("jobs").textContent, /未归属/);
  h.$("other-running").click();
  await until(() => h.$("jobs").querySelectorAll(".job").length === 2);
  assert.equal(h.$("job-status").value, "running");
  assert.equal(h.findButton("归档任务").disabled, true);
});

test("a late list response cannot refill a different view or search", async (t) => {
  const h = await harness(t),
    pending = deferred();
  h.state.handlers.jobs = () => pending.promise;
  h.$("refresh").click();
  await tick();
  h.$("view-archived").click();
  h.input("search", "archived-only");
  delete h.state.handlers.jobs;
  pending.resolve({
    jobs: clone(h.state.jobs),
    total: 2,
    updated_at: h.state.updated,
  });
  await until(() => h.$("jobs").textContent.includes("没有匹配的任务"));
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 0);
  assert.equal(h.$("view-archived").getAttribute("aria-pressed"), "true");
  const last = h.state.calls
    .filter((call) => call.action === "jobs")
    .at(-1).payload;
  assert.equal(last.view, "archived");
  assert.equal(last.search, "archived-only");
});

test("server pagination and searching find tasks beyond the first fifty", async (t) => {
  const state = fixture();
  for (let index = 3; index <= 62; index++)
    state.jobs.push(
      job("d", "completed", {
        job_id: index.toString(16).padStart(16, "0"),
        title: `History ${index}`,
      }),
    );
  const h = await harness(t, state);
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 50);
  h.$("page-next").click();
  await until(() => h.$("page-info").textContent === "51–62 / 62");
  assert.equal(h.$("page-next").disabled, true);
  h.input("search", "History 61");
  await until(() => h.$("page-info").textContent === "1–1 / 1");
  assert.match(h.$("jobs").textContent, /History 61/);
  assert.equal(h.$("page-prev").disabled, true);
});

test("finishing tasks stay above exceptions without losing selection, focus or collapsed history", async (t) => {
  const state = fixture();
  state.jobs.push(job("c", "completed"), job("d", "completed"), job("e", "completed"), job("f", "failed"));
  const h = await harness(t, state);
  const completed = h.$("jobs").querySelector(".completed-group"),
    attention = h.$("jobs").querySelector(".attention-group"),
    running = h.$("jobs").querySelector('[data-job-id="bbbbbbbbbbbbbbbb"]');
  completed.open = attention.open = false;
  running.click();
  running.focus();
  const finished = state.jobs.splice(1, 1)[0];
  finished.status = "completed";
  finished.finished_at = "2026-11-03T00:12:00Z";
  state.jobs.unshift(finished); // The server promotes the newly completed task before paging.
  await h.refresh();
  const recent = h.$("jobs").querySelector(".recent-completed");
  assert.equal(recent.querySelector(".job"), running);
  assert.equal(running.getAttribute("aria-pressed"), "true");
  assert.equal(h.window.document.activeElement, running);
  assert.equal(h.$("detail").querySelector("h2").textContent, finished.title);
  assert.equal(h.$("jobs").children[0], recent);
  assert.equal(h.$("jobs").children[1], attention);
  assert.equal(attention.open, false);
  assert.equal(completed.open, false);
  assert.equal(h.$("jobs").querySelectorAll(".job").length, state.jobs.length);
  state.config.app_settings.values.recent_completed_count = 1;
  await h.refresh();
  assert.equal(recent.querySelectorAll(".job").length, 1);
  assert.equal(completed.querySelectorAll(".job").length, 4);
  change(h, "job-status", "completed");
  await until(() => h.$("jobs").querySelectorAll(":scope > .job").length === 5);
  assert.equal(h.$("jobs").querySelectorAll("details").length, 0);
});

test("storage uses a reviewed preview before cleanup and preserves editable retention on refresh", async (t) => {
  const h = await harness(t);
  h.$("tab-storage").click();
  await until(() => !h.$("storage-save").disabled);
  assert.match(h.$("storage-note").textContent, /2 个任务/);
  assert.equal(h.$("storage-cleanup").disabled, true);
  h.input("retention-days", "90");
  h.$("storage-refresh").click();
  await until(() => !h.$("storage-refresh").disabled);
  assert.equal(h.$("retention-days").value, "90");
  h.$("storage-save").click();
  await until(() => h.state.storage.policy.retention_days === 90);
  await until(() => !h.$("storage-preview").disabled);
  h.$("storage-preview").click();
  await until(() => !h.$("storage-cleanup").disabled);
  assert.match(h.$("cleanup-summary").textContent, /1 个任务、2 个日志文件/);
  assert.equal(
    h.state.calls.some((call) => call.action === "storage-cleanup"),
    false,
  );
  h.$("storage-cleanup").click();
  await until(() => h.$("storage-alert").textContent.includes("已清理"));
  await until(() => !h.$("storage-refresh").disabled);
  assert.equal(h.$("storage-cleanup").disabled, true);
  assert.equal(h.state.storage.bytes.logs, 0);
  assert.ok(h.state.jobs[0].result_chars > 0);
});

test("changing retention invalidates an in-flight preview and stale errors", async (t) => {
  const h = await harness(t);
  h.$("tab-storage").click();
  await until(() => !h.$("storage-save").disabled);
  const pending = deferred();
  h.state.handlers["storage-preview"] = () => pending.promise;
  h.$("storage-preview").click();
  h.input("retention-days", "60");
  pending.resolve({
    token: "stale",
    file_count: 10,
    job_count: 1,
    bytes: 100,
    jobs: [],
    expires_in: 300,
  });
  await until(() => !h.$("storage-preview").disabled);
  assert.equal(h.$("cleanup-preview").hidden, true);
  assert.equal(h.$("storage-cleanup").disabled, true);
  delete h.state.handlers["storage-preview"];
  h.$("storage-preview").click();
  await until(() => !h.$("storage-cleanup").disabled);
  h.input("retention-days", "120");
  assert.equal(h.$("storage-cleanup").disabled, true);
});

test("cleanup failures consume the UI preview and repeated clicks send only one request", async (t) => {
  const h = await harness(t);
  h.$("tab-storage").click();
  await until(() => !h.$("storage-save").disabled);
  h.$("storage-preview").click();
  await until(() => !h.$("storage-cleanup").disabled);
  const pending = deferred();
  h.state.handlers["storage-cleanup"] = () => pending.promise;
  h.$("storage-cleanup").click();
  h.$("storage-cleanup").click();
  assert.equal(
    h.state.calls.filter((call) => call.action === "storage-cleanup").length,
    1,
  );
  pending.reject(new Error("预览已过期"));
  await until(() => h.$("storage-alert").textContent.includes("预览已过期"));
  assert.equal(h.$("storage-cleanup").disabled, true);
  assert.equal(h.$("storage-preview").disabled, false);
  assert.equal(h.$("retention-days").disabled, false);
});

test("a statistics error after cleanup does not hide the completed deletion result", async (t) => {
  const h = await harness(t);
  h.$("tab-storage").click();
  await until(() => !h.$("storage-save").disabled);
  h.$("storage-preview").click();
  await until(() => !h.$("storage-cleanup").disabled);
  h.state.handlers.storage = () => {
    throw new Error("statistics offline");
  };
  h.$("storage-cleanup").click();
  await until(() =>
    h.$("storage-alert").textContent.includes("最新统计读取失败"),
  );
  assert.match(
    h.$("storage-alert").textContent,
    /已清理 1 个任务的 2 个日志文件/,
  );
  assert.equal(h.$("storage-cleanup").disabled, true);
});

test("a delayed configuration read cannot undo a successful save", async (t) => {
  const h = await harness(t),
    pending = deferred(),
    old = clone(h.state.config);
  h.$("tab-settings").click();
  for (let i = 0; i < 5; i++) await tick();
  h.$("providers").querySelector('[data-backend="claude"]').click();
  h.input("model", "saved-new-model");
  h.state.handlers.config = (payload) =>
    payload
      ? {
          ...clone(h.state.config),
          profiles: {
            ...clone(h.state.config.profiles),
            claude: {
              ...clone(h.state.config.profiles.claude),
              revision: "claude-saved",
              model: "saved-new-model",
            },
          },
        }
      : pending.promise;
  h.$("tab-settings").click();
  h.$("save").click();
  await until(() => h.$("settings-alert").textContent.includes("配置已保存"));
  pending.resolve(old);
  for (let i = 0; i < 6; i++) await tick();
  assert.equal(h.$("model").value, "saved-new-model");
  assert.match(h.$("task-profile").textContent, /saved-new-model/);
});

test("failed terminal diff refresh keeps a visible stale marker and does not retry forever", async (t) => {
  const h = await harness(t);
  h.select("b");
  h.findButton("查看变更").click();
  await until(() => h.$("detail").querySelector(".diff").textContent.length);
  h.state.handlers.changes = () => {
    throw new Error("diff unavailable");
  };
  h.state.jobs[1].status = "completed";
  await h.refresh();
  await until(() => h.$("detail").textContent.includes("diff unavailable"));
  await h.refresh();
  assert.equal(
    h.state.calls.filter((item) => item.action === "changes").length,
    2,
  );
  assert.match(h.$("detail").textContent, /预览可能已过期/);
});

test("testing is separate from saving and a deliberate task provider is preserved", async (t) => {
  const h = await harness(t);
  h.$("tab-task").click();
  h.$("task-backend").value = "gemini";
  h.$("task-backend").dispatchEvent(new h.window.Event("change"));
  h.$("tab-settings").click();
  h.$("providers").querySelector('[data-backend="claude"]').click();
  h.$("test").click();
  await until(() =>
    h.$("settings-alert").textContent.includes("测试不会保存配置"),
  );
  assert.equal(
    h.state.calls.filter(
      (item) => item.action === "config" && item.payload.backend,
    ).length,
    0,
  );
  h.$("tab-task").click();
  for (let i = 0; i < 5; i++) await tick();
  assert.equal(h.$("task-backend").value, "gemini");
});

test("running tasks load the entire description on expansion and retain it across polls and switches", async (t) => {
  const h = await harness(t),
    full = "完整要求🙂\n".repeat(12000) + "描述末尾",
    before = h.state.calls.filter((item) => item.action === "job").length;
  h.state.descriptions["b".repeat(16)] = { text: full, complete: true };
  h.select("b");
  assert.equal(
    h.state.calls.filter((item) => item.action === "job").length,
    before,
  );
  const details = h.$("detail").querySelector(".task-description");
  details.open = true;
  details.dispatchEvent(new h.window.Event("toggle"));
  await until(() => details.querySelector("pre").textContent === full);
  const pre = details.querySelector("pre");
  assert.match(details.textContent, /完整任务描述/);
  assert.equal(
    h.$("detail").querySelector("h2").textContent,
    "Review project b",
  );
  await h.refresh();
  h.select("a");
  h.select("b");
  assert.equal(h.$("detail").querySelector(".task-description"), details);
  assert.equal(details.open, true);
  assert.equal(details.querySelector("pre"), pre);
  assert.equal(pre.textContent, full);
  assert.equal(
    h.state.calls.filter((item) => item.action === "job").length,
    before + 1,
  );
  assert.ok(
    h.state.calls
      .filter(
        (item) => item.action === "job" && item.payload.max_chars === 20000,
      )
      .every((item) => item.payload.include_description === false),
  );
});

test("description errors are retryable and legacy fragments are clearly marked", async (t) => {
  const h = await harness(t);
  h.select("b");
  const pending = deferred();
  h.state.handlers.job = () => pending.promise;
  const details = h.$("detail").querySelector(".task-description");
  details.open = true;
  details.dispatchEvent(new h.window.Event("toggle"));
  h.select("a");
  pending.reject(new Error("description unavailable"));
  for (let i = 0; i < 6; i++) await tick();
  assert.doesNotMatch(h.$("detail").textContent, /description unavailable/);
  h.select("b");
  assert.match(details.textContent, /description unavailable/);
  delete h.state.handlers.job;
  h.state.descriptions["b".repeat(16)] = {
    text: "历史内容只有这些",
    complete: false,
  };
  h.findButton("重试读取描述").click();
  await until(
    () => details.querySelector("pre").textContent === "历史内容只有这些",
  );
  assert.match(details.textContent, /历史任务未保存完整描述/);
});

test("connectivity form submits a short title and simple automatic choice without copying the saved model", async (t) => {
  const state = fixture();
  state.config.profiles.claude.auto_select_model = true;
  const h = await harness(t, state);
  h.$("tab-task").click();
  h.input("task-title", "检查 Claude 连接");
  h.input("task-prompt", "仅回复 OK");
  h.$("task-complexity").value = "complex";
  h.$("task-kind").value = "connectivity";
  h.$("task-kind").dispatchEvent(new h.window.Event("change"));
  assert.equal(h.$("task-complexity-field").hidden, true);
  assert.equal(
    h.window.document.querySelector("[name=mode][value=edit]").disabled,
    true,
  );
  h.$("task-form").dispatchEvent(
    new h.window.Event("submit", { cancelable: true }),
  );
  await until(() => state.calls.some((item) => item.action === "tasks"));
  const payload = state.calls.find((item) => item.action === "tasks").payload;
  assert.equal(payload.title, "检查 Claude 连接");
  assert.equal(payload.task, "仅回复 OK");
  assert.equal(payload.task_kind, "connectivity");
  assert.equal(payload.task_complexity, "simple");
  assert.equal(payload.mode, "review");
  assert.equal(payload.model, "");
  h.$("task-kind").value = "general";
  h.$("task-kind").dispatchEvent(new h.window.Event("change"));
  assert.equal(h.$("task-complexity-field").hidden, false);
  assert.equal(h.$("task-complexity").value, "complex");
});
