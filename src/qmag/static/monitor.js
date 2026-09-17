/* Read-only charts built from the recorded market/position API. No remote assets. */
(() => {
  let records = [];
  const $ = id => document.getElementById(id), ns = 'http://www.w3.org/2000/svg';
  const money = v => v == null ? '—' : new Intl.NumberFormat('en-US',{style:'currency',currency:'USD'}).format(v);
  function el(tag, text, cls) { const e=document.createElement(tag); if(text!=null)e.textContent=text; if(cls)e.className=cls; return e; }
  function shape(svg, name, attrs, text) {const e=document.createElementNS(ns,name); for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v)); if(text)e.textContent=text;svg.append(e);return e;}
  function chart(row) {
    const n=Number($('monitor-range').value), bars=row.bars.slice(-n).filter(b=>['open','high','low','close'].every(k=>Number.isFinite(b[k])));
    const svg=document.createElementNS(ns,'svg');svg.setAttribute('viewBox','0 0 700 390');svg.setAttribute('role','img');svg.setAttribute('aria-label',`${row.symbol} candlesticks, setup and trade levels`);
    if(!bars.length)return svg;
    const levels=Object.values(row.levels).filter(Number.isFinite), hi=Math.max(...bars.map(b=>b.high),...levels), lo=Math.min(...bars.map(b=>b.low),...levels), span=(hi-lo)||1;
    const x=i=>60+(i+.5)*500/bars.length, y=p=>25+(hi+.07*span-p)/(span*1.14)*255;
    for(let i=0;i<5;i++){const p=lo+i*span/4;shape(svg,'line',{x1:58,y1:y(p),x2:572,y2:y(p),stroke:'var(--border)'});shape(svg,'text',{x:54,y:y(p)+4,fill:'var(--muted)','font-size':11,'text-anchor':'end'},p.toFixed(2));}
    const flag=Number(row.details.flag_days), flagLow=Number(row.details.flag_low), flagHigh=Number(row.details.flag_high);
    if(flag>0&&flagHigh>flagLow)shape(svg,'rect',{x:x(Math.max(0,bars.length-flag-1)),y:y(flagHigh),width:500*Math.min(flag,bars.length)/bars.length,height:Math.max(1,y(flagLow)-y(flagHigh)),fill:'#8b5cf61a',stroke:'#8b5cf6','stroke-dasharray':'4 3'});
    const maxV=Math.max(1,...bars.map(b=>b.volume||0)), w=Math.max(1,500/bars.length*.6);
    bars.forEach((b,i)=>{const color=b.close>=b.open?'#17806c':'#cd4f52';shape(svg,'line',{x1:x(i),x2:x(i),y1:y(b.high),y2:y(b.low),stroke:color});const candle=shape(svg,'rect',{x:x(i)-w/2,y:Math.min(y(b.open),y(b.close)),width:w,height:Math.max(1,Math.abs(y(b.close)-y(b.open))),fill:color});shape(candle,'title',{},`${b.date.slice(0,10)} · O ${b.open.toFixed(2)} H ${b.high.toFixed(2)} L ${b.low.toFixed(2)} C ${b.close.toFixed(2)} · volume ${b.volume??'unknown'}`);shape(svg,'rect',{x:x(i)-w/2,y:350-(b.volume||0)/maxV*48,width:w,height:(b.volume||0)/maxV*48,fill:color,opacity:.35});});
    for(const [key,color] of [['sma_10','#bd669c'],['sma_20','#669ac5'],['sma_50','#b49b42']]){const points=bars.map((b,i)=>b[key]!=null?`${x(i)},${y(b[key])}`:null).filter(Boolean).join(' ');if(points)shape(svg,'polyline',{points,fill:'none',stroke:color,'stroke-width':1});}
    let lastLabelY=-20;
    const priceLevels=[['entry','#17806c'],['stop','#cd4f52'],['target','#3989c8'],['pivot','#b88628']].filter(([key])=>Number.isFinite(row.levels[key])).sort((a,b)=>y(row.levels[a[0]])-y(row.levels[b[0]]));
    for(const [key,color] of priceLevels){const p=row.levels[key],labelY=Math.max(y(p)-5,lastLabelY+16);lastLabelY=labelY;shape(svg,'line',{x1:58,x2:568,y1:y(p),y2:y(p),stroke:color,'stroke-dasharray':key==='entry'?'':'5 3'});shape(svg,'line',{x1:568,x2:576,y1:y(p),y2:labelY-4,stroke:color});shape(svg,'text',{x:578,y:labelY,fill:color,'font-size':12},`${key==='entry'?(row.kind==='position'?'Entry':'Plan'):key==='target'?'TP':key} ${p.toFixed(2)}`);}
    for(const f of row.fills){if(!f.date)continue;const i=bars.findIndex(b=>b.date.slice(0,10)===f.date.slice(0,10));if(i<0)continue;const dot=shape(svg,'circle',{cx:x(i),cy:y(f.price),r:4,fill:f.side==='buy'?'#17806c':'#cd4f52',stroke:'var(--panel)','stroke-width':1.5});shape(dot,'title',{},`${f.side} ${f.quantity} @ ${f.price}`);}
    shape(svg,'text',{x:30,y:377,fill:'var(--muted)','font-size':10},bars[0].date.slice(0,10));shape(svg,'text',{x:490,y:377,fill:'var(--muted)','font-size':10},bars.at(-1).date.slice(0,10));return svg;
  }
  function render(){const q=$('monitor-search').value.toUpperCase(),kind=$('monitor-kind').value,rows=records.filter(r=>r.symbol.includes(q)&&(kind==='all'||r.kind===kind));$('monitor-grid').replaceChildren();$('monitor-empty').hidden=rows.length>0;$('monitor-count').textContent=`${rows.length} stock${rows.length===1?'':'s'}`;
    for(const row of rows){const card=el('article',null,'panel monitor-card'),header=el('div',null,'monitor-card-heading'),link=el('a',row.symbol);link.href='/symbol/'+encodeURIComponent(row.symbol);header.append(link,el('span',row.kind,'pill'));const plot=el('div',null,'chart-scroll');plot.append(chart(row));card.append(header,el('p',`${row.setup.replaceAll('_',' ')} · ${row.asof} · ${row.shares??'—'} shares`,'m'),plot);const metrics=el('div',null,'monitor-metrics');metrics.append(el('span',`Unrealized ${money(row.unrealized)}`),el('span',`Realized ${money(row.realized)}`));card.append(el('p','MA 10 · pink   /   MA 20 · blue   /   MA 50 · gold · '+(row.volume_note||'Recorded bar volume'),'chart-legend'),metrics,el('small',row.kind==='position'?`${row.evidence??'estimated'} · ${row.fees_known?'reported costs included':'costs not fully reported'} · policy ${row.policy_version}`:'Potential levels; no execution implied.','m'));$('monitor-grid').append(card);}}
  async function refresh(){try{const response=await fetch('/api/monitor');if(!response.ok)throw Error('Monitor unavailable');records=(await response.json()).records;render();}catch(e){$('monitor-count').textContent=e.message;}}
  for(const id of ['monitor-search','monitor-kind','monitor-range'])$(id).addEventListener('input',render);refresh();setInterval(refresh,60000);
})();
