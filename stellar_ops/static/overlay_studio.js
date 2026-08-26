const D=window.__OVERLAY_STUDIO__;
const $=selector=>document.querySelector(selector);
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
const previewState={playing:true,busy:false,timer:null,initialized:false};
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
 renderPackages();renderSelection();renderPhases();renderPreviewControls();renderCatalog();
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
function renderPreviewControls(){
 const packages=publicPackages(),currentPackage=Number($('#preview-package').value)||D.overlay_selection?.active_package_id||packages[0]?.id;
 $('#preview-phase').innerHTML=PHASES.map(phase=>'<option '+(phase===(D.overlay_selection?.active_phase||D.operation?.state)?'selected':'')+'>'+phase+'</option>').join('');
 $('#preview-package').innerHTML=packageOptions(currentPackage);
 if(!previewState.initialized){
  previewState.initialized=true;
  previewState.timer=setInterval(()=>{
   if(!previewState.playing||previewState.busy)return;
   let t=Number($('#preview-time').value)+0.5;if(t>12)t=-5;$('#preview-time').value=t;
   if(t<0){$('#preview-pressure').value=0;$('#preview-thrust').value=0}
   else if(t<=1.2){const r=t/1.2;$('#preview-pressure').value=(62*r).toFixed(1);$('#preview-thrust').value=Math.round(900*r)}
   else if(t<=5.5){$('#preview-pressure').value=(58+4*Math.sin(t*2.2)).toFixed(1);$('#preview-thrust').value=Math.round(850+70*Math.sin(t*2.7))}
   else{$('#preview-pressure').value=Math.max(0,60-(t-5.5)*12).toFixed(1);$('#preview-thrust').value=Math.max(0,850-(t-5.5)*220)}
   updatePreview();
  },800);
  updatePreview();
 }
}
function missionClock(value){const sign=value<0?'T-':'T+';const v=Math.abs(value);return sign+'00:'+String(v.toFixed(1)).padStart(4,'0')}
function updatePreview(){
 const packageId=Number($('#preview-package').value);if(!packageId)return;
 const t=Number($('#preview-time').value),pressure=Number($('#preview-pressure').value),thrust=Number($('#preview-thrust').value);
 $('#preview-time-value').textContent=missionClock(t);$('#preview-pressure-value').textContent=pressure.toFixed(1)+' bar';$('#preview-thrust-value').textContent=Math.round(thrust)+' N';
 const mode=$('#preview-mode').value;$('#preview-stage').classList.toggle('transparent',mode==='OVERLAY');$('#preview-loading').classList.add('show');previewState.busy=true;
 const query=new URLSearchParams({t,pressure,thrust,mode,width:960,_:Date.now()});
 $('#preview-image').src='/api/media/overlay-preview/'+packageId+'.png?'+query;
}
function selectPreviewPhase(){
 const phase=$('#preview-phase').value,mapping=(D.phase_overlay_assignments||[]).find(item=>item.phase===phase);
 if(mapping)$('#preview-package').value=String(mapping.package_id);updatePreview();
}
function renderCatalog(){
 const query=$('#catalog-filter').value.trim().toLowerCase();const rows=D.telemetry_catalog.filter(item=>!query||[item.channel_id,item.label,item.category,item.canonical_unit].some(value=>String(value).toLowerCase().includes(query)));
 const counts={};D.telemetry_catalog.forEach(item=>counts[item.category]=(counts[item.category]||0)+1);$('#catalog-count')?.remove();
 $('#catalog-summary').innerHTML=Object.entries(counts).map(([name,count])=>'<span>'+esc(name)+' · '+count+'</span>').join('');
 $('#catalog').innerHTML=rows.map(item=>'<tr><td>'+esc(item.channel_id)+'</td><td><b>'+esc(item.label)+'</b><br><small>'+esc(item.category)+'</small></td><td>'+esc(item.data_type)+'</td><td>'+esc(item.canonical_unit)+'</td><td>'+esc(item.source_kind)+'</td><td>'+tag(item.classification)+'</td></tr>').join('');
}
$('#catalog-filter').addEventListener('input',renderCatalog);
$('#preview-phase').addEventListener('change',selectPreviewPhase);
$('#preview-package').addEventListener('change',updatePreview);
$('#preview-mode').addEventListener('change',updatePreview);
['#preview-time','#preview-pressure','#preview-thrust'].forEach(selector=>$(selector).addEventListener('input',()=>{previewState.playing=false;$('#preview-play').textContent='PLAY';updatePreview()}));
$('#preview-play').addEventListener('click',()=>{previewState.playing=!previewState.playing;$('#preview-play').textContent=previewState.playing?'PAUSE':'PLAY';if(previewState.playing)updatePreview()});
$('#preview-reset').addEventListener('click',()=>{$('#preview-time').value=1.2;$('#preview-pressure').value=42;$('#preview-thrust').value=720;previewState.playing=false;$('#preview-play').textContent='PLAY';updatePreview()});
$('#preview-fullscreen').addEventListener('click',()=>$('#preview-stage').requestFullscreen?.());
$('#preview-image').addEventListener('load',()=>{previewState.busy=false;$('#preview-loading').classList.remove('show');$('#preview-status').textContent='RENDERED'});
$('#preview-image').addEventListener('error',()=>{previewState.busy=false;$('#preview-loading').textContent='PREVIEW RENDER FAILED';$('#preview-loading').classList.add('show');$('#preview-status').textContent='ERROR'});
$('#selection-mode').addEventListener('change',event=>{$('#manual-package').disabled=event.target.value!=='MANUAL'});
$('#selection-form').addEventListener('submit',async event=>{event.preventDefault();try{const mode=$('#selection-mode').value;await post('/api/media/overlay-selection',{mode,package_id:mode==='MANUAL'?Number($('#manual-package').value):null});toast('Layout selection set to '+mode);await refresh()}catch(error){toast(error.message,true)}});
$('#phase-assignments').addEventListener('click',async event=>{if(!event.target.classList.contains('save-phase'))return;const row=event.target.closest('tr');try{await post('/api/media/phase-overlay',{phase:row.dataset.phase,package_id:Number(row.querySelector('.phase-package').value),transition:row.querySelector('.phase-transition').value});toast(row.dataset.phase+' layout saved');await refresh()}catch(error){toast(error.message,true)}});
$('#package-input').addEventListener('change',event=>{$('#selected-package').textContent=event.target.files[0]?.name||'No package selected'});
$('#package-form').addEventListener('submit',async event=>{event.preventDefault();const data=new FormData(event.currentTarget);try{const response=await fetch('/api/media/overlay-package',{method:'POST',body:data});const body=await response.json();if(!response.ok)throw Error(body.error||'Upload failed');toast(body.detail+' · '+(body.public_safe?'PUBLIC SAFE':'INTERNAL CHANNELS'));event.currentTarget.reset();$('#selected-package').textContent='No package selected';await refresh()}catch(error){toast(error.message,true)}});
$('#channel-form').addEventListener('submit',async event=>{event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));try{const body=await post('/api/media/telemetry-channel',data);toast('Channel registered: '+body.channel.channel_id);event.currentTarget.reset();await refresh()}catch(error){toast(error.message,true)}});
render();