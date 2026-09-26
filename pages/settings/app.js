const $ = (id) => document.getElementById(id);
const bridge = window.AstrBotPluginPage;
let state, settings, dirty = false, busy = false, jsonDirty = false;
let page = 1, pageCount = 1, searchTimer, catalog = [];
const openCommands = new Set();
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
  for (const id of ["reload", "refresh", "save", "excludes", "search", "overrides", "apply-json", "plugin-search", "plugin-filter", "state-filter", "page-size", "clear-filters", "collapse", "page-number"]) $(id).disabled = busy || !state;
  document.querySelectorAll("section").forEach((section) => { section.inert = busy || !state; });
  $("refresh").disabled = busy || !state || dirty;
  $("reload").textContent = dirty ? "放弃草稿并重载" : "重新载入";
  $("previous").disabled = busy || !state || page <= 1;
  $("next").disabled = busy || !state || page >= pageCount;
  $("previous-top").disabled = $("previous").disabled;
  $("next-top").disabled = $("next").disabled;
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
  const pluginTitles = new Map(state.plugins.map(p => [p.name, p.title]));
  catalog = state.commands.map((command, index) => ({command, index,
    search: `${pluginTitles.get(command.plugin) || ""} ${command.plugin} ${command.command} ${command.tool} ${command.description || ""} ${(command.params || []).map(p => `${p.name} ${p.description || ""}`).join(" ")}`.toLowerCase()}));
  const selectedPlugin = $("plugin-filter").value;
  $("plugin-filter").replaceChildren();
  const all = node("option", "全部插件"); all.value = ""; $("plugin-filter").append(all);
  for (const name of [...new Set(state.commands.map(c => c.plugin))].sort((a, b) => a.localeCompare(b, 'zh-CN', {numeric: true}))) {
    const option = node("option", pluginTitles.get(name) || name); option.value = name; $("plugin-filter").append(option);
  }
  $("plugin-filter").value = selectedPlugin;
  if ($("plugin-filter").selectedIndex < 0) $("plugin-filter").value = "";
  openCommands.clear();
  $("summary").textContent = `当前已桥接 ${state.registered_count} 个工具 · 指令目录 ${state.commands.length} 条`;
  $("excludes").value = settings.exclude_commands.join("\n");
  syncJson(); renderPlugins(); renderCommands();
}
function renderPlugins() {
  const parent = $("plugins"); parent.replaceChildren();
  $("plugin-summary").textContent = `已选 ${settings.plugins.length} / ${state.plugins.length}`;
  const query = $("plugin-search").value.trim().toLowerCase();
  for (const plugin of state.plugins) {
    if (!`${plugin.title} ${plugin.name}`.toLowerCase().includes(query)) continue;
    const label = node("label", undefined, "check"), input = node("input");
    input.type = "checkbox"; input.checked = settings.plugins.includes(plugin.name);
    input.onchange = () => {
      settings.plugins = input.checked ? [...new Set([...settings.plugins, plugin.name])] : settings.plugins.filter((x) => x !== plugin.name);
      changed();
      $("plugin-summary").textContent = `已选 ${settings.plugins.length} / ${state.plugins.length}`;
    };
    const text = node("span", `${plugin.title}${plugin.enabled ? "" : "（未启用或未安装）"}`);
    text.append(node("br"), node("small", plugin.name)); label.append(input, text); parent.append(label);
  }
  if (!parent.children.length) parent.append(node("p", query ? "没有匹配的插件。" : "暂无插件目录。", "empty"));
}
function editOverride(command, key, value, parameter) {
  // 保留高级编辑器草稿，避免可视化编辑静默覆盖它。
  if (jsonDirty) applyJson();
  const current = settings.overrides[command.tool] || {
    description: command.description,
    params: Object.fromEntries(command.params.map((param) => [param.name, param.description || ""])),
  };
  const entry = {...current};
  if (parameter) entry.params = {...(current.params || {}), [key]: value};
  else entry[key] = value;
  settings.overrides[command.tool] = entry;
  // 大量覆盖项只在打开高级编辑器或保存时序列化，避免每次输入重写完整 JSON。
  if ($("overrides").closest("details").open) syncJson();
  changed();
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
  const words = $("search").value.trim().toLowerCase().split(/\s+/).filter(Boolean);
  const plugin = $("plugin-filter").value, mode = $("state-filter").value;
  const rows = catalog.filter(({command, search}) => (!plugin || command.plugin === plugin)
    && words.every(word => search.includes(word))
    && (mode === "all" || mode === "registered" && command.registered
      || mode === "pending" && !command.registered && !command.reason
      || mode === "skipped" && !!command.reason
      || mode === "overridden" && Object.hasOwn(settings.overrides, command.tool)));
  const size = Number($("page-size").value);
  pageCount = Math.max(1, Math.ceil(rows.length / size)); page = Math.min(page, pageCount);
  const start = (page - 1) * size;
  $("result-count").textContent = `匹配 ${rows.length} / ${catalog.length} 条 · 显示 ${rows.length ? start + 1 : 0}–${Math.min(start + size, rows.length)} 条`;
  $("page-number").value = page; $("page-number").max = pageCount;
  $("page-count").textContent = `/ ${pageCount} 页`;
  $("page-count-top").textContent = `第 ${page} / ${pageCount} 页`;
  for (const {command, index} of rows.slice(start, start + size)) {
    const card = node("details", undefined, "entry");
    card.dataset.index = index;
    const summary = node("summary", `${command.command || "（未命名）"} → ${command.tool} `);
    summary.append(node("span", command.reason || (command.registered ? "已桥接" : "符合条件，尚未注册"), "pill"));
    card.append(summary, node("p", command.plugin, "muted"));
    if (command.inspection_error) card.append(node("p", command.inspection_error, "muted"));
    let editor;
    const buildEditor = () => {
      if (editor) return;
      editor = node("div");
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
      editor.append(grid, node("p", "编辑会使用工具全名保存覆盖；其他匹配项继续保留。", "muted"), reset);
      card.append(editor);
    };
    card.ontoggle = () => {
      if (!card.isConnected) return;
      if (card.open) { openCommands.add(index); buildEditor(); }
      else { openCommands.delete(index); editor?.remove(); editor = undefined; }
    };
    if (openCommands.has(index)) { card.open = true; buildEditor(); }
    parent.append(card);
  }
  if (!rows.length) parent.append(node("p", "没有匹配的指令。", "empty"));
  controls();
}
$("excludes").oninput = () => {
  settings.exclude_commands = $("excludes").value.split("\n").map((x) => x.trim()).filter(Boolean); changed();
};
function filterChanged() { clearTimeout(searchTimer); page = 1; renderCommands(); }
$("search").oninput = () => { clearTimeout(searchTimer); searchTimer = setTimeout(filterChanged, 180); };
$("plugin-search").oninput = renderPlugins;
for (const id of ["plugin-filter", "state-filter", "page-size"]) $(id).onchange = filterChanged;
$("clear-filters").onclick = () => { $("search").value = ""; $("plugin-filter").value = ""; $("state-filter").value = "all"; filterChanged(); };
$("collapse").onclick = () => {
  for (const card of $("commands").children) openCommands.delete(Number(card.dataset.index));
  renderCommands();
};
function goToPage(next) {
  if (!Number.isInteger(next)) { $("page-number").value = page; return; }
  page = Math.max(1, Math.min(pageCount, next)); renderCommands();
  $("command-section").scrollIntoView({block: "start"});
}
$("previous").onclick = () => goToPage(page - 1);
$("next").onclick = () => goToPage(page + 1);
$("previous-top").onclick = $("previous").onclick;
$("next-top").onclick = $("next").onclick;
$("page-number").onchange = () => goToPage(Number($("page-number").value));
$("overrides").closest("details").ontoggle = event => {
  if (event.target.open && !jsonDirty) syncJson();
};
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
