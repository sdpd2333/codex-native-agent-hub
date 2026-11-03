const { test } = require("node:test");
const assert = require("node:assert/strict");
const { JSDOM } = require("jsdom");
const {
  html,
  script,
  fixture,
  job,
  clone,
  deferred,
} = require("./dashboard-fixture.cjs");
const tick = () => new Promise((resolve) => setImmediate(resolve));
async function until(predicate) {
  for (let i = 0; i < 80; i++) {
    if (predicate()) return;
    await tick();
  }
  assert.ok(predicate(), "Expected UI state did not arrive");
}
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
  range.setStart(pre.firstChild, 0);
  range.setEnd(pre.firstChild, 4);
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
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 1);
  h.input("search", "no-match");
  assert.match(h.$("jobs").textContent, /没有匹配的任务/);
  h.$("jobs").querySelector("button").click();
  assert.equal(h.$("jobs").querySelectorAll(".job").length, 2);
  h.state.jobs = [];
  await h.refresh();
  assert.match(h.$("jobs").textContent, /还没有任务/);
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
