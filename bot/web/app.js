'use strict';
const $ = id => document.getElementById(id);
const html = value => String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const paths={today:'<rect x="3" y="4" width="18" height="17" rx="2"/><path d="M8 2v4M16 2v4M3 9h18M8 14h8"/>',tasks:'<path d="m3 7 2 2 4-4M12 7h9M3 17l2 2 4-4M12 17h9"/>',inbox:'<path d="M4 4h16l2 10v6H2v-6L4 4Z"/><path d="M2 14h6l2 3h4l2-3h6"/>',ideas:'<path d="M9 18h6M10 22h4M8 15a7 7 0 1 1 8 0l-1 2H9z"/>'};
const icon=id=>`<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[id]}</svg>`;
const names={today:'Сегодня',tasks:'Задачи',inbox:'Входящие',ideas:'Идеи'};
const state={page:'today',scope:'work',tasks:[],cursor:null,today:null,inbox:[],ideas:[],projects:[],tz:'Europe/Moscow',csrf:'',generation:0,controller:null,connected:false,busy:false};
let detailTask=null,toastTimer;
const errors={db_unavailable:'Сервер временно недоступен. Запись остаётся в поле ввода.',login_rate_limited:'Слишком много попыток. Попробуйте через минуту.',unauthorized:'Ключ не подошёл. Проверьте его и повторите.',web_not_configured:'Вход через браузер ещё не настроен на сервере.',csrf_failed:'Сессия требует обновления. Выйдите и подключитесь снова.',task_conflict:'Задача изменилась на другом устройстве. Обновите карточку перед сохранением.',task_not_active:'Эта задача уже завершена или отложена.',not_an_inbox_task:'Эта запись уже разобрана. Обновите список.',capture_conflict:'Эта попытка уже сохранена с другим текстом. Обновите страницу.',network:'Нет связи с сервером. Попробуйте ещё раз.'};
function message(e){return errors[e.code] || (e.status===401?'Сессия завершилась. Подключитесь снова.':'Не удалось выполнить действие. Попробуйте ещё раз.');}
function notify(text){$('toast').textContent=text;$('toast').classList.add('show');clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('toast').classList.remove('show'),2600);}
function storageGet(key){try{return sessionStorage.getItem(key);}catch{return null;}}
function storageSet(key,value){try{if(value===null)sessionStorage.removeItem(key);else sessionStorage.setItem(key,value);}catch{/* Draft remains in the visible input when browser storage is unavailable. */}}
function readPending(){try{return JSON.parse(storageGet('assistant.pending')||'null');}catch{return null;}}
function draft(){storageSet('assistant.draft',$('capture').value);}
function showLogin(){state.connected=false;state.csrf='';state.generation++;state.controller?.abort();$('shell').hidden=true;$('login').hidden=false;$('task-dialog').close();}
async function api(path,{method='GET',body,signal}={}){
 let r;
 try{r=await fetch('/api/v1'+path,{method,credentials:'same-origin',cache:'no-store',signal,headers:{...(body?{'Content-Type':'application/json'}:{}),...(method!=='GET'?{'X-CSRF-Token':state.csrf}:{})},...(body?{body:JSON.stringify(body)}:{})});}
 catch(e){if(e.name==='AbortError')throw e;throw {code:'network'};}
 let data;try{data=await r.json();}catch{throw {code:'network',status:r.status};}
 if(!r.ok||data.ok===false){if(r.status===401&&path!=='/session')showLogin();throw {code:data.error,status:r.status};}
 return data;
}
function dateKey(value){if(!value)return null;return new Intl.DateTimeFormat('en-CA',{timeZone:state.tz,year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date(value));}
function dateLabel(value){if(!value)return 'Без даты';const key=dateKey(value),now=dateKey(new Date());if(key===now)return 'Сегодня';return new Intl.DateTimeFormat('ru-RU',{timeZone:state.tz,day:'numeric',month:'short'}).format(new Date(value));}
function time(value){return new Intl.DateTimeFormat('ru-RU',{timeZone:state.tz,hour:'2-digit',minute:'2-digit'}).format(new Date(value));}
function nav(){
 $('desktop-nav').innerHTML=['today','inbox','tasks','ideas'].map(id=>`<button class="nav-button ${state.page===id?'active':''}" data-page="${id}" ${state.page===id?'aria-current="page"':''}>${icon(id)}${names[id]}</button>`).join('');
 $('mobile-nav').innerHTML=['today','tasks','ideas'].map(id=>`<button class="${state.page===id?'active':''}" data-page="${id}" ${state.page===id?'aria-current="page"':''}>${icon(id)}${id==='today'?'Главная':names[id]}</button>`).join('');$('breadcrumb').textContent=names[state.page];
}
function heading(subtitle,withSwitch=true){return `<div class="heading"><div><h1>${names[state.page]}</h1><p class="subtitle">${html(subtitle)}</p></div>${withSwitch?`<div class="switch" aria-label="Область задач"><button data-scope="work" class="${state.scope==='work'?'active':''}" aria-pressed="${state.scope==='work'}">Работа</button><button data-scope="personal" class="${state.scope==='personal'?'active':''}" aria-pressed="${state.scope==='personal'}">Личное</button></div>`:''}</div>`;}
function rows(tasks){return tasks.length?`<div class="list">${tasks.map(t=>`<div class="task"><button class="check" data-done="${t.id}" aria-label="Завершить: ${html(t.title)}" title="Отметить готовой"></button><div class="task-body"><button class="task-open" data-open="${t.id}">${html(t.title)}</button><div class="task-meta">${t.project&&t.project!=='INBOX'?`<span>${html(t.project)}</span>`:''}<span class="${t.deadline&&dateKey(t.deadline)<dateKey(new Date())?'overdue':''}">${t.deadline&&dateKey(t.deadline)<dateKey(new Date())?'Срок: ':''}${dateLabel(t.deadline)}</span></div></div><button class="row-action ${t.today_plan?'current':''}" data-plan="${t.id}" data-planned="${Boolean(t.today_plan)}" aria-label="${t.today_plan?'Убрать из «На сегодня»: ':'Добавить на сегодня: '}${html(t.title)}">${t.today_plan?'Сегодня ✓':'На сегодня'}</button></div>`).join('')}</div>`:'<div class="empty">Задач пока нет.</div>';}
function todayView(){
 // «На сегодня» is one shared list for work and personal tasks.
 const data=state.today||{},tasks=data.tasks||[],plan=tasks.filter(t=>t.today_plan),other=tasks.filter(t=>!t.today_plan);
 const events=data.events||[];
 const schedule=events.length?`<div class="agenda">${events.map(e=>`<div class="meeting"><time>${time(e.start)}</time><div class="meeting-body ${e.kind==='personal'?'personal':''}"><div class="meeting-title">${html(e.title)}</div><div class="meeting-meta">до ${time(e.end)}</div></div></div>`).join('')}</div>`:(!data.calendar_unavailable&&!data.calendar_pending?'<div class="empty">Предстоящих встреч нет.</div>':'');
 const day=new Intl.DateTimeFormat('ru-RU',{timeZone:state.tz,weekday:'long',day:'numeric',month:'long'}).format(new Date());
 return heading(day,false)+`<div class="columns"><div><section class="section"><div class="section-head"><h2>На сегодня</h2></div>${plan.length?rows(plan):'<div class="empty">Пока ничего не выбрано. Добавьте 2–3 дела из списка ниже.</div>'}</section><section class="section"><div class="section-head"><h2>Можно добавить</h2><button class="text-button" data-page="tasks">Все задачи →</button></div>${rows(other)}</section></div><aside class="right-column"><section><div class="section-head"><h2>Расписание</h2></div>${schedule}${data.calendar_unavailable?'<p class="calendar-note">Календарь временно недоступен. Расписание может быть неполным.</p>':data.calendar_pending?'<p class="calendar-note">Календарь обновляется…</p>':''}${state.calendarUpdated?`<p class="subtitle">Обновлён в ${time(state.calendarUpdated)}</p>`:''}</section>${(data.reminders||[]).length?`<section class="mini-inbox"><div class="section-head"><h2>Напоминания</h2></div>${data.reminders.map(r=>`<div class="mini-row">${html(r.text)}<div class="subtitle">${r.at?time(r.at):''}</div></div>`).join('')}</section>`:''}<section class="mini-inbox"><div class="section-head"><h2>Входящие</h2><button class="text-button" data-page="inbox">Все записи →</button></div><div class="list">${state.inbox.slice(0,3).map(t=>`<div class="mini-row"><button data-open="${t.id}">${html(t.title)}</button></div>`).join('')||(state.inboxUnavailable?'<div class="empty">Входящие пока не загружены.</div>':'<div class="empty">Нет новых записей.</div>')}</div>${state.inboxUnavailable?'<p class="calendar-note">Не удалось обновить Входящие. Показаны последние загруженные записи.</p>':''}</section></aside></div>`;
}
function render(){nav();state.renderedKey=state.page+':'+state.scope;const content=$('content');
 if(state.page==='today')content.innerHTML=todayView();
 if(state.page==='tasks')content.innerHTML=heading('Рабочие и личные дела')+`<div class="task-area" id="swipe-area"><div class="list-caption">${state.tasks.length}${state.cursor!==null?' и ещё': ''} записей</div>${rows(state.tasks)}${state.cursor!==null?'<button class="load-more" data-more="tasks">Загрузить ещё</button>':''}</div>`;
 if(state.page==='inbox')content.innerHTML=heading('Записи без объекта. Можно уточнить задачу или перенести её в идеи.',false)+`<div class="task-area"><div class="list">${state.tasks.map(t=>`<div class="inbox-row"><span class="entry-title">${html(t.title)}</span><div class="entry-actions"><button data-open="${t.id}">Изменить</button><button data-idea="${t.id}">В идеи</button></div></div>`).join('')||'<div class="empty">Нет неразобранных записей.</div>'}</div>${state.cursor!==null?'<button class="load-more" data-more="tasks">Загрузить ещё</button>':''}</div>`;
 if(state.page==='ideas')content.innerHTML=heading('Отдельно от обязательных дел',false)+`<div class="task-area"><div class="list">${state.ideas.map(i=>`<div class="idea-row"><span class="entry-title">${html(i.text)}</span><div class="entry-actions"><button data-promote="${i.id}">В задачи</button><button data-archive="${i.id}">В архив</button></div></div>`).join('')||'<div class="empty">Пока нет идей.</div>'}</div></div>`;
}
async function loadPage({more=false,quiet=false}={}){
 if(!state.connected)return;
 const generation=++state.generation,page=state.page,scope=state.scope;
 state.controller?.abort();state.controller=new AbortController();const signal=state.controller.signal;
 if(!quiet&&!more&&state.renderedKey!==page+':'+scope){nav();$('content').innerHTML='<p class="loading"><span class="loading-dot"></span>Загрузка…</p>';}
 try{
  let result;
  if(page==='today'){
   const [day,inbox]=await Promise.allSettled([api('/today',{signal}),api('/tasks?scope=all&inbox=true&limit=3',{signal})]);
   if(day.status==='rejected')throw day.reason;
   result=[day.value,inbox.status==='fulfilled'?inbox.value:{tasks:state.inbox}];
   result.inboxUnavailable=inbox.status!=='fulfilled';
  }
  else if(page==='ideas')result=await api('/ideas?limit=200',{signal});
  else {
   const base=`/tasks?scope=${page==='inbox'?'all':scope}&inbox=${page==='inbox'}&limit=100`;
   result=await api(base+(more&&state.cursor!==null?'&cursor='+state.cursor:''),{signal});
   if(quiet&&!more){
    const wanted=state.tasks.length;
    while(result.tasks.length<wanted&&result.next_cursor!==null&&result.next_cursor!==undefined){
     const previous=result.next_cursor, next=await api(base+'&cursor='+previous,{signal});
     result.tasks.push(...next.tasks);result.next_cursor=next.next_cursor;
     if(!next.tasks.length||next.next_cursor===previous)break;
    }
   }
  }
  if(generation!==state.generation||page!==state.page||scope!==state.scope)return;
  if(page==='today'){state.today=result[0];state.inboxUnavailable=result.inboxUnavailable;state.tz=result[0].timezone||state.tz;state.inbox=result[1].tasks;if(!state.today.calendar_unavailable&&!state.today.calendar_pending)state.calendarUpdated=new Date().toISOString();}
  else if(page==='ideas')state.ideas=result.ideas||[];
  else{state.tasks=more?[...state.tasks,...result.tasks.filter(t=>!state.tasks.some(x=>x.id===t.id))]:result.tasks;state.cursor=result.next_cursor??null;state.tz=result.timezone||state.tz;}
  $('page-error').hidden=true;$('connection-state').textContent='Подключено к серверу';render();
 }catch(e){if(e.name==='AbortError'||generation!==state.generation)return;$('page-error').hidden=false;$('page-error').textContent=message(e);$('connection-state').textContent='Связь недоступна';if(!quiet&&!more&&state.renderedKey!==page+':'+scope)$('content').innerHTML='<button class="secondary" data-refresh="1">Повторить загрузку</button>';}
}
async function connect(){state.connected=true;$('login').hidden=true;$('shell').hidden=false;$('capture').value=storageGet('assistant.draft')||'';const pending=readPending();if(pending)$('capture-status').innerHTML='Есть неподтверждённая запись. <button type="button" data-retry="1">Повторить сохранение</button>';loadPage();try{const r=await api('/projects');state.projects=r.projects||[];}catch{/* Tasks remain usable; project choices can be retried on opening details. */}}
$('connect-form').addEventListener('submit',async e=>{e.preventDefault();const b=e.submitter;b.disabled=true;$('login-error').textContent='';try{const r=await api('/session',{method:'POST',body:{token:$('access-key').value}});state.csrf=r.csrf_token;$('access-key').value='';await connect();}catch(err){$('login-error').textContent=message(err);}finally{b.disabled=false;}});
$('logout').addEventListener('click',async()=>{try{await api('/session',{method:'DELETE'});showLogin();}catch(e){$('page-error').hidden=false;$('page-error').textContent=message(e);}});
$('capture').addEventListener('input',draft);
async function saveCapture(retry=false){
 if(state.captureBusy)return;
 const old=readPending();let pending=old;
 if(!retry){const text=$('capture').value.trim();if(!text)return;if(old&&old.text!==text){$('capture-status').innerHTML='Сначала подтвердите предыдущую запись. Новый текст сохранён в поле. <button type="button" data-retry="1">Повторить сохранение</button>';return;}pending=old||{text,client_request_id:crypto.randomUUID()};storageSet('assistant.pending',JSON.stringify(pending));}
 if(!pending)return;
 state.captureBusy=true;$('capture-status').textContent='Сохраняется…';
 try{await api('/capture',{method:'POST',body:pending});if($('capture').value.trim()===pending.text){$('capture').value='';draft();}storageSet('assistant.pending',null);$('capture-status').textContent='Сохранено во Входящие';state.page='inbox';await loadPage();}
 catch(e){$('capture-status').innerHTML=html(message(e))+' <button type="button" data-retry="1">Повторить сохранение</button>';}
 finally{state.captureBusy=false;}
}
$('capture-form').addEventListener('submit',e=>{e.preventDefault();saveCapture();});
async function mutate(path,success){if(state.busy)return;state.busy=true;try{await api(path,{method:'POST',body:{}});$('task-dialog').close();notify(success);await loadPage({quiet:true});}catch(e){notify(message(e));}finally{state.busy=false;}}
function localDateInput(value){if(!value)return '';const date=dateKey(value);return date+'T'+time(value);}
async function openTask(id){
 try{const r=await api(`/tasks?scope=all&cursor=${id-1}&limit=1`);const t=r.tasks[0];if(!t||t.id!==id){notify('Задача уже завершена или изменена.');loadPage({quiet:true});return;}state.tz=r.timezone||state.tz;detailTask=t;
  if(!state.projects.length){try{state.projects=(await api('/projects')).projects||[];}catch{/* Project error gets an explicit retry below. */}}
  const dialog=$('task-dialog');dialog.innerHTML=`<div class="detail-top"><span>Задача</span><button class="close" data-close="1" aria-label="Закрыть">×</button></div><h2 id="detail-title">${html(t.title)}</h2><form id="edit-form" class="edit-form"><label class="field">Название<input name="title" aria-label="Название" value="${html(t.title)}" required maxlength="1000"></label>${t.kind!=='personal'?`<label class="field">Объект<select name="project" aria-label="Объект">${[...state.projects,...(!state.projects.some(p=>p.code===t.project)?[{code:t.project,name:t.project}]:[])].map(p=>`<option value="${html(p.code)}" ${p.code===t.project?'selected':''}>${html(p.code==='INBOX'?'Без объекта':p.code)}</option>`).join('')}</select></label>${!state.projects.length?'<p class="error">Не удалось загрузить объекты. <button type="button" class="text-button" data-reopen="1">Повторить</button></p>':''}`:''}<label class="field">Когда <span>Часовой пояс: ${html(state.tz)}</span><input type="datetime-local" name="deadline" aria-label="Когда" value="${localDateInput(t.deadline)}"></label><div><button type="submit" class="primary">Сохранить</button></div></form><div id="detail-error" class="error detail-error" role="alert"></div><div class="detail-actions"><button class="secondary" data-done="${t.id}">Отметить готовой</button><button class="secondary" data-plan="${t.id}" data-planned="${Boolean(t.today_plan)}">${t.today_plan?'Убрать из «На сегодня»':'Добавить на сегодня'}</button></div>`;
  if(!dialog.open)dialog.showModal();
  $('edit-form').addEventListener('submit',saveEdit);
 }catch(e){notify(message(e));}
}
async function saveEdit(e){e.preventDefault();if(!detailTask)return;const form=e.target,b=form.querySelector('[type=submit]');b.disabled=true;const title=form.elements.title.value.trim();const body={title,deadline:form.elements.deadline.value||null,expected_updated_at:detailTask.updated_at};if(form.elements.project&&form.elements.project.value!==detailTask.project)body.project_code=form.elements.project.value;
 try{await api(`/tasks/${detailTask.id}`,{method:'PATCH',body});$('task-dialog').close();notify('Изменения сохранены');await loadPage({quiet:true});}catch(err){$('detail-error').textContent=message(err);if(err.code==='task_conflict')$('detail-error').innerHTML=html(message(err))+'<button type="button" class="text-button" data-reopen="1">Обновить карточку</button>';}finally{b.disabled=false;}
}
document.addEventListener('click',e=>{const b=e.target.closest('button');if(!b)return;const d=b.dataset;
 if(d.page){state.page=d.page;$('task-dialog').close();loadPage();}
 if(d.scope&&d.scope!==state.scope){state.scope=d.scope;loadPage();}
 if(d.more)loadPage({more:true});if(d.refresh)loadPage();if(d.retry)saveCapture(true);
 if(d.open)openTask(Number(d.open));if(d.close)$('task-dialog').close();if(d.reopen&&detailTask)openTask(detailTask.id);
 if(d.done)mutate(`/tasks/${d.done}/done`,'Задача завершена');
 if(d.plan)mutate(`/tasks/${d.plan}/${d.planned==='true'?'unplan':'plan'}`,d.planned==='true'?'Убрано из «На сегодня»':'Добавлено на сегодня');
 if(d.idea)mutate(`/tasks/${d.idea}/idea`,'Перенесено в идеи');
 if(d.promote)mutate(`/ideas/${d.promote}/promote`,'Добавлено в задачи');if(d.archive)mutate(`/ideas/${d.archive}/archive`,'Идея в архиве');
});
$('task-dialog').addEventListener('click',e=>{if(e.target===$('task-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close();}});
let startTouch=null;document.addEventListener('touchstart',e=>{startTouch=e.target.closest('#swipe-area')&&!e.target.closest('button,input,select')?{x:e.touches[0].clientX,y:e.touches[0].clientY}:null;},{passive:true});document.addEventListener('touchend',e=>{if(!startTouch)return;const dx=e.changedTouches[0].clientX-startTouch.x,dy=e.changedTouches[0].clientY-startTouch.y;startTouch=null;if(Math.abs(dx)>70&&Math.abs(dx)>Math.abs(dy)*2){state.scope=dx<0?'personal':'work';loadPage();}},{passive:true});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&state.connected)loadPage({quiet:true});});setInterval(()=>{if(!document.hidden&&state.connected&&!state.busy&&!$('task-dialog').open&&!state.captureBusy)loadPage({quiet:true});},30000);
document.addEventListener('keydown',e=>{if(e.key==='/'&&!['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName)&&state.connected){e.preventDefault();$('capture').focus();}});
(async()=>{try{const r=await api('/session');state.csrf=r.csrf_token;await connect();}catch{showLogin();}})();
