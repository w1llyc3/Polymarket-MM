(()=>{'use strict';
const $=id=>document.getElementById(id);let samples=[],job=null,lanes=[];
async function get(url){const r=await fetch(url,{cache:'no-store'});if(!r.ok)throw Error('HTTP '+r.status);return r.json();}
async function post(url,body){const b=await get('/api/bootstrap');const r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json','X-Console-Token':b.token},body:JSON.stringify(body)});const v=await r.json();if(!r.ok)throw Error(v.error||'请求失败');return v;}
const pretty=v=>JSON.stringify(v,null,2);
$('refresh').onclick=async()=>{try{const v=await get('/api/offline/samples');samples=v.samples;$('sample').replaceChildren(new Option('明确合成比赛样本','synthetic'),...samples.map(s=>new Option(`${s.source} · ${s.period} · ${s.duration_s.toFixed(0)}秒 · ${s.path}`,s.id)));$('sample-info').textContent=`${samples.length}份样本；${v.errors.length}份读取失败。真实赛前样本不能充当赛中验证。`;}catch(e){$('status').textContent=e.message;}};
$('sample').onchange=()=>{$('sample-info').textContent=pretty(samples.find(s=>s.id===$('sample').value)||{source:'SYNTHETIC_DATA',not_real_game:true});};
function link(text,name,lane){const a=document.createElement('a');a.textContent=text;a.href='/api/offline/download?'+new URLSearchParams({id:job,name,...(lane?{lane}:{})});a.download=name;return a;}
function show(v){lanes=v.lanes||[];$('lane').replaceChildren(...lanes.map(l=>new Option(`${l.lane} · 距中点${l.distance_from_book_mid} · ${l.scenario}`,l.lane)));$('results').replaceChildren(link('下载比较CSV','comparison.csv'),document.createTextNode(' · '),link('下载完整实验JSON','comparison.json'));
const meta=document.createElement('p');meta.textContent=`本次结果绑定：${v.sample?.source} / ${v.sample?.period} · 每组虚拟资金${v.budget_per_lane??'见完整JSON'} · 现金构造份额${v.explicit_split_shares??'见完整JSON'}；上方表单仅用于下一次实验。`;$('results').append(meta);
for(const l of lanes){const p=document.createElement('p');const m=l.metrics;p.textContent=`${l.lane} · 成交${m.fill_events} · 模拟净PnL ${m.account.net_pnl??'未知'} · 权益${m.account.equity??'未知'} · 账本一致${l.shutdown_ledger_consistent}`;$('results').append(p);}display();}
async function display(){const l=lanes.find(x=>x.lane===$('lane').value);if(!l)return;$('details').textContent=pretty(l.metrics);$('exports').replaceChildren(...['summary.md','analysis.json','orders.json','fills.json','markouts.json','quote-markouts.json','timeline.jsonl','decisions.jsonl'].flatMap(name=>[link(name,name,l.lane),document.createTextNode(' · ')]));
try{
$('exports').append(link('calibration.json','calibration.json',l.lane));
const url='/api/offline/plot?'+new URLSearchParams({id:job,lane:l.lane});const v=await get(url);
const c=$('offline-chart'),x=c.getContext('2d');x.clearRect(0,0,c.width,c.height);x.fillStyle='#a9c6d3';
x.fillText(`中点${v.points.length}/${v.total_points} · 目标${v.targets.values.length}/${v.targets.total} · 挂单${v.accepted_orders.values.length}/${v.accepted_orders.total} · 绘图抽样不影响实验`,15,15);
const points=v.points;if(!points.length)return;
const t0=points[0].t,t1=points[points.length-1].t;const px=t=>30+(t-t0)/Math.max(1,t1-t0)*(c.width-60),py=p=>c.height-35-Number(p)*(c.height-70);
const assets=[...new Set(points.map(p=>p.asset))];
for(const [i,a] of assets.entries()){x.beginPath();x.strokeStyle=i?'#70b9dc':'#54dfb2';let last=null;for(const p of points.filter(p=>p.asset===a)){if(last&&last.segment===p.segment&&(p.segment!==undefined||p.t-last.t<=v.max_gap_ms))x.lineTo(px(p.t),py(p.mid));else x.moveTo(px(p.t),py(p.mid));last=p;}x.stroke();}
x.globalAlpha=.4;for(const p of v.targets.values){x.fillStyle=p.side==='BUY'?'#5de6ac':'#f18d95';x.fillRect(px(p.t)-1,py(p.price)-1,2,2);}x.globalAlpha=1;
x.setLineDash([5,3]);for(const o of v.accepted_orders.values){x.strokeStyle=o.side==='BUY'?'#5de6ac':'#f18d95';x.beginPath();x.moveTo(px(o.start_ms),py(o.price));x.lineTo(px(Math.min(t1,o.end_ms??t1)),py(o.price));x.stroke();}x.setLineDash([]);
x.fillStyle='#ffd16b';for(const f of v.fills)x.fillRect(px(f.time_ms)-3,py(f.price)-3,6,6);
x.fillStyle='#a9c6d3';for(const price of [0,.5,1])x.fillText((price*100)+'¢',0,py(price));
x.strokeStyle='#d881be';x.globalAlpha=.25;for(const e of v.events.values){x.beginPath();x.moveTo(px(e.t),30);x.lineTo(px(e.t),c.height-30);x.stroke();}x.globalAlpha=1;
x.fillStyle='#a9c6d3';x.fillText(new Date(t0).toISOString(),30,c.height-5);x.fillText(new Date(t1).toISOString(),c.width-205,c.height-5);
const ic=$('inventory-chart'),ix=ic.getContext('2d');ix.clearRect(0,0,ic.width,ic.height);ix.fillStyle='#a9c6d3';ix.fillText(`库存份数 · ${v.inventory.values.length}/${v.inventory.total}个变化 · 与上图同一UTC时间轴`,15,15);
const maximum=Math.max(1,...v.inventory.values.flatMap(p=>Object.values(p.positions).map(Number)));const iy=q=>ic.height-20-Number(q)/maximum*(ic.height-45);
ix.fillText('0',2,iy(0));ix.fillText(String(maximum),2,iy(maximum));
for(const [i,a] of assets.entries()){ix.strokeStyle=i?'#70b9dc':'#54dfb2';ix.beginPath();let last=null;for(const p of v.inventory.values){const q=p.positions[a]??0;if(last)ix.lineTo(px(p.t),iy(last.q));else ix.moveTo(px(p.t),iy(q));ix.lineTo(px(p.t),iy(q));last={q};}if(last)ix.lineTo(px(t1),iy(last.q));ix.stroke();}
}catch(e){$('status').textContent=e.message;}}
$('lane').onchange=display;
$('run').onclick=async()=>{try{$('run').disabled=true;const selected=$('sample').value;const v=await post('/api/offline/run',{...(selected==='synthetic'?{synthetic:true}:{sample_id:selected}),budget:$('budget').value,split_shares:$('split').value,sensitivity:$('sensitivity').checked,seconds:Number($('seconds').value)});job=v.id;location.hash=job;$('status').textContent='离线实验运行中，不改变采集或原账户。';poll();}catch(e){$('status').textContent=e.message;$('run').disabled=false;}};
async function poll(){try{const v=await get('/api/offline/job?id='+job);$('status').textContent=v.status+(v.message?' · '+v.message:'');if(v.status==='RUNNING'){setTimeout(poll,1500);return;}$('run').disabled=false;if(v.status==='FINISHED')show(v);}catch(e){$('status').textContent=e.message;$('run').disabled=false;}}
$('account').onclick=async()=>{try{$('account-view').textContent='读取公开持仓…';$('account-view').textContent=pretty(await post('/api/offline/account',{address:$('wallet').value}));}catch(e){$('account-view').textContent=e.message;}};
$('account-mock').onclick=async()=>{try{$('account-view').textContent='明确合成Mock账户，只读…';$('account-view').textContent=pretty(await post('/api/offline/account-mock',{}));}catch(e){$('account-view').textContent=e.message;}};
if(/^#[0-9a-f]{32}$/.test(location.hash)){job=location.hash.slice(1);poll();}
})();
