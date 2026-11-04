const fs = require("node:fs");
const path = require("node:path");
const root = path.resolve(__dirname, "..");
const settingsSchema = require("../settings-schema.json");
const settingsDefaults = Object.fromEntries(Object.entries(settingsSchema).map(([name, field]) => [name, field.default]));
const html = fs.readFileSync(path.join(root, "dashboard.html"), "utf8");
const script = fs.readFileSync(path.join(root, "dashboard.js"), "utf8");
const markdownScript = fs.readFileSync(path.join(root, "vendor/markdown-it.min.js"), "utf8");
const markdownResult = [
  "完整结果🙂",
  "",
  "# 审查结果",
  "",
  "## 已完成",
  "",
  "**重点**与*说明*、`inline <code>`。",
  "",
  "- 第一项",
  "- 第二项",
  "",
  "1. 检查",
  "2. 验证",
  "",
  "> 引用说明",
  "",
  "| 项目 | 状态 | 模型 | 调用 | 备注 |",
  "| --- | --- | --- | --- | --- |",
  "| " + "长路径/".repeat(30) + " | 通过 | 示例 | 2 | 已验证 |",
  "",
  "```js",
  "const value = '<script>literal</script>'; // " + "long-code-".repeat(35),
  "```",
  "",
  "[参考](https://example.com/guide)",
  "",
].join("\r\n");
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
    conversation_id: "fixture-chat",
    archived: false,
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
        local_sync: {
          mode: "off",
          revision: "sync-r1",
          fields: [],
          status: "off",
          message: "本地同步已关闭。",
        },
      },
    ]),
  );
  const state = {
    config: {
      app_settings: { schema: clone(settingsSchema), values: clone(settingsDefaults), saved_values: clone(settingsDefaults),
        revision: "app-r1", overrides: {} },
      profiles,
      runtimes: {},
      default_project_dir: "C:/Projects/example",
      panel_context: { conversation_id: "fixture-chat" },
      routing_policy: {
        enabled: false,
        revision: "rules-r1",
        rules: [],
        fallback: [],
      },
    },
    jobs: [job("a", "completed"), job("b")],
    results: {},
    descriptions: {},
    calls: [],
    handlers: {},
    clipboard: "",
    updated: "2026-11-03T10:20:30Z",
    archivePolicy: { enabled: true, after_days: 7, revision: "archive-r1" },
    storage: {
      bytes: { records: 4096, logs: 8192, worktrees: 1024, other: 512 },
      total_bytes: 13824,
      job_count: 2,
      file_count: 8,
      skipped: 0,
      partial: false,
      policy: { retention_days: 30, revision: "storage-r1" },
    },
  };
  state.results[state.jobs[0].job_id] = "完整结果🙂\n".repeat(12000);
  state.jobs[0].result_chars = Array.from(
    state.results[state.jobs[0].job_id],
  ).length;
  for (const item of state.jobs)
    state.descriptions[item.job_id] = {
      text: `完整任务描述 ${item.job_id}\n检查交互、布局与可访问性。`,
      complete: true,
    };
  state.api = async (action, payload) => {
    state.calls.push({ action, payload: clone(payload || {}) });
    if (state.handlers[action]) return state.handlers[action](payload);
    if (action === "app-settings") {
      if (payload) {
        if (payload.revision !== state.config.app_settings.revision)
          throw Object.assign(new Error("设置已更新，请重新加载后合并修改"), { status: 409 });
        Object.assign(state.config.app_settings.saved_values, payload.values);
        Object.assign(state.config.app_settings.values, payload.values);
        state.config.app_settings.revision += "-next";
      }
      return clone(state.config.app_settings);
    }
    if (action === "jobs") {
      const {
        view = "current",
        status = "all",
        search = "",
        offset = 0,
        limit = 50,
      } = payload || {};
      const matching = state.jobs.filter(
        (job) =>
          !!job.archived === (view === "archived") &&
          (view !== "current" ||
            job.conversation_id ===
              state.config.panel_context?.conversation_id) &&
          (status === "all" ||
            status === job.status ||
            (status === "attention" &&
              ["failed", "timed_out", "interrupted", "cancelled"].includes(
                job.status,
              ))) &&
          [
            job.title,
            job.job_id,
            job.model,
            job.backend,
            job.project_dir,
            job.conversation_id,
          ].some((value) =>
            String(value || "")
              .toLowerCase()
              .includes(search.toLowerCase()),
          ),
      );
      const start = Math.min(
        offset,
        matching.length ? Math.floor((matching.length - 1) / limit) * limit : 0,
      );
      return {
        jobs: clone(matching.slice(start, start + limit)),
        recent_completed_ids: status === "all" && view !== "archived"
          ? matching.filter((item) => item.status === "completed")
            .sort((a, b) => Date.parse(b.finished_at || b.started_at) - Date.parse(a.finished_at || a.started_at))
            .slice(0, state.config.app_settings.values.recent_completed_count).map((item) => item.job_id)
          : [],
        total: matching.length,
        offset: start,
        next_offset: start + limit < matching.length ? start + limit : null,
        running_count: state.jobs.filter((job) => job.status === "running")
          .length,
        other_running_count: state.jobs.filter(
          (job) =>
            job.status === "running" &&
            job.conversation_id !== state.config.panel_context?.conversation_id,
        ).length,
        updated_at: state.updated,
      };
    }
    if (action === "archive") {
      const job = state.jobs.find((item) => item.job_id === payload.job_id);
      if (job.status === "running") throw new Error("运行中的任务不能归档");
      job.archived = payload.archived;
      job.archive_reason = payload.archived ? "manual" : null;
      job.archived_at = payload.archived ? state.updated : null;
      if (!payload.archived) job.auto_archive_exempt = true;
      return clone(payload);
    }
    if (action === "archive-policy") {
      if (!payload) return clone(state.archivePolicy);
      if (payload.revision !== state.archivePolicy.revision)
        throw Object.assign(
          new Error("归档规则已被其他面板更新，请重新加载规则后再保存。"),
          { status: 409 },
        );
      state.archivePolicy = {
        ...payload,
        revision: payload.revision + "-saved",
      };
      return clone(state.archivePolicy);
    }
    if (action === "archive-preview") {
      const jobs = state.jobs.filter(
        (job) =>
          !job.conversation_id &&
          job.status === "completed" &&
          !job.archived &&
          !job.auto_archive_exempt,
      );
      return {
        token: "archive-preview-fixture",
        expires_in: 300,
        jobs: clone(jobs),
        job_count: jobs.length,
      };
    }
    if (action === "archive-bulk") {
      let archived = 0;
      for (const job of state.jobs) {
        if (
          !job.conversation_id &&
          job.status === "completed" &&
          !job.archived &&
          !job.auto_archive_exempt
        ) {
          job.archived = true;
          job.archive_reason = "legacy";
          job.archived_at = state.updated;
          archived++;
        }
      }
      return { archived, skipped: 0, errors: 0 };
    }
    if (action === "storage") return clone(state.storage);
    if (action === "storage-policy") {
      if (payload.revision !== state.storage.policy.revision)
        throw Object.assign(new Error("存储策略已更新"), { status: 409 });
      state.storage.policy = {
        retention_days: payload.retention_days,
        revision: payload.revision + "-saved",
      };
      return clone(state.storage.policy);
    }
    if (action === "storage-preview")
      return {
        token: "preview-fixture",
        expires_in: 300,
        retention_days: payload.retention_days,
        jobs: [
          {
            job_id: state.jobs[0].job_id,
            title: state.jobs[0].title,
            finished_at: state.jobs[0].finished_at,
            bytes: 8192,
            file_count: 2,
          },
        ],
        job_count: 1,
        file_count: 2,
        bytes: 8192,
        skipped: 0,
      };
    if (action === "storage-cleanup") {
      state.storage.bytes.logs = 0;
      state.storage.total_bytes -= 8192;
      return {
        jobs_cleaned: 1,
        deleted_files: 2,
        freed_bytes: 8192,
        skipped_files: 0,
        errors: 0,
      };
    }
    if (action === "config" && !payload) return clone(state.config);
    if (action === "local-detect")
      return {
        sources: [
          {
            token: "detect-token",
            source: { label: "客户端配置" },
            files: ["fixture/settings.json"],
            values: {
              base_url: "http://127.0.0.1:4321",
              model: `${payload.backend}-imported`,
              effort: "",
            },
            key_available: true,
            auth: "api_key",
            notes: [],
          },
        ],
      };
    if (action === "local-sync") {
      const profile = state.config.profiles[payload.backend];
      if (
        payload.revision !== profile.revision ||
        payload.sync_revision !== profile.local_sync.revision
      )
        throw Object.assign(new Error("同步设置已更新，请重新加载"), {
          status: 409,
        });
      profile.local_sync = {
        mode: payload.mode,
        fields: payload.fields,
        revision: "sync-r2",
        status: "synced",
        message: "已应用同步设置。",
      };
      profile.revision += "-synced";
      return clone(state.config);
    }
    if (action === "routing") {
      if (!payload) return clone(state.config.routing_policy);
      if (payload.revision !== state.config.routing_policy.revision)
        throw Object.assign(new Error("规则已更新，请重新加载"), {
          status: 409,
        });
      state.config.routing_policy = {
        ...clone(payload.policy),
        revision: payload.revision + "-saved",
      };
      return clone(state.config.routing_policy);
    }
    if (action === "routing-catalog")
      return {
        candidates: [
          {
            id: "claude:" + "a".repeat(24),
            backend: "claude",
            model: "claude-fixture",
          },
          {
            id: "deepseek:" + "b".repeat(24),
            backend: "deepseek",
            model: "deepseek-v4-pro",
          },
        ],
        excluded: [],
      };
    if (action === "routing-context")
      return {
        ...clone(state.config.routing_policy),
        token: "routing-token",
        rules: state.config.routing_policy.rules,
      };
    if (action === "routing-preview")
      return {
        candidate_id: "claude:" + "a".repeat(24),
        rule_name: "规则示例",
        preference: "适合当前任务",
        reason: "首个可用候选",
        pool: [
          {
            id: "claude:" + "a".repeat(24),
            backend: "claude",
            model: "claude-fixture",
          },
        ],
        trace: [{ name: "规则示例", unavailable: [] }],
      };
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
        ...(payload.include_description === false
          ? {}
          : {
              task_description:
                state.descriptions[payload.job_id]?.text || summary.title,
              task_description_complete:
                state.descriptions[payload.job_id]?.complete ?? false,
            }),
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
module.exports = { html, script, markdownScript, markdownResult, fixture, job, clone, deferred };
