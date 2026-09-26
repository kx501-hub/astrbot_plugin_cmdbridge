const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
let state, settings, dirty = false, busy = false, jsonDirty = false;
function node(tag, text, className) {
  const el = document.createElement(tag);
  if (text !== undefined) el.textContent = text;
  if (className) el.className = className;
  return el;
}
function message(text, error = false) {
  $("status").textContent = text;
  $("status").className = `status${error ? " error" : ""}`;
}
function controls() {
  for (const id of ["reload", "refresh", "save", "excludes", "search", "overrides", "apply-json"]) $(id).disabled = busy || !state;
  document.querySelectorAll("section").forEach((section) => { section.inert = busy || !state; });
  $("refresh").disabled = busy || !state || dirty;
  $("reload").textContent = dirty ? "放弃草稿并重载" : "重新载入";
}
function changed() { dirty = true; message("有未保存的草稿；指令状态将在保存并应用后更新。"); controls(); }
async function run(action) {
  if (busy) return;
  busy = true; controls();
  try { await action(); } catch (error) { message(error.message || String(error), true); }
  finally { busy = false; controls(); }
}
function syncJson() {
  $("overrides").value = JSON.stringify(settings.overrides, null, 2); jsonDirty = false;
}
function applyJson() {
  const value = JSON.parse($("overrides").value);
  if (!value || typeof value !== "object" || Array.isArray(value) || Object.values(value).some((x) => !x || typeof x !== "object" || Array.isArray(x))) {
    throw new Error("覆盖配置必须是工具名到对象的 JSON 映射。");
  }
  settings.overrides = value; jsonDirty = false;
}
function adopt(loaded) {
  const next = JSON.parse(JSON.stringify(loaded.settings));
  if (typeof next.overrides === "string") {
    try { next.overrides = JSON.parse(next.overrides || "{}"); }
    catch { throw new Error("原有覆盖 JSON 无效，请先在插件基础配置中修正，页面不会覆盖它。"); }
  }
  if (!next.overrides || typeof next.overrides !== "object" || Array.isArray(next.overrides)) throw new Error("原有覆盖配置必须是对象。");
  settings = next; state = loaded; dirty = false; jsonDirty = false;
  $("summary").textContent = `当前已桥接 ${state.registered_count} 个工具 · 指令目录 ${state.commands.length} 条`;
  $("excludes").value = settings.exclude_commands.join("\n");
  syncJson(); renderPlugins(); renderCommands();
}
function renderPlugins() {
  const parent = $("plugins"); parent.replaceChildren();
  for (const plugin of state.plugins) {
    const label = node("label", undefined, "check"), input = node("input");
    input.type = "checkbox"; input.checked = settings.plugins.includes(plugin.name);
    input.onchange = () => {
      settings.plugins = input.checked ? [...new Set([...settings.plugins, plugin.name])] : settings.plugins.filter((x) => x !== plugin.name);
      changed();
    };
    const text = node("span", `${plugin.title}${plugin.enabled ? "" : "（未启用或未安装）"}`);
    text.append(node("br"), node("small", plugin.name)); label.append(input, text); parent.append(label);
  }
  if (!state.plugins.length) parent.append(node("p", "暂无插件目录。", "empty"));
}
function editOverride(command, key, value, parameter) {
  // Never silently discard edits in the advanced JSON editor.
  if (jsonDirty) applyJson();
  const current = settings.overrides[command.tool] || {
    description: command.description,
    params: Object.fromEntries(command.params.map((param) => [param.name, param.description || ""])),
  };
  const entry = {...current};
  if (parameter) entry.params = {...(current.params || {}), [key]: value};
  else entry[key] = value;
  settings.overrides[command.tool] = entry;
  syncJson(); changed();
}
function inputField(parent, labelText, value, onChange) {
  const label = node("label", labelText), input = node("textarea"); input.value = value || "";
  input.oninput = () => {
    try { onChange(input.value); }
    catch (error) { message(error.message, true); }
  };
  label.append(input); parent.append(label);
}
function renderCommands() {
  const parent = $("commands"); parent.replaceChildren();
  const query = $("search").value.trim().toLowerCase();
  const rows = state.commands.filter((row) => `${row.plugin} ${row.command} ${row.tool}`.toLowerCase().includes(query));
  for (const command of rows) {
    const card = node("details", undefined, "entry");
    const summary = node("summary", `${command.command || "（未命名）"} → ${command.tool} `);
    summary.append(node("span", command.reason || (command.registered ? "已桥接" : "符合条件，尚未注册"), "pill"));
    card.append(summary, node("p", command.plugin, "muted"));
    if (command.inspection_error) card.append(node("p", command.inspection_error, "muted"));
    const override = settings.overrides[command.tool] || {};
    const grid = node("div", undefined, "grid");
    inputField(grid, "工具说明", override.description ?? override.desc ?? command.description,
      (value) => editOverride(command, "description", value, false));
    for (const param of command.params) {
      inputField(grid, `参数 ${param.name}（${param.type}）`, override.params?.[param.name] ?? override[param.name] ?? param.description,
        (value) => editOverride(command, param.name, value, true));
    }
    const reset = node("button", "移除此工具专属覆盖");
    reset.onclick = () => {
      try {
        if (jsonDirty) applyJson();
        delete settings.overrides[command.tool]; syncJson(); changed(); renderCommands();
        message("已从草稿移除此工具全名的覆盖。保存后重新计算其他匹配规则和原始说明。");
      } catch (error) { message(error.message, true); }
    };
    card.append(grid, node("p", "编辑会使用工具全名保存覆盖；其他匹配项继续保留。", "muted"), reset);
    parent.append(card);
  }
  if (!rows.length) parent.append(node("p", "没有匹配的指令。", "empty"));
}
$("excludes").oninput = () => {
  settings.exclude_commands = $("excludes").value.split("\n").map((x) => x.trim()).filter(Boolean); changed();
};
$("search").oninput = renderCommands;
$("overrides").oninput = () => { jsonDirty = true; changed(); };
$("apply-json").onclick = () => {
  try { applyJson(); changed(); renderCommands(); message("JSON 已应用到草稿，点击“保存并应用”持久化。"); }
  catch (error) { message(error.message, true); }
};
$("reload").onclick = () => run(async () => { adopt(await bridge.apiGet("settings")); message("已重新载入配置和运行状态。"); });
$("refresh").onclick = () => run(async () => { adopt(await bridge.apiPost("refresh", {})); message("已按保存的配置刷新桥接。"); });
$("save").onclick = () => run(async () => {
  if (jsonDirty) applyJson();
  adopt(await bridge.apiPost("settings/save", {settings, revision: state.revision}));
  message(`配置已保存，当前已桥接 ${state.registered_count} 个工具。`);
});
run(async () => {
  if (!bridge) throw new Error("请从 AstrBot → 插件 → CmdBridge → Pages 打开此页。");
  await bridge.ready(); adopt(await bridge.apiGet("settings")); message("已载入。勾选插件或编辑说明后保存并应用。");
});
