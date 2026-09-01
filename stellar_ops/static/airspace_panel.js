(() => {
  let snapshot = null;
  let radiusKm = 50;
  let refreshTimer = null;
  let loading = false;
  let requestSerial = 0;

  function escLocal(v){return String(v ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));}
  function fmt(v,d=0,s=''){return v===null||v===undefined||Number.isNaN(Number(v))?'—':`${Number(v).toFixed(d)}${s}`;}
  function statusText(){return snapshot?.status || (loading ? 'LOADING' : 'WAITING');}
  function traffic(){return snapshot?.traffic || null;}

  function airspacePanel(item){
    const t=traffic();
    const aircraft=t?.aircraft || [];
    const nearest=t?.nearest_distance_km;
    const body=!snapshot?`<div class="airspace-empty"><b>${loading?'LOADING LIVE TRAFFIC':'WAITING'}</b><span>Retrieving ADS-B / MLAT observations for the operation site.</span><button type="button" id="airspace-refresh">${loading?'RETRY NOW':'LOAD TRAFFIC'}</button></div>`:(!snapshot.ok||!t)?`<div class="airspace-empty"><b>${escLocal(snapshot.status||'UNAVAILABLE')}</b><span>${escLocal(snapshot.message||'Traffic source unavailable')}</span><button type="button" id="airspace-refresh">RETRY</button></div>`:`
      <div class="airspace-toolbar">
        <label>MONITOR RADIUS
          <select id="airspace-radius">
            ${[10,25,50,100,150].map(v=>`<option value="${v}" ${v===radiusKm?'selected':''}>${v} km</option>`).join('')}
          </select>
        </label>
        <button type="button" id="airspace-refresh">REFRESH TRAFFIC</button>
        <div class="airspace-state">
          <span><small>SOURCE</small><b>${escLocal(t.provider||'ADS-B')}</b></span>
          <span><small>STATUS</small><b>${escLocal(statusText())}</b></span>
          <span><small>SITE</small><b>${escLocal(snapshot.site?.site_name||'Operation Site')}</b></span>
        </div>
      </div>
      <div class="airspace-layout">
        <div class="airspace-map" id="airspace-map"><div class="map-label map-radius">RADIUS ${radiusKm} km</div><div class="map-label map-attribution">© OpenStreetMap contributors · ADS-B observational data</div></div>
        <div class="airspace-side">
          <div class="airspace-summary">
            <div class="metric"><small>OBSERVED TARGETS</small><strong>${aircraft.length}</strong><em>within ${radiusKm} km</em></div>
            <div class="metric"><small>NEAREST</small><strong>${nearest==null?'—':fmt(nearest,1,' km')}</strong><em>${aircraft[0]?.callsign||'no observed target'}</em></div>
            <div class="metric"><small>AIRSPACE STATUS</small><strong>UNVERIFIED</strong><em>CAA/AIS confirmation required</em></div>
          </div>
          <div class="airspace-table-wrap">
            ${aircraft.length?`<table class="airspace-table"><thead><tr><th>CALLSIGN</th><th>DIST</th><th>ALT</th><th>SPEED</th><th>TRACK</th></tr></thead><tbody>${aircraft.slice(0,30).map(a=>{const c=a.distance_km<=10?'distance-critical':a.distance_km<=25?'distance-near':'';return `<tr data-aircraft-row="${escLocal(a.hex)}"><td><code>${escLocal(a.callsign)}</code><br><small>${escLocal(a.aircraft_type||'—')}</small></td><td class="${c}">${fmt(a.distance_km,1,' km')}</td><td>${fmt(a.altitude_ft,0,' ft')}</td><td>${fmt(a.ground_speed_kmh,0,' km/h')}</td><td>${fmt(a.track_deg,0,'°')}</td></tr>`}).join('')}</tbody></table>`:'<div class="airspace-empty">NO ADS-B / MLAT TARGETS OBSERVED IN THIS RADIUS</div>'}
          </div>
          <div class="airspace-warning"><b>OBSERVATIONAL ONLY.</b> No observed target does not mean the airspace is clear. Live traffic data must not replace CAA/AIS/ATC coordination or NOTAM verification.</div>
        </div>
      </div>`;
    return panelShell(item,body,'ADS-B / MLAT · SITUATIONAL AWARENESS');
  }

  function worldPixel(lat,lon,z){
    const scale=256*Math.pow(2,z);
    const x=(lon+180)/360*scale;
    const sin=Math.sin(lat*Math.PI/180);
    const y=(0.5-Math.log((1+sin)/(1-sin))/(4*Math.PI))*scale;
    return {x,y};
  }
  function zoomForRadius(r){if(r<=12)return 11;if(r<=28)return 10;if(r<=60)return 9;if(r<=120)return 8;return 7;}

  function drawMap(){
    const map=document.getElementById('airspace-map');
    const t=traffic();
    const site=snapshot?.site;
    if(!map||!t||site?.latitude==null||site?.longitude==null)return;
    map.querySelectorAll('.osm-tile,.airspace-ring,.site-marker,.aircraft-marker,.aircraft-popover').forEach(n=>n.remove());
    const w=map.clientWidth||720,h=map.clientHeight||340,z=zoomForRadius(radiusKm);
    const center=worldPixel(Number(site.latitude),Number(site.longitude),z);
    const left=center.x-w/2,top=center.y-h/2;
    const minTx=Math.floor(left/256)-1,maxTx=Math.floor((left+w)/256)+1,minTy=Math.floor(top/256)-1,maxTy=Math.floor((top+h)/256)+1,n=Math.pow(2,z);
    for(let tx=minTx;tx<=maxTx;tx++)for(let ty=minTy;ty<=maxTy;ty++){
      if(ty<0||ty>=n)continue;
      const wrapped=((tx%n)+n)%n,img=document.createElement('img');img.className='osm-tile';img.alt='';img.draggable=false;img.src=`/api/airspace/tile/${z}/${wrapped}/${ty}.png`;img.style.left=`${tx*256-left}px`;img.style.top=`${ty*256-top}px`;map.appendChild(img);
    }
    const metersPerPixel=156543.03392*Math.cos(Number(site.latitude)*Math.PI/180)/Math.pow(2,z);
    const ring=document.createElement('div');ring.className='airspace-ring';const diameter=(radiusKm*2000)/metersPerPixel;ring.style.width=`${diameter}px`;ring.style.height=`${diameter}px`;ring.style.left=`${w/2}px`;ring.style.top=`${h/2}px`;map.appendChild(ring);
    const siteMarker=document.createElement('div');siteMarker.className='site-marker';siteMarker.style.left=`${w/2}px`;siteMarker.style.top=`${h/2}px`;siteMarker.title=site.site_name||'Operation Site';map.appendChild(siteMarker);
    (t.aircraft||[]).forEach(a=>{
      const p=worldPixel(a.lat,a.lon,z),x=p.x-left,y=p.y-top;if(x<-20||x>w+20||y<-20||y>h+20)return;
      const b=document.createElement('button');b.type='button';b.className='aircraft-marker '+(a.distance_km<=10?'critical':a.distance_km<=25?'near':'');b.textContent='✈';b.style.left=`${x}px`;b.style.top=`${y}px`;b.style.rotate=`${Number(a.track_deg||0)}deg`;b.title=`${a.callsign} · ${fmt(a.distance_km,1,' km')}`;
      b.onclick=e=>{e.stopPropagation();map.querySelectorAll('.aircraft-popover').forEach(n=>n.remove());const pop=document.createElement('div');pop.className='aircraft-popover';pop.style.left=`${Math.min(w-205,Math.max(5,x+12))}px`;pop.style.top=`${Math.min(h-150,Math.max(5,y+12))}px`;pop.innerHTML=`<b>${escLocal(a.callsign)} · ${escLocal(a.aircraft_type)}</b><div><span>Distance</span><span>${fmt(a.distance_km,1,' km')}</span></div><div><span>Altitude</span><span>${fmt(a.altitude_ft,0,' ft')}</span></div><div><span>Speed</span><span>${fmt(a.ground_speed_kmh,0,' km/h')}</span></div><div><span>Track</span><span>${fmt(a.track_deg,0,'°')}</span></div><div><span>Registration</span><span>${escLocal(a.registration)}</span></div>`;map.appendChild(pop);};map.appendChild(b);
    });
    map.onclick=()=>map.querySelectorAll('.aircraft-popover').forEach(n=>n.remove());
  }

  async function loadTraffic(force=false){
    const serial=++requestSerial;
    loading=true;
    snapshot=null;
    if(document.querySelector('[data-panel="airspace"]'))renderWorkspace();
    const controller=new AbortController();
    const fetchPromise=fetch(`/api/airspace/traffic?radius_km=${radiusKm}${force?'&refresh=1':''}`,{cache:'no-store',signal:controller.signal});
    const timeoutPromise=new Promise((_,reject)=>setTimeout(()=>reject(new Error('CLIENT_TIMEOUT')),10000));
    try{
      const r=await Promise.race([fetchPromise,timeoutPromise]);
      if(serial!==requestSerial)return;
      const type=r.headers.get('content-type')||'';
      if(!type.includes('application/json'))throw new Error(`Traffic API returned HTTP ${r.status}`);
      const payload=await r.json();
      if(serial!==requestSerial)return;
      snapshot=payload;
      if(!r.ok&&snapshot?.ok!==true) snapshot={...snapshot,ok:false,status:snapshot.status||`HTTP ${r.status}`};
    }catch(e){
      if(serial!==requestSerial)return;
      controller.abort();
      snapshot={ok:false,status:e.message==='CLIENT_TIMEOUT'?'TIMEOUT':'UNAVAILABLE',message:e.message==='CLIENT_TIMEOUT'?'Live traffic request exceeded 10 seconds. Press RETRY. If this repeats, the local server cannot reach the external ADS-B provider.':e.message};
    }finally{
      if(serial===requestSerial)loading=false;
    }
    if(serial===requestSerial&&document.querySelector('[data-panel="airspace"]')){renderWorkspace();setTimeout(drawMap,0);}
  }

  document.addEventListener('change',e=>{if(e.target?.id==='airspace-radius'){radiusKm=Number(e.target.value)||50;loadTraffic(true);}});
  document.addEventListener('click',e=>{if(e.target?.id==='airspace-refresh')loadTraffic(true);});
  window.addEventListener('resize',()=>{if(document.querySelector('[data-panel="airspace"]'))drawMap();});

  PANEL_NAMES.airspace='AIRSPACE & LIVE TRAFFIC';
  renderers.airspace=airspacePanel;
  const requested=new URLSearchParams(location.search).get('panel');
  if(requested==='airspace'){document.body.classList.add('popout');layout=[{panel:'airspace',span:3,order:0}];}
  else if(!layout.some(x=>x.panel==='airspace')){
    const commandIndex=layout.findIndex(x=>x.panel==='command');
    const insertAt=commandIndex>=0?commandIndex+1:layout.length;
    layout.splice(insertAt,0,{panel:'airspace',span:2,order:insertAt});
    layout.forEach((x,i)=>x.order=i);
  }
  renderWorkspace();
  loadTraffic(false);
  refreshTimer=setInterval(()=>loadTraffic(false),15000);
})();