(() => {
  const el = (tag, text, cls) => {const x=document.createElement(tag); x.textContent=text; if(cls)x.className=cls; return x;};
  async function refresh(){
    try {
      const response=await fetch('/api/operations'); if(!response.ok)throw Error('Status unavailable'); const s=await response.json();
      document.querySelector('#overall').textContent=s.status;
      const issues=document.querySelector('#issues'); issues.replaceChildren();
      for(const issue of s.issues){const row=el('div','', 'ops-issue'); row.append(el('strong',issue.detail),el('p',issue.action,'m')); issues.append(row);}
      if(!s.issues.length)issues.append(el('p','No outstanding operational exceptions.','empty'));
      const state=document.querySelector('#state'); state.replaceChildren();
      for(const [name,value] of Object.entries({'Broker':s.daemon.broker||'Not started','Mode':s.daemon.live?'Live':'Paper','Positions':s.positions,'Pending entries':s.pending_entries,'Active policy':s.autonomy.active.id,'Learning':s.autonomy.trial?.status||s.autonomy.research.status,'Last backup':s.maintenance.at||'No backup yet'}))state.append(el('dt',name),el('dd',String(value)));
      const jobs=document.querySelector('#jobs'); jobs.replaceChildren();
      for(const job of s.daemon.tasks||[]){const tr=el('tr',''); for(const value of [job.name,job.last_run?new Date(job.last_run).toLocaleString():'Waiting',job.last_error|| (job.runs?'Completed':'Scheduled')])tr.append(el('td',value)); jobs.append(tr);}
    }catch(error){document.querySelector('#overall').textContent=error.message;}
  }
  document.querySelector('#backup').onclick=async()=>{const button=document.querySelector('#backup'); button.disabled=true; try{const r=await fetch('/api/maintenance',{method:'POST'}); if(!r.ok)throw Error('Backup failed'); const s=await r.json(); document.querySelector('#result').textContent='Saved '+s.backup; await refresh();}catch(e){document.querySelector('#result').textContent=e.message;}finally{button.disabled=false;}};
  refresh(); setInterval(refresh,30000);
})();
