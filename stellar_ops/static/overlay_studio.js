const D=window.__OVERLAY_STUDIO__;
const $=selector=>document.querySelector(selector);
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const PHASES=['STANDBY','CHECKOUT','COUNTDOWN','HOLD','IGNITION','LIFTOFF','POWERED_ASCENT','FIRING','BURNOUT','COAST','APOGEE','DESCENT','RECOVERY','POST_FIRE','LANDING','IMPACT','COMPLETE','CLOSED','ABORT'];
function toast(message,error=false){const node=$('#toast');node.textContent=message;node.className=error?'show error':'show';setTimeout(()=>node.className='',3000)}
function tag(value){return '<span class="tag '+esc(String(value).toLowerCase())+'">'+esc(value)+'</span>'}
async function post(url,data){const response=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw Error(body.error||'Request failed');return body}
async function refresh(){const response=await fetch('/api/media/snapshot');if(!response.ok)throw Error('Could not refresh media state');Object.assign(D,await response.json());render()}
function publicPackages(){return D.overlay_packages.filter(item=>item.public_safe&&item.state==='VALIDATED')}
function packageOptions(selected){const packages=publicPackages();if(!packages.length)return '<option value="">NO PUBLIC-SAFE PACKAGE</option>';return packages.map(item=>'<option value="'+item.id+'" '+(Number(selected)===Number(item.id)?'selected':'')+'>'+esc(item.name)+' · v'+esc(item.version)+'</option>').join('')}
function render(){
 $('#operation').textContent=D.operation?.code||'—';$('#phase').textContent=D.operation?.state||'—';$('#package-count').textContent=D.overlay_packages.length;
 const active=D.overlay_selection?.package;$('#active-layout').textContent=active?active.name:'NONE';
 renderPackages();renderSelection();renderPhases();renderCatalog();
}
function renderPackages(){
 const target=$('#packages');if(!D.overlay_packages.length){target.innerHTML='<div class="empty">NO ROTPL PACKAGES REGISTERED</div>';return}
 target.innerHTML=D.overlay_packages.map(item=>'<div class="package"><div><b>'+esc(item.name)+'</b> <code>v'+esc(item.version)+'</code><small>'+esc(item.template_id)+' · '+esc(item.canvas)+'<br>SHA-256 '+esc(item.sha256.slice(0,16))+'…</small></div><div>'+tag(item.state)+' '+tag(item.public_safe?'PUBLIC':'INTERNAL')+'</div></div>').join('');
}
function renderSelection(){
 const selection=D.overlay_selection||{mode:'AUTO'};$('#selection-mode').value=selection.mode||'AUTO';$('#selection-mode-tag').textContent=selection.mode||'AUTO';
 $('#manual-package').innerHTML=packageOptions(selection.active_package_id);$('#manual-package').disabled=selection.mode!=='MANUAL';
 const item=selection.package;$('#active-card').innerHTML=item?'<small>ON-AIR LAYOUT FOR '+esc(selection.active_phase)+'</small><b>'+esc(item.name)+' · v'+esc(item.version)+'</b><span>'+esc(selection.transition||'CUT')+' · '+esc(item.canvas)+'</span>':'<small>ON-AIR LAYOUT</small><b>NO PUBLIC LAYOUT ASSIGNED</b><span>Upload or assign a public-safe package.</span>';
}
function renderPhases(){
 const mapped=new Map((D.phase_overlay_assignments||[]).map(item=>[item.phase,item]));const packages=publicPackages();
 $('#phase-coverage').textContent=mapped.size+'/'+PHASES.length+' ASSIGNED';
 $('#phase-assignments').innerHTML=PHASES.map(phase=>{const item=mapped.get(phase);return '<tr data-phase="'+phase+'"><td>'+phase+(D.operation?.state===phase?' '+tag('ACTIVE'):'')+'</td><td><select class="phase-package">'+packageOptions(item?.package_id)+'</select></td><td><select class="phase-transition">'+['CUT','DISSOLVE','FADE'].map(v=>'<option '+(item?.transition===v?'selected':'')+'>'+v+'</option>').join('')+'</select></td><td><button class="mini-action save-phase" '+(!packages.length?'disabled':'')+'>SAVE</button></td></tr>'}).join('');
}
function renderCatalog(){
 const query=$('#catalog-filter').value.trim().toLowerCase();const rows=D.telemetry_catalog.filter(item=>!query||[item.channel_id,item.label,item.category,item.canonical_unit].some(value=>String(value).toLowerCase().includes(query)));
 const counts={};D.telemetry_catalog.forEach(item=>counts[item.category]=(counts[item.category]||0)+1);$('#catalog-count')?.remove();
 $('#catalog-summary').innerHTML=Object.entries(counts).map(([name,count])=>'<span>'+esc(name)+' · '+count+'</span>').join('');
 $('#catalog').innerHTML=rows.map(item=>'<tr><td>'+esc(item.channel_id)+'</td><td><b>'+esc(item.label)+'</b><br><small>'+esc(item.category)+'</small></td><td>'+esc(item.data_type)+'</td><td>'+esc(item.canonical_unit)+'</td><td>'+esc(item.source_kind)+'</td><td>'+tag(item.classification)+'</td></tr>').join('');
}
$('#catalog-filter').addEventListener('input',renderCatalog);
$('#selection-mode').addEventListener('change',event=>{$('#manual-package').disabled=event.target.value!=='MANUAL'});
$('#selection-form').addEventListener('submit',async event=>{event.preventDefault();try{const mode=$('#selection-mode').value;await post('/api/media/overlay-selection',{mode,package_id:mode==='MANUAL'?Number($('#manual-package').value):null});toast('Layout selection set to '+mode);await refresh()}catch(error){toast(error.message,true)}});
$('#phase-assignments').addEventListener('click',async event=>{if(!event.target.classList.contains('save-phase'))return;const row=event.target.closest('tr');try{await post('/api/media/phase-overlay',{phase:row.dataset.phase,package_id:Number(row.querySelector('.phase-package').value),transition:row.querySelector('.phase-transition').value});toast(row.dataset.phase+' layout saved');await refresh()}catch(error){toast(error.message,true)}});
$('#package-input').addEventListener('change',event=>{$('#selected-package').textContent=event.target.files[0]?.name||'No package selected'});
$('#package-form').addEventListener('submit',async event=>{event.preventDefault();const data=new FormData(event.currentTarget);try{const response=await fetch('/api/media/overlay-package',{method:'POST',body:data});const body=await response.json();if(!response.ok)throw Error(body.error||'Upload failed');toast(body.detail+' · '+(body.public_safe?'PUBLIC SAFE':'INTERNAL CHANNELS'));event.currentTarget.reset();$('#selected-package').textContent='No package selected';await refresh()}catch(error){toast(error.message,true)}});
$('#channel-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));try{const body=await post('/api/media/telemetry-channel',data);toast('Channel registered: '+body.channel.channel_id);event.currentTarget.reset();await refresh()}catch(error){toast(error.message,true)}});
render();