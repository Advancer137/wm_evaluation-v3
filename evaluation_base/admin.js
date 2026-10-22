const $ = id => document.getElementById(id);
const state = {token:null,batches:[],tasks:[],scope:'',busy:false,report:null};
const titles = {setup:'组织一次评测', questions:'题目与作答', results:'收集与汇总结果'};
function message(text,error=false){$('message').textContent=text;$('message').classList.toggle('error',error);}
async function api(path,body){const response=await fetch(path,{method:body===undefined?'GET':'POST',headers:{'Content-Type':'application/json','X-Admin-Token':state.token||''},body:body===undefined?undefined:JSON.stringify(body)});const data=await response.json();if(!response.ok)throw Error(data.error||'请求失败');return data;}
async function work(text,fn){if(state.busy)return;state.busy=true;message(text);document.querySelectorAll('button').forEach(b=>b.disabled=true);try{await fn();}catch(e){message(e.message,true);}finally{state.busy=false;document.querySelectorAll('button').forEach(b=>b.disabled=false);updateControls();}}
function metrics(id,items){$(id).replaceChildren(...items.map(([label,value])=>{const div=document.createElement('div');div.className='metric';const b=document.createElement('b'),span=document.createElement('span');b.textContent=value;span.textContent=label;div.append(b,span);return div;}));}
function el(tag,text,cls){const element=document.createElement(tag);if(text!==undefined)element.textContent=text;if(cls)element.className=cls;return element;}
function showPage(page){document.querySelectorAll('.page').forEach(p=>p.classList.toggle('hidden',p.id!==`page-${page}`));document.querySelectorAll('[data-page]').forEach(b=>b.classList.toggle('active',b.dataset.page===page));$('page-title').textContent=titles[page];document.querySelectorAll('video').forEach(v=>v.pause());window.scrollTo({top:0,behavior:'instant'});}
document.querySelectorAll('[data-page]').forEach(b=>b.onclick=()=>showPage(b.dataset.page));
function fillSelect(id,items,placeholder){const select=$(id),old=select.value;select.replaceChildren();if(placeholder)select.add(new Option(placeholder,''));for(const [value,label]of items)select.add(new Option(label,value));if([...select.options].some(o=>o.value===old))select.value=old;}
async function refresh(){const result=await api('/api/status');state.token=result.token;state.batches=result.batches;state.job=result.job;state.packagesDirectory=result.packages_directory;$('source-path').textContent=result.source;const items=state.batches.map(b=>[b.name,`${b.name} · ${b.task_count} 题`]);for(const id of ['batch-select','result-batch','package-batch'])fillSelect(id,items,'请选择题库');fillSelect('question-scope',items,'输入数据（含缺失项）');if(state.batches.length===1)for(const id of ['batch-select','result-batch','package-batch'])$(id).value=state.batches[0].name;updateControls();}
function fmtSize(bytes){return bytes>=1073741824?(bytes/1073741824).toFixed(2)+' GB':(bytes/1048576).toFixed(1)+' MB';}
function fmtTime(text){return text?new Date(text).toLocaleString():'—';}
function planLabel(p){return `${p.plan_id} · ${p.slots} 人 × 每题 ${p.redundancy} 份 · 每人 ${p.per_slot_min===p.per_slot_max?p.per_slot_min:p.per_slot_min+'–'+p.per_slot_max} 题`;}
function renderPlans(){
  const batch=state.batches.find(b=>b.name===$('package-batch').value),plans=batch?.plans||[];
  fillSelect('plan-select',plans.map(p=>[p.plan_id,planLabel(p)]),plans.length?'':'尚无分配方案');
  const plan=plans.find(p=>p.plan_id===$('plan-select').value);
  const box=$('plan-packages');box.replaceChildren();
  if(!plan)return;
  const table=el('table'),head=el('tr');for(const t of ['编号','题数','评测包','下载'])head.append(el('th',t));const thead=el('thead');thead.append(head);table.append(thead);
  const body=el('tbody');
  for(const pk of plan.packages){const tr=el('tr');tr.append(el('td',pk.slot),el('td',pk.tasks),el('td',pk.exists?fmtSize(pk.size):'尚未生成'));const td=el('td');if(pk.exists){const a=el('a','下载','button');a.href=`/api/download-slot?batch=${encodeURIComponent(batch.name)}&plan=${plan.plan_id}&slot=${encodeURIComponent(pk.slot)}`;td.append(a);}tr.append(td);body.append(tr);}
  table.append(body);const wrap=el('div',undefined,'scroll');wrap.append(table);box.append(wrap);
}
function renderPlanPreview(){
  const batch=state.batches.find(b=>b.name===$('package-batch').value),k=Number($('plan-slots').value),r=Number($('plan-redundancy').value);
  if(!batch||!k||!r){$('plan-preview').textContent='';return;}
  $('plan-preview').textContent=r>k?'每题份数不能大于标注人数（同一道题必须由不同的人做）。':`共 ${batch.task_count} 题 × ${r} 份 = ${batch.task_count*r} 份答案，平均每人约 ${Math.ceil(batch.task_count*r/k)} 题。`;
}
function updateControls(){
  const name=$('batch-select').value,batch=state.batches.find(b=>b.name===name),running=batch?.running;
  $('prepare').disabled=state.busy||!state.report?.summary.can_prepare;
  $('start-eval').disabled=state.busy||!name||!!running;
  $('stop-eval').disabled=state.busy||!running;
  for(const id of ['limit','target'])$(id).disabled=state.busy||!!running;
  const packageName=$('package-batch').value;
  const packageBatch=state.batches.find(b=>b.name===packageName);
  $('download-kit').classList.toggle('hidden',!packageBatch?.package_exists);
  $('build-package').disabled=state.busy||!packageName;
  $('build-plan').disabled=state.busy||!packageName||Number($('plan-redundancy').value)>Number($('plan-slots').value);
  $('repackage').disabled=state.busy||!$('plan-select').value;
  renderPlanPreview();renderPlans();
  fillSelect('report-plan',(state.batches.find(b=>b.name===$('result-batch').value)?.plans||[]).map(p=>[p.plan_id,planLabel(p)]),'');
  $('refresh-report').disabled=state.busy||!$('result-batch').value;
  const shownPlan=packageBatch?.plans?.find(p=>p.plan_id===$('plan-select').value);
  $('package-path').textContent=shownPlan?.package_dir||state.packagesDirectory||'';
  $('download-kit').href=`/api/download-kit?batch=${encodeURIComponent(packageName)}`;
  $('volunteer-link').classList.toggle('hidden',!running);
  if(running)$('volunteer-link').href=running.url+(state.resume?.batch===name?`?resume=${state.resume.token}`:'');
  $('batch-status').textContent=running?'本机评测已启动。点击“打开志愿者界面”交给志愿者作答。':name?'此题库已就绪，点击“启动本机评测”开始或继续。':'尚未选择题库。请先准备评测包，或解压组织者提供的评测包。';
  const local=batch?.local_results;
  $('local-result-path').textContent=local?.path||'';
  $('local-result-files').replaceChildren(...(local?.files||[]).map(f=>el('p',`${f.name} · ${f.answers} 份答案 · ${f.current?'已更新':'待重新导出'}\n${f.path}`)));
  $('local-result-status').textContent=!local?'选择题库后查看。':!local.exists&&!local.answers?'尚无作答。提交答案后会按姓名生成结果文件，并持续更新。':`本机已保存 ${local.answers} 份答案 · ${local.people} 个作答会话。${local.current?'结果文件已更新'+(local.exported_at?'（'+new Date(local.exported_at).toLocaleString()+'）':'')+'。':'结果文件缺失或未更新，请点击“下载本机最新结果”重新导出后收集。'}`;
  $('download-local-results').classList.toggle('hidden',!local||!local.exists&&!local.answers);
  $('download-local-results').href=`/api/download-results?batch=${encodeURIComponent(name)}`;
  const result=$('result-batch').value;
  const resultLocal=state.batches.find(b=>b.name===result)?.local_results;
  for(const [id,path]of [['download-results','download-results'],['download-aggregate','download-aggregate']]){
    $(id).classList.toggle('hidden',!result||(id==='download-results'&&!resultLocal?.exists&&!resultLocal?.answers));
    $(id).href=`/api/${path}?batch=${encodeURIComponent(result)}`;
  }
  for(const id of ['aggregate','import-results'])$(id).disabled=state.busy||!result;
}
function renderReport(report){state.report=report;const s=report.summary;const tr=s.translation||{status:'missing'};metrics('check-summary',[['基础信息',s.metadata_rows],['视频',s.video_files],['完整配对',s.complete_pairs],['可用题目',s.eligible_tasks],['缺初始帧（警告）',s.tasks_without_frame??0],['排除分组',s.excluded_groups],['参考译文',tr.status==='enabled'?`已启用 ${tr.tasks_fully_translated}/${s.eligible_tasks} 题`:tr.status==='disabled'?'文件不可用，已关闭':'未提供（不显示）']]);$('prepare-hint').textContent=s.can_prepare?`将复制约 ${(s.snapshot_media_bytes/1024/1024).toFixed(1)} MB 媒体`:'请先处理缺失项或错误；当前无法创建评测题库。';renderIssues(report);updateControls();}
const LEVELS=[['fatal','致命错误','必须处理，否则无法创建题库'],['error','错误','相关视频组不会进入题库'],['warning','警告','不影响创建题库']];
function issueLevel(i){return i.fatal?'fatal':(i.severity==='warning'?'warning':'error');}
function issueContext(i){return Object.entries(i).filter(([k])=>!['message','fatal','severity','code'].includes(k)).map(([k,v])=>`${k}=${typeof v==='object'?JSON.stringify(v):v}`).join('  ');}
function renderIssues(report){
  const root=$('check-issues');root.replaceChildren();
  const labels=report.issue_labels||{};
  const counts={fatal:0,error:0,warning:0};for(const i of report.issues)counts[issueLevel(i)]++;
  $('check-details').querySelector('summary').textContent=report.issues.length?`查看检查明细：致命错误 ${counts.fatal} · 错误 ${counts.error} · 警告 ${counts.warning}`:'查看检查明细：检查通过';
  if(counts.fatal||counts.error)$('check-details').open=true;
  if(!report.issues.length){root.append(el('p','检查通过。'));return;}
  for(const [level,name,hint] of LEVELS){
    const items=report.issues.filter(i=>issueLevel(i)===level);if(!items.length)continue;
    const box=el('div',undefined,`issue-level ${level}`);box.append(el('h4',`${name} · ${items.length} 条`),el('p',hint,'muted'));
    const groups=new Map();for(const i of items){if(!groups.has(i.code))groups.set(i.code,[]);groups.get(i.code).push(i);}
    for(const [code,list] of [...groups].sort((a,b)=>b[1].length-a[1].length)){
      const d=el('details',undefined,'issue-group'),sum=el('summary');
      sum.append(el('b',labels[code]||list[0].message),el('span',` · ${list.length} 条`,'muted'));d.append(sum);
      const messages=[...new Set(list.map(i=>i.message))];
      if(messages.length===1&&messages[0]!==labels[code])d.append(el('p',messages[0],'muted'));
      const ul=el('ul',undefined,'scroll issue-items');
      d.addEventListener('toggle',()=>{if(!d.open||ul.childElementCount)return;
        for(const i of list.slice(0,500)){const li=el('li');if(messages.length>1)li.append(el('span',i.message+' — '));li.append(el('code',issueContext(i)));ul.append(li);}
        if(list.length>500)ul.append(el('li',`……还有 ${list.length-500} 条，完整列表见 reports/input_check.md`,'muted'));});
      d.append(ul);box.append(d);
    }
    root.append(box);
  }
}
async function check(){const result=await api('/api/check',{});renderReport(result.report);state.tasks=result.tasks;state.scope='';$('question-scope').value='';renderFilters();renderTasks();message(`检查完成：${result.report.summary.eligible_tasks} 道可用题目。所有输入题目（含缺失项）可在“题目与作答”查看。`);}
$('check').onclick=()=>work('正在检查基础信息、首帧和视频配对…',check);
function showJob(job){
  $('prepare-progress').classList.remove('hidden');
  $('prepare-bar').value=job.percent;
  $('prepare-progress-text').textContent=`${job.percent}% · ${job.message}`;
  if(job.package_dir)$('package-path').textContent=job.package_dir;else if(job.package_path)$('package-path').textContent=job.package_path;
}
async function followJob(job){
  showJob(job);
  while(job.status==='running'){
    await new Promise(resolve=>setTimeout(resolve,800));
    job=await api(`/api/job?id=${encodeURIComponent(job.id)}`);
    showJob(job);
  }
  await refresh();
  if(job.status==='failed')throw Error(job.kind==='prepare'?job.message:`${job.message}。可选择已有方案并点击“重新打包”重试。`);
  if(job.report)renderReport(job.report);
  for(const id of ['batch-select','result-batch','package-batch'])$(id).value=job.batch;
  if(job.kind==='plan')message(`已生成 ${job.packages.length} 个评测包，保存在：\n${job.package_dir}\n把各编号的 ZIP 发给对应的电脑即可。`);
  else if(job.kind==='prepare')message(job.message);
  else message(`完整分发包已生成：\n${job.package_path}`);
}
$('prepare').onclick=()=>work('正在创建题库（含视频播放兼容转换，可能需要几分钟）…',async()=>followJob(await api('/api/prepare',{batch:$('new-batch').value.trim()})));
$('build-plan').onclick=()=>work('正在生成分配方案并打包…',async()=>{const seed=$('plan-seed').value.trim();const job=await api('/api/plan',{batch:$('package-batch').value,slots:Number($('plan-slots').value),redundancy:Number($('plan-redundancy').value),seed:seed===''?null:Number(seed)});await followJob(job);$('plan-select').value=job.plan_id;updateControls();});
$('repackage').onclick=()=>work('正在重新打包…',async()=>followJob(await api('/api/plan',{batch:$('package-batch').value,plan_id:$('plan-select').value})));
for(const id of ['plan-slots','plan-redundancy'])$(id).addEventListener('input',updateControls);$('plan-select').onchange=renderPlans;
$('build-package').onclick=()=>work('正在生成分发包…',async()=>followJob(await api('/api/package',{batch:$('package-batch').value})));
$('refresh-batches').onclick=()=>work('正在刷新…',async()=>{await refresh();message('题库和本机结果状态已更新。');});
$('batch-select').onchange=updateControls;$('package-batch').onchange=updateControls;$('go-results').onclick=()=>{if($('batch-select').value)$('result-batch').value=$('batch-select').value;updateControls();showPage('results');};$('result-batch').onchange=()=>{updateControls();$('aggregate-rows').replaceChildren();$('aggregate-summary').replaceChildren();$('import-log').replaceChildren();$('report-summary').replaceChildren();$('report-warnings').replaceChildren();$('report-slots').replaceChildren();$('report-under').replaceChildren();$('report-imports').replaceChildren();};
$('start-eval').onclick=()=>work('正在校验固定媒体并开启评测…',async()=>{
  const seedFile=$('seed-file').files[0];let seedPackage=null;
  if(seedFile){try{seedPackage=JSON.parse(await seedFile.text());}catch{throw Error('所选结果文件不是有效的 JSON');}}
  const batch=$('batch-select').value;
  const batchInfo=state.batches.find(b=>b.name===batch);
  const limit=seedPackage?Math.max(Number($('limit').value),batchInfo?.task_count||0):Number($('limit').value);
  await api('/api/start',{batch,target:Number($('target').value),limit});
  await refresh();
  if(!seedPackage){message('本机评测已启动。点击“打开志愿者界面”进入，答案会自动保存到本机结果文件。');return;}
  const seed=await api('/api/seed',{batch,package:seedPackage});
  $('seed-file').value='';
  state.resume={batch,token:seed.token};updateControls();
  message(`已从结果文件导入 ${seed.seeded} 题（作答者「${seed.name}」）${seed.skipped_removed||seed.skipped_changed?`；跳过本题库没有的 ${seed.skipped_removed} 题、内容已变的 ${seed.skipped_changed} 题`:''}。\n点击“打开志愿者界面”，输入姓名「${seed.name}」即可逐题校验（改动后点“保存修改”），并继续作答剩余 ${seed.total_tasks-seed.seeded} 题。`);
});
$('stop-eval').onclick=()=>work('正在停止服务…',async()=>{await api('/api/stop',{batch:$('batch-select').value});await refresh();message('服务已停止，已保存答案仍保留。');});
function renderFilters(){fillSelect('filter-model',[...new Set(state.tasks.map(t=>t.world_model).filter(Boolean))].sort().map(x=>[x,x]),'所有模型');fillSelect('filter-track',[...new Set(state.tasks.map(t=>t.track).filter(Boolean))].sort().map(x=>[x,x]),'所有 track');$('question-detail').classList.add('hidden');}
function renderTasks(){const search=$('search').value.toLowerCase(),model=$('filter-model').value,track=$('filter-track').value,answer=$('filter-answer').value;const tasks=state.tasks.filter(t=>(!model||t.world_model===model)&&(!track||t.track===track)&&(!search||[t.source_case_id,t.world_model,t.track,t.base_condition,t.stage].join(' ').toLowerCase().includes(search))&&(!answer||(answer==='answered'?t.annotation_count>0:!t.annotation_count)));$('question-count').textContent=`显示 ${tasks.length} / ${state.tasks.length} 题 · ${state.scope?'已合并本机及导入的答案':'输入数据预览，不代表已经发布；能否建题库以检查报告为准'}`;$('question-rows').replaceChildren(...tasks.map(t=>{const tr=el('tr');const cell=el('td',t.source_case_id);cell.append(el('small',t.track));tr.append(cell,el('td',t.world_model||'尚无视频'),el('td',`${t.stage||'—'} / ${t.method||'未声明'}`),el('td',t.status));const action=el('td'),button=el('button','查看详情');button.onclick=()=>detail(t);action.append(button);tr.append(action);return tr;}));}
for(const id of ['search','filter-model','filter-track','filter-answer'])$(id).addEventListener('input',renderTasks);
$('load-questions').onclick=()=>work('正在加载题目及最新作答…',async()=>{const name=$('question-scope').value;if(!name)return check();state.tasks=(await api(`/api/tasks?batch=${encodeURIComponent(name)}`)).tasks;state.scope=name;renderFilters();renderTasks();message(`已加载 ${name} 的完整题目与作答。`);});
function media(path,image=false){if(!path)return el('p',image?'未提供初始帧':'素材缺失','muted');const node=el(image?'img':'video');node.src=`/api/media?batch=${encodeURIComponent(state.scope)}&path=${encodeURIComponent(path)}`;if(image)node.className='frame';else{node.controls=true;node.preload='metadata';}node.onerror=()=>node.replaceWith(el('p','素材无法加载，请检查文件。','muted'));return node;}
const labels={yes:'是',no:'否',uncertain:'无法判断',valid:'有效',invalid:'损坏',insufficient:'证据不足',pass:'保持',fail:'未保持'};
const baseLabels={yes:'完成',partial:'部分完成',no:'未完成',uncertain:'无法判断'};
const reasonLabels={subject:'主体不一致',scene:'场景不一致',camera:'视角/镜头突变',other:'其他'};
function detail(task){const root=$('question-detail');root.replaceChildren();root.classList.remove('hidden');root.append(el('h2',`${task.source_case_id} · ${task.track}`));const tags=el('div');for(const text of [task.world_model||'未生成',task.stage||'无版本',`方法：${task.method||'未声明'}`,`seed：${task.generation_seed??'未记录'}`,`domain：${task.domain||'—'}`,`index：${task.index??task.case_index??'—'}`])tags.append(el('span',text,'pill'));root.append(tags);const conditions=el('div',undefined,'conditions'),image=el('div');image.append(el('h3','INITIAL FRAME'),media(task.initial_frame,true));const text=el('div');text.append(el('h3','SHARED BASE CONDITION'),el('p',task.base_condition));if(task.base_condition_zh)text.append(el('p','参考译文：'+task.base_condition_zh,'muted'));const prefs=el('div',undefined,'pref-grid');for(const side of ['a','b']){const div=el('div');div.append(el('h3',`PREFERENCE ${side.toUpperCase()}`),el('p',task[`preference_${side}`]));if(task[`preference_${side}_zh`])div.append(el('p','参考译文：'+task[`preference_${side}_zh`],'muted'));prefs.append(div);}text.append(prefs);conditions.append(image,text);root.append(conditions);const videos=el('div',undefined,'media-grid');for(const side of ['a','b']){const div=el('div');div.append(el('h3',`VIDEO ${side.toUpperCase()} · 生成目标 Preference ${side.toUpperCase()}`),media(task[`video_${side}`]));videos.append(div);}root.append(videos);root.append(el('h3',`作答详情 · ${task.annotation_count||0} 份`));if(!task.answers?.length)root.append(el('p','暂无已汇入的答案。输入数据预览不关联作答；请选择评测题库查看。','muted'));for(const answer of task.answers||[]){const box=el('div',undefined,'answer-box');box.append(el('p',`${answer.slot?answer.slot+' · ':''}${answer.annotator_name||"未填写姓名"} · 设备 ${answer.device_id.slice(0,8)} · 志愿者 ${answer.original_id.slice(0,8)} · 版本 ${answer.revision} · ${answer.submitted_at}`));box.append(el('p',`原显示映射：X = ${answer.x_is_a?'A':'B'}，Y = ${answer.x_is_a?'B':'A'}`));for(const side of ['a','b']){const a=answer.normalized[side];const why=a.retention==='fail'?`（${(a.retention_reasons||[]).map(r=>reasonLabels[r]||r).join('、')}${a.retention_note?'：'+a.retention_note:''}）`:'';const later=a.validity!=='valid'?'；其余题未作答':a.legacy?`；旧版答案（无基础任务/共同条件）；偏好 A ${labels[a.preference_a]||'—'}；偏好 B ${labels[a.preference_b]||'—'}`:`；基础任务 ${baseLabels[a.base_task]||'—'}；共同条件 ${labels[a.retention]||'—'}${why}；偏好 A ${a.base_task==='no'?'（按“否”计）':labels[a.preference_a]||'—'}；偏好 B ${a.base_task==='no'?'（按“否”计）':labels[a.preference_b]||'—'}`;box.append(el('p',`视频 ${side.toUpperCase()}：有效性 ${labels[a.validity]||'—'}${later}`));}root.append(box);}root.scrollIntoView({behavior:'smooth',block:'start'});}
$('import-results').onclick=()=>work('正在校验并合并结果文件…',async()=>{const files=[...$('result-files').files];if(!files.length)throw Error('请先选择结果 JSON 文件');$('import-log').replaceChildren();let failed=0;for(const file of files){try{if(file.size>25*1024*1024)throw Error('文件超过 25 MB，请分批导出');const result=await api('/api/import',{batch:$('result-batch').value,file_name:file.name,package:JSON.parse(await file.text())});const who=result.slot?`编号 ${result.slot}${result.annotator_name?' · '+result.annotator_name:''}`:(result.annotator_name||'未编号');$('import-log').append(el('p',`${file.name}（${who}）：新增 ${result.added}，更新 ${result.updated}，重复 ${result.unchanged}，忽略旧版 ${result.older_ignored}${result.planned?`；该编号计划 ${result.planned} 题`:''}${result.warnings.length?'\n⚠ '+result.warnings.join('；'):''}`));}catch(e){failed++;if(e instanceof SyntaxError)e=Error('不是有效的 JSON 文件');$('import-log').append(el('p',`${file.name}：未导入 — ${e.message}`));}}message(`处理完成：${files.length-failed} 个文件成功，${failed} 个未导入。下方“进度总览”已刷新。`,failed>0);await loadReport();});
async function loadReport(){
  const batch=$('result-batch').value;if(!batch)return;
  const plan=$('report-plan').value;
  const r=(await api(`/api/report?batch=${encodeURIComponent(batch)}${plan?'&plan='+plan:''}`)).report;
  const sum=r.summary;
  metrics('report-summary',[...(r.plan?[['编号已收到',`${sum.slots_received} / ${sum.slots}`],['编号已完成',`${sum.slots_complete} / ${sum.slots}`]]:[]),['达标题目',`${sum.tasks_meeting_target} / ${sum.tasks}`],['答案份数',sum.answers],['目标份数/题',sum.target]]);
  const w=$('report-warnings');w.replaceChildren();
  if(!r.warnings.length)w.append(el('p',r.slots.length&&sum.slots_complete===sum.slots&&sum.tasks_meeting_target===sum.tasks?'✓ 全部编号已完成，所有题目份数达标。':'暂无提示。','muted'));
  for(const x of r.warnings)w.append(el('p',(x.level==='error'?'✗ ':x.level==='warn'?'⚠ ':'ℹ ')+x.text,x.level==='error'?'note error':'note'));
  const rows=r.slots.map(sl=>{const tr=el('tr');tr.append(el('td',sl.slot),el('td',sl.name||'—'),el('td',`${sl.done} / ${sl.planned}`),el('td',sl.status+(sl.edits?`（修改过 ${sl.edits} 题）`:'')),el('td',fmtTime(sl.last_submitted)),el('td',sl.devices.length?sl.devices.map(d=>d.slice(0,8)).join('、'):'—'));return tr;});
  for(const o of r.others.filter(o=>!o.slot||!r.slots.some(sl=>sl.slot===o.slot&&o.plan_id===r.plan?.plan_id))){const tr=el('tr');tr.append(el('td',o.slot||'（无编号）'),el('td',o.name||'—'),el('td',`${o.answers} 份答案`),el('td',o.plan_id?`其他方案 ${o.plan_id}`:'不在分配方案内'),el('td','—'),el('td','—'));rows.push(tr);}
  $('report-slots').replaceChildren(...rows);
  $('report-under-title').textContent=`份数不足的题目（${r.coverage.under_total} 道${r.coverage.under_total>r.coverage.under.length?'，仅显示前 '+r.coverage.under.length+' 道':''}）`;
  $('report-under').replaceChildren(...r.coverage.under.map(t=>{const tr=el('tr');const c=el('td',t.source_case_id);c.append(el('small',t.track));tr.append(c,el('td',`${t.have} / ${t.need}`),el('td',t.missing_slots.length?t.missing_slots.join('、'):'—'));return tr;}));
  $('report-imports').replaceChildren(...r.imports.map(h=>{const tr=el('tr');tr.append(el('td',fmtTime(h.imported_at)),el('td',h.file_name||'—'),el('td',`${h.slot||'—'}${h.annotator_name?' · '+h.annotator_name:''}`),el('td',h.status==='ok'?`新增 ${h.added}，更新 ${h.updated}，重复 ${h.unchanged}，旧版 ${h.older_ignored}${h.warnings.length?'；⚠ '+h.warnings.join('；'):''}`:`✗ 被拒绝：${h.error}`));return tr;}));
}
$('refresh-report').onclick=()=>work('正在汇总进度…',async()=>{await loadReport();message('进度总览已更新。');});
$('report-plan').onchange=()=>work('正在汇总进度…',loadReport);
$('aggregate').onclick=()=>work('正在合并本机作答并计算共识…',async()=>{const result=await api('/api/aggregate',{batch:$('result-batch').value});metrics('aggregate-summary',[['题目',result.task_count],['有效去重答案',result.answer_count],['待复核题目',result.needs_adjudication]]);$('aggregate-rows').replaceChildren(...result.groups.map(g=>{const tr=el('tr');for(const value of [`${g.model} / ${g.track}`,`${g.variant} / ${g.method||'未声明'}`,g.tasks,g.answers,g.scored,g.strict_sbps_rate===null?'—':`${(100*g.strict_sbps_rate).toFixed(1)}% (${g.passed}/${g.scored})`,g.needs_adjudication])tr.append(el('td',value));return tr;}));message('汇总已更新。可下载结果，或在“题目与作答”查看每题的独立答案。');});
refresh().then(()=>{if(state.job?.status==='running'){work('继续显示创建进度…',()=>followJob(state.job));return;}if(state.job)showJob(state.job);if(state.batches.length){$('prepare-data').open=false;message('已找到本地题库，可直接从第 2 步启动本机评测。');}}).catch(e=>message(e.message,true));
window.addEventListener('focus',()=>{if(!state.busy)refresh().catch(e=>message(e.message,true));});
