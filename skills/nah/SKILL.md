---
name: nah
description: Open or reopen the Native Agent Hub (NAH) panel on the right in Codex, scoped to the current chat and project. Use for "$nah", "打开 NAH", "打开代理面板", or requests to view NAH task progress. Opening the panel alone does not submit tasks or call models.
---

# Open Native Agent Hub

1. Discover the installed `native-agent-hub` MCP tools and the host's `open_in_codex` tool. Read `get_configuration` first when no configuration has been read in this chat. Do not test connections, fetch model catalogs, submit tasks, or request credentials just to open the panel.
2. Resolve the current project's existing absolute directory from workspace context. Omit `project_dir` if there is no project. Do not use the skill's installation directory as the project.
3. Prefer the current chat's host-provided stable thread/conversation ID. Do not infer it from project paths, screenshots, other chats, old panel URLs, or server processes. If the host ID is unavailable, call `get_panel` once without `conversation_id` and retain the returned ID for this chat. Reuse this same ID for later panel calls and any separately requested `submit_task` calls.
4. Call `get_panel(project_dir=..., conversation_id=...)` with the resolved context. Retain its returned URL and conversation ID only in the current chat's context. If a panel already exists for this exact chat/project, reuse its tab; never navigate a different chat's panel. `get_panel` reuses an existing context on the same service, so it is safe to call again with the same ID to obtain a fresh valid URL. Do not construct token-bearing URLs yourself.
5. Open the returned URL with `open_in_codex`, `placement="right"`, `target={"type":"browser","url":...}`. When a valid matching tab is already known, use its `tabId` to focus it instead of creating another tab. After a service restart, obtain a new URL with the same conversation ID and update the known tab where the host supports navigation.
6. Verify the page loaded when browser inspection is available. Briefly confirm that the panel is open. If the host cannot open a right panel, provide the returned local URL for the user to open. Do not claim it opened if the operation failed.

## Recovery

- Missing MCP tools: report that the Native Agent Hub MCP service must be enabled or registered. If the repository is in the workspace, point to its `install.ps1`; otherwise ask for its location only if repair is requested. Do not silently start another service or modify Codex configuration.
- Old tool schema without `conversation_id`: explain that the running MCP service needs restarting or reloading after upgrading. Do not silently open an unscoped panel or make up an unsupported parameter. An already verified current-chat panel may still be focused. Avoid stopping a service with active tasks.
- Expired panel: call `get_panel` again with the retained context, then open the new URL. Do not probe unrelated local ports or read credential files.
- Delegation, model choice, archiving, and storage cleanup remain governed by the NAH server and the user's separate request. This skill is an entry point, not a duplicate implementation of those policies.
