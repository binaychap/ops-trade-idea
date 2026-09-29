'use strict';
const $ = id => document.getElementById(id);
const state = {data: null, page: 1, pageSize: 20, selected: null, loading: false};
const labels = {queued:'Queued',ordered:'Ordered',skipped:'Skipped',dry_run:'Dry run',failed:'Failed',unlinked:'No linked idea',waiting_entry:'Waiting for entry',scheduled:'Scheduled',cancelling:'Cancelling legs',submitted:'Market exit submitted',complete:'Complete',untracked:'Not tracked'};
const descriptions = {
  waiting_entry:'Waiting for a confirmed entry fill. No next-day deadline is set yet.',
  scheduled:'An entry fill has been recorded. The next-day deadline does not force another sale if the bracket closes the trade first.',
  cancelling:'Outstanding bracket legs are being cancelled. The worker must confirm their terminal states before selling.',
  submitted:'A market-exit attempt is recorded. Submission alone does not confirm a fill; check the recorded order status below.',
  complete:'The tracked workflow is complete. No next-day sale remains pending. An unfilled, cancelled entry can also complete without a sale.',
};
const formatter = new Intl.DateTimeFormat('en-US',{timeZone:'America/New_York',month:'short',day:'numeric',hour:'numeric',minute:'2-digit'});
const money = new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'});
function text(tag, content, className) { const node=document.createElement(tag); node.textContent=content == null ? '—' : String(content); if(className)node.className=className; return node; }
function date(value) { const parsed=new Date(value); return value && !Number.isNaN(parsed.getTime()) ? formatter.format(parsed) : '—'; }
function badge(value) { const tone=['complete','ordered','FILLED'].includes(value)?'good':['scheduled','submitted','SUBMITTED'].includes(value)?'active':['waiting_entry','cancelling','queued','PENDING','PARTIAL_FILLED'].includes(value)?'warn':['failed','FAILED'].includes(value)?'bad':''; return text('span',labels[value]||value||'Unknown',`badge ${tone}`); }
function hasError(trade) { return trade.status==='failed'||Boolean(trade.exit?.last_error); }
function cell(...nodes) {const td=document.createElement('td');td.append(...nodes);return td;}
function filtered() {
 const query=$('search').value.trim().toLowerCase();
 return (state.data?.trades||[]).filter(t=>(!query||`${t.symbol} ${t.trade_id||''}`.toLowerCase().includes(query))&&(!$('idea-filter').value||t.status===$('idea-filter').value)&&(!$('exit-filter').value||(t.exit?.status||'untracked')===$('exit-filter').value)&&(!$('attention-filter').checked||hasError(t)));
}
function renderRows() {
 const rows=filtered(), pages=Math.max(1,Math.ceil(rows.length/state.pageSize)); state.page=Math.min(state.page,pages);
 const start=(state.page-1)*state.pageSize; $('trades').replaceChildren();
 for(const t of rows.slice(start,start+state.pageSize)) {
  const tr=document.createElement('tr'), symbol=text('strong',t.symbol||'Unknown'), id=text('span',t.trade_id||'Standalone exit job','secondary'); id.title=t.trade_id||'';
  const statusCell=cell(badge(t.exit?.status||'untracked'));
  if(t.exit?.last_error)statusCell.append(text('span','Reconciliation deferred','error-note'));
  const due=cell(text('span',t.exit?.status==='complete'?'Finished':date(t.exit?.due_at)));
  if(t.exit?.due_at&&t.exit.status!=='complete')due.append(text('span','Next trading session exit','secondary'));
  const fills=cell(text('span',t.exit?.entry_filled_quantity!=null?`${t.exit.entry_filled_quantity} entry filled`:'Not recorded'));
  if(t.exit?.remaining_quantity!=null)fills.append(text('span',`${t.exit.remaining_quantity} remaining`,'secondary'));
  const button=text('button','View →','view-button'); button.type='button';button.setAttribute('aria-label',`View ${t.symbol} trade ${t.trade_id||''}`);button.addEventListener('click',()=>openDetail(t.key));
  tr.append(cell(symbol,id),cell(badge(t.status)),statusCell,due,fills,cell(text('span',date(t.exit?.updated_at||t.updated_at))),cell(button));$('trades').append(tr);
 }
 $('empty').hidden=rows.length>0;
 if(!rows.length){$('empty').querySelector('h3').textContent=state.data?.trades.length?'No matching trades':'No trades recorded yet';$('empty').querySelector('p').textContent=state.data?.trades.length?'Adjust your search or filters to see more activity.':state.data?.notice||'Trades will appear when the bot processes an idea. Existing broker positions are not imported automatically.';}
 $('row-count').textContent=`${rows.length?start+1:0}–${Math.min(start+state.pageSize,rows.length)} of ${rows.length} records`;
 $('page-number').textContent=`${state.page} / ${pages}`;$('previous').disabled=state.page===1;$('next').disabled=state.page===pages;
}
function render() {
 for(const [key,value] of Object.entries(state.data.summary))$(key).textContent=value;
 const s=state.data.settings;
 $('schedule-copy').textContent=`${s.exit_time} New York time · next trading day after the entry fill`;
 $('scheduler-badge').replaceWith(Object.assign(badge(s.scheduler_running?'scheduled':'untracked'),{id:'scheduler-badge',textContent:s.scheduler_running?'Enabled':s.scheduler_enabled?'Paused · dry run':'Disabled'}));
 $('mode-badge').textContent=s.dry_run?'Dry run':'Broker submission mode';
 $('freshness').textContent=`Updated ${date(state.data.as_of)} ET`;
 renderRows();
 if(state.selected && $('detail').open)renderDetail();
}
function detailSection(title){const section=document.createElement('section');section.className='detail-section';section.append(text('h3',title));$('detail-body').append(section);return section;}
function detailsGrid(section,pairs){const dl=document.createElement('dl');dl.className='detail-grid';for(const [key,value] of pairs){const div=document.createElement('div');div.append(text('dt',key),text('dd',value));dl.append(div);}section.append(dl);}
function renderDetail(){
 const trade=state.data.trades.find(t=>t.key===state.selected);if(!trade){$('detail').close();return;}
 $('detail-title').textContent=trade.symbol||'Trade';$('detail-body').replaceChildren();
 const idea=detailSection('Trade idea');idea.append(badge(trade.status));
 detailsGrid(idea,[['Trade ID',trade.trade_id],['Pipeline',trade.pipeline],['Strategy',trade.strategy],['Action',trade.action],['Decision notional',trade.notional_usd==null?'—':money.format(trade.notional_usd)],['Idea recorded · ET',date(trade.created_at)],['Idea updated · ET',date(trade.updated_at)]]);
 if(trade.rationale)idea.append(text('p',trade.rationale,'detail-copy'));
 if(trade.status==='ordered')idea.append(text('p','Ordered means the bracket was submitted. It does not confirm a filled or currently open position.','detail-copy'));
 const exit=detailSection('Exit workflow'), job=trade.exit;
 if(!job){
  exit.append(text('p','This trade has no linked exit job. Its broker fills are not tracked by this dashboard.','detail-copy'));
  const references=Object.entries(trade.bracket||{}).filter(([,value])=>value);
  if(references.length){const section=detailSection('Bracket references');detailsGrid(section,references);}
  return;
 }
 exit.append(badge(job.status),text('p',descriptions[job.status]||'Check the recorded order statuses below.','detail-copy'));
 if(job.last_error)exit.append(text('p',job.last_error,'alert'));
 if(job.entry_submission_error)exit.append(text('p',`Original submission error: ${job.entry_submission_error}`,'detail-copy'));
 if(job.next_check_at)exit.append(text('p',`Next broker lookup: ${date(job.next_check_at)} ET`,'detail-copy'));
 detailsGrid(exit,[['Scheduled exit · ET',date(job.due_at)],['Entry fill recorded · ET',date(job.entry_filled_at)],['Entry shares filled',job.entry_filled_quantity],['Bracket exit shares filled',job.bracket_filled_quantity],['Remaining after reconciliation',job.remaining_quantity],['Last worker update · ET',date(job.updated_at)]]);
 const orders=detailSection('Recorded broker orders');
 const renderOrder=(name,id,snapshot)=>{const row=document.createElement('div');row.className='order-row';row.append(text('strong',name),badge(snapshot?.status||'Not observed'),text('code',id||'ID not recorded'));if(snapshot?.filled_quantity!=null)row.append(text('span',`${snapshot.filled_quantity} shares filled`,'secondary'));orders.append(row);};
 for(const [name,key] of [['Master buy','entry_id'],['Take profit','profit_id'],['Stop loss','stop_id']])renderOrder(name,job[key],job.order_snapshots[job[key]]);
 job.market_orders.forEach((attempt,index)=>renderOrder(`Scheduled market sell ${index+1}`,attempt.id,job.order_snapshots[attempt.id]||attempt));
 orders.append(text('p','These are saved observations, not a fresh broker query. The worker may have newer information on its next cycle.','detail-copy'));
}
function openDetail(key){state.selected=key;renderDetail();$('detail').showModal();}
let refreshTimer;
let refreshIntervalMs=3600000;
let nextRefreshAt=0;
function scheduleRefresh(){
 clearTimeout(refreshTimer);
 nextRefreshAt=Date.now()+refreshIntervalMs;
 refreshTimer=setTimeout(()=>{if($('auto-refresh').checked&&!document.hidden)refresh();},refreshIntervalMs);
}
async function refresh(){
 if(state.loading)return;state.loading=true;$('refresh').disabled=true;
 try{const response=await fetch('/api/trades',{cache:'no-store'});if(!response.ok)throw new Error('Unable to read the trade ledger.');const data=await response.json();if(!Array.isArray(data.trades))throw new Error('Unexpected ledger response.');state.data=data;const seconds=data.settings?.dashboard_refresh_interval_seconds;if(Number.isInteger(seconds)&&seconds>0&&seconds<=2147483)refreshIntervalMs=seconds*1000;$('auto-refresh-label').textContent=`Auto-refresh · ${refreshIntervalMs%3600000===0?refreshIntervalMs/3600000+'h':refreshIntervalMs/1000+'s'}`;$('error').hidden=true;render();}
 catch(error){$('error').textContent=`${error.message} ${state.data?'Showing the last successful snapshot.':'Try refreshing again.'}`;$('error').hidden=false;$('freshness').textContent='Refresh failed';if(!state.data){$('empty').querySelector('h3').textContent='Trade activity unavailable';$('empty').querySelector('p').textContent='The ledger could not be loaded. Use Refresh to try again.';}}
 finally{state.loading=false;$('refresh').disabled=false;scheduleRefresh();}
}
$('refresh').addEventListener('click',refresh);
for(const id of ['search','idea-filter','exit-filter','attention-filter'])$(id).addEventListener('input',()=>{state.page=1;renderRows();});
$('previous').addEventListener('click',()=>{state.page--;renderRows();});$('next').addEventListener('click',()=>{state.page++;renderRows();});
$('close-detail').addEventListener('click',()=>$('detail').close());$('detail').addEventListener('close',()=>{state.selected=null;});
$('auto-refresh').addEventListener('change',()=>{if($('auto-refresh').checked&&Date.now()>=nextRefreshAt)refresh();});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&$('auto-refresh').checked&&Date.now()>=nextRefreshAt)refresh();});
refresh();
