const D=window.__OVERLAY_STUDIO__;
const $=selector=>document.querySelector(selector);
const esc=value=>String(value??'').replace(/[&<>"']/g,char=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[char]));
function toast(message,error=false){const node=$('#toast');node.textContent=message;node.className=error?'show error':'show';setTimeout(()=>node.className='',3000)}
function tag(value){return '<span class="tag '+esc(String(value).toLowerCase())+'">'+esc(value)+'</span>'}
async function refresh(){const response=await fetch('/api/media/snapshot');if(!response.ok)throw Error('Could not refresh media state');Object.assign(D,await response.json());render()}
function render(){
 $('#operation').textContent=D.operation?.code||'—';
 $('#phase').textContent=D.operation?.state||'—';
 $('#catalog-count').textContent=D.telemetry_catalog.length;
 $('#package-count').textContent=D.overlay_packages.length;
 renderPackages();renderCatalog();
}
function renderPackages(){
 const target=$('#packages');
 if(!D.overlay_packages.length){target.innerHTML='<div class="empty">NO ROTPL PACKAGES REGISTERED</div>';return}
 target.innerHTML=D.overlay_packages.map(item=>'<div class="package"><div><b>'+esc(item.name)+'</b> <code>v'+esc(item.version)+'</code><small>'+esc(item.template_id)+' · '+esc(item.canvas)+'<br>SHA-256 '+esc(item.sha256.slice(0,16))+'…</small></div><div>'+tag(item.state)+' '+tag(item.public_safe?'PUBLIC':'INTERNAL')+'</div></div>').join('');
}
function renderCatalog(){
 const query=$('#catalog-filter').value.trim().toLowerCase();
 const rows=D.telemetry_catalog.filter(item=>!query||[item.channel_id,item.label,item.category,item.canonical_unit].some(value=>String(value).toLowerCase().includes(query)));
 const counts={};D.telemetry_catalog.forEach(item=>counts[item.category]=(counts[item.category]||0)+1);
 $('#catalog-summary').innerHTML=Object.entries(counts).map(([name,count])=>'<span>'+esc(name)+' · '+count+'</span>').join('');
 $('#catalog').innerHTML=rows.map(item=>'<tr><td>'+esc(item.channel_id)+'</td><td><b>'+esc(item.label)+'</b><br><small>'+esc(item.category)+'</small></td><td>'+esc(item.data_type)+'</td><td>'+esc(item.canonical_unit)+'</td><td>'+esc(item.source_kind)+'</td><td>'+tag(item.classification)+'</td></tr>').join('');
}
$('#catalog-filter').addEventListener('input',renderCatalog);
$('#package-input').addEventListener('change',event=>{$('#selected-package').textContent=event.target.files[0]?.name||'No package selected'});
$('#package-form').addEventListener('submit',async event=>{
 event.preventDefault();const data=new FormData(event.currentTarget);
 try{const response=await fetch('/api/media/overlay-package',{method:'POST',body:data});const body=await response.json();if(!response.ok)throw Error(body.error||'Upload failed');toast(body.detail+' · '+(body.public_safe?'PUBLIC SAFE':'INTERNAL CHANNELS'));event.currentTarget.reset();$('#selected-package').textContent='No package selected';await refresh()}catch(error){toast(error.message,true)}
});
$('#channel-form').addEventListener('submit',async event=>{
 event.preventDefault();const data=Object.fromEntries(new FormData(event.currentTarget));
 try{const response=await fetch('/api/media/telemetry-channel',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const body=await response.json();if(!response.ok)throw Error(body.error||'Registration failed');toast('Channel registered: '+body.channel.channel_id);event.currentTarget.reset();await refresh()}catch(error){toast(error.message,true)}
});
render();