(() => {
  let cfg = null;
  let cfgError = null;
  let frame = null;

  function esc(v){return String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}

  function installStyles(){
    if(document.getElementById('airspace-v4-style')) return;
    const s=document.createElement('style');
    s.id='airspace-v4-style';
    s.textContent=`
      .airspace-live-shell{display:flex;flex-direction:column;min-height:420px;background:#061117}
      .airspace-live-meta{display:grid;grid-template-columns:1.2fr .8fr .8fr;gap:0;border-bottom:1px solid #143445;background:#07141b}
      .airspace-live-meta>span{padding:8px 10px;border-right:1px solid #143445;min-width:0}
      .airspace-live-meta>span:last-child{border-right:0}
      .airspace-live-meta small{display:block;font-size:8px;letter-spacing:.08em;color:#4d7d93;text-transform:uppercase}
      .airspace-live-meta b{display:block;margin-top:3px;font-size:11px;color:#d9eef7;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
      .airspace-live-host{position:relative;min-height:360px;height:48vh;max-height:620px;background:#020609;overflow:hidden}
      .airspace-live-host iframe{width:100%;height:100%;border:0;display:block;background:#020609}
      .airspace-live-loading{height:100%;min-height:360px;display:flex;align-items:center;justify-content:center;flex-direction:column;gap:7px;color:#6c8794;font-size:10px}
      .airspace-live-loading b{color:#d9eef7;font-size:12px}
      .airspace-live-warning{padding:7px 10px;border-top:1px solid #143445;background:#07141b;color:#6c8794;font-size:8px;line-height:1.45}
      .airspace-live-warning b{color:#e9ae42}
      @media(max-width:900px){.airspace-live-meta{grid-template-columns:1fr}.airspace-live-meta>span{border-right:0;border-bottom:1px solid #143445}.airspace-live-meta>span:last-child{border-bottom:0}.airspace-live-host{height:420px}}
    `;
    document.head.appendChild(s);
  }

  function mapUrl(){
    if(!cfg || cfg.latitude==null || cfg.longitude==null) return null;
    const lat=Number(cfg.latitude),lon=Number(cfg.longitude);
    const p=new URLSearchParams({
      lat: lat.toFixed(6),
      lon: lon.toFixed(6),
      SiteLat: lat.toFixed(6),
      SiteLon: lon.toFixed(6),
      zoom: '9',
      kiosk: '',
      enableLabels: '',
      extendedLabels: '1',
      mapDim: '0.35'
    });
    return `https://globe.airplanes.live/?${p.toString()}`;
  }

  function ensureFrame(){
    const host=document.getElementById('airspace-live-host');
    const src=mapUrl();
    if(!host || !src) return;
    if(!frame){
      frame=document.createElement('iframe');
      frame.id='airspace-live-frame';
      frame.title='Live ADS-B and MLAT traffic map';
      frame.referrerPolicy='no-referrer';
      frame.loading='eager';
      frame.allow='fullscreen';
      frame.src=src;
    } else if(frame.src!==src){
      frame.src=src;
    }
    if(frame.parentElement!==host){
      host.replaceChildren(frame);
    }
  }

  function airspacePanel(item){
    let body;
    if(cfgError){
      body=`<div class="airspace-live-shell"><div class="airspace-live-loading"><b>LOCATION UNAVAILABLE</b><span>${esc(cfgError)}</span></div></div>`;
    } else if(!cfg){
      body='<div class="airspace-live-shell"><div class="airspace-live-loading"><b>LOADING AIRSPACE MAP</b><span>Reading operation-site coordinates…</span></div></div>';
    } else if(cfg.latitude==null || cfg.longitude==null){
      body='<div class="airspace-live-shell"><div class="airspace-live-loading"><b>OPERATION SITE NOT CONFIGURED</b><span>Set the location from the Weather panel first.</span></div></div>';
    } else {
      body=`<div class="airspace-live-shell">
        <div class="airspace-live-meta">
          <span><small>OPERATION SITE</small><b>${esc(cfg.site_name||'Operation Site')}</b></span>
          <span><small>LIVE SOURCE</small><b>AIRPLANES.LIVE · ADS-B / MLAT</b></span>
          <span><small>AIRSPACE STATUS</small><b>UNVERIFIED</b></span>
        </div>
        <div class="airspace-live-host" id="airspace-live-host"></div>
        <div class="airspace-live-warning"><b>SITUATIONAL AWARENESS ONLY.</b> Aircraft shown are received ADS-B/MLAT observations. Absence of a target is not an airspace-clearance determination; CAA/AIS/ATC and NOTAM verification remain authoritative.</div>
      </div>`;
    }
    return panelShell(item,body,'LIVE MAP · EXTERNAL ADS-B / MLAT');
  }

  async function loadConfig(){
    try{
      const r=await fetch('/api/weather',{cache:'no-store'});
      const payload=await r.json();
      cfg=payload.settings||null;
      cfgError=null;
    }catch(e){
      cfgError=e.message||String(e);
    }
    renderWorkspace();
    setTimeout(ensureFrame,0);
  }

  installStyles();
  PANEL_NAMES.airspace='AIRSPACE & LIVE TRAFFIC';
  renderers.airspace=airspacePanel;

  const baseRender=renderWorkspace;
  renderWorkspace=function(){
    if(frame && frame.parentElement) frame.remove();
    baseRender();
    setTimeout(ensureFrame,0);
  };

  const requested=new URLSearchParams(location.search).get('panel');
  if(requested==='airspace'){
    document.body.classList.add('popout');
    layout=[{panel:'airspace',span:3,order:0}];
  }else if(!layout.some(x=>x.panel==='airspace')){
    const commandIndex=layout.findIndex(x=>x.panel==='command');
    const insertAt=commandIndex>=0?commandIndex+1:layout.length;
    layout.splice(insertAt,0,{panel:'airspace',span:2,order:insertAt});
    layout.forEach((x,i)=>x.order=i);
  }

  renderWorkspace();
  loadConfig();
})();
