(() => {
  const root = document.querySelector('.baseline-shell');
  const operationId = root.dataset.operationId;
  const ctx = window.BASELINE_CONTEXT;
  const defaults = [
    ['ARTICLE', ctx.article.serial_number, ctx.article.configuration_revision, 'ARTICLE_REGISTRY', 'VERIFIED'],
    ['PROCEDURE', 'UNASSIGNED', 'WORKING', 'DOCUMENT_CONTROL', 'DRAFT'],
    ['CHANNEL_MAP', 'UNASSIGNED', 'WORKING', 'INSTRUMENTATION', 'DRAFT'],
    ['LIMIT_PROFILE', 'UNASSIGNED', 'WORKING', 'SAFETY_ENGINEERING', 'DRAFT'],
    ['DEVICE_MANIFEST', 'UNASSIGNED', 'WORKING', 'DEVICE_REGISTRY', 'DRAFT'],
    ['CAMERA_MANIFEST', 'UNASSIGNED', 'WORKING', 'VIDEO_CONTROL', 'DRAFT'],
    ['SOFTWARE', 'SMTCS', 'WORKING', 'SOFTWARE_CONFIGURATION', 'DRAFT'],
    ['BROADCAST_TEMPLATE', 'UNASSIGNED', 'WORKING', 'STUDIO_REGISTRY', 'DRAFT'],
  ];
  if (ctx.required.includes('VEHICLE_CONFIGURATION')) defaults.push(['VEHICLE_CONFIGURATION','UNASSIGNED','WORKING','VEHICLE_ENGINEERING','DRAFT']);
  if (ctx.required.includes('RECOVERY_CONFIGURATION')) defaults.push(['RECOVERY_CONFIGURATION','UNASSIGNED','WORKING','RECOVERY_ENGINEERING','DRAFT']);
  const saved = new Map(((ctx.baseline && ctx.baseline.items) || []).map(x => [x.item_type, x]));
  const body = document.querySelector('#baseline-items');
  defaults.forEach(([type, ref, rev, source, status]) => {
    const x = saved.get(type) || {item_type:type, reference:ref, revision:rev, source, verification_status:status};
    const locked = type === 'ARTICLE' ? 'disabled' : '';
    body.insertAdjacentHTML('beforeend', `<tr data-type="${type}"><td><b>${type.replaceAll('_',' ')}</b><small>${ctx.required.includes(type) ? 'MANDATORY' : 'OPTIONAL'}</small></td><td><input class="ref" value="${escapeHtml(x.reference)}" ${locked}></td><td><input class="rev" value="${escapeHtml(x.revision)}" ${locked}></td><td><input class="source" value="${escapeHtml(x.source)}" ${locked}></td><td><select class="status" ${locked}><option>DRAFT</option><option>VERIFIED</option><option>APPROVED</option><option>NOT_APPLICABLE</option></select></td></tr>`);
    body.lastElementChild.querySelector('.status').value = x.verification_status;
  });
  function escapeHtml(v){ const d=document.createElement('div'); d.textContent=v || ''; return d.innerHTML; }
  function payload(){ return {baseline_code:document.querySelector('#baseline-code').value, revision:document.querySelector('#baseline-revision').value, notes:document.querySelector('#baseline-notes').value, items:[...body.rows].map(row => ({item_type:row.dataset.type,reference:row.querySelector('.ref').value,revision:row.querySelector('.rev').value,source:row.querySelector('.source').value,verification_status:row.querySelector('.status').value}))}; }
  async function call(url, data){ const message=document.querySelector('#baseline-message'); message.textContent='PROCESSING…'; const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); const result=await response.json(); if(!response.ok){message.textContent=result.error; message.className='error'; return null;} message.textContent='CONTROLLED RECORD UPDATED'; message.className='ok'; return result; }
  document.querySelector('#save-baseline').onclick=async()=>{ if(await call(`/api/ops/${operationId}/baseline`,payload())) location.reload(); };
  document.querySelector('#release-baseline').onclick=async()=>{ const saved=await call(`/api/ops/${operationId}/baseline`,payload()); if(!saved)return; const released=await call(`/api/ops/${operationId}/baseline/release`,{released_by:document.querySelector('#release-authority').value}); if(released) location.href=released.url; };
  if(ctx.baseline && ctx.baseline.state==='RELEASED') document.querySelectorAll('input,textarea,select,button').forEach(x=>x.disabled=true);
})();
