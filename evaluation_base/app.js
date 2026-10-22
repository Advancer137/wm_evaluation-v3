const $ = (id) => document.getElementById(id);
const state = { token: localStorage.getItem("pw_token"), task: null, done: 0, total: 0,
  history: [], saved: false, savedAnswers: null, busy: false, drafts: new Map() };

// Opened from "从已有结果开始": adopt the prepared record, then open its first saved answer.
const resumeToken = new URLSearchParams(location.search).get("resume");
if (resumeToken && /^[0-9a-f]{32}$/.test(resumeToken)) {
  state.token = resumeToken; state.reviewSeed = true;
  try { localStorage.setItem("pw_token", resumeToken); } catch { /* token kept in memory */ }
  history.replaceState(null, "", location.pathname);
}

function show(which) {
  for (const id of ["welcome", "workspace", "finished"]) $(id).classList.toggle("hidden", id !== which);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(state.token ? { Authorization: `Bearer ${state.token}` } : {}), ...(options.headers || {}) },
  });
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `HTTP ${response.status}`);
  return body;
}

function setText(id, value) { $(id).textContent = value == null ? "" : String(value); }

function demoScene(type, still = false) {
  const scene = document.createElement("div");
  scene.className = `demo-scene ${type}`;
  for (const cls of ["demo-road", "demo-light", "demo-car"]) {
    const part = document.createElement("div"); part.className = cls; scene.appendChild(part);
  }
  if (!still) {
    const button = document.createElement("button"); button.type = "button";
    button.className = "demo-controls"; button.textContent = "▶ 播放模拟动画";
    button.addEventListener("click", () => {
      scene.classList.remove("playing"); void scene.offsetWidth; scene.classList.add("playing");
      button.textContent = "↻ 重播";
    });
    scene.appendChild(button);
    const caption = document.createElement("span"); caption.className = "demo-caption";
    caption.textContent = "演示画面"; scene.appendChild(caption);
  }
  return scene;
}

function renderMedia(holder, url, image = false) {
  holder.replaceChildren();
  if (!url) { holder.textContent = image ? "未提供初始帧" : "素材暂不可用"; return; }
  if (url.startsWith("demo:")) { holder.appendChild(demoScene(url.slice(5), image)); return; }
  const el = document.createElement(image ? "img" : "video");
  el.src = url;
  if (!image) { el.controls = true; el.preload = "metadata"; el.playsInline = true; }
  el.addEventListener("error", () => { holder.textContent = "素材加载失败，请联系研究人员；不要凭猜测作答。"; });
  holder.appendChild(el);
}

function choices(side, field, prompt, options, help = "") {
  const name = `${side}_${field}`;
  const wrap = document.createElement("div"); wrap.className = "question"; wrap.dataset.q = name;
  const label = document.createElement("div"); label.className = "question-label"; label.textContent = prompt;
  wrap.appendChild(label);
  if (help) { const note = document.createElement("div"); note.className = "question-help"; note.textContent = help; wrap.appendChild(note); }
  const group = document.createElement("div"); group.className = "choices";
  for (const [value, title] of options) {
    const item = document.createElement("label"); item.className = "choice";
    const input = document.createElement("input"); input.type = "radio"; input.name = name; input.value = value;
    if (["validity", "base_task", "retention"].includes(field)) input.addEventListener("change", () => updateSide(side));
    const span = document.createElement("span"); span.textContent = title;
    item.append(input, span); group.appendChild(item);
  }
  wrap.appendChild(group);
  return wrap;
}

const RETENTION_REASONS = [["subject", "主体不一致"], ["scene", "场景不一致"], ["camera", "视角 / 镜头突变"], ["other", "其他"]];

function reasonBox(side) {
  const wrap = document.createElement("div"); wrap.className = "question reasons"; wrap.dataset.q = `${side}_retention_reasons`;
  const label = document.createElement("div"); label.className = "question-label"; label.textContent = "哪里没有保持？（可多选）";
  const group = document.createElement("div"); group.className = "choices";
  for (const [value, title] of RETENTION_REASONS) {
    const item = document.createElement("label"); item.className = "choice";
    const input = document.createElement("input"); input.type = "checkbox"; input.name = `${side}_retention_reasons`; input.value = value;
    const span = document.createElement("span"); span.textContent = title;
    item.append(input, span); group.appendChild(item);
  }
  const note = document.createElement("input"); note.type = "text"; note.className = "reason-note"; note.maxLength = 200;
  note.name = `${side}_retention_note`; note.placeholder = "补充说明（可选）";
  wrap.append(label, group, note);
  return wrap;
}

// Show only the questions that apply: an unusable video skips everything after the validity
// question; if the base task was not done the preferences are not asked (counted as "no").
function updateSide(side) {
  const validity = selected(`${side}_validity`);
  const base = selected(`${side}_base_task`);
  const retention = selected(`${side}_retention`);
  const scored = validity === "valid";
  const visible = { base_task: scored, retention: scored, retention_reasons: scored && retention === "fail",
    preference_a: scored && base !== "no", preference_b: scored && base !== "no" };
  for (const [field, show] of Object.entries(visible)) {
    const wrap = document.querySelector(`[data-q="${side}_${field}"]`);
    if (!wrap) continue;
    wrap.classList.toggle("hidden", !show);
    for (const input of wrap.querySelectorAll("input")) input.disabled = !show || state.busy;
  }
  const note = document.querySelector(`[data-skip="${side}"]`);
  const text = validity && !scored ? "该视频不能用于评测，后面的问题已跳过。"
    : scored && base === "no" ? "基础任务未完成：本段视频的偏好判断不再询问，汇总时按“否”记录。" : "";
  note.textContent = text; note.classList.toggle("hidden", !text);
}

function renderQuestions() {
  const root = $("questions"); root.replaceChildren();
  for (const side of ["x", "y"]) {
    const card = document.createElement("div"); card.className = "question-card";
    const h = document.createElement("h3"); h.textContent = `Video ${side.toUpperCase()}`;
    const sub = document.createElement("div"); sub.className = "sub"; sub.textContent = "请只依据这段视频的画面作答";
    card.append(h, sub);
    card.appendChild(choices(side, "validity", "视频是否能正常评测？", [["valid", "有效"], ["invalid", "损坏 / 无法播放"], ["insufficient", "证据不足"]],
      "只看技术上能否评测：能正常播放、主体在画面中、长度足够看清。动作做反、时间倒放属于内容问题，仍选“有效”。"));
    card.appendChild(choices(side, "base_task", "视频是否完成了“共同条件”描述的基础任务？", [["yes", "完成"], ["partial", "部分完成"], ["no", "未完成"], ["uncertain", "无法判断"]],
      "完成：共同条件里的动作和结果都出现了（缺前置步骤、小瑕疵不影响）。部分完成：方向、主体正确，事件明显开始，但缺后半或结果（如只跑没跳）。未完成：动作没发生、做反或倒放、做成别的事，或视频基本静止。"));
    card.appendChild(choices(side, "retention", "与初始画面相比，主体与场景是否保持一致？", [["pass", "保持"], ["fail", "未保持"], ["uncertain", "无法判断"]],
      "主体（人 / 车 / 物体）还是同一个，场景与环境没有被替换或突变。只比较一致性，不管基础任务是否完成。"));
    card.appendChild(reasonBox(side));
    card.appendChild(choices(side, "preference_a", "是否呈现 Preference A？", [["yes", "是"], ["no", "否"], ["uncertain", "无法判断"]],
      "只依据画面里已经出现的内容：相符且无矛盾选“是”；与偏好矛盾选“否”；已出现的内容不足以判断（偏好说的是没出现的部分、或看不清）才选“无法判断”。事件没做完不是选“无法判断”的理由。"));
    card.appendChild(choices(side, "preference_b", "是否呈现 Preference B？", [["yes", "是"], ["no", "否"], ["uncertain", "无法判断"]]));
    const skip = document.createElement("p"); skip.className = "skip-note hidden"; skip.dataset.skip = side; card.appendChild(skip);
    root.appendChild(card);
  }
}

function draftKey(id) { return `pw_draft:${state.token}:${id}`; }

function getDraft(id) {
  const key = draftKey(id);
  if (state.drafts.has(key)) return state.drafts.get(key);
  try { return JSON.parse(localStorage.getItem(key)); } catch { return null; }
}

function removeDraft(id) {
  const key = draftKey(id);
  state.drafts.delete(key);
  try { localStorage.removeItem(key); } catch { /* In-memory drafts remain usable. */ }
}

function buildAnswers(strict) {
  const answers = {};
  for (const side of ["x", "y"]) {
    const who = `Video ${side.toUpperCase()}`;
    const need = (name, message) => {
      const value = selected(`${side}_${name}`);
      if (strict && !value) throw new Error(message);
      return value;
    };
    const validity = need("validity", `请回答 ${who} 是否有效。`);
    const scored = validity === "valid";
    const base = scored ? need("base_task", `请回答 ${who} 是否完成了基础任务。`) : null;
    const retention = scored ? need("retention", `请回答 ${who} 的主体与场景是否保持一致。`) : null;
    let reasons = null, note = null;
    if (retention === "fail") {
      reasons = [...document.querySelectorAll(`input[name="${side}_retention_reasons"]:checked`)].map(input => input.value);
      if (strict && !reasons.length) throw new Error(`请选择 ${who} 哪里没有保持（可多选）。`);
      note = document.querySelector(`input[name="${side}_retention_note"]`).value.trim() || null;
    }
    Object.assign(answers, { [`${side}_validity`]: validity, [`${side}_base_task`]: base, [`${side}_retention`]: retention,
      [`${side}_retention_reasons`]: reasons, [`${side}_retention_note`]: note });
    for (const pref of ["preference_a", "preference_b"])
      answers[`${side}_${pref}`] = scored && base !== "no" ? need(pref, `请完成 ${who} 的偏好判断。`) : null;
  }
  return answers;
}

function readAnswers() { return buildAnswers(false); }

function saveDraft() {
  if (!state.task) return;
  const answers = readAnswers();
  const unchanged = state.savedAnswers && Object.keys(answers).every(key => JSON.stringify(answers[key]) === JSON.stringify(state.savedAnswers[key]));
  if (unchanged) removeDraft(state.task.id);
  else {
    const key = draftKey(state.task.id);
    state.drafts.set(key, answers);
    try { localStorage.setItem(key, JSON.stringify(answers)); } catch { /* Keep it in memory while navigating. */ }
  }
  renderNavigation();
}

function renderNavigation() {
  $("task-navigation").classList.remove("hidden");
  const index = state.history.findIndex(task => task.id === state.task?.id);
  const select = $("task-select");
  select.replaceChildren();
  if (index < 0) select.add(new Option("选择题目查看或修改", ""));
  for (const task of state.history) {
    const status = getDraft(task.id) ? "草稿未提交" : task.saved ? "已保存" : "未作答";
    select.add(new Option(`第 ${task.number} 题 · ${status}`, task.id));
  }
  select.value = state.task?.id || "";
  select.disabled = state.busy || !state.history.length;
  $("previous-task").disabled = state.busy || (index < 0 ? !state.history.length : index === 0);
  $("next-task").disabled = state.busy || index < 0 || (index === state.history.length - 1 && !state.saved);
  $("continue-tasks").disabled = state.busy;
  const draftCount = state.history.filter(task => getDraft(task.id)).length;
  setText("navigation-status", state.task
    ? `第 ${index + 1} 题 · ${getDraft(state.task.id) ? "修改已暂存于本浏览器，请提交或保存修改后再结束评测。" : state.saved ? "答案已保存，可继续修改。" : "尚未提交。切换题目时会保留当前选择。"}`
    : draftCount ? `还有 ${draftCount} 题草稿未提交，请从题目列表返回并保存修改。` : "可随时返回已答题目查看和修改答案。");
  if (!state.task) {
    $("finished").querySelector("h1").textContent = draftCount ? "还有修改待保存" : "本轮评测已完成";
    $("finished").querySelector("p").textContent = draftCount
      ? "先前提交的答案已保存，但草稿中的修改尚未提交。请从上方题目导航返回并点击“保存修改”。"
      : "你的答案已保存。你仍可以通过上方的题目导航回到任意已答题目，修改后点击“保存修改”。";
  }
  $("submit").textContent = state.saved ? "保存修改" : "提交并进入下一对 →";
}

function hideZh() { try { return localStorage.getItem("pw_hide_zh") === "1"; } catch { return false; } }

function renderTranslation(zh) {
  const has = !!zh && Object.keys(zh).length > 0;
  const hidden = hideZh();
  for (const [id, field] of [["base-zh", "base_condition"], ["pref-a-zh", "preference_a"], ["pref-b-zh", "preference_b"]]) {
    const text = has ? zh[field] : null;
    setText(id, text || "");
    $(id).classList.toggle("hidden", !text || hidden);
  }
  $("zh-toggle").classList.toggle("hidden", !has);
  $("zh-toggle").textContent = hidden ? "显示参考译文" : "隐藏参考译文";
  $("zh-note").classList.toggle("hidden", !has || hidden);
}

$("zh-toggle").addEventListener("click", () => {
  try { localStorage.setItem("pw_hide_zh", hideZh() ? "0" : "1"); } catch { /* keep current view */ }
  renderTranslation(state.task?.translation_zh);
});

function setBusy(busy) {
  state.busy = busy;
  $("submit").disabled = busy;
  $("restart-demo").disabled = busy;
  for (const input of document.querySelectorAll("#questions input")) input.disabled = busy;
  if (!busy) for (const side of ["x", "y"]) updateSide(side);
  renderNavigation();
}

async function refreshHistory() {
  state.history = (await api("/api/history")).tasks;
  state.done = state.history.filter(task => task.saved).length;
}

async function openTask(id) {
  const result = await api(`/api/tasks/${encodeURIComponent(id)}`);
  renderTask(result.task, result.answers);
}

async function navigate(action) {
  if (state.busy) return;
  setBusy(true);
  try { await action(); }
  catch (error) { setText("form-error", error.message); alert(`无法切换题目：${error.message}`); }
  finally { setBusy(false); }
}

function renderTask(task, savedAnswers = null) {
  state.task = task;
  state.saved = savedAnswers !== null;
  state.savedAnswers = savedAnswers;
  if (task.demo) localStorage.setItem("pw_demo", "1");
  show("workspace");
  $("demo-banner").classList.toggle("hidden", !task.demo);
  setText("case-tags", `样本 ${task.display_id}`);
  setText("base-text", task.base_condition);
  setText("pref-a", task.preference_a);
  setText("pref-b", task.preference_b);
  renderTranslation(task.translation_zh);
  renderMedia($("frame-wrap"), task.initial_frame_url, true);
  renderMedia($("video-x"), task.video_x_url);
  renderMedia($("video-y"), task.video_y_url);
  renderQuestions(); setText("form-error", "");
  const answers = getDraft(task.id) || savedAnswers;
  // Answers saved with the old question set have no base-task answer, and their old "retention" meant something else.
  const legacy = !!answers && !("x_base_task" in answers);
  if (answers) for (const input of document.querySelectorAll("#questions input")) {
    const value = answers[input.name];
    if (input.type === "checkbox") input.checked = Array.isArray(value) && value.includes(input.value);
    else if (input.type === "text") input.value = typeof value === "string" ? value : "";
    else input.checked = !(legacy && input.name.endsWith("_retention")) && value === input.value;
  }
  for (const side of ["x", "y"]) updateSide(side);
  if (legacy && savedAnswers && !getDraft(task.id)) setText("form-error", "这是旧版题目的答案：请补答“基础任务”和“主体与场景是否一致”后点击“保存修改”。");
  renderNavigation();
  setText("progress-label", `${state.done} / ${state.total} 已完成`);
  $("progress-fill").style.width = `${state.total ? 100 * state.done / state.total : 0}%`;
  window.scrollTo({ top: 0, behavior: "instant" });
}

async function nextTask() {
  const result = await api("/api/next");
  state.done = result.done; state.total = result.total;
  await refreshHistory();
  localStorage.setItem("pw_demo", result.demo_mode ? "1" : "0");
  if (result.task) renderTask(result.task);
  else {
    document.querySelectorAll("video").forEach(video => video.pause());
    state.task = null;
    show("finished");
    setText("finish-count", `本次共完成 ${result.done} 对视频` + (result.result_file ? `\n请把这个结果文件发给组织者：\n${result.result_file}` : ""));
    $("finish-count").style.whiteSpace = "pre-line";
    $("restart-demo").classList.toggle("hidden", localStorage.getItem("pw_demo") !== "1");
    renderNavigation();
  }
}

async function nextTaskCounts() {
  // progress numbers (also queues the next unanswered task) without leaving the review
  const result = await api("/api/next");
  state.done = result.done; state.total = result.total;
  await refreshHistory();
}

async function createSession(forceNew=false) {
  const session = await api("/api/session", { method: "POST", body: JSON.stringify({name:$("participant-name").value.trim(),new_session:forceNew||$("new-participant").checked}) });
  state.token = session.token;
  localStorage.setItem("pw_token", state.token);
  setText("participant", session.slot ? `${session.slot} · ${session.name}` : session.name);
  $("new-participant").checked=false;
}

function selected(name) { return document.querySelector(`input[name="${name}"]:checked`)?.value || null; }

function collectAnswers() { return buildAnswers(true); }

$("read-guide").addEventListener("change", (e) => { $("start").disabled = !e.target.checked; });
$("start").addEventListener("click", async () => {
  $("start").disabled = true;
  try {
    await createSession();
    if (state.reviewSeed) {
      state.reviewSeed = false;
      await nextTaskCounts();
      const first = state.history.find(task => task.saved);
      if (first) { await openTask(first.id); return; }
    }
    try { await nextTask(); }
    catch (error) {
      if (error.message !== "Invalid session") throw error;
      state.token = null;
      localStorage.removeItem("pw_token");
      await createSession();
      await nextTask();
    }
  } catch (error) {
    alert(`无法开始：${error.message}`); $("start").disabled = false;
  }
});
$("annotation-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (state.busy) return;
  try {
    const answers = collectAnswers();
    const taskId = state.task.id;
    const editing = state.saved;
    setBusy(true); setText("form-error", "正在保存…");
    const submission = await api("/api/submit", { method: "POST", body: JSON.stringify({ task_id: taskId, answers }) });
    if (submission.export_warning) alert(submission.export_warning);
    removeDraft(taskId);
    state.saved = true; state.savedAnswers = answers;
    await refreshHistory();
    if (editing) {
      renderTask(state.task, answers);
      setText("form-error", "修改已保存。");
    } else await nextTask();
  } catch (error) {
    setText("form-error", error.message);
  } finally { setBusy(false); }
});
$("annotation-form").addEventListener("change", saveDraft);
$("task-select").addEventListener("change", event => {
  const id = event.target.value;
  if (id) navigate(() => openTask(id));
});
$("previous-task").addEventListener("click", () => navigate(async () => {
  const index = state.history.findIndex(task => task.id === state.task?.id);
  const previous = state.history[index < 0 ? state.history.length - 1 : index - 1];
  if (previous) await openTask(previous.id);
}));
$("next-task").addEventListener("click", () => navigate(async () => {
  const index = state.history.findIndex(task => task.id === state.task?.id);
  const next = state.history[index + 1];
  if (next) await openTask(next.id);
  else await nextTask();
}));
$("continue-tasks").addEventListener("click", () => navigate(nextTask));
$("restart-demo").addEventListener("click", async () => {
  try {
    await createSession(true);
    await nextTask();
  } catch (error) { alert(`无法重新开始：${error.message}`); }
});
api("/api/profile").then(p=>{
  if(p.slot){
    $("new-participant-row").classList.add("hidden");
    $("slot-note").classList.remove("hidden");
    setText("slot-note", `这是评测编号 ${p.slot} 的评测包，请只由一个人使用。中途退出后，输入同样的姓名即可从上次的位置继续。`);
  }
  if(p.name){$("participant-name").value=p.name;setText("participant",p.slot?`${p.slot} · ${p.name}`:p.name);}
  else if(p.slot){setText("participant",p.slot);}
}).catch(()=>{});
