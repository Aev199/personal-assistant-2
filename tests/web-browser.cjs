/* Local browser acceptance harness. Synthetic API, no user data or server. */
const http=require('node:http');
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const root=path.resolve(__dirname,'../bot/web');
let connected=false, failCapture=false, captureKeys=[], committed=new Map();
const today=new Date().toISOString().slice(0,10);
let tasks=[
 {id:1,title:'Проверить результаты расчёта осадок',project:'Лимонарий',kind:'task',status:'todo',deadline:today+'T14:00:00+03:00',updated_at:'2026-10-17T08:00:00+00:00',today_plan:false},
 {id:2,title:'Ответить на замечания по КЖ',project:'Жилой комплекс',kind:'task',status:'todo',deadline:today+'T17:00:00+03:00',updated_at:'2026-10-17T08:00:00+00:00',today_plan:false},
 {id:3,title:'Уточнить исходные нагрузки',project:'INBOX',kind:'task',status:'todo',deadline:null,updated_at:'2026-10-17T08:00:00+00:00',today_plan:false},
 {id:4,title:'Записаться на сервис автомобиля',project:'',kind:'personal',status:'todo',deadline:null,updated_at:'2026-10-17T08:00:00+00:00',today_plan:false},
 {id:5,title:'Проверить экспорт в PLAXIS',project:'SoilStiffness',kind:'task',status:'todo',deadline:null,updated_at:'2026-10-17T08:00:00+00:00',today_plan:false}
];
let ideas=[{id:1,text:'Контекстный поиск по рабочему архиву'}];
let conflict=false,failTasksOnce=false,failInboxOnce=false,confirmed=false;
const server=http.createServer(async(req,res)=>{
 const u=new URL(req.url,'http://localhost');let body='';for await(const c of req)body+=c;
 const data=body?JSON.parse(body):{};
 function json(value,status=200){res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(value));}
 if(u.pathname.startsWith('/api/')){
  if(u.pathname==='/api/v1/session'){
   if(req.method==='POST'){connected=true;return json({ok:true,csrf_token:'test-csrf'},201);}
   if(req.method==='DELETE'){connected=false;return json({ok:true});}
   return json(connected?{ok:true,csrf_token:'test-csrf'}:{ok:false},connected?200:401);
  }
  if(!connected)return json({ok:false,error:'unauthorized'},401);
  if(u.pathname==='/api/v1/today')return json({ok:true,date:today,timezone:'Europe/Moscow',tasks:tasks.filter(t=>t.status!=='done').slice(0,3),events:[{id:'m1',title:'Совещание с проектировщиками',start:today+'T12:00:00+03:00',end:today+'T13:00:00+03:00',kind:'work'}],reminders:[],calendar_unavailable:true,calendar_pending:false});
  if(u.pathname==='/api/v1/projects')return json({ok:true,projects:[{code:'Лимонарий',name:'Лимонарий'},{code:'Жилой комплекс',name:'Жилой комплекс'},{code:'SoilStiffness',name:'SoilStiffness'},{code:'INBOX',name:'Входящие'}]});
  if(u.pathname==='/api/v1/tasks'){
   if(failTasksOnce||(failInboxOnce&&u.searchParams.get('inbox')==='true')){failTasksOnce=false;failInboxOnce=false;return json({ok:false,error:'db_unavailable'},503);}
   const scope=u.searchParams.get('scope');let list=tasks.filter(t=>t.status!=='done');
   if(scope==='personal')list=list.filter(t=>t.kind==='personal');
   if(scope==='work')list=list.filter(t=>t.kind!=='personal');
   if(u.searchParams.get('inbox')==='true')list=list.filter(t=>t.project==='INBOX'&&t.kind==='task');
   const cursor=Number(u.searchParams.get('cursor')||0);list=list.filter(t=>t.id>cursor);
   return json({ok:true,timezone:'Europe/Moscow',tasks:list.slice(0,2),next_cursor:list.length>2?list[1].id:null});
  }
  if(u.pathname==='/api/v1/ideas')return json({ok:true,ideas,total:ideas.length});
  if(u.pathname==='/api/v1/intake'){
   captureKeys.push(data.client_id);
   if(data.text.startsWith('встреча')&&!committed.has(data.client_id)){committed.set(data.client_id,null);return json({ok:true,status:'needs_confirmation',saved:[],needs_input:[],pending:[{pending_action_id:8,title:'Встреча с подрядчиком',kind:'event'}]});}
   if(data.text.startsWith('напомни')&&!data.context)return json({ok:true,status:'needs_input',saved:[],pending:[],context:data.text,needs_input:[{prompt:'Когда напомнить?'}]});
   if(data.context)return json({ok:true,status:'saved',saved:[{status:'saved',kind:'reminder',title:'Позвонить'}],needs_input:[],pending:[]});
   if(!committed.has(data.client_id)){
    const t={id:tasks.length+1,title:data.text,project:'INBOX',kind:'task',status:'todo',deadline:null,updated_at:new Date().toISOString()};tasks.push(t);committed.set(data.client_id,t);
   }
   if(failCapture){failCapture=false;return json({ok:false,error:'db_unavailable'},503);}
   return json({ok:true,status:'saved',inbox_fallback:true,saved:[{status:'saved',kind:'task',title:data.text}],needs_input:[],pending:[]});
  }
  if(u.pathname==='/api/v1/intake/8/confirm'){confirmed=true;return json({ok:true,status:'saved'});}
  const m=u.pathname.match(/\/tasks\/(\d+)(?:\/(\w+))?$/);
  if(m){const t=tasks.find(t=>t.id===Number(m[1]));
   if(req.method==='PATCH'){if(conflict){conflict=false;return json({ok:false,error:'task_conflict'},409);}Object.assign(t,data,{updated_at:new Date().toISOString()});}
   if(m[2]==='done')t.status='done';
   if(m[2]==='plan')t.today_plan=true;
   if(m[2]==='unplan')t.today_plan=false;
   if(m[2]==='idea'){ideas.push({id:2,text:t.title});t.status='done';}
   return json({ok:true,updated_at:t.updated_at});
  }
  return json({ok:true});
 }
 const name=u.pathname==='/app/'?'index.html':u.pathname.replace('/app/','');
 if(!['index.html','app.css','app.js'].includes(name)){res.writeHead(404);return res.end();}
 res.writeHead(200,{'Content-Type':name.endsWith('html')?'text/html':name.endsWith('css')?'text/css':'text/javascript'});res.end(fs.readFileSync(path.join(root,name)));
});
(async()=>{
 await new Promise(r=>server.listen(0,'127.0.0.1',r));
 const browser=await chromium.launch({headless:true,...(process.env.CHROMIUM_EXECUTABLE_PATH?{executablePath:process.env.CHROMIUM_EXECUTABLE_PATH,args:['--no-sandbox','--disable-dev-shm-usage','--disable-gpu','--disable-software-rasterizer']}: {})});
 try{
  const page=await browser.newPage({viewport:{width:1440,height:960}});const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(`http://127.0.0.1:${server.address().port}/app/`);
  await page.getByLabel('Ключ доступа').fill('synthetic-test-key');await page.getByRole('button',{name:'Подключиться',exact:true}).click();
  await page.getByRole('heading',{name:'Сегодня',exact:true}).waitFor();
  await page.getByText('Календарь временно недоступен',{exact:false}).waitFor();
  if(process.env.SCREENSHOT_DIR){fs.mkdirSync(process.env.SCREENSHOT_DIR,{recursive:true});await page.evaluate(()=>{const el=document.createElement('div');el.textContent='Тестовые данные · проверка интерфейса';el.className='test-banner';document.body.prepend(el)});await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'assistant-web-desktop.png'),fullPage:true});}
  await page.getByRole('button',{name:'Задачи',exact:true}).first().click();
  await page.getByRole('button',{name:'Загрузить ещё',exact:true}).click();
  await page.getByText('Проверить экспорт в PLAXIS',{exact:true}).waitFor();
  await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));
  await page.waitForTimeout(150);
  assert.equal(await page.getByText('Проверить экспорт в PLAXIS',{exact:true}).count(),1,'refresh must preserve loaded pages');
  failTasksOnce=true;await page.getByRole('button',{name:'Задачи',exact:true}).first().click();await page.waitForTimeout(150);
  assert.equal(await page.getByText('Проверить экспорт в PLAXIS',{exact:true}).count(),1,'outage must not hide loaded tasks');
  failInboxOnce=true;await page.getByRole('button',{name:'Сегодня',exact:true}).first().click();await page.waitForTimeout(150);
  assert.equal(await page.getByText(tasks[0].title,{exact:true}).count(),1,'Inbox failure must not hide Today');
  await page.getByRole('button',{name:'Задачи',exact:true}).first().click();await page.getByText(tasks[0].title,{exact:true}).waitFor();
  await page.getByRole('button',{name:'Добавить на сегодня: '+tasks[0].title,exact:true}).click();assert(tasks[0].today_plan);
  await page.getByRole('button',{name:'Сегодня',exact:true}).first().click();
  await page.getByRole('heading',{name:'На сегодня',exact:true}).waitFor();
  await page.getByRole('button',{name:'Убрать из «На сегодня»: '+tasks[0].title,exact:true}).waitFor();
  await page.getByRole('button',{name:'Задачи',exact:true}).first().click();await page.getByText(tasks[0].title,{exact:true}).waitFor();
  await page.getByText(tasks[0].title,{exact:true}).click();
  await page.getByLabel('Название').fill('Изменённая задача');conflict=true;
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();await page.getByText('Задача изменилась на другом устройстве',{exact:false}).waitFor();
  await page.keyboard.press('Escape');
  failCapture=true;
  const capture=page.getByLabel('Добавить задачу или мысль');await capture.fill('Новая запись после обрыва');await capture.press('Enter');
  await page.getByRole('button',{name:'Повторить сохранение',exact:true}).waitFor();assert.equal(await capture.inputValue(),'Новая запись после обрыва');
  await page.getByRole('button',{name:'Повторить сохранение',exact:true}).click();
  await page.getByText('Записано во Входящие',{exact:true}).waitFor();
  assert.equal(await page.getByRole('heading',{name:'Задачи',exact:true}).count(),1,'capture must not move the user to another page');
  await page.getByRole('button',{name:'Входящие',exact:true}).first().click();
  await page.getByText('Новая запись после обрыва',{exact:true}).waitFor();assert.equal(captureKeys[0],captureKeys[1]);assert.equal(tasks.filter(t=>t.title==='Новая запись после обрыва').length,1);
  await page.getByRole('button',{name:'В идеи',exact:true}).first().click();
  await capture.fill('напомни позвонить');await capture.press('Enter');
  await page.getByText('Когда напомнить?',{exact:false}).waitFor();
  await capture.fill('завтра в 10');await capture.press('Enter');
  await page.getByText('Напоминание: Позвонить',{exact:true}).waitFor();
  await capture.fill('встреча с подрядчиком в пятницу');await capture.press('Enter');
  await page.getByRole('button',{name:'Добавить',exact:true}).click();
  await page.getByText('Добавлено в календарь',{exact:true}).waitFor();assert(confirmed);
  await page.getByRole('button',{name:'Идеи',exact:true}).first().click();await page.getByText('Контекстный поиск по рабочему архиву',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('button',{name:'Задачи',exact:true}).last().click();await page.getByRole('button',{name:'Личное',exact:true}).click();
  await page.getByText('Записаться на сервис автомобиля',{exact:true}).waitFor();
  assert(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  if(process.env.SCREENSHOT_DIR)await page.screenshot({path:path.join(process.env.SCREENSHOT_DIR,'assistant-web-mobile.png'),fullPage:true});
  assert.deepEqual(errors,[]);console.log('Browser acceptance passed: login, calendar failure, pagination, today plan, conflict, capture retry, clarification, calendar confirmation, ideas, mobile.');
 }finally{await browser.close();server.close();}
})().catch(e=>{console.error(e);server.close();process.exitCode=1});
