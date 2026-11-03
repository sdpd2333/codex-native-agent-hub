(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const names = { gemini: "Gemini", claude: "Claude", deepseek: "DeepSeek" };
  const statuses = {
    running: "运行中",
    completed: "已完成",
    failed: "失败",
    timed_out: "超时",
    cancelled: "已取消",
    interrupted: "已中断",
  };
  const protocols = {
    gemini: "需要 Gemini 原生接口 /v1beta/models/*，以及流式响应与工具调用。",
    claude:
      "需要 Anthropic Messages /v1/messages，以及流式响应与 Claude Code 工具调用。",
    deepseek:
      "此版本 dsh 使用 DeepSeek Messages /v1/messages。仅支持 Chat Completions 的中转路径无法运行。",
  };
  let config = { profiles: {}, runtimes: {} },
    backend = "gemini",
    jobs = [],
    selected = null;
  let busy = false,
    paused = false,
    timer,
    pollPromise,
    pendingForce = false,
    failures = 0;
  let activeTab = "monitor",
    configSequence = 0,
    taskBackendTouched = false,
    lastUpdated = "",
    mountedState = null;
  const drafts = new Map(),
    modelLists = new Map(),
    taskModelLists = new Map(),
    taskModelRequests = new Set();
  const cards = new Map(),
    states = new Map();
  let view = "current",
    listVersion = 0,
    listOffset = 0,
    listTotal = 0,
    listNext = null,
    listLoading = false;
  const completedGroup = document.createElement("details"),
    completedLabel = document.createElement("summary"),
    completedJobs = document.createElement("div");
  completedGroup.className = "completed-group";
  completedJobs.className = "completed-jobs";
  completedGroup.open = true;
  completedGroup.append(completedLabel, completedJobs);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = String(text);
    return node;
  }
  function text(node, value) {
    value = String(value ?? "");
    if (node.textContent !== value) node.textContent = value;
  }
  function button(label, action, cls = "btn small") {
    const node = el("button", cls, label);
    node.type = "button";
    node.onclick = action;
    return node;
  }
  function alert(id, message, type = "") {
    const node = $(id);
    text(node, message);
    node.className = "alert " + type;
    node.hidden = !message;
  }
  function stamp(value) {
    const date = new Date(value);
    return value && !isNaN(date)
      ? date.toLocaleTimeString("zh-CN", { hour12: false })
      : "—";
  }
  function duration(job) {
    const start = Date.parse(job.started_at);
    const end = job.finished_at
      ? Date.parse(job.finished_at)
      : job.status === "running"
        ? Date.now()
        : NaN;
    if (!Number.isFinite(start) || !Number.isFinite(end)) return "—";
    const seconds = Math.max(0, Math.floor((end - start) / 1000));
    return seconds >= 60
      ? `${Math.floor(seconds / 60)}分 ${seconds % 60}秒`
      : `${seconds}秒`;
  }
  async function request(action, payload) {
    const controller = new AbortController();
    const timeout = setTimeout(
      () => controller.abort(),
      [
        "models",
        "test",
        "tasks",
        "changes",
        "storage",
        "storage-preview",
        "storage-cleanup",
        "archive-preview",
        "archive-bulk",
      ].includes(action)
        ? 155000
        : 30000,
    );
    try {
      const response = await fetch(new URL("api/" + action, location.href), {
        cache: "no-store",
        signal: controller.signal,
        ...(payload === undefined
          ? {}
          : {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "X-Agent-Hub": "1",
              },
              body: JSON.stringify(payload),
            }),
      });
      let data;
      try {
        data = await response.json();
      } catch {
        throw new Error("服务未返回有效 JSON，请刷新面板后重试。");
      }
      if (!response.ok) {
        const error = new Error(data.error || `请求失败 (${response.status})`);
        error.status = response.status;
        throw error;
      }
      return data;
    } catch (error) {
      if (error.name === "AbortError")
        throw new Error(
          action === "tasks"
            ? "提交响应超时，请先在任务进展中检查是否已启动，再决定是否重试。"
            : "请求超时，请稍后重试。",
        );
      throw error;
    } finally {
      clearTimeout(timeout);
    }
  }

  // Configuration drafts and catalog identities never contain keys outside this in-memory map.
  function profileValues(profile = {}) {
    return {
      base_url: profile.base_url || "",
      key: "",
      model: profile.model || "",
      effort: profile.effort || "",
      auto_select_model: profile.auto_select_model === true,
    };
  }
  function newDraft(profile) {
    return {
      values: profileValues(profile),
      revision: profile?.revision,
      dirty: false,
      generation: 0,
      conflict: false,
    };
  }
  function captureDraft() {
    const draft = drafts.get(backend) || newDraft(config.profiles[backend]);
    const values = {
      base_url: $("base-url").value.trim(),
      key: $("api-key").value,
      model: $("model").value.trim(),
      effort: $("effort").value,
      auto_select_model: $("auto-select-model").checked,
    };
    if (
      values.base_url !== draft.values.base_url ||
      values.key !== draft.values.key
    ) {
      draft.generation++;
      modelLists.delete(backend);
    }
    draft.values = values;
    draft.dirty =
      JSON.stringify(values) !==
      JSON.stringify(profileValues(config.profiles[backend]));
    if (!draft.dirty) {
      draft.revision = config.profiles[backend]?.revision;
      draft.conflict = false;
    }
    drafts.set(backend, draft);
    return draft;
  }
  function showConflict() {
    const draft = drafts.get(backend);
    alert(
      "settings-conflict",
      draft?.conflict
        ? "此代理的配置已在其他窗口更新。本地草稿已保留；请记录需要的修改，重新加载已保存配置后合并。"
        : "",
      "error",
    );
    $("reload-config").hidden = !draft?.dirty && !draft?.conflict;
    text(
      $("draft-status"),
      draft?.dirty
        ? "有未保存的修改。切换代理会保留草稿；关闭或刷新页面会清除草稿。"
        : "",
    );
    $("draft-status").hidden = !draft?.dirty;
  }
  function acceptConfig(data) {
    const previous = config;
    config = data;
    for (const b of Object.keys(names)) {
      const profile = config.profiles[b] || {};
      if (previous.profiles[b]?.revision !== profile.revision) {
        taskModelLists.delete(b);
        modelLists.delete(b);
      }
      const draft = drafts.get(b);
      if (!draft?.dirty) drafts.set(b, newDraft(profile));
      else draft.conflict = draft.revision !== profile.revision;
    }
    const ready = Object.keys(names).filter((b) => config.profiles[b]?.ready);
    if (!taskBackendTouched && ready.length === 1)
      $("task-backend").value = ready[0];
    text($("connection"), `${ready.length} / 3 已配置`);
    $("connection").className = "connection" + (ready.length ? " ready" : "");
    if (!busy) settingsForm();
    else showConflict();
    taskProfile();
  }
  async function refreshConfig() {
    const sequence = ++configSequence;
    const data = await request("config");
    if (sequence !== configSequence) return;
    acceptConfig(data);
  }
  async function refreshForTab() {
    try {
      await refreshConfig();
      if (activeTab === "task") await loadTaskModels();
    } catch (error) {
      alert(
        activeTab === "task" ? "task-alert" : "settings-alert",
        error.message,
        "error",
      );
    }
  }
  function settingsForm() {
    const profile = config.profiles[backend] || {};
    const draft = drafts.get(backend) || newDraft(profile);
    drafts.set(backend, draft);
    for (const node of document.querySelectorAll(
      "#providers button[data-backend]",
    ))
      node.setAttribute(
        "aria-pressed",
        String(node.dataset.backend === backend),
      );
    $("base-url").value = draft.values.base_url;
    $("api-key").value = draft.values.key;
    $("model").value = draft.values.model;
    $("auto-select-model").checked = draft.values.auto_select_model;
    text($("protocol"), protocols[backend]);
    text(
      $("key-status"),
      profile.key_source === "environment"
        ? "· 来自环境变量"
        : profile.key_configured
          ? "· 已加密保存"
          : "· 尚未配置",
    );
    const strengths =
      backend === "claude"
        ? ["", "low", "medium", "high", "xhigh", "max"]
        : backend === "deepseek"
          ? ["", "high", "max"]
          : [""];
    $("effort-field").hidden = backend === "gemini";
    $("effort").replaceChildren(
      ...strengths.map((value) => {
        const option = el("option", "", value || "默认");
        option.value = value;
        return option;
      }),
    );
    $("effort").value = draft.values.effort;
    const runtime = config.runtimes[backend] || {};
    text(
      $("runtime"),
      `${profile.runtime || names[backend]} · ${runtime.installed ? "SDK " + runtime.version + " 已安装" : "SDK 尚未安装，请运行 install.ps1"}`,
    );
    renderModels();
    showConflict();
  }
  function settingsBusy(value) {
    busy = value;
    for (const node of document.querySelectorAll(
      "#settings input,#settings select,#settings button",
    ))
      node.disabled = value;
  }
  function renderModels() {
    const cache = modelLists.get(backend),
      draft = drafts.get(backend);
    const list =
      cache?.revision === draft?.revision &&
      cache?.generation === draft?.generation
        ? cache.models
        : [];
    $("model-browser").hidden =
      !cache ||
      cache.revision !== draft?.revision ||
      cache.generation !== draft?.generation;
    const query = $("model-search").value.toLowerCase();
    // Keep existing buttons when only the selected model changes, preserving keyboard focus.
    const existing = new Map(
      [...$("models").children].map((node) => [node.dataset.model, node]),
    );
    const nodes = list
      .filter((model) => model.toLowerCase().includes(query))
      .map((model) => {
        const node =
          existing.get(model) ||
          button(
            model,
            () => {
              $("model").value = model;
              captureDraft();
              renderModels();
              showConflict();
            },
            "",
          );
        node.dataset.model = model;
        node.className = model === $("model").value ? "selected" : "";
        node.setAttribute("aria-pressed", String(model === $("model").value));
        return node;
      });
    reconcile($("models"), nodes);
    text(
      $("model-count"),
      `显示 ${nodes.length} / ${list.length} 个模型；ID 按接口原样保留。`,
    );
  }
  async function settingAction(action) {
    if (busy) return;
    const b = backend,
      draft = captureDraft(),
      generation = draft.generation,
      revision = draft.revision;
    const payload = { backend: b, ...draft.values, revision };
    settingsBusy(true);
    alert(
      "settings-alert",
      action === "models"
        ? "正在拉取模型…"
        : action === "test"
          ? "正在测试基础生成…"
          : "正在保存配置…",
    );
    try {
      const data = await request(action, payload);
      if (action === "config") {
        ++configSequence; // Ignore configuration GETs issued before this save completed.
        drafts.set(b, newDraft(data.profiles[b]));
        acceptConfig(data);
        settingsForm();
        alert("settings-alert", "配置已保存。新任务将使用此配置。", "success");
      } else if (
        config.profiles[b]?.revision !== revision ||
        drafts.get(b)?.generation !== generation
      ) {
        alert("settings-alert", "配置已变化，本次响应已过期，请重新操作。");
      } else if (action === "models") {
        modelLists.set(b, {
          revision,
          generation,
          models: Array.isArray(data.models) ? data.models : [],
        });
        renderModels();
        alert(
          "settings-alert",
          `已获取 ${data.total_count} 个模型。选择后保存。`,
          "success",
        );
      } else {
        alert(
          "settings-alert",
          `基础生成通过：${data.reply}\n测试不会保存配置。请先保存，再提交只读任务验证原生工具调用。`,
          "success",
        );
      }
    } catch (error) {
      if (error.status === 409) {
        draft.conflict = true;
        try {
          await refreshConfig();
        } catch {
          /* Keep the draft and original conflict message. */
        }
        showConflict();
      }
      alert("settings-alert", error.message, "error");
    } finally {
      settingsBusy(false);
    }
  }
  for (const node of document.querySelectorAll(
    "#providers button[data-backend]",
  ))
    node.onclick = () => {
      if (busy) return;
      captureDraft();
      backend = node.dataset.backend;
      $("model-search").value = "";
      settingsForm();
      alert("settings-alert", "");
    };
  for (const id of [
    "base-url",
    "api-key",
    "model",
    "effort",
    "auto-select-model",
  ]) {
    $(id).addEventListener("input", () => {
      captureDraft();
      renderModels();
      showConflict();
    });
    $(id).addEventListener("change", () => {
      captureDraft();
      renderModels();
      showConflict();
    });
  }
  $("model-search").oninput = renderModels;
  $("fetch-models").onclick = () => settingAction("models");
  $("test").onclick = () => settingAction("test");
  $("save").onclick = () => settingAction("config");
  $("reload-config").onclick = async () => {
    if (busy) return;
    const b = backend;
    settingsBusy(true);
    try {
      await refreshConfig();
      drafts.set(b, newDraft(config.profiles[b]));
      modelLists.delete(b);
      settingsForm();
      alert("settings-alert", "已重新加载保存的配置，本地草稿已清除。");
    } catch (error) {
      alert("settings-alert", error.message, "error");
    } finally {
      settingsBusy(false);
    }
  };

  function taskProfile() {
    const b = $("task-backend").value,
      profile = config.profiles[b] || {},
      cache = taskModelLists.get(b);
    const matches = cache && cache.revision === profile.revision;
    const models = matches ? cache.models : [];
    const loading = taskModelRequests.has(
      JSON.stringify([b, profile.revision]),
    );
    const select = $("task-model"),
      previous =
        select.dataset.backend === b &&
        select.dataset.revision === profile.revision
          ? select.value
          : "";
    text(
      $("task-profile"),
      profile.ready
        ? `已配置 · ${profile.runtime} · 默认模型 ${profile.model} · 自动选型${profile.auto_select_model ? "已开启" : "已关闭"}`
        : `${names[b]} 尚未配置，请先打开连接设置。`,
    );
    const ids = [
      ...new Set([...models, ...(profile.model ? [profile.model] : [])]),
    ];
    const signature = JSON.stringify([
      b,
      profile.revision,
      ids,
      profile.auto_select_model,
    ]);
    if (select.dataset.signature !== signature) {
      const fallback = el(
        "option",
        "",
        profile.auto_select_model
          ? "自动选择 · 按任务复杂度匹配"
          : profile.model
            ? `默认模型 · ${profile.model}`
            : "使用默认模型",
      );
      fallback.value = "";
      select.replaceChildren(
        fallback,
        ...ids.map((id) => {
          const node = el("option", "", id);
          node.value = id;
          return node;
        }),
      );
      select.value = ids.includes(previous) ? previous : "";
      select.dataset.signature = signature;
    }
    select.dataset.backend = b;
    select.dataset.revision = profile.revision || "";
    $("task-fetch-models").disabled = !profile.ready || loading;
    text($("task-fetch-models"), loading ? "拉取中…" : "刷新模型");
    text(
      $("task-model-hint"),
      loading
        ? "正在拉取当前代理的模型…"
        : matches
          ? `已拉取 ${models.length} 个模型；选择仅影响本次任务。`
          : "可刷新当前代理的模型列表；选择仅影响本次任务。",
    );
    $("task-complexity-field").hidden =
      !profile.auto_select_model ||
      Boolean(select.value) ||
      $("task-kind").value === "connectivity";
  }
  async function loadTaskModels(force = false) {
    const b = $("task-backend").value,
      profile = config.profiles[b] || {},
      revision = profile.revision;
    const key = JSON.stringify([b, revision]);
    if (
      !profile.ready ||
      taskModelRequests.has(key) ||
      (!force && taskModelLists.get(b)?.revision === revision)
    )
      return;
    taskModelRequests.add(key);
    taskProfile();
    try {
      const data = await request("models", {
        backend: b,
        base_url: profile.base_url,
        revision,
      });
      if (
        config.profiles[b]?.revision === revision &&
        data.revision === revision
      )
        taskModelLists.set(b, {
          revision,
          models: Array.isArray(data.models) ? data.models : [],
        });
    } catch (error) {
      if (
        $("task-backend").value === b &&
        config.profiles[b]?.revision === revision
      )
        alert("task-alert", error.message, "error");
      if (error.status === 409) {
        try {
          await refreshConfig();
        } catch {
          /* Retain the last known profile. */
        }
      }
    } finally {
      taskModelRequests.delete(key);
      taskProfile();
    }
  }
  $("task-model").onchange = taskProfile;
  $("task-fetch-models").onclick = async () => {
    try {
      await refreshConfig();
      await loadTaskModels(true);
    } catch (error) {
      alert("task-alert", error.message, "error");
    }
  };
  $("task-backend").onchange = () => {
    taskBackendTouched = true;
    $("task-model").value = "";
    taskProfile();
    alert("task-alert", "");
    loadTaskModels();
  };
  for (const radio of document.querySelectorAll("[name=mode]"))
    radio.onchange = () => {
      $("workspace-field").hidden =
        document.querySelector("[name=mode]:checked").value !== "edit";
    };
  $("task-kind").onchange = () => {
    const connectivity = $("task-kind").value === "connectivity";
    $("task-kind-hint").hidden = !connectivity;
    document.querySelector("[name=mode][value=edit]").disabled = connectivity;
    if (connectivity) {
      document.querySelector("[name=mode][value=review]").checked = true;
      $("workspace-field").hidden = true;
    }
    taskProfile();
  };
  $("task-form").onsubmit = async (event) => {
    event.preventDefault();
    if ($("submit").disabled) return;
    const b = $("task-backend").value;
    if (!config.profiles[b]?.ready) {
      alert(
        "task-alert",
        "请先在连接设置中保存此代理的地址、Key 和模型。",
        "error",
      );
      return;
    }
    $("submit").disabled = true;
    alert("task-alert", "正在提交任务…");
    try {
      const data = await request("tasks", {
        backend: b,
        project_dir: $("task-project").value.trim(),
        task: $("task-prompt").value,
        title: $("task-title").value.trim() || null,
        task_kind: $("task-kind").value,
        model: $("task-model").value,
        mode: document.querySelector("[name=mode]:checked").value,
        workspace_mode: $("task-workspace").value,
        timeout_seconds: Number($("task-timeout").value),
        task_complexity:
          config.profiles[b]?.auto_select_model && !$("task-model").value
            ? $("task-kind").value === "connectivity"
              ? "simple"
              : $("task-complexity").value
            : null,
      });
      selected = data.job_id;
      view = config.panel_context?.conversation_id ? "current" : "all";
      $("search").value = "";
      $("job-status").value = "all";
      listOffset = 0;
      listVersion++;
      alert("task-alert", "");
      tab("monitor");
      await poll(true);
    } catch (error) {
      alert("task-alert", error.message, "error");
    } finally {
      $("submit").disabled = false;
    }
  };

  // Retain nodes for each job, so polling another job cannot destroy focus or selected text.
  function reconcile(parent, nodes) {
    const keep = new Set(nodes),
      focused = document.activeElement;
    for (const child of [...parent.children])
      if (!keep.has(child)) child.remove();
    nodes.forEach((node, index) => {
      if (parent.children[index] !== node)
        parent.insertBefore(node, parent.children[index] || null);
    });
    if (focused?.isConnected && document.activeElement !== focused)
      focused.focus({ preventScroll: true });
  }
  function setBadge(node, status) {
    node.className = "badge " + status;
    text(node, statuses[status] || status);
  }
  function renderList() {
    const query = $("search").value.trim(),
      list = jobs;
    for (const name of ["current", "all", "archived"])
      $("view-" + name).setAttribute("aria-pressed", String(name === view));
    text(
      $("context-note"),
      view === "current"
        ? config.panel_context?.conversation_id
          ? "仅显示本对话的任务；其他对话和未归属的旧任务可在「全部任务」查看。"
          : "此面板未绑定对话，请从当前对话重新打开面板，或查看「全部任务」。"
        : view === "archived"
          ? "所有对话的归档任务，可随时恢复。归档不会释放存储空间。"
          : "显示所有对话的未归档任务。未记录对话来源的旧任务标为「未归属」。",
    );
    $("page-prev").disabled = listLoading || listOffset === 0;
    $("page-next").disabled = listLoading || listNext === null;
    text(
      $("page-info"),
      listLoading
        ? "正在加载…"
        : listTotal
          ? `${listOffset + 1}–${listOffset + list.length} / ${listTotal}`
          : "0 个任务",
    );
    if (!list.length) {
      const empty = el("div", "empty");
      empty.append(
        el(
          "strong",
          "",
          listLoading
            ? "正在加载任务…"
            : query
              ? "没有匹配的任务"
              : "此视图暂无任务",
        ),
        el(
          "p",
          "",
          query
            ? "试试其他关键词，或清除筛选。"
            : view === "current"
              ? "从「新建任务」开始，或切换到「全部任务」查看历史。"
              : "可切换视图或状态筛选。",
        ),
      );
      if (query)
        empty.append(
          button("清除筛选", () => {
            $("search").value = "";
            changeList();
            $("search").focus();
          }),
        );
      $("jobs").replaceChildren(empty);
      return;
    }
    const nodes = list.map((job) => {
      let card = cards.get(job.job_id);
      if (!card) {
        const node = button(
          "",
          () => {
            selected = job.job_id;
            render();
          },
          "job",
        );
        const title = el("div", "job-title"),
          meta = el("div", "job-meta"),
          project = el("div", "job-meta");
        const footer = el("div", "job-footer"),
          badge = el("span"),
          time = el("span", "sub");
        time.dataset.duration = job.job_id;
        footer.append(badge, time);
        node.append(title, meta, project, footer);
        card = { node, title, meta, project, badge, time };
        cards.set(job.job_id, card);
      }
      card.node.title = job.title || "未命名任务";
      card.node.dataset.jobId = job.job_id;
      card.node.className =
        "job" + (selected === job.job_id ? " selected" : "");
      card.node.setAttribute("aria-pressed", String(selected === job.job_id));
      text(card.title, job.title || "未命名任务");
      text(card.meta, `${names[job.backend] || job.backend} · ${job.model}`);
      const owner = !job.conversation_id
        ? "未归属"
        : job.conversation_id === config.panel_context?.conversation_id
          ? "本对话"
          : "其他对话";
      text(
        card.project,
        `${job.project_name || job.project_dir || ""} · ${owner}`,
      );
      card.project.title = job.project_dir || "";
      setBadge(card.badge, job.status);
      text(card.time, duration(job));
      return card.node;
    });
    const complete = nodes.filter(
      (_, index) => list[index].status === "completed",
    );
    reconcile(completedJobs, complete);
    text(completedLabel, `已完成 · 本页 ${complete.length} 个`);
    reconcile($("jobs"), [
      ...nodes.filter((_, index) => list[index].status !== "completed"),
      ...(complete.length ? [completedGroup] : []),
    ]);
  }
  function metric(label) {
    const node = el("div", "metric"),
      value = el("div", "value");
    node.append(el("div", "label", label), value);
    return { node, value };
  }
  function makeState(id) {
    const state = {
      id,
      cancelPending: false,
      archivePending: false,
      operationError: "",
      changes: null,
      changesPending: false,
      changesError: "",
      autoDiffSignature: "",
      result: null,
      description: { text: null, complete: true, pending: false, error: "" },
      copying: false,
      copyMessage: "",
    };
    const root = el("div"),
      top = el("div", "card"),
      head = el("div", "detail-top");
    const title = el("h2"),
      badge = el("span"),
      meta = el("div", "detail-meta"),
      selection = el("div", "selection-note");
    head.append(title, badge);
    const metrics = el("div", "metrics"),
      time = metric("运行时长"),
      models = metric("模型调用"),
      tools = metric("工具调用");
    time.value.dataset.duration = id;
    metrics.append(time.node, models.node, tools.node);
    const phase = el("div", "phase"),
      cancel = button("终止任务", () => cancelTask(state), "btn danger small");
    const archive = button("归档任务", () => archiveTask(state)),
      archiveNote = el("div", "hint archive-note"),
      owner = el("div", "detail-meta"),
      actions = el("div", "detail-actions");
    actions.append(cancel, archive);
    const operation = el("div", "alert error");
    operation.setAttribute("role", "status");
    top.append(
      head,
      meta,
      owner,
      el("div", "detail-meta", id),
      selection,
      metrics,
      phase,
      actions,
      archiveNote,
      operation,
    );
    const description = el("details", "card task-description"),
      descriptionText = el("pre", "description-text"),
      descriptionNote = el("p", "hint"),
      descriptionError = el("div", "alert error"),
      descriptionRetry = button("重试读取描述", () => loadDescription(state));
    descriptionError.setAttribute("role", "status");
    description.append(
      el("summary", "", "任务描述"),
      descriptionNote,
      descriptionText,
      descriptionError,
      descriptionRetry,
    );
    description.ontoggle = () => {
      if (description.open) loadDescription(state);
    };
    const error = el("div", "alert error"),
      workspace = el("div", "card"),
      workspaceRow = el("div", "section-row");
    const workspaceTitle = el("h3"),
      copyPath = button("复制路径", async () => {
        try {
          await navigator.clipboard.writeText(
            state.job.workspace?.path || state.job.project_dir,
          );
          text(copyPath, "路径已复制");
        } catch {
          text(copyPath, "请手动复制路径");
        }
      });
    workspaceRow.append(workspaceTitle, copyPath);
    const source = el("div", "workspace"),
      path = el("div", "workspace"),
      branch = el("div", "hint");
    const diffRow = el("div", "row"),
      diffButton = button("查看变更", () => loadChanges(state));
    const wrap = button("关闭换行", () => {
      state.diffWrapped = !state.diffWrapped;
      updateChanges(state);
    });
    state.diffWrapped = true;
    diffRow.append(diffButton, wrap);
    const diffNote = el("p", "hint"),
      diffError = el("div", "alert error"),
      diff = el("pre", "diff");
    diff.tabIndex = 0;
    diff.setAttribute("aria-label", "工作区差异");
    const untrackedTitle = el("p", "hint", "新增文件："),
      untracked = el("ul", "hint"),
      truncation = el("p", "hint", "差异已截断，请在工作区查看完整内容。");
    workspace.append(
      workspaceRow,
      source,
      path,
      branch,
      diffRow,
      diffNote,
      diffError,
      diff,
      truncation,
      untrackedTitle,
      untracked,
    );
    const activity = el("div", "card"),
      events = el("ol", "events"),
      eventHint = el("p", "hint", "运行环境启动后，这里会显示工具活动。");
    events.dataset.jobId = id;
    events.tabIndex = 0;
    events.setAttribute("aria-label", "最近活动，最新在前");
    activity.append(el("h3", "", "最近活动"), events, eventHint);
    const resultCard = el("div", "card"),
      resultRow = el("div", "section-row");
    const copy = button("复制完整结果", () => copyResult(state)),
      result = el("pre", "result");
    result.tabIndex = 0;
    result.setAttribute("aria-label", "执行结果");
    resultRow.append(el("h3", "", "执行结果"), copy);
    const resultHint = el("p", "hint"),
      more = button("加载更多", () => loadResult(state)),
      resultError = el("div", "alert error");
    resultCard.append(resultRow, result, resultHint, more, resultError);
    root.append(top, description, error, workspace, activity, resultCard);
    state.nodes = {
      root,
      title,
      badge,
      meta,
      selection,
      description,
      descriptionText,
      descriptionNote,
      descriptionError,
      descriptionRetry,
      time: time.value,
      models: models.value,
      tools: tools.value,
      phase,
      cancel,
      archive,
      archiveNote,
      owner,
      operation,
      error,
      workspace,
      workspaceTitle,
      source,
      path,
      branch,
      diffRow,
      diffButton,
      wrap,
      diffNote,
      diffError,
      diff,
      truncation,
      untrackedTitle,
      untracked,
      events,
      eventHint,
      resultCard,
      result,
      copy,
      resultHint,
      more,
      resultError,
    };
    states.set(id, state);
    return state;
  }
  function changeSignature(job) {
    return JSON.stringify([
      job.status,
      job.finished_at,
      job.progress?.tool_calls,
      job.progress?.last_activity_at,
      job.workspace,
    ]);
  }
  async function cancelTask(state) {
    if (state.cancelPending) return;
    state.cancelPending = true;
    state.operationError = "";
    renderCurrent(state);
    try {
      await request("cancel", { job_id: state.id });
      await poll(true);
    } catch (error) {
      state.operationError = error.message;
    } finally {
      state.cancelPending = false;
      renderCurrent(state);
    }
  }
  async function archiveTask(state) {
    if (state.archivePending || state.job.status === "running") return;
    state.archivePending = true;
    state.operationError = "";
    const archived = !state.job.archived;
    renderCurrent(state);
    try {
      await request("archive", { job_id: state.id, archived });
      if (archivePreview || archiveBusy)
        clearArchivePreview("任务状态已变化，请重新预览旧任务。");
      alert(
        "history-alert",
        archived
          ? "任务已归档，可在「已归档」中恢复。"
          : "任务已恢复，可在「全部任务」或所属对话中查看。",
        "success",
      );
      await poll(true);
    } catch (error) {
      state.operationError = error.message;
    } finally {
      state.archivePending = false;
      renderCurrent(state);
    }
  }
  async function loadChanges(state, automatic = false) {
    if (state.changesPending) return;
    const signature = changeSignature(state.job);
    if (automatic) state.autoDiffSignature = signature;
    state.changesPending = true;
    state.changesError = "";
    renderCurrent(state);
    try {
      state.changes = {
        data: await request("changes", { job_id: state.id }),
        signature,
        at: new Date().toISOString(),
        terminal:
          state.job.status !== "running" &&
          signature === changeSignature(state.job),
      };
    } catch (error) {
      state.changesError = error.message;
    } finally {
      state.changesPending = false;
      renderCurrent(state);
    }
  }
  function updateChanges(state) {
    const n = state.nodes,
      cache = state.changes,
      job = state.job;
    n.diffRow.hidden = job.mode !== "edit" || !job.workspace;
    n.diffButton.disabled = state.changesPending;
    text(
      n.diffButton,
      state.changesPending ? "读取变更中…" : cache ? "刷新变更" : "查看变更",
    );
    const stale =
      cache &&
      (cache.signature !== changeSignature(job) || job.status === "running");
    const freshness = stale
      ? " · 预览可能已过期，请刷新。" +
        (job.status === "running" ? "任务结束后会自动更新一次。" : "")
      : "";
    text(
      n.diffNote,
      cache
        ? `读取于 ${stamp(cache.at)}${freshness}`
        : "修改保留在工作区，检查差异后再合并到项目。",
    );
    n.diffNote.hidden = job.mode !== "edit";
    text(n.diffError, state.changesError);
    n.diffError.hidden = !state.changesError;
    n.diff.hidden = !cache;
    n.wrap.hidden = !cache;
    n.diff.classList.toggle("nowrap", !state.diffWrapped);
    text(n.wrap, state.diffWrapped ? "关闭换行" : "开启换行");
    if (cache) text(n.diff, cache.data.diff || "没有已跟踪文件的差异。");
    n.truncation.hidden = !cache?.data.diff_truncated;
    const files = cache?.data.untracked_files || [];
    if (n.untracked.dataset.files !== JSON.stringify(files)) {
      n.untracked.replaceChildren(...files.map((file) => el("li", "", file)));
      n.untracked.dataset.files = JSON.stringify(files);
    }
    n.untracked.hidden = n.untrackedTitle.hidden = !files.length;
    if (
      cache &&
      !cache.terminal &&
      job.status !== "running" &&
      cache.signature !== changeSignature(job) &&
      state.autoDiffSignature !== changeSignature(job) &&
      !state.changesPending
    )
      loadChanges(state, true);
  }
  function resultSignature(job) {
    return JSON.stringify([job.status, job.finished_at, job.result_chars || 0]);
  }
  async function loadDescription(state) {
    const description = state.description;
    if (description.pending || description.text !== null) return;
    description.pending = true;
    description.error = "";
    renderCurrent(state);
    try {
      const data = await request("job", {
        job_id: state.id,
        max_chars: 100,
        include_description: true,
      });
      if (
        typeof data.task_description !== "string" ||
        typeof data.task_description_complete !== "boolean"
      )
        throw new Error("任务描述响应无效，请重新加载服务后重试。");
      description.text = data.task_description;
      description.complete = data.task_description_complete;
    } catch (error) {
      description.error = error.message;
    } finally {
      description.pending = false;
      renderCurrent(state);
    }
  }
  function updateDescription(state) {
    const n = state.nodes,
      description = state.description;
    text(n.descriptionText, description.text || "");
    text(
      n.descriptionNote,
      description.pending
        ? "正在读取任务描述…"
        : description.text === null
          ? "展开后读取任务描述。"
          : description.complete
            ? "完整任务描述（已脱敏）。"
            : "历史任务未保存完整描述，以下仅为当时保留的片段，可能已被截断。",
    );
    text(n.descriptionError, description.error);
    n.descriptionError.hidden = n.descriptionRetry.hidden = !description.error;
    n.descriptionRetry.disabled = description.pending;
  }
  async function loadResult(state) {
    const result = state.result;
    if (!result || result.next === null) return;
    if (result.promise) return result.promise;
    result.error = "";
    result.promise = (async () => {
      try {
        const page = await request("job", {
          job_id: state.id,
          offset: result.next,
          max_chars: 20000,
          include_description: false,
        });
        if (state.result !== result) return;
        if (
          page.offset !== result.next ||
          typeof page.result !== "string" ||
          !Number.isInteger(page.total_chars) ||
          !(
            page.next_offset === null ||
            (Number.isInteger(page.next_offset) &&
              page.next_offset > result.next)
          )
        )
          throw new Error("结果分页响应无效，请刷新后重试。");
        result.text += page.result;
        result.next = page.next_offset;
        result.loaded = page.next_offset ?? page.total_chars;
        result.total = page.total_chars;
      } catch (error) {
        if (state.result === result) result.error = error.message;
      } finally {
        result.promise = null;
        renderCurrent(state);
      }
    })();
    renderCurrent(state);
    return result.promise;
  }
  async function copyResult(state) {
    if (state.copying) return;
    const result = state.result;
    state.copying = true;
    state.copyMessage = "正在读取完整结果…";
    renderCurrent(state);
    try {
      while (state.result === result && result.next !== null) {
        await loadResult(state);
        if (result.error) throw new Error(result.error);
      }
      if (state.result !== result) throw new Error("结果已更新，请重新复制。");
      await navigator.clipboard.writeText(result.text);
      state.copyMessage = "完整结果已复制";
    } catch (error) {
      state.copyMessage = `复制未完成：${error.message}；可加载完整结果后手动选择文本。`;
    } finally {
      state.copying = false;
      renderCurrent(state);
    }
  }
  function updateResult(state) {
    const n = state.nodes,
      result = state.result;
    n.resultCard.hidden = !result;
    if (!result) return;
    text(n.result, result.text);
    text(
      n.resultHint,
      state.copyMessage ||
        (result.promise
          ? "正在读取结果…"
          : `已加载 ${result.loaded} / ${result.total} 字符${result.next === null ? " · 完整结果" : ""}`),
    );
    text(n.resultError, result.error);
    n.resultError.hidden = !result.error;
    n.more.hidden = result.next === null;
    n.more.disabled = !!result.promise || state.copying;
    text(n.more, result.error ? "重试读取结果" : "加载更多");
    n.copy.disabled = state.copying;
    text(n.copy, state.copying ? "读取并复制中…" : "复制完整结果");
  }
  function updateEvents(state) {
    const n = state.nodes,
      events = [...(state.job.progress?.events || [])].reverse();
    const mark = JSON.stringify(events);
    if (state.eventMark === mark) return;
    const scroll = n.events.scrollTop;
    const anchor =
      scroll > 0
        ? [...n.events.children].find(
            (node) => node.offsetTop + node.offsetHeight > scroll,
          )
        : null;
    const offset = anchor ? anchor.offsetTop - scroll : 0,
      key = anchor?.dataset.eventKey;
    const occurrences = new Map();
    n.events.replaceChildren(
      ...events.map((event) => {
        const node = el("li"),
          base = JSON.stringify([event.timestamp, event.label]);
        const occurrence = occurrences.get(base) || 0;
        occurrences.set(base, occurrence + 1);
        node.dataset.eventKey = base + occurrence;
        node.append(
          el("time", "", stamp(event.timestamp)),
          el("span", "", event.label),
        );
        return node;
      }),
    );
    const current = [...n.events.children].find(
      (node) => node.dataset.eventKey === key,
    );
    n.events.scrollTop = current ? current.offsetTop - offset : scroll;
    n.eventHint.hidden = !!events.length;
    state.eventMark = mark;
  }
  function renderCurrent(state) {
    if (selected === state.id && activeTab === "monitor" && !document.hidden)
      renderDetail();
  }
  function rememberDetailScroll() {
    if (!mountedState) return;
    mountedState.scroll = Object.fromEntries(
      ["events", "result", "diff"].map((key) => [
        key,
        {
          top: mountedState.nodes[key].scrollTop,
          left: mountedState.nodes[key].scrollLeft,
        },
      ]),
    );
  }
  function renderDetail() {
    const job = jobs.find((item) => item.job_id === selected);
    if (!job) {
      const empty = el("div", "empty");
      empty.append(
        el(
          "strong",
          "",
          selected ? "所选任务不在当前列表中" : "在这里查看代理的任务进展",
        ),
        el(
          "p",
          "",
          selected
            ? "任务可能已归档或不在本页，可切换视图、翻页或搜索。"
            : "选择任务以查看阶段、工具活动和执行结果。",
        ),
      );
      rememberDetailScroll();
      mountedState = null;
      $("detail").replaceChildren(empty);
      text($("monitor-status"), selected ? "所选任务暂不可用" : "");
      return;
    }
    const state = states.get(job.job_id) || makeState(job.job_id),
      n = state.nodes;
    state.job = job;
    if (mountedState !== state) {
      rememberDetailScroll();
      $("detail").replaceChildren(n.root);
      mountedState = state;
      for (const [key, position] of Object.entries(state.scroll || {})) {
        n[key].scrollTop = position.top;
        n[key].scrollLeft = position.left;
      }
    }
    text(n.title, job.title || "任务详情");
    text(
      n.owner,
      job.conversation_id
        ? `对话：${job.conversation_id}${job.conversation_id === config.panel_context?.conversation_id ? " · 本对话" : ""}`
        : "对话：未归属（旧任务或未提供对话标识）",
    );
    setBadge(n.badge, job.status);
    text(
      n.meta,
      `${job.runtime || names[job.backend]} · ${job.model} · ${job.mode === "edit" ? "编辑执行" : "只读审查"}`,
    );
    const labels = {
        automatic: "自动选型",
        fallback: "选型回退",
        explicit: "本次指定",
        default: "默认模型",
      },
      selection = job.model_selection;
    n.selection.hidden = !selection;
    if (selection)
      text(
        n.selection,
        `${labels[selection.mode] || "模型选择"} · ${selection.reason || ""}${selection.default_model && selection.default_model !== job.model ? ` 默认模型仍为 ${selection.default_model}。` : ""}`,
      );
    text(n.time, duration(job));
    text(
      n.models,
      `${job.progress?.model_calls || 0} / ${job.progress?.budget_model_calls || 40}`,
    );
    text(n.tools, job.progress?.tool_calls || 0);
    text(
      n.phase,
      job.progress?.phase || statuses[job.status] || "正在启动运行环境",
    );
    text(
      $("monitor-status"),
      `${job.title || "任务"}：${statuses[job.status] || job.status}`,
    );
    n.cancel.hidden = job.status !== "running" || !job.can_cancel;
    n.cancel.disabled = state.cancelPending;
    text(n.cancel, state.cancelPending ? "正在终止…" : "终止任务");
    n.archive.disabled = state.archivePending || job.status === "running";
    text(
      n.archive,
      state.archivePending
        ? "正在更新…"
        : job.archived
          ? "恢复任务"
          : "归档任务",
    );
    n.archive.title = job.status === "running" ? "运行中的任务不能归档" : "";
    const archiveReasons = {
      manual: "手动归档",
      automatic: "到期自动归档",
      legacy: "旧任务批量归档",
    };
    text(
      n.archiveNote,
      job.archived
        ? `${archiveReasons[job.archive_reason] || "已归档"} · ${job.archived_at ? new Date(job.archived_at).toLocaleString("zh-CN") : "时间未知"}`
        : job.auto_archive_exempt
          ? "此任务已恢复，免于自动归档和旧任务批量归档；仍可手动归档。"
          : "",
    );
    n.archiveNote.hidden = !n.archiveNote.textContent;
    text(n.operation, state.operationError);
    n.operation.hidden = !state.operationError;
    text(n.error, job.error);
    n.error.hidden = !job.error;
    n.workspace.hidden = !job.project_dir && !job.workspace;
    text(
      n.workspaceTitle,
      job.workspace?.strategy === "worktree" ? "独立工作区" : "项目工作区",
    );
    text(n.source, `源项目：${job.project_dir || "—"}`);
    text(
      n.path,
      job.workspace?.path
        ? `执行目录：${job.workspace.path}`
        : "执行目录准备中…",
    );
    text(n.branch, job.workspace?.branch ? `分支 ${job.workspace.branch}` : "");
    updateChanges(state);
    updateDescription(state);
    updateEvents(state);
    const signature = resultSignature(job);
    if (state.resultMark !== signature) {
      state.resultMark = signature;
      state.result = job.result_chars
        ? {
            text: "",
            loaded: 0,
            total: job.result_chars,
            next: 0,
            promise: null,
            error: "",
          }
        : null;
      state.copyMessage = "";
      if (state.result) loadResult(state);
    }
    updateResult(state);
  }
  function render() {
    if (activeTab !== "monitor" || document.hidden) return;
    renderList();
    renderDetail();
  }
  function changeList() {
    listVersion++;
    listOffset = 0;
    selected = null;
    jobs = [];
    listLoading = true;
    alert("history-alert", "");
    render();
    poll(true);
  }
  $("search").oninput = changeList;
  $("job-status").onchange = changeList;
  for (const name of ["current", "all", "archived"])
    $("view-" + name).onclick = () => {
      view = name;
      changeList();
    };
  $("other-running").onclick = () => {
    view = "all";
    $("job-status").value = "running";
    $("search").value = "";
    changeList();
  };
  function page(offset) {
    listOffset = offset;
    listVersion++;
    selected = null;
    jobs = [];
    listLoading = true;
    render();
    poll(true);
  }
  $("page-prev").onclick = () => page(Math.max(0, listOffset - 50));
  $("page-next").onclick = () => {
    if (listNext !== null) page(listNext);
  };
  function sync(message, error = false) {
    $("sync").replaceChildren(
      el("span", "dot" + (error ? " error" : "")),
      document.createTextNode(message),
    );
  }
  function syncUpdated() {
    sync(`${paused ? "自动刷新已暂停 · " : ""}更新于 ${stamp(lastUpdated)}`);
  }
  function poll(force = false) {
    clearTimeout(timer);
    if (pollPromise) {
      if (force) pendingForce = true;
      return pollPromise;
    }
    if (!force && (paused || document.hidden)) return Promise.resolve();
    pollPromise = (async () => {
      do {
        pendingForce = false;
        const version = listVersion;
        try {
          const data = await request("jobs", {
            view,
            search: $("search").value,
            status: $("job-status").value,
            offset: listOffset,
            limit: 50,
          });
          if (version !== listVersion) continue;
          listLoading = false;
          jobs = Array.isArray(data.jobs) ? data.jobs : [];
          listTotal = data.total ?? jobs.length;
          listOffset = data.offset ?? 0;
          listNext = data.next_offset ?? null;
          alert("archive-maintenance-alert", data.archive_error || "", "error");
          if (!selected && jobs.length) selected = jobs[0].job_id;
          // Keep the current job's UI state even if it falls outside the bounded history list.
          const retained = new Set(jobs.map((job) => job.job_id));
          for (const id of cards.keys())
            if (!retained.has(id) && id !== selected) cards.delete(id);
          for (const [id, state] of states) {
            const job = jobs.find((item) => item.job_id === id);
            if (job) state.job = job;
            else if (
              id !== selected &&
              !state.cancelPending &&
              !state.archivePending &&
              !state.changesPending &&
              !state.copying &&
              !state.description.pending &&
              !state.result?.promise
            )
              states.delete(id);
          }
          render();
          text(
            $("running-count"),
            data.running_count ??
              jobs.filter((job) => job.status === "running").length,
          );
          $("other-running").hidden = !data.other_running_count;
          text(
            $("other-running"),
            `其他对话 ${data.other_running_count || 0} 个任务运行中`,
          );
          lastUpdated = data.updated_at;
          failures = 0;
          syncUpdated();
        } catch (error) {
          if (version !== listVersion) continue;
          listLoading = false;
          renderList();
          failures++;
          sync(
            paused
              ? "刷新失败 · 自动刷新已暂停"
              : document.hidden
                ? "刷新失败 · 页面隐藏时暂停"
                : "连接中断，将延后重试",
            true,
          );
        }
      } while (pendingForce);
    })().finally(() => {
      pollPromise = null;
      if (pendingForce) return poll(true);
      if (!paused && !document.hidden)
        timer = setTimeout(
          () => poll(),
          Math.min(30000, 1500 * 2 ** Math.min(failures, 5)),
        );
    });
    return pollPromise;
  }
  $("refresh").onclick = () => poll(true);
  $("pause").onclick = () => {
    paused = !paused;
    text($("pause"), paused ? "继续" : "暂停");
    if (paused) {
      clearTimeout(timer);
      syncUpdated();
    } else poll();
  };
  let storageData = null,
    storageBusy = "",
    storagePreview = null,
    storageEpoch = 0,
    retentionDirty = false,
    previewTimer;
  function bytes(value) {
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let size = Number(value) || 0,
      unit = 0;
    while (size >= 1024 && unit < units.length - 1) {
      size /= 1024;
      unit++;
    }
    return `${size.toFixed(unit ? 1 : 0)} ${units[unit]}`;
  }
  function storageControls() {
    $("storage-refresh").disabled = !!storageBusy;
    $("storage-save").disabled = !!storageBusy || !storageData;
    $("storage-preview").disabled = !!storageBusy;
    $("storage-cleanup").disabled =
      !!storageBusy || !storagePreview?.file_count;
    $("retention-days").disabled = ["cleanup", "save"].includes(storageBusy);
    text(
      $("storage-refresh"),
      storageBusy === "stats" ? "正在统计…" : "刷新统计",
    );
    text(
      $("storage-preview"),
      storageBusy === "preview" ? "正在预览…" : "预览可清理内容",
    );
    text(
      $("storage-cleanup"),
      storageBusy === "cleanup" ? "正在清理…" : "清理列出的运行日志",
    );
  }
  function invalidatePreview() {
    storageEpoch++;
    storagePreview = null;
    clearTimeout(previewTimer);
    $("cleanup-preview").hidden = true;
    storageControls();
  }
  function renderStorage() {
    if (!storageData) return;
    const labels = {
      records: "任务记录与结果",
      logs: "运行日志与会话缓存",
      worktrees: "编辑工作区",
      other: "其他配置与数据",
    };
    $("storage-metrics").replaceChildren(
      ...Object.entries(labels).map(([key, label]) => {
        const item = metric(label);
        text(item.value, bytes(storageData.bytes[key]));
        return item.node;
      }),
    );
    text(
      $("storage-note"),
      `共 ${storageData.job_count} 个任务，占用 ${bytes(storageData.total_bytes)}（文件大小合计）。${storageData.partial ? `有 ${storageData.skipped} 项无法读取或为链接，统计不完整。` : ""}`,
    );
  }
  async function loadStorage() {
    if (storageBusy) return;
    storageBusy = "stats";
    invalidatePreview();
    alert("storage-alert", "");
    try {
      storageData = await request("storage");
      if (!retentionDirty)
        $("retention-days").value = storageData.policy.retention_days;
      renderStorage();
    } catch (error) {
      text($("storage-note"), "统计读取失败，可点击刷新重试。");
      alert("storage-alert", error.message, "error");
    } finally {
      storageBusy = "";
      storageControls();
    }
  }
  function retentionDays() {
    const input = $("retention-days"),
      days = Number(input.value);
    if (
      !input.checkValidity() ||
      !Number.isInteger(days) ||
      days < 1 ||
      days > 3650
    ) {
      input.reportValidity();
      throw new Error("请输入 1 到 3650 之间的整数天数。");
    }
    return days;
  }
  $("retention-days").oninput = () => {
    retentionDirty = true;
    invalidatePreview();
  };
  $("storage-refresh").onclick = loadStorage;
  $("storage-save").onclick = async () => {
    if (storageBusy || !storageData) return;
    try {
      const days = retentionDays();
      storageBusy = "save";
      invalidatePreview();
      storageData.policy = await request("storage-policy", {
        retention_days: days,
        revision: storageData.policy.revision,
      });
      retentionDirty = false;
      alert(
        "storage-alert",
        "已保存保留天数。日志仅在预览并点击清理后删除。",
        "success",
      );
    } catch (error) {
      alert("storage-alert", error.message, "error");
    } finally {
      storageBusy = "";
      storageControls();
    }
  };
  $("storage-preview").onclick = async () => {
    if (storageBusy) return;
    let epoch;
    try {
      const days = retentionDays();
      storageBusy = "preview";
      invalidatePreview();
      epoch = storageEpoch;
      alert("storage-alert", "");
      const data = await request("storage-preview", { retention_days: days });
      if (epoch !== storageEpoch) return;
      storagePreview = data;
      $("cleanup-preview").hidden = false;
      text(
        $("cleanup-summary"),
        `${data.job_count} 个任务、${data.file_count} 个日志文件，预计释放 ${bytes(data.bytes)}。${data.skipped ? `已跳过 ${data.skipped} 项较新、不可读或不适合清理的数据。` : ""}`,
      );
      $("cleanup-list").replaceChildren(
        ...data.jobs.map((job) =>
          el(
            "li",
            "",
            `${job.title} · ${new Date(job.finished_at).toLocaleDateString("zh-CN")} · ${bytes(job.bytes)}`,
          ),
        ),
      );
      if (!data.file_count)
        alert("storage-alert", "没有符合保留天数的旧运行日志。");
      previewTimer = setTimeout(() => {
        if (storagePreview === data) {
          invalidatePreview();
          alert("storage-alert", "预览已过期，请重新预览后清理。");
        }
      }, data.expires_in * 1000);
    } catch (error) {
      if (epoch === undefined || epoch === storageEpoch)
        alert("storage-alert", error.message, "error");
    } finally {
      storageBusy = "";
      storageControls();
    }
  };
  $("storage-cleanup").onclick = async () => {
    if (storageBusy || !storagePreview?.file_count) return;
    const token = storagePreview.token;
    let completedMessage = "";
    storageBusy = "cleanup";
    invalidatePreview();
    alert("storage-alert", "正在清理预览中的旧运行日志…");
    try {
      const data = await request("storage-cleanup", { token });
      completedMessage = `已清理 ${data.jobs_cleaned} 个任务的 ${data.deleted_files} 个日志文件，释放 ${bytes(data.freed_bytes)}。跳过 ${data.skipped_files} 个文件，${data.errors} 个错误。任务记录、结果和编辑工作区已保留。`;
      alert(
        "storage-alert",
        completedMessage,
        data.errors ? "error" : "success",
      );
      storageData = await request("storage");
      renderStorage();
    } catch (error) {
      alert(
        "storage-alert",
        completedMessage
          ? `${completedMessage} 最新统计读取失败：${error.message} 请刷新统计。`
          : `${error.message} 请刷新统计并重新预览。`,
        "error",
      );
    } finally {
      storageBusy = "";
      storageControls();
    }
  };
  // Archive policy drafts are independent of job polling and server-side previews.
  let archivePolicy = null,
    archivePolicyBusy = false,
    archiveDraftVersion = 0;
  let archivePreview = null,
    archiveBusy = false,
    archivePreviewVersion = 0,
    archiveExpiry;
  function renderArchiveControls() {
    $("archive-enabled").disabled = !archivePolicy || archivePolicyBusy;
    $("archive-days").disabled = !archivePolicy || archivePolicyBusy;
    $("archive-policy-save").disabled = !archivePolicy || archivePolicyBusy;
    $("archive-policy-reload").disabled = archivePolicyBusy;
    $("archive-preview").disabled = archiveBusy;
    $("archive-bulk").disabled = archiveBusy || !archivePreview?.job_count;
  }
  function archiveRuleSummary() {
    text(
      $("archive-rule-summary"),
      archivePolicy.enabled
        ? `成功完成 ${archivePolicy.after_days} 天后自动归档`
        : "自动归档已关闭",
    );
  }
  async function loadArchivePolicy() {
    if (archivePolicyBusy) return;
    archivePolicyBusy = true;
    const version = archiveDraftVersion;
    renderArchiveControls();
    alert("archive-policy-alert", "");
    try {
      const data = await request("archive-policy");
      if (version !== archiveDraftVersion) return;
      archivePolicy = data;
      $("archive-enabled").checked = data.enabled;
      $("archive-days").value = data.after_days;
      archiveRuleSummary();
    } catch (error) {
      text($("archive-rule-summary"), "规则读取失败");
      alert("archive-policy-alert", error.message, "error");
    } finally {
      archivePolicyBusy = false;
      renderArchiveControls();
    }
  }
  for (const id of ["archive-enabled", "archive-days"])
    $(id).oninput = () => {
      archiveDraftVersion++;
    };
  $("archive-policy-reload").onclick = loadArchivePolicy;
  $("archive-policy-save").onclick = async () => {
    if (archivePolicyBusy || !archivePolicy) return;
    const days = Number($("archive-days").value);
    if (!Number.isInteger(days) || days < 1 || days > 3650) {
      alert(
        "archive-policy-alert",
        "自动归档天数须为 1 到 3650 之间的整数。",
        "error",
      );
      return;
    }
    archivePolicyBusy = true;
    renderArchiveControls();
    try {
      archivePolicy = await request("archive-policy", {
        enabled: $("archive-enabled").checked,
        after_days: days,
        revision: archivePolicy.revision,
      });
      archiveRuleSummary();
      alert(
        "archive-policy-alert",
        "归档规则已保存，适用于本机所有对话。",
        "success",
      );
      if (archivePreview || archiveBusy)
        clearArchivePreview("归档规则已更新，请重新预览旧任务。");
      await poll(true);
    } catch (error) {
      alert("archive-policy-alert", error.message, "error");
    } finally {
      archivePolicyBusy = false;
      renderArchiveControls();
    }
  };
  function clearArchivePreview(message = "") {
    archivePreview = null;
    archivePreviewVersion++;
    clearTimeout(archiveExpiry);
    $("archive-preview-list").replaceChildren();
    $("archive-preview-result").hidden = !message;
    text($("archive-preview-note"), message);
    renderArchiveControls();
  }
  $("archive-preview").onclick = async () => {
    if (archiveBusy) return;
    clearArchivePreview();
    const version = archivePreviewVersion;
    archiveBusy = true;
    renderArchiveControls();
    alert("archive-bulk-alert", "正在预览…");
    try {
      const data = await request("archive-preview", {});
      if (version !== archivePreviewVersion) return;
      archivePreview = data;
      $("archive-preview-result").hidden = false;
      text(
        $("archive-preview-note"),
        data.job_count
          ? `${data.job_count} 个未归属且成功完成的任务。预览 ${Math.floor(data.expires_in / 60)} 分钟内有效；执行前会再次检查。`
          : "没有可批量归档的未归属已完成任务。",
      );
      $("archive-preview-list").replaceChildren(
        ...data.jobs.map((job) =>
          el("li", "", `${job.title || "未命名任务"} · ${job.job_id}`),
        ),
      );
      text($("archive-bulk"), `归档这 ${data.job_count} 个任务`);
      archiveExpiry = setTimeout(
        () => clearArchivePreview("预览已过期，请重新预览。"),
        data.expires_in * 1000,
      );
      alert("archive-bulk-alert", "");
    } catch (error) {
      if (version === archivePreviewVersion)
        alert("archive-bulk-alert", error.message, "error");
    } finally {
      archiveBusy = false;
      renderArchiveControls();
    }
  };
  $("archive-bulk").onclick = async () => {
    if (archiveBusy || !archivePreview?.job_count) return;
    const token = archivePreview.token;
    archiveBusy = true;
    clearArchivePreview(); // A preview is single-use even if its response is lost.
    alert("archive-bulk-alert", "正在归档…");
    try {
      const data = await request("archive-bulk", { token });
      alert(
        "archive-bulk-alert",
        `已归档 ${data.archived} 个任务，跳过 ${data.skipped} 个已变化的任务，${data.errors} 个未成功。可在「已归档」中恢复。`,
        data.errors ? "error" : "success",
      );
      await poll(true);
    } catch (error) {
      alert(
        "archive-bulk-alert",
        `${error.message} 请检查「已归档」，重新预览后再重试。`,
        "error",
      );
    } finally {
      archiveBusy = false;
      renderArchiveControls();
    }
  };
  const tabs = ["monitor", "task", "storage", "settings"];
  function tab(id) {
    activeTab = id;
    for (const name of tabs) {
      $(name).hidden = name !== id;
      $("tab-" + name).setAttribute("aria-selected", String(name === id));
      $("tab-" + name).tabIndex = name === id ? 0 : -1;
    }
    if (id === "monitor") render();
    else if (id === "storage") {
      if (!storageData) loadStorage();
    } else {
      if (id === "task") taskProfile();
      refreshForTab();
    }
  }
  for (const [index, id] of tabs.entries()) {
    $("tab-" + id).onclick = () => tab(id);
    $("tab-" + id).onkeydown = (event) => {
      const target = {
        ArrowRight: (index + 1) % tabs.length,
        ArrowLeft: (index + tabs.length - 1) % tabs.length,
        Home: 0,
        End: tabs.length - 1,
      }[event.key];
      if (target === undefined) return;
      event.preventDefault();
      tab(tabs[target]);
      $("tab-" + tabs[target]).focus();
    };
  }
  $("connection").onclick = () => {
    tab("settings");
    $("tab-settings").focus();
  };
  document.addEventListener("visibilitychange", () => {
    clearTimeout(timer);
    if (!document.hidden) {
      render();
      if (!paused) poll(true);
      refreshForTab();
    }
  });
  setInterval(() => {
    if (document.hidden || activeTab !== "monitor") return;
    for (const node of document.querySelectorAll("[data-duration]")) {
      const job = jobs.find((item) => item.job_id === node.dataset.duration);
      if (job) text(node, duration(job));
    }
  }, 1000);
  async function init() {
    try {
      await refreshConfig();
      if (!config.panel_context?.conversation_id) view = "all";
      if (config.default_project_dir)
        $("task-project").value = config.default_project_dir;
      if (!Object.values(config.profiles).some((profile) => profile.ready)) {
        tab("settings");
        alert("settings-alert", "选择一个代理，填写连接配置后开始使用。");
      }
    } catch (error) {
      tab("settings");
      alert("settings-alert", error.message, "error");
    }
    poll();
    loadArchivePolicy();
  }
  init();
})();
