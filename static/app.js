const $ = (id) => document.getElementById(id);
const state = { portfolio: null, wheel: null, position: null, selectedCall: null, selectedPut: null, pendingOrder: null, pendingCancel: null, tradingLock: {configured:false,unlocked:false,expires_at:null}, lockExpiryTimer:null, charts: {} };
const money = (v, digits = 0) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-US', {style:'currency', currency:'USD', minimumFractionDigits:digits, maximumFractionDigits:digits});
const num = (v, digits = 2) => v == null || !Number.isFinite(Number(v)) ? '—' : Number(v).toLocaleString('en-US', {minimumFractionDigits:digits, maximumFractionDigits:digits});
const pct = (v) => v == null || !Number.isFinite(Number(v)) ? '—' : `${Number(v) >= 0 ? '+' : ''}${(Number(v) * 100).toFixed(2)}%`;
const cls = (v) => Number(v) > 0 ? 'positive' : Number(v) < 0 ? 'negative' : '';

async function api(path, options={}) {
  const response = await fetch(path, {headers:{'Content-Type':'application/json'}, ...options});
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(data.error || `Request failed (${response.status})`);
  return data;
}

function toast(message, error=false) {
  const el = document.createElement('div'); el.className = `toast${error?' error':''}`; el.textContent = message;
  $('toastStack').append(el); setTimeout(() => el.remove(), 5500);
}

function setBusy(busy) {
  $('refreshBtn').disabled = busy; $('refreshBtn').textContent = busy ? '⋯' : '↻';
}

function showMode(data) {
  const demo = data.mode === 'demo';
  $('modeBanner').classList.toggle('hidden', !demo);
  if (demo) $('modeBanner').textContent = `Demo data is being shown because Alpaca is unavailable. ${data.warning || ''}`;
  $('dataSource').textContent = demo ? 'Demo fallback' : 'Alpaca';
}

async function loadPortfolio(preserveSymbol=false) {
  setBusy(true);
  try {
    const previous = preserveSymbol ? $('symbolSelect').value : '';
    const data = await api('/api/portfolio'); state.portfolio = data; showMode(data); renderPortfolio(data);
    const symbols = data.wheel_symbols || data.positions.map(p => p.symbol);
    $('symbolSelect').innerHTML = symbols.map(s => `<option value="${s}">${s}</option>`).join('');
    $('symbolSelect').value = symbols.includes(previous) ? previous : symbols[0] || '';
    $('optionsLevel').textContent = data.account.options_level || '—';
    if (symbols.length) await loadMaturities(); else clearWheel('No current holdings or wheel trade history found');
  } catch (error) { toast(error.message, true); clearWheel(error.message); }
  finally { setBusy(false); }
}

function renderPortfolio(data) {
  $('updatedAt').textContent = `Updated ${new Date(data.updated_at).toLocaleString()}`;
  const market=data.market||{}, marketOpen=market.is_open===true, marketPill=$('marketPill');
  $('marketStatus').textContent=marketOpen?'Market open':'Market closed';
  marketPill.classList.toggle('open',marketOpen);marketPill.classList.toggle('closed',!marketOpen);
  const nextEvent=marketOpen?market.next_close:market.next_open;
  marketPill.title=nextEvent?`${marketOpen?'Closes':'Opens'} ${new Date(nextEvent).toLocaleString()}`:'US equity market status';
  const a = data.account;
  const cards = [
    ['Net equity', money(a.equity), `${data.paper?'Paper':'Live'} account`, '#5b8cff'],
    ['Cash available', money(a.cash), `${money(a.buying_power)} buying power`, '#27c2df'],
    ['Day P/L', money(a.day_pl), `${a.equity ? ((a.day_pl/a.equity)*100).toFixed(2):'0.00'}% today`, a.day_pl>=0?'#23c98b':'#f26374'],
    ['Unrealized P/L', money(a.unrealized_pl), `${data.positions.length} stock positions`, a.unrealized_pl>=0?'#23c98b':'#f26374'],
    ['Premium collected', money(a.premium_collected), 'Open short option basis', '#a986f8'],
    ['Options buying power', money(a.options_buying_power), `Trading level ${a.options_level || 0}`, '#f5ad5d'],
  ];
  $('kpiGrid').innerHTML = cards.map(([label,value,delta,color]) => `<article class="kpi" style="--kpi:${color}"><div class="label">${label}</div><div class="value">${value}</div><div class="delta ${label.includes('P/L')?cls(label==='Day P/L'?a.day_pl:a.unrealized_pl):''}">${delta}</div></article>`).join('');
  renderTrades(data.trades); renderEquity(data.equity_curve);
}

function renderTrades(trades) {
  const q = $('tradeSearch').value.toLowerCase();
  const rows = trades.filter(t => Object.values(t).join(' ').toLowerCase().includes(q));
  $('tradesBody').innerHTML = rows.length ? rows.map(t => `<tr><td>${t.time?new Date(t.time).toLocaleDateString():'—'}</td><td><strong>${t.underlying}</strong></td><td><span class="mono">${t.symbol}</span><br><span style="color:var(--muted)">${t.strategy}</span></td><td>${t.side}</td><td class="mono">${num(t.qty,0)}</td><td class="mono">${money(t.price,2)}</td><td><span class="status ${t.status}">${t.status}</span></td><td>${t.cancelable&&t.order_id?`<button class="cancel-order-btn" data-order-id="${t.order_id}" data-symbol="${t.symbol}">Cancel order</button>`:'—'}</td></tr>`).join('') : '<tr><td colspan="8" class="empty-cell">No matching trades</td></tr>';
  [...$('tradesBody').querySelectorAll('.cancel-order-btn')].forEach(button=>button.onclick=()=>openCancelOrder(button.dataset.orderId,button.dataset.symbol));
}

function chartDefaults() {
  return {responsive:true, maintainAspectRatio:false, animation:{duration:350}, plugins:{legend:{display:false}, tooltip:{backgroundColor:'#172436',titleColor:'#e7edf5',bodyColor:'#b5c0ce',borderColor:'#34465d',borderWidth:1}}, scales:{x:{grid:{display:false},ticks:{color:'#6f7d8e',font:{size:9}}}, y:{grid:{color:'rgba(130,150,175,.10)'},ticks:{color:'#6f7d8e',font:{size:9}}}}};
}

function renderEquity(curve) {
  if (state.charts.equity) state.charts.equity.destroy();
  if (!window.Chart) return;
  const ctx = $('equityChart').getContext('2d'), gradient = ctx.createLinearGradient(0,0,0,250); gradient.addColorStop(0,'rgba(91,140,255,.35)');gradient.addColorStop(1,'rgba(91,140,255,0)');
  state.charts.equity = new Chart(ctx,{type:'line',data:{labels:curve.labels,datasets:[{data:curve.values,borderColor:'#5b8cff',backgroundColor:gradient,fill:true,tension:.34,borderWidth:2,pointRadius:0,pointHoverRadius:4}]},options:{...chartDefaults(),scales:{x:{grid:{display:false},ticks:{color:'#6f7d8e',maxTicksLimit:6,font:{size:9}}},y:{grid:{color:'rgba(130,150,175,.10)'},ticks:{color:'#6f7d8e',callback:v=>'$'+Math.round(v/1000)+'k',font:{size:9}}}}}});
}

async function loadMaturities() {
  const symbol = $('symbolSelect').value; if (!symbol) return;
  $('maturitySelect').innerHTML = '<option>Loading…</option>'; clearChains('Loading option maturities…');
  try {
    const data = await api(`/api/maturities?symbol=${encodeURIComponent(symbol)}`); showMode(data);
    $('maturitySelect').innerHTML = data.maturities.map(m => `<option value="${m}">${new Date(m+'T12:00:00').toLocaleDateString('en-US',{month:'short',day:'numeric',year:'numeric'})}</option>`).join('');
    const todayET=new Date().toLocaleDateString('sv-SE',{timeZone:'America/New_York'}),preferred=data.maturities.find(m=>m>todayET)||data.maturities[0];if(preferred)$('maturitySelect').value=preferred;
    if (data.maturities.length) await loadWheel(); else clearWheel('No active maturities found');
  } catch(error) { toast(error.message,true); clearWheel(error.message); }
}

async function loadWheel() {
  const symbol=$('symbolSelect').value, expiration=$('maturitySelect').value; if(!symbol||!expiration) return;
  clearChains('Resolving OTM chain…');
  try {
    const data=await api(`/api/wheel?symbol=${encodeURIComponent(symbol)}&expiration=${encodeURIComponent(expiration)}`); state.wheel=data;state.position=data.position;state.selectedCall=data.calls[0]||null;state.selectedPut=data.puts[0]||null;showMode(data);renderWheel(data);
  } catch(error){toast(error.message,true);clearWheel(error.message)}
}

function renderWheel(data) {
  const p=data.position,held=p.held!==false&&Number(p.shares)>0,basisLabel=held?'Cost basis':'Last cost basis'; $('scenarioSymbol').textContent=`${p.symbol} Wheel`; $('wheelChartTitle').textContent=held?`${p.symbol} · ${num(p.shares,0)} shares @ ${money(p.average_cost,2)}`:`${p.symbol} · put phase · ${basisLabel.toLowerCase()} ${money(p.average_cost,2)}`;
  $('positionCostLabel').textContent=held?'Avg cost':'Last cost basis';$('costBasisLegend').textContent=basisLabel;
  $('positionShares').textContent=num(p.shares,0);$('positionCost').textContent=money(p.average_cost,2);$('positionSpot').textContent=money(p.spot,2);
  renderChain('call',data.calls);renderChain('put',data.puts);renderWheelChart();updateScenario();syncButtons();
}

function renderChain(type, rows) {
  const body=$(type==='call'?'callsBody':'putsBody'); $(type==='call'?'callCount':'putCount').textContent=`${rows.length} contracts`;
  if(!rows.length){body.innerHTML='<tr><td colspan="6" class="empty-cell">No OTM contracts returned</td></tr>';return}
  body.innerHTML=rows.map((r,i)=>`<tr data-symbol="${r.symbol}" class="${i===0?'selected':''}"><td>${money(r.strike,2)}</td><td>${money(r.bid,2)}</td><td>${money(r.ask,2)}</td><td>${money(r.mid,2)}</td><td>${r.delta?Number(r.delta).toFixed(4):'—'}</td><td>${r.iv?(r.iv*100).toFixed(1)+'%':'—'}</td></tr>`).join('');
  [...body.querySelectorAll('tr[data-symbol]')].forEach(row=>row.onclick=()=>selectOption(type,row.dataset.symbol,row));
}

function selectOption(type,symbol,row) {
  const item=state.wheel[`${type}s`].find(r=>r.symbol===symbol); state[type==='call'?'selectedCall':'selectedPut']=item;
  [...row.parentElement.children].forEach(r=>r.classList.remove('selected'));row.classList.add('selected');updateScenario();renderWheelChart();syncButtons();
  $('selectionNote').textContent=`${type.toUpperCase()} ${money(item.strike,2)} · mid ${money(item.mid,2)}`;
}

function scenario() {
  const p=state.position,c=state.selectedCall,u=state.selectedPut;if(!p)return{};const held=p.held!==false&&Number(p.shares)>0,contracts=held?Math.max(1,Math.floor(Math.abs(p.shares)/100)):1;
  return {contracts,stockPl:held?(p.spot-p.average_cost)*p.shares:null,callPremium:c?c.bid*100*contracts:null,putPremium:u?u.bid*100*contracts:null,calledAway:held&&c?(c.strike-p.average_cost)*100*contracts:null,putCash:u?u.strike*100*contracts:null,putBasis:u?(p.shares*p.average_cost+100*contracts*u.strike)/(p.shares+100*contracts):null};
}

function metric(id,value) { const el=$(id);el.textContent=money(value,2);el.className=cls(value); }
function updateScenario(){const s=scenario(),p=state.position;if(!p)return;const held=p.held!==false&&Number(p.shares)>0;metric('stockPl',s.stockPl);metric('callPremium',s.callPremium);metric('putPremium',s.putPremium);metric('calledAway',s.calledAway);$('putCash').textContent=money(s.putCash,2);$('putBasis').textContent=s.putBasis?money(s.putBasis,2):'—';$('stockPlLabel').textContent=held?'Unrealized P/L from cost basis':'No unrealized stock P/L';$('stockPlHint').textContent=held?`${num(p.shares,0)} sh · cost basis ${money(p.average_cost,2)} → current ${money(p.spot,2)} · updates on refresh`:`No shares held · last cost basis ${money(p.average_cost,2)} · current ${money(p.spot,2)}`;$('callPremiumHint').textContent=state.selectedCall?`${s.contracts} contract(s) · bid × 100`:'Pick a call row';$('putPremiumHint').textContent=state.selectedPut?`${s.contracts} contract(s) · bid × 100`:'Pick a put row';$('calledAwayHint').textContent=held&&state.selectedCall?`${s.contracts} contract(s) @ ${money(state.selectedCall.strike,2)} · ex premium`:'Requires owned shares';$('putCashHint').textContent=state.selectedPut?`${s.contracts} contract(s) @ ${money(state.selectedPut.strike,2)}`:'Pick a put row';}

const wheelLinePlugin={id:'wheelLines',afterDatasetsDraw(chart,args,opts){const {ctx,chartArea,scales}=chart;if(!opts)return;ctx.save();[['cost','#27c2df',opts.costLabel||'Cost basis',false,false],['filledCall','#69e1b3','Filled call K',false,true],['filledPut','#ff9daa','Filled put K',false,true],['call','#23c98b','Call scenario K',true,false],['put','#f26374','Put scenario K',true,false]].forEach(([key,color,label,dotted,filled])=>{if(opts[key]==null)return;const y=scales.y.getPixelForValue(opts[key]);if(y<chartArea.top||y>chartArea.bottom)return;ctx.strokeStyle=color;ctx.setLineDash(dotted?[5,4]:[]);ctx.lineWidth=filled?2.4:dotted?1.4:1.5;ctx.beginPath();ctx.moveTo(chartArea.left,y);ctx.lineTo(chartArea.right,y);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle=color;ctx.font='600 9px JetBrains Mono';const premium=filled&&opts[`${key}Premium`]?` · fill $${Number(opts[`${key}Premium`]).toFixed(2)}`:'';const text=`${label} ${Number(opts[key]).toFixed(2)}${premium}`;if(filled){const width=ctx.measureText(text).width;ctx.fillText(text,chartArea.right-width-7,y+12)}else ctx.fillText(text,chartArea.left+7,y-5)});(opts.pending||[]).forEach(order=>{const y=scales.y.getPixelForValue(order.strike);if(y<chartArea.top||y>chartArea.bottom)return;ctx.strokeStyle='#7d8795';ctx.setLineDash([]);ctx.lineWidth=1.8;ctx.beginPath();ctx.moveTo(chartArea.left,y);ctx.lineTo(chartArea.right,y);ctx.stroke();ctx.fillStyle='#9aa5b3';ctx.font='600 9px JetBrains Mono';const progress=order.status==='partially_filled'?'partially filled':'not filled',text=`Submitted ${order.option_type} K ${Number(order.strike).toFixed(2)} · limit $${Number(order.price).toFixed(2)} · ${progress}`,width=ctx.measureText(text).width;ctx.fillText(text,chartArea.right-width-7,y+13)});ctx.restore()}};
function renderWheelChart(){const p=state.position;if(!p||!window.Chart)return;if(state.charts.wheel)state.charts.wheel.destroy();const current=Number(p.spot),rawBasis=Number(p.average_cost),costBasis=rawBasis>0?rawBasis:null,held=p.held!==false&&Number(p.shares)>0,filledCall=(p.short_legs||[]).find(l=>l.type==='call'),filledPut=(p.short_legs||[]).find(l=>l.type==='put'),pending=state.wheel?.pending_orders||[];const moveFromBasis=costBasis?((current-costBasis)/costBasis):0;const inOrangeBand=costBasis?Math.abs(moveFromBasis)<=.005:true;const barColor=inOrangeBand?'#f5ad5d':moveFromBasis>.005?'#23c98b':'#f26374';const barFill=inOrangeBand?'rgba(245,173,93,.60)':moveFromBasis>.005?'rgba(35,201,139,.58)':'rgba(242,99,116,.58)';const levels=[current,costBasis,state.selectedCall?.strike,state.selectedPut?.strike,filledCall?.strike,filledPut?.strike,...pending.map(order=>order.strike)].filter(v=>Number.isFinite(Number(v))&&Number(v)>0).map(Number);const scaleFloor=Math.min(...levels)*.90;const scaleCeiling=Math.max(...levels)*1.08;state.charts.wheel=new Chart($('wheelChart'),{type:'bar',data:{labels:['Current price'],datasets:[{data:[current],backgroundColor:barFill,borderColor:barColor,borderWidth:1,borderRadius:7,barThickness:72}]},options:{...chartDefaults(),plugins:{...chartDefaults().plugins,wheelLines:{cost:costBasis,costLabel:held?'Cost basis':'Last cost basis',call:state.selectedCall?.strike,put:state.selectedPut?.strike,filledCall:filledCall?.strike,filledCallPremium:Math.abs(Number(filledCall?.avg_entry_price||0)),filledPut:filledPut?.strike,filledPutPremium:Math.abs(Number(filledPut?.avg_entry_price||0)),pending},tooltip:{callbacks:{label:()=>costBasis?`Current ${money(current,2)} · ${moveFromBasis>=0?'+':''}${(moveFromBasis*100).toFixed(2)}% vs ${held?'cost basis':'last cost basis'}`:`Current ${money(current,2)}`}}},scales:{x:{grid:{display:false},ticks:{color:'#8c9aac'}},y:{min:scaleFloor,max:scaleCeiling,grid:{color:'rgba(130,150,175,.10)'},ticks:{color:'#6f7d8e',callback:v=>'$'+Number(v).toFixed(2)}}}},plugins:[wheelLinePlugin]});}

async function loadTradingLock(){try{state.tradingLock=await api('/api/trading-lock');renderTradingLock()}catch(error){toast(`Trading lock: ${error.message}`,true)}}
function renderTradingLock(){const lock=state.tradingLock,button=$('tradeLockBtn');button.classList.toggle('unlocked',lock.unlocked);button.innerHTML=lock.unlocked?'<span>🔓</span> Lock selling':'<span>🔒</span> Unlock selling';button.title=!lock.configured?'Set TRADING_UNLOCK_PASSWORD in .env first':lock.unlocked?'Click to lock Sell call, Sell put, and Close call':'Password required to enable Sell call, Sell put, and Close call';if(state.lockExpiryTimer){clearTimeout(state.lockExpiryTimer);state.lockExpiryTimer=null}if(lock.unlocked&&lock.expires_at){const delay=Math.max(0,lock.expires_at*1000-Date.now());state.lockExpiryTimer=setTimeout(loadTradingLock,Math.min(delay+250,2147483647))}syncButtons()}
function openUnlockModal(){if(!state.tradingLock.configured){toast('Set TRADING_UNLOCK_PASSWORD in .env and restart the app first.',true);return}$('tradingPassword').value='';$('unlockModal').classList.remove('hidden');setTimeout(()=>$('tradingPassword').focus(),0)}
function closeUnlockModal(){$('unlockModal').classList.add('hidden');$('tradingPassword').value=''}
async function toggleTradingLock(){if(!state.tradingLock.unlocked){openUnlockModal();return}try{state.tradingLock=await api('/api/trading/lock',{method:'POST',body:'{}'});renderTradingLock();toast('Sell call, Sell put, and Close call are locked.')}catch(error){toast(error.message,true)}}
async function unlockTrading(){const password=$('tradingPassword').value;if(!password){toast('Enter the trading password.',true);return}$('confirmUnlock').disabled=true;$('confirmUnlock').textContent='Unlocking…';try{state.tradingLock=await api('/api/trading/unlock',{method:'POST',body:JSON.stringify({password})});renderTradingLock();closeUnlockModal();toast('Selling unlocked for this session.')}catch(error){toast(error.message,true);$('tradingPassword').select()}finally{$('confirmUnlock').disabled=false;$('confirmUnlock').textContent='Unlock selling'}}
function syncButtons(){const contracts=scenario().contracts||1,unlocked=state.tradingLock.unlocked,hasShares=state.position?.held!==false&&Number(state.position?.shares)>=100;$('sellCallBtn').disabled=!unlocked||!state.selectedCall||!hasShares;$('sellPutBtn').disabled=!unlocked||!state.selectedPut;const legs=state.position?.short_legs||[],hasCall=legs.some(l=>l.type==='call'),hasPut=legs.some(l=>l.type==='put');$('closeCallBtn').disabled=!unlocked||!hasCall;$('closePutBtn').disabled=!hasPut;const lockHint=unlocked?`${contracts} contract(s)`:'Unlock selling first';$('sellCallBtn').title=!hasShares?'Covered calls require at least 100 owned shares':lockHint;$('sellPutBtn').title=lockHint;$('closeCallBtn').title=!hasCall?'No open call to close':unlocked?'Close the open call':'Unlock selling first';}
function prepareSell(type){const item=type==='call'?state.selectedCall:state.selectedPut;if(!item)return;openOrder({symbol:item.symbol,qty:scenario().contracts,limit_price:item.mid,position_intent:'sell_to_open',label:`Sell ${type} @ mid`});}
function prepareClose(type){const leg=(state.position?.short_legs||[]).find(l=>l.type===type);if(!leg)return;openOrder({symbol:leg.symbol,qty:Math.abs(Math.round(Number(leg.qty))),limit_price:Math.abs(Number(leg.current_price||leg.avg_entry_price||0)),position_intent:'buy_to_close',label:`Close short ${type}`});}
function openOrder(order){if(!order.limit_price){toast('No valid midpoint is available for this contract.',true);return}state.pendingOrder=order;state.pendingCancel=null;$('modalTitle').textContent=order.label;$('orderTicket').innerHTML=`<div><span>Account</span><strong>${document.body.dataset.paper==='true'?'PAPER':'LIVE'}</strong></div><div><span>Contract</span><strong>${order.symbol}</strong></div><div><span>Intent</span><strong>${order.position_intent.replaceAll('_',' ')}</strong></div><div><span>Quantity</span><strong>${order.qty}</strong></div><div><span>DAY limit</span><strong>${money(order.limit_price,2)}</strong></div>`;$('modalWarning').textContent='This submits a real order to the currently configured Alpaca account. Limit orders may not fill.';$('confirmOrder').className=`action-btn ${order.position_intent==='sell_to_open'?'call':'put'}`;$('confirmOrder').textContent='Submit order';$('orderModal').classList.remove('hidden');}
function openCancelOrder(orderId,symbol){state.pendingOrder=null;state.pendingCancel={orderId,symbol};$('modalTitle').textContent='Cancel open order';$('orderTicket').innerHTML=`<div><span>Account</span><strong>${document.body.dataset.paper==='true'?'PAPER':'LIVE'}</strong></div><div><span>Contract</span><strong>${symbol}</strong></div><div><span>Status</span><strong>Submitted · not filled</strong></div>`;$('modalWarning').textContent='This requests cancellation from Alpaca. The order may still fill if execution completes before the cancellation.';$('confirmOrder').className='action-btn put';$('confirmOrder').textContent='Cancel order';$('orderModal').classList.remove('hidden')}
function closeModal(){$('orderModal').classList.add('hidden');state.pendingOrder=null;state.pendingCancel=null}
async function submitOrder(){if(!state.pendingOrder&&!state.pendingCancel)return;$('confirmOrder').disabled=true;const canceling=Boolean(state.pendingCancel);$('confirmOrder').textContent=canceling?'Canceling…':'Submitting…';try{if(canceling){const data=await api(`/api/orders/${encodeURIComponent(state.pendingCancel.orderId)}`,{method:'DELETE'});toast(`Cancellation requested for ${data.symbol}.`)}else{const data=await api('/api/orders',{method:'POST',body:JSON.stringify({...state.pendingOrder,confirm:true})});toast(`${data.paper?'Paper':'Live'} order accepted: ${data.status}`)}closeModal();await loadPortfolio(true)}catch(error){toast(error.message,true);if(!canceling)await loadTradingLock()}finally{$('confirmOrder').disabled=false;$('confirmOrder').textContent='Submit order'}}

function clearChains(message){$('callsBody').innerHTML=`<tr><td colspan="6" class="empty-cell">${message}</td></tr>`;$('putsBody').innerHTML=`<tr><td colspan="6" class="empty-cell">${message}</td></tr>`;}
function clearWheel(message){clearChains(message);state.wheel=null;state.position=null;state.selectedCall=null;state.selectedPut=null;['positionShares','positionCost','positionSpot','stockPl','callPremium','putPremium','calledAway','putCash','putBasis'].forEach(id=>$(id).textContent='—');syncButtons()}

$('symbolSelect').onchange=loadMaturities;$('maturitySelect').onchange=loadWheel;$('refreshBtn').onclick=()=>loadPortfolio(true);$('tradeSearch').oninput=()=>state.portfolio&&renderTrades(state.portfolio.trades);$('sellCallBtn').onclick=()=>prepareSell('call');$('sellPutBtn').onclick=()=>prepareSell('put');$('tradeLockBtn').onclick=toggleTradingLock;$('closeCallBtn').onclick=()=>prepareClose('call');$('closePutBtn').onclick=()=>prepareClose('put');$('modalClose').onclick=closeModal;$('cancelOrder').onclick=closeModal;$('orderModal').onclick=e=>{if(e.target===$('orderModal'))closeModal()};$('confirmOrder').onclick=submitOrder;$('unlockModalClose').onclick=closeUnlockModal;$('cancelUnlock').onclick=closeUnlockModal;$('unlockModal').onclick=e=>{if(e.target===$('unlockModal'))closeUnlockModal()};$('confirmUnlock').onclick=unlockTrading;$('tradingPassword').onkeydown=e=>{if(e.key==='Enter')unlockTrading()};document.addEventListener('keydown',e=>{if(e.key==='Escape'){closeModal();closeUnlockModal()}});
loadTradingLock();loadPortfolio();
