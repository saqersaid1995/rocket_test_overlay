(() => {
  const STORAGE_KEY='stellar_airspace_site_v1';
  let cfg=null;
  let frame=null;

  function esc(v){return String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function validLatLon(lat,lon){return Number.isFinite(lat)&&Number.isFinite(lon)&&lat>=-90&&lat<=90&&lon>=-180&&lon<=180;}
  function readStored(){try{const raw=localStorage.getItem(STORAGE_KEY);if(!raw)return null;const j=JSON.parse(raw);const lat=Number(j.latitude),lon=Number(j.longitude);return validLatLon(lat,lon)?{site_name:j.site_name||'Operation Site',latitude:lat,longitude:lon}:null;}catch(_){return null;}}
  function persist(next){try{localStorage.setItem(STORAGE_KEY,JSON.stringify(next));return true;}catch(_){return false;}}

  function installStyles(){
    if(document.getElementById('airspace-v6-style'))return;
    const s=document.createElement('style');s.id='airspace-v6-style';s.textContent=`
      .airspace-v6-shell{display:flex;flex-direction:column;min-height:420px;background:#061117}
      .airspace-v6-setup{min-height:360px;display:flex;align-items:center;justify-content:center;padding:24px}
      .airspace-v6-card{width:min(620px,92%);border:1px solid #143445;background:#07141b;padding:16px}
      .airspace-v6-card h3{margin:0 0 6px;font-size:13px;color:#d9eef7}.airspace-v6-card p{margin:0 0 14px;color:#6c8794;font-size:9px;line-height:1.5}
      .airspace-v6-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px}.airspace-v6-grid label:first-child{grid-column:1/-1}
      .airspace-v6-grid label{display:flex;flex-direction:column;gap:5px;font-size:8px;color:#4d7d93;letter-spacing:.08em}.airspace-v6-grid input{height:32px;border:1px solid #245063;background:#041016;color:#d9eef7;padding:0 9px}
      .airspace-v6-actions{display:flex;gap:8px;margin-top:12px;align-items:center}.airspace-v6-actions button{height:30px;padding:0 12px}.airspace-v6-error{color:#ff7b7b;font-size:9px}.airspace-v6-note{color:#7ea6b7;font-size:9px}
      .airspace-v6-meta{display:grid;grid-template-columns:1.2fr .8fr .8fr auto;border-bottom:1px solid #143445;background:#07141b}.airspace-v6-meta>span{padding:8px 10px;border-right:1px solid #143445}.airspace-v6-meta small{display:block;font-size:8px;color:#4d7d93}.airspace-v6-meta b{display:block;margin-top:3px;font-size:11px;color:#d9eef7}.airspace-v6-meta button{margin:7px;height:27px}
      .airspace-v6-host{position:relative;min-height:360px;height:48vh;max-height:620px;background:#020609}.airspace-v6-host iframe{width:100%;height:100%;border:0;display:block}
      .airspace-v6-warning{padding:7px 10px;border-top:1px solid #143445;background:#07141b;color:#6c8794;font-size:8px;line-height:1.45}.airspace-v6-warning b{color:#e9ae42}
      @media(max-width:900px){.airspace-v6-grid{grid-template-columns:1fr}.airspace-v6-grid label:first-child{grid-column:auto}.airspace-v6-meta{grid-template-columns:1fr}.airspace-v6-host{height:420px}}
    `;document.head.appendChild(s);
  }

  function mapUrl(){if(!cfg)return null;const p=new URLSearchParams({lat:Number(cfg.latitude).toFixed(6),lon:Number(cfg.longitude).toFixed(6),zoom:'9',kiosk:'',enableLabels:'',extendedLabels:'1',mapDim:'0.35'});return `https://globe.airplanes.live/?${p.toString()}`;}
  function ensureFrame(){const host=document.getElementById('airspace-v6-host'),src=mapUrl();if(!host||!src)return;if(!frame){frame=document.createElement('iframe');frame.title='Live ADS-B and MLAT traffic map';frame.loading='eager';frame.referrerPolicy='no-referrer';frame.allow='fullscreen';}if(frame.getAttribute('src')!==src)frame.src=src;if(frame.parentElement!==host)host.replaceChildren(frame);}

  function setupBody(){return `<div class="airspace-v6-shell"><div class="airspace-v6-setup"><div class="airspace-v6-card"><h3>SET OPERATION SITE</h3><p>Enter the coordinates once, then press SAVE & OPEN MAP.</p><div class="airspace-v6-grid"><label>SITE NAME<input id="airspace-site-name" value="Operation Site" maxlength="120"></label><label>LATITUDE<input id="airspace-site-lat" type="number" step="any" min="-90" max="90" value="24.2601511"></label><label>LONGITUDE<input id="airspace-site-lon" type="number" step="any" min="-180" max="180" value="55.789476"></label></div><div class="airspace-v6-actions"><button type="button" id="airspace-save-site">SAVE & OPEN MAP</button><span id="airspace-site-message" class="airspace-v6-note"></span></div></div></div></div>`;}
  function airspacePanel(item){let body;if(!cfg)body=setupBody();else{const direct=mapUrl();body=`<div class="airspace-v6-shell"><div class="airspace-v6-meta"><span><small>OPERATION SITE</small><b>${esc(cfg.site_name||'Operation Site')}</b></span><span><small>LATITUDE</small><b>${Number(cfg.latitude).toFixed(6)}</b></span><span><small>LONGITUDE</small><b>${Number(cfg.longitude).toFixed(6)}</b></span><button id="airspace-change-site" type="button">CHANGE LOCATION</button></div><div class="airspace-v6-host" id="airspace-v6-host"></div><div class="airspace-v6-warning"><b>SITUATIONAL AWARENESS ONLY.</b> ADS-B/MLAT observations do not constitute airspace clearance. <a href="${esc(direct)}" target="_blank" rel="noopener">OPEN SOURCE MAP</a></div></div>`;}return panelShell(item,body,'LIVE MAP · EXTERNAL ADS-B / MLAT');}

  function saveFromInputs(){
    const latEl=document.getElementById('airspace-site-lat'),lonEl=document.getElementById('airspace-site-lon'),nameEl=document.getElementById('airspace-site-name'),msg=document.getElementById('airspace-site-message');
    if(!latEl||!lonEl||!nameEl)return;
    const lat=Number(latEl.value),lon=Number(lonEl.value),name=(nameEl.value||'Operation Site').trim();
    if(!validLatLon(lat,lon)){if(msg){msg.className='airspace-v6-error';msg.textContent='Enter valid latitude and longitude.';}return;}
    cfg={site_name:name||'Operation Site',latitude:lat,longitude:lon};
    const stored=persist(cfg);
    renderWorkspace();
    requestAnimationFrame(()=>ensureFrame());
    if(!stored)console.warn('Airspace site could not be persisted in localStorage; current session will still work.');
  }

  document.addEventListener('click',e=>{
    const target=e.target instanceof Element?e.target.closest('button,a'):null;
    if(!target)return;
    if(target.id==='airspace-save-site'){e.preventDefault();e.stopPropagation();saveFromInputs();return;}
    if(target.id==='airspace-change-site'){e.preventDefault();cfg=null;try{localStorage.removeItem(STORAGE_KEY);}catch(_){}if(frame&&frame.parentElement)frame.remove();renderWorkspace();}
  },true);

  installStyles();cfg=readStored();PANEL_NAMES.airspace='AIRSPACE & LIVE TRAFFIC';renderers.airspace=airspacePanel;
  const baseRender=renderWorkspace;renderWorkspace=function(){if(frame&&frame.parentElement)frame.remove();baseRender();requestAnimationFrame(()=>ensureFrame());};
  const requested=new URLSearchParams(location.search).get('panel');if(requested==='airspace'){document.body.classList.add('popout');layout=[{panel:'airspace',span:3,order:0}];}else if(!layout.some(x=>x.panel==='airspace')){const i=layout.findIndex(x=>x.panel==='command'),at=i>=0?i+1:layout.length;layout.splice(at,0,{panel:'airspace',span:2,order:at});layout.forEach((x,n)=>x.order=n);}
  renderWorkspace();requestAnimationFrame(()=>ensureFrame());
})();