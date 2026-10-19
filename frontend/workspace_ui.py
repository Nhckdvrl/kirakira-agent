"""Project and directory controls use the same validated application methods as RPC."""

WORKSPACE_HTML = r'''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>项目与目录 · Kirakira</title>
<style>
body{font:16px/1.6 system-ui,sans-serif;max-width:1000px;margin:32px auto;padding:0 18px;color:#17211f;background:#f4f6f7}
section,article{background:white;border:1px solid #dce3e1;border-radius:10px;padding:18px;margin:16px 0}
button,input,select{font:inherit;padding:8px;margin:4px;max-width:100%;box-sizing:border-box}input{width:min(96%,600px)}
button,a{cursor:pointer}code,pre{white-space:pre-wrap;overflow-wrap:anywhere}#status{min-height:1.6em}label{display:block}
</style><a href="/">← 聊天</a><h1>项目与会话工作目录</h1>
<p>目录位于运行 Kirakira 的机器上。项目默认目录绑定后保持固定；每个新会话继承一次，之后可独立切换。</p>
<p id="status" role="status" aria-live="polite"></p>
<section><h2>创建项目</h2><form id="create"><label>名称<input id="name" required maxlength="80"></label>
<label>默认目录（可留空，已有目录的绝对路径）<input id="path"></label><button>创建</button></form></section>
<section><h2>项目</h2><button id="refresh">刷新</button><div id="projects"></div></section>
<section><h2>会话目录</h2><label>会话<select id="sessions"></select></label><button id="inspect">查看当前目录</button>
<pre id="directory"></pre><label>切换到已有绝对目录<input id="target"></label><button id="switch">切换当前会话目录</button>
<p>切换等待当前轮次结束，不移动已经运行的进程，也不更改项目默认目录。</p></section>
<section><h2>浏览服务端目录</h2><label>已有绝对路径<input id="browse-path" placeholder="例如 /home/user/projects"></label>
<button id="browse">浏览</button><div id="entries"></div><button id="more" hidden>下一页</button></section>
<script>
const $=s=>document.querySelector(s);let info=null;let pendingCreate=null;let pendingSwitch=null;let after=null;
async function api(method,params={}){const r=await fetch('/api/workspace',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({method,params})});const d=await r.json();if(!r.ok||d.error)throw Error(d.error||r.statusText);return d.result}
function button(text,fn){const b=document.createElement('button');b.textContent=text;b.onclick=()=>action(fn);return b}
async function action(fn){$('#status').textContent='处理中…';try{await fn();$('#status').textContent='已完成'}catch(e){$('#status').textContent=e.message}}
async function projects(){const d=await api('project/list');$('#projects').replaceChildren();for(const p of d.items){const a=document.createElement('article');const title=document.createElement('h3');title.textContent=p.name+(p.archived?'（已归档）':'');a.append(title);const path=document.createElement('code');path.textContent=p.directory||'未绑定目录';a.append(path);if(!p.archived){const chat=document.createElement('a');chat.textContent=' 打开项目聊天 ';chat.href='/?project_id='+encodeURIComponent(p.id);a.append(chat)}
a.append(button('改名',async()=>{const name=prompt('项目新名称',p.name);if(name===null)return;await api('project/update',{projectId:p.id,name,expectedRevision:p.revision});await projects()}));
a.append(button(p.archived?'取消归档':'归档',async()=>{await api('project/update',{projectId:p.id,archived:!p.archived,expectedRevision:p.revision});await projects()}));
if(!p.directory)a.append(button('绑定默认目录',async()=>{const path=prompt('已有目录的绝对路径。绑定后不可更改。');if(path===null)return;await api('project/bindDirectory',{projectId:p.id,path});await projects()}));$('#projects').append(a)}}
async function sessions(){const r=await fetch('/api/sessions');const d=await r.json();$('#sessions').replaceChildren();for(const s of d.sessions){const option=document.createElement('option');option.value=s.key;option.textContent=s.key;$('#sessions').append(option)}}
async function inspect(){const key=$('#sessions').value;if(!key)throw Error('请选择已有会话');info=await api('directory/current',{threadId:key});$('#directory').textContent=(info.path||'未指定目录')+'\n状态：'+info.status+'\n仓库规则：'+(info.agents.sources.join('\n')||'无')+(info.agents.error?'\n'+info.agents.error:'')}
$('#create').onsubmit=e=>{e.preventDefault();action(async()=>{const args={name:$('#name').value,directory:$('#path').value||null};if(!pendingCreate||pendingCreate.name!==args.name||pendingCreate.directory!==args.directory)pendingCreate={projectId:'p_'+crypto.randomUUID().replaceAll('-',''),...args};await api('project/create',pendingCreate);pendingCreate=null;$('#create').reset();await projects()})};
$('#refresh').onclick=()=>action(async()=>{await projects();await sessions()});$('#inspect').onclick=()=>action(inspect);
$('#switch').onclick=()=>action(async()=>{await inspect();const args={threadId:$('#sessions').value,path:$('#target').value,expectedRevision:info.revision};if(!pendingSwitch||pendingSwitch.path!==args.path||pendingSwitch.threadId!==args.threadId)pendingSwitch={requestId:crypto.randomUUID(),...args};await api('directory/set',pendingSwitch);pendingSwitch=null;await inspect()});
async function browse(reset){if(reset){after=null;$('#entries').replaceChildren()}const d=await api('directory/browse',{path:$('#browse-path').value,after});for(const row of d.items)$('#entries').append(button(row.name,async()=>{$('#browse-path').value=row.path;await browse(true)}));after=d.next;$('#more').hidden=!after}
$('#browse').onclick=()=>action(()=>browse(true));$('#more').onclick=()=>action(()=>browse(false));action(async()=>{await projects();await sessions()});
</script></html>'''
