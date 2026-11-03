const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const html = fs.readFileSync(path.join(root, "dashboard.html"), "utf8");
const script = fs.readFileSync(path.join(root, "dashboard.js"), "utf8");
const clone = (value) => JSON.parse(JSON.stringify(value));
const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
};
function job(id, status = "running", overrides = {}) {
  return {
    job_id: id.repeat(16),
    title: `Review project ${id}`,
    backend: "claude",
    runtime: "Claude Code",
    model: "claude-fixture",
    mode: "edit",
    project_name: `project-${id}`,
    project_dir: `C:/Projects/project-${id}`,
    workspace: {
      path: `C:/Worktrees/${id}`,
      strategy: "worktree",
      branch: `agent/${id}`,
    },
    started_at: "2026-11-03T00:00:00Z",
    finished_at: status === "running" ? null : "2026-11-03T00:10:00Z",
    status,
    can_cancel: status === "running",
    result_chars: 0,
    progress: {
      phase: "读取文件",
      model_calls: 2,
      tool_calls: 3,
      events: Array.from({ length: 25 }, (_, i) => ({
        timestamp: `2026-11-03T00:00:${String(i).padStart(2, "0")}Z`,
        label: `读取文件 ${i}`,
      })),
    },
    ...overrides,
  };
}
function fixture() {
  const profiles = Object.fromEntries(
    ["gemini", "claude", "deepseek"].map((backend) => [
      backend,
      {
        backend,
        base_url: "https://relay.example.com",
        model: `${backend}-fixture`,
        effort: "",
        auto_select_model: false,
        ready: backend === "claude",
        key_configured: backend === "claude",
        key_source: backend === "claude" ? "local" : "none",
        revision: `${backend}-r1`,
        runtime: `${backend} runtime`,
      },
    ]),
  );
  const state = {
    config: {
      profiles,
      runtimes: {},
      default_project_dir: "C:/Projects/example",
    },
    jobs: [job("a", "completed"), job("b")],
    results: {},
    calls: [],
    handlers: {},
    clipboard: "",
    updated: "2026-11-03T10:20:30Z",
  };
  state.results[state.jobs[0].job_id] = "完整结果🙂\n".repeat(12000);
  state.jobs[0].result_chars = Array.from(
    state.results[state.jobs[0].job_id],
  ).length;
  state.api = async (action, payload) => {
    state.calls.push({ action, payload: clone(payload || {}) });
    if (state.handlers[action]) return state.handlers[action](payload);
    if (action === "jobs")
      return { jobs: clone(state.jobs), updated_at: state.updated };
    if (action === "config" && !payload) return clone(state.config);
    if (action === "config") {
      const current = state.config.profiles[payload.backend];
      if (payload.revision !== current.revision)
        throw Object.assign(new Error("配置已更新"), { status: 409 });
      Object.assign(current, {
        base_url: payload.base_url,
        model: payload.model,
        effort: payload.effort,
        auto_select_model: payload.auto_select_model,
        revision: current.revision + "-saved",
        ready: true,
      });
      return clone(state.config);
    }
    if (action === "models")
      return {
        backend: payload.backend,
        base_url: payload.base_url,
        revision: payload.revision,
        models: [`${payload.backend}-fixture`, `${payload.backend}-extra`],
        total_count: 2,
      };
    if (action === "test") return { reply: "OK" };
    if (action === "changes")
      return {
        diff: "diff --git a/example b/example\n+new line",
        untracked_files: ["one.txt", "two.txt"],
      };
    if (action === "job") {
      const summary = state.jobs.find((item) => item.job_id === payload.job_id);
      const chars = Array.from(state.results[payload.job_id] || ""),
        offset = payload.offset || 0,
        max = payload.max_chars || 20000;
      return {
        ...clone(summary),
        result: chars.slice(offset, offset + max).join(""),
        offset,
        total_chars: chars.length,
        next_offset: offset + max < chars.length ? offset + max : null,
      };
    }
    if (action === "tasks") {
      const created = job("c");
      state.jobs.unshift(created);
      return clone(created);
    }
    if (action === "cancel") {
      const target = state.jobs.find((item) => item.job_id === payload.job_id);
      target.status = "cancelled";
      target.can_cancel = false;
      target.finished_at = "2026-11-03T00:11:00Z";
      return { job_id: target.job_id, status: target.status };
    }
    throw new Error(`Unhandled fixture action: ${action}`);
  };
  return state;
}
module.exports = { html, script, fixture, job, clone, deferred };
