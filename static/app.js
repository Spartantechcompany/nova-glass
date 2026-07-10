/* NOVA Glass — frontend controller
   Paleta de series validada (CVD-safe, dark surface #151c28):
     CPU #5b8def · RAM #c07617 · NET #0f9e8c · GPU #a371f7
   El acento teal (#00e5c7) es solo UI, nunca serie de datos. */
const SERIES = {
  cpu:  {c:"#5b8def", label:"CPU"},
  ram:  {c:"#c07617", label:"RAM"},
  net:  {c:"#0f9e8c", label:"Red"},
  gpu:  {c:"#a371f7", label:"GPU"},
  disk: {c:"#6e7be8", label:"Disco"},
};
const INSIGHTS = [
  {k:"summary",     ico:"🧭", t:"Resumen de Infraestructura", d:"Salud general, disponibilidad y qué atender ahora."},
  {k:"anomaly",     ico:"🔍", t:"Análisis de Anomalías",      d:"Métricas fuera de patrón y causas probables correlacionadas."},
  {k:"capacity",    ico:"📈", t:"Capacity Planning",          d:"Proyección de recursos y cuándo ampliar."},
  {k:"performance", ico:"⚡", t:"Optimización de Rendimiento", d:"Desperdicio, saturación y acciones concretas."},
];
const $ = s => document.querySelector(s);
const fmt = (n,d=1)=> (n==null||isNaN(n)) ? "—" : Number(n).toFixed(d);
const esc = s => String(s??"").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const api = p => fetch(p).then(r=>r.json());
const post = (p,body) => fetch(p,{method:"POST",headers:{"Content-Type":"application/json"},
  body: body?JSON.stringify(body):undefined}).then(r=>r.json());
const del = p => fetch(p,{method:"DELETE"}).then(r=>r.json());
const MBs2Mbps = v => v==null ? null : v * 8.388608;   // MiB/s → Mbit/s
const when = ts => new Date(ts*1000).toLocaleString("es-MX",{hour12:false});

/* ---------- tabs ---------- */
document.getElementById("tabs").addEventListener("click", e=>{
  const t = e.target.closest(".tab"); if(!t) return;
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
  t.classList.add("active");
  $("#v-"+t.dataset.v).classList.add("active");
  const v=t.dataset.v;
  if(v==="storage") loadStorage();
  if(v==="noc") loadDevices();
  if(v==="updates") loadUpdates();
  if(v==="logs") loadLogs();
  if(v==="alerts"){ loadAlerts(); loadThresholds(); }
  if(v==="nodes") loadNodeDetail();
  if(v==="agents") loadAgents();
  if(v==="insights"){ loadInsightMeta(); loadInsightHistory(); }
  if(v==="network") loadNetwork();
});

/* ---------- clock ---------- */
setInterval(()=>{ $("#clock").textContent =
  new Date().toLocaleString("es-MX",{hour12:false}); }, 1000);

/* ---------- sparkline (area, baseline-anchored) ---------- */
function spark(el, pts, color, max){
  const w=el.clientWidth||260, h=44, n=pts.length;
  if(n<2){ el.innerHTML=""; return; }
  const mx = max || Math.max(...pts, 1);
  const X = i => (i/(n-1))*w;
  const Y = v => h-2 - (Math.min(v,mx)/mx)*(h-4);
  let d=`M0 ${h} L${X(0)} ${Y(pts[0])}`;
  pts.forEach((v,i)=> d+=` L${X(i)} ${Y(v)}`);
  d+=` L${w} ${h} Z`;
  let line=`M${X(0)} ${Y(pts[0])}`;
  pts.forEach((v,i)=> line+=` L${X(i)} ${Y(v)}`);
  const id="g"+Math.random().toString(36).slice(2,7);
  el.innerHTML=`<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${color}" stop-opacity=".35"/>
      <stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${d}" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="2"
      stroke-linejoin="round" stroke-linecap="round"/></svg>`;
}

/* ---------- interactive chart with hover tooltip (Nodos) ---------- */
function chart(el, points, color, max, unit){
  // points: [{t, v}]  — renders area chart + crosshair tooltip with exact ts/value
  const w = el.clientWidth||520, h = 120, n = points.length;
  el.classList.add("chart");
  if(n<2){ el.innerHTML=`<p class="muted">sin datos en este rango</p>`; return; }
  const vals = points.map(p=>p.v);
  const mx = max || Math.max(...vals, 1e-6)*1.1;
  const X = i => (i/(n-1))*w;
  const Y = v => h-3 - (Math.min(v,mx)/mx)*(h-8);
  let area=`M0 ${h} L${X(0)} ${Y(vals[0])}`;
  vals.forEach((v,i)=> area+=` L${X(i)} ${Y(v)}`);
  area+=` L${w} ${h} Z`;
  let line=`M${X(0)} ${Y(vals[0])}`;
  vals.forEach((v,i)=> line+=` L${X(i)} ${Y(v)}`);
  const id="c"+Math.random().toString(36).slice(2,7);
  el.innerHTML=`<svg viewBox="0 0 ${w} ${h}" preserveAspectRatio="none">
    <defs><linearGradient id="${id}" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${color}" stop-opacity=".3"/>
      <stop offset="1" stop-color="${color}" stop-opacity="0"/></linearGradient></defs>
    <path d="${area}" fill="url(#${id})"/>
    <path d="${line}" fill="none" stroke="${color}" stroke-width="1.8"
      stroke-linejoin="round" vector-effect="non-scaling-stroke"/>
    <circle class="pt" r="3.5" fill="${color}" stroke="#0a0e14" stroke-width="1.5" style="display:none"/>
  </svg>
  <div class="xhair"></div><div class="tip"></div>`;
  const svg=el.querySelector("svg"), tip=el.querySelector(".tip"),
        xh=el.querySelector(".xhair"), pt=el.querySelector("circle.pt");
  el.onmousemove = e=>{
    const r = el.getBoundingClientRect();
    const fx = (e.clientX - r.left)/r.width;
    const i = Math.max(0, Math.min(n-1, Math.round(fx*(n-1))));
    const p = points[i];
    const px = X(i)/w*r.width;
    xh.style.display="block"; xh.style.left=px+"px";
    pt.style.display="block"; pt.setAttribute("cx",X(i)); pt.setAttribute("cy",Y(p.v));
    tip.style.display="block";
    tip.innerHTML=`<b style="color:${color}">${fmt(p.v,2)}${unit||""}</b> · ${when(p.t)}`;
    const tw = tip.offsetWidth;
    tip.style.left = Math.min(Math.max(px - tw/2, 0), r.width - tw)+"px";
    tip.style.top = (Y(p.v)/h*r.height - 34)+"px";
  };
  el.onmouseleave = ()=>{ tip.style.display=xh.style.display=pt.style.display="none"; };
}

/* ---------- overview (con latido por dato nuevo) ---------- */
const HIST = {};                                  // metric -> [values] client cache
let LAST_TS = {};                                 // node -> last_sample_ts
function pushHist(key,val,cap=60){ (HIST[key]=HIST[key]||[]).push(val);
  if(HIST[key].length>cap) HIST[key].shift(); }

async function loadOverview(){
  let nodes;
  try{ nodes = await api("/api/nodes"); $("#hubDot").classList.remove("off");
       $("#hubTxt").textContent="hub online"; }
  catch{ $("#hubDot").classList.add("off"); $("#hubTxt").textContent="hub offline"; return; }

  // heartbeat: pulse when any node delivered a fresh sample since last poll
  const fresh = new Set();
  for(const [name,n] of Object.entries(nodes)){
    if(n.last_sample_ts && n.last_sample_ts !== LAST_TS[name]) fresh.add(name);
    LAST_TS[name] = n.last_sample_ts;
  }
  if(fresh.size){
    const orb=$("#orb"); orb.classList.remove("beat"); void orb.offsetWidth; orb.classList.add("beat");
    $("#lastBeat").textContent = "♥ dato recibido "+new Date().toLocaleTimeString("es-MX",{hour12:false});
  }

  const online = Object.values(nodes).filter(n=>n.online).length;
  const total  = Object.keys(nodes).length;

  // fleet aggregates
  let cpu=[],mem=[],net=[],gpu=[],gpuN=0;
  for(const n of Object.values(nodes)){
    const m=n.metrics;
    if(m["cpu.total"]!=null) cpu.push(m["cpu.total"]);
    if(m["mem.used_pct"]!=null) mem.push(m["mem.used_pct"]);
    const r=(m["net.total.rx"]||0)+(m["net.total.tx"]||0); net.push(r);
    if(m["gpu.util"]!=null){ gpu.push(m["gpu.util"]); gpuN++; }
  }
  const avg=a=>a.length? a.reduce((x,y)=>x+y,0)/a.length : 0;
  const sum=a=>a.reduce((x,y)=>x+y,0);
  pushHist("f.cpu",avg(cpu)); pushHist("f.mem",avg(mem));
  pushHist("f.net",sum(net)); pushHist("f.gpu",avg(gpu));

  const tiles=[
    {t:"CPU fleet (avg)", v:fmt(avg(cpu)), u:"%",  s:SERIES.cpu, hk:"f.cpu", max:100,
     sub:`${total} nodos · ${online} online`},
    {t:"RAM fleet (avg)", v:fmt(avg(mem)), u:"%",  s:SERIES.ram, hk:"f.mem", max:100,
     sub:"memoria usada"},
    {t:"Red total", v:fmt(MBs2Mbps(sum(net)),1), u:"Mbps", s:SERIES.net, hk:"f.net", max:null,
     sub:"rx+tx todas las ifaces"},
    {t:"GPU (avg)", v: gpuN?fmt(avg(gpu)):"—", u:"%", s:SERIES.gpu, hk:"f.gpu", max:100,
     sub:`${gpuN} GPU activas`},
  ];
  $("#fleetMetrics").innerHTML = tiles.map(x=>`
    <div class="card">
      <h3><span style="width:8px;height:8px;border-radius:2px;background:${x.s.c};display:inline-block"></span>${x.t}</h3>
      <div class="big">${x.v}<span class="unit">${x.u}</span></div>
      <div class="spark" data-hk="${x.hk}" data-c="${x.s.c}" data-max="${x.max||''}"></div>
      <div class="sub"><span>${x.sub}</span></div>
    </div>`).join("");
  document.querySelectorAll("#fleetMetrics .spark").forEach(el=>{
    const h=HIST[el.dataset.hk]||[]; spark(el,h,el.dataset.c,
      el.dataset.max?+el.dataset.max:null);
  });

  // node cards
  $("#nodeCards").innerHTML = Object.entries(nodes).map(([name,n])=>{
    const m=n.metrics;
    const bar=(lbl,val,color,unit="%")=>{
      const pct = unit==="%" ? Math.min(val||0,100) : 0;
      return `<div class="metric-row"><div class="ml"><span>${lbl}</span>
        <b>${fmt(val)}${unit}</b></div>
        <div class="track"><div class="fill" style="width:${pct}%;background:${color}"></div></div></div>`;
    };
    return `<div class="card node-card" data-node="${esc(name)}">
      <div class="nc-head"><div class="nc-name">
        <span class="dot ${n.online?'':'off'}"></span>${esc(name)}</div>
        <span class="chip">${n.online?'online':'offline'}</span></div>
      <div class="bars">
        ${bar("CPU",m["cpu.total"],SERIES.cpu.c)}
        ${bar("RAM",m["mem.used_pct"],SERIES.ram.c)}
        ${bar("GPU",m["gpu.util"],SERIES.gpu.c)}
        ${bar("Swap",m["swap.used_pct"],"#8a94a6")}
      </div>
      <div class="sub" style="margin-top:12px">
        <span>↓ ${fmt(MBs2Mbps(m["net.total.rx"]),1)} Mbps</span>
        <span>↑ ${fmt(MBs2Mbps(m["net.total.tx"]),1)} Mbps</span>
        <span>load ${fmt(m["cpu.load1"],2)}</span>
        ${m["gpu.temp"]!=null?`<span>${fmt(m["gpu.temp"],0)}°C</span>`:''}
      </div></div>`;
  }).join("") || `<p class="muted">Sin nodos aún. Usa «＋ Agregar nodo» o inicia <code>nova_collector.py</code> en cada equipo.</p>`;
  // pulse cards whose node just delivered data
  fresh.forEach(name=>{
    const c=document.querySelector(`#nodeCards .card[data-node="${CSS.escape(name)}"]`);
    if(c){ c.classList.remove("beat"); void c.offsetWidth; c.classList.add("beat"); }
  });
}

/* ---------- add-node modal ---------- */
const RANGE_OPTS=[["15 min",15],["1 h",60],["6 h",360],["24 h",1440],["2 días",2880]];
function nmRender(){
  const name=$("#nmName").value.trim()||"nuevo-nodo";
  const tok=$("#nmToken").value.trim();
  $("#nmCmd").textContent =
`# en el nodo nuevo (requiere python3 + psutil):
pip install psutil
NOVA_HUB=${location.origin} NOVA_NODE=${name}${tok?` NOVA_TOKEN=${tok}`:""} \\
  python3 nova_collector.py`;
}
$("#addNodeBtn").addEventListener("click",()=>{ nmRender(); $("#nodeModal").classList.add("open"); });
$("#nmClose").addEventListener("click",()=> $("#nodeModal").classList.remove("open"));
$("#nodeModal").addEventListener("click",e=>{ if(e.target.id==="nodeModal") $("#nodeModal").classList.remove("open"); });
$("#nmName").addEventListener("input",nmRender);
$("#nmToken").addEventListener("input",nmRender);
$("#nmCopy").addEventListener("click",()=>{
  navigator.clipboard.writeText($("#nmCmd").textContent);
  $("#nmCopy").textContent="Copiado ✓"; setTimeout(()=>$("#nmCopy").textContent="Copiar comando",1500);
});

/* ---------- node detail: gráficas interactivas con hover tooltip + rango ---------- */
let NODE_RANGE = +(localStorage.getItem("nova.range")||60);
$("#rangeBar").innerHTML = RANGE_OPTS.map(([l,m])=>
  `<button class="range-btn ${m===NODE_RANGE?'active':''}" data-m="${m}">${l}</button>`).join("");
$("#rangeBar").addEventListener("click",e=>{
  const b=e.target.closest(".range-btn"); if(!b) return;
  NODE_RANGE=+b.dataset.m; localStorage.setItem("nova.range",NODE_RANGE);
  document.querySelectorAll(".range-btn").forEach(x=>x.classList.toggle("active",+x.dataset.m===NODE_RANGE));
  loadNodeDetail();
});

async function loadNodeDetail(){
  const nodes = await api("/api/nodes");
  const names = Object.keys(nodes);
  const lbl = RANGE_OPTS.find(([,m])=>m===NODE_RANGE)?.[0] || NODE_RANGE+" min";
  $("#nodeDetail").innerHTML = names.map(n=>`
    <div class="card"><h3>${esc(n)} · historial (${lbl})</h3>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(320px,1fr))" id="nd-${cssId(n)}"></div>
    </div>`).join("") || `<p class="muted">Sin nodos.</p>`;
  for(const n of names){
    const wrap = $("#nd-"+cssId(n));
    for(const [metric,s,unit,max] of [["cpu.total",SERIES.cpu,"%",100],
                                      ["mem.used_pct",SERIES.ram,"%",100],
                                      ["net.total.rx",SERIES.net," MB/s",null],
                                      ["gpu.util",SERIES.gpu,"%",100]]){
      const d = await api(`/api/series?node=${encodeURIComponent(n)}&metric=${metric}&mins=${NODE_RANGE}`);
      const div=document.createElement("div"); div.className="metric-row";
      const last = d.points.length? d.points[d.points.length-1].v : null;
      div.innerHTML=`<div class="ml"><span>${s.label} · ${metric}</span>
        <b>${fmt(last)}${unit.trim()}</b></div><div class="chart-sm"></div>`;
      wrap.appendChild(div);
      chart(div.querySelector(".chart-sm"), d.points, s.c, max, unit);
    }
  }
}
const cssId = s => s.replace(/[^a-z0-9]/gi,"_");

/* ---------- network (Mbps + link) ---------- */
async function loadNetwork(){
  const nodes = await api("/api/nodes");
  let html="";
  for(const [name,n] of Object.entries(nodes)){
    const ifmeta = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=ifaces`)).data||{};
    const ifaces={};
    for(const [k,v] of Object.entries(n.metrics)){
      const mm = k.match(/^net\.(.+)\.(rx|tx)$/);
      if(mm && mm[1]!=="total"){ (ifaces[mm[1]]=ifaces[mm[1]]||{})[mm[2]]=v; }
    }
    const rows = Object.entries(ifaces).map(([ifn,d])=>{
      const info = ifmeta[ifn]||{};
      const speed = info.speed_mbps>0 ? info.speed_mbps : null;
      const rxM=MBs2Mbps(d.rx)||0, txM=MBs2Mbps(d.tx)||0;
      const pct = speed ? Math.min((rxM+txM)/speed*100,100) : Math.min((rxM+txM)/10,100);
      const link = speed ? `link ${speed>=1000?(speed/1000)+" Gb/s":speed+" Mb/s"}` : "link ?";
      const state = info.up===false ? `<span class="badge b-crit">down</span>` : "";
      return `<div class="metric-row" style="margin-bottom:10px">
        <div class="ml"><span><code>${esc(ifn)}</code> <span class="chip">${link}</span>${info.mtu?` <span class="chip">mtu ${info.mtu}</span>`:""} ${state}</span>
          <b style="color:${SERIES.net.c}">↓${fmt(rxM,1)} ↑${fmt(txM,1)} Mbps</b></div>
        <div class="track"><div class="fill" style="width:${pct}%;background:${SERIES.net.c}"></div></div>
        ${speed?`<div class="ml"><span class="muted">ocupación ${fmt(pct,1)}% del link</span></div>`:""}
      </div>`;
    }).join("") || `<p class="muted">sin interfaces</p>`;
    html += `<div class="card"><div class="nc-head"><div class="nc-name">
      <span class="dot ${n.online?'':'off'}"></span>${esc(name)}</div></div>${rows}</div>`;
  }
  $("#netCards").innerHTML = html || `<p class="muted">Sin datos de red.</p>`;
}

/* ---------- storage ---------- */
async function loadStorage(){
  const nodes = await api("/api/nodes");
  let html="";
  for(const name of Object.keys(nodes)){
    const mounts = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=mounts`)).data||[];
    const users  = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=users_storage`)).data||[];
    const mrows = mounts.map(m=>{
      const cls = m.pct>=90?"b-crit":m.pct>=75?"b-warn":"b-ok";
      return `<tr><td><code>${esc(m.mount)}</code></td><td>${esc(m.fs)}</td>
        <td>${fmt(m.used_gb,0)}/${fmt(m.total_gb,0)} GB</td>
        <td><div class="track" style="width:120px"><div class="fill"
          style="width:${m.pct}%;background:var(--${m.pct>=90?'crit':m.pct>=75?'warn':'ok'})"></div></div></td>
        <td><span class="badge ${cls}">${m.pct}%</span></td></tr>`;
    }).join("");
    const urows = users.map(u=>`<tr><td>${esc(u.user)}</td>
      <td style="color:${SERIES.ram.c};font-weight:700">${u.gb} GB</td></tr>`).join("")
      || `<tr><td colspan=2 class="muted">sin datos (revisa user_storage_root)</td></tr>`;
    html += `<div class="card"><h3>💽 ${esc(name)}</h3>
      <div class="grid" style="grid-template-columns:2fr 1fr;gap:20px">
        <div><div class="sec-title" style="margin:0 0 10px">Filesystems</div>
          <table><thead><tr><th>Mount</th><th>FS</th><th>Uso</th><th></th><th>%</th></tr></thead>
          <tbody>${mrows||'<tr><td colspan=5 class="muted">—</td></tr>'}</tbody></table></div>
        <div><div class="sec-title" style="margin:0 0 10px">Usuarios (GB)</div>
          <table><thead><tr><th>Usuario</th><th>Espacio</th></tr></thead>
          <tbody>${urows}</tbody></table></div>
      </div></div>`;
  }
  $("#storageWrap").innerHTML = html || `<p class="muted">Sin datos.</p>`;
}

/* ---------- NOC devices (registro en caliente) ---------- */
async function loadDevices(){
  const d = await api("/api/devices");
  const tb = $("#devTable tbody");
  tb.innerHTML = d.map(x=>{
    const cls = x.ok?"b-ok":"b-crit";
    const ago = x.ts? Math.round(Date.now()/1000-x.ts)+"s":"—";
    return `<tr><td><span class="badge ${cls}">${x.ok?'UP':'DOWN'}</span></td>
      <td><b>${esc(x.name)}</b></td><td><code>${esc(x.host)}</code></td>
      <td>${esc(x.kind)}</td><td>${x.ok?fmt(x.latency_ms,1)+' ms':'—'}</td>
      <td class="muted">${esc(x.detail||'')}</td><td class="muted">${ago}</td>
      <td>${x.dynamic?`<button class="btn small danger" data-del="${esc(x.name)}">✕</button>`:''}</td></tr>`;
  }).join("") || `<tr><td colspan=8 class="muted">Sin dispositivos configurados.</td></tr>`;
  tb.querySelectorAll("[data-del]").forEach(b=> b.addEventListener("click", async ()=>{
    await del(`/api/devices/${encodeURIComponent(b.dataset.del)}`); loadDevices();
  }));
}
$("#devAdd").addEventListener("click", async ()=>{
  const kind=$("#devKind").value, extra=$("#devExtra").value.trim();
  const body={name:$("#devName").value.trim(), host:$("#devHost").value.trim(), kind};
  if(kind==="tcp" && extra) body.port=+extra;
  if(kind==="http" && extra) body.url=extra;
  if(kind==="snmp" && extra) body.community=extra;
  const r = await fetch("/api/devices",{method:"POST",
    headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});
  const j = await r.json().catch(()=>({}));
  $("#devMsg").textContent = r.ok ? `✓ ${body.name} registrado, sondeando…`
    : `Error: ${j.detail||r.status}`;
  if(r.ok){ $("#devName").value=$("#devHost").value=$("#devExtra").value="";
    setTimeout(loadDevices, 1200); }
});

/* ---------- updates / CVE inventario propio ---------- */
async function loadUpdates(){
  const nodes = await api("/api/nodes");
  let html="";
  for(const name of Object.keys(nodes)){
    const upR  = await api(`/api/meta?node=${encodeURIComponent(name)}&key=updates`);
    const secR = await api(`/api/meta?node=${encodeURIComponent(name)}&key=security_updates`);
    const fwR  = await api(`/api/meta?node=${encodeURIComponent(name)}&key=firmware_updates`);
    const up=upR.data||{}, sec=secR.data||{}, fw=fwR.data||[];
    const secN = sec.count||0, upN = up.count||0, fwN = fw.length;
    const secCls = secN>0?"b-crit":"b-ok";
    const checked = secR.ts ? when(secR.ts) : (upR.ts? when(upR.ts):"—");
    const secRows = (sec.detail&&sec.detail.length)
      ? `<table style="margin-top:6px"><thead><tr><th>Paquete</th><th>Canal</th><th>Instalada</th><th>Disponible</th></tr></thead>
         <tbody>${sec.detail.map(p=>`<tr><td><code>${esc(p.pkg)}</code></td>
           <td class="muted">${esc(p.suite||'')}</td><td>${esc(p.old||'?')}</td>
           <td style="color:var(--crit);font-weight:700">${esc(p.new||'?')}</td></tr>`).join("")}</tbody></table>`
      : (secN? `<div style="display:flex;flex-wrap:wrap;gap:6px;margin-top:6px">${(sec.packages||[]).map(p=>`<span class="chip" style="color:var(--crit)">${esc(p)}</span>`).join("")}</div>`:'');
    html += `<div class="card"><h3>📦 ${esc(name)}
        <span class="chip" style="margin-left:auto">última verificación: ${checked}</span></h3>
      <div class="grid" style="grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px">
        <div class="card" style="background:var(--panel2)"><h3>Seguridad (CVE)</h3>
          <div class="big" style="font-size:30px">${secN}<span class="unit">pkgs</span></div>
          <span class="badge ${secCls}" style="margin-top:8px;display:inline-block">${secN>0?'atender':'al día'}</span></div>
        <div class="card" style="background:var(--panel2)"><h3>Paquetes</h3>
          <div class="big" style="font-size:30px">${upN}<span class="unit">pkgs</span></div></div>
        <div class="card" style="background:var(--panel2)"><h3>Firmware</h3>
          <div class="big" style="font-size:30px">${fwN}<span class="unit">upd</span></div></div>
      </div>
      ${secN? `<div class="sec-title" style="margin:6px 0 8px">🔒 Parches de seguridad pendientes</div>${secRows}
        <p class="muted" style="margin-top:8px">Detectados vía canal <code>-security</code> de apt. Los CVE exactos por paquete: <code>apt changelog &lt;pkg&gt;</code> en el nodo.</p>`:''}
      ${upN? `<div class="sec-title" style="margin:14px 0 8px">Otros paquetes</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${(up.packages||[]).map(p=>`<span class="chip">${esc(p)}</span>`).join("")}</div>`:''}
      ${fwN? `<div class="sec-title" style="margin:14px 0 8px">Firmware</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${fw.map(p=>`<span class="chip" style="color:var(--warn)">${esc(p)}</span>`).join("")}</div>`:''}
      ${(!secN&&!upN&&!fwN)?'<p class="muted">Sistema al día ✅ (o collector aún recopilando — ciclo lento cada 5 min).</p>':''}
    </div>`;
  }
  $("#updatesWrap").innerHTML = html || `<p class="muted">Sin datos.</p>`;
}

/* ---------- logs ---------- */
async function loadLogs(){
  const data = await api("/api/logs");
  const entries = Object.entries(data);
  $("#logsWrap").innerHTML = entries.map(([node,d])=>`
    <div class="card"><h3>📜 ${esc(node)}
      <span class="chip" style="margin-left:auto">${d.count_1h||0} errores/h · ${d.ts?when(d.ts):"—"}</span></h3>
      <div class="logbox">${(d.lines||[]).map(l=>{
        const bad=/(fail|error|crit|denied|segfault)/i.test(l);
        return `<div class="${bad?'err':''}">${esc(l)}</div>`;
      }).join("")||'<span class="muted">sin errores en la última hora ✅</span>'}</div>
    </div>`).join("") || `<p class="muted">Sin logs aún — el collector los envía en el ciclo lento (cada 5 min).</p>`;
}

/* ---------- alerts + thresholds ---------- */
async function loadAlerts(){
  const a = await api("/api/alerts?limit=120");
  const open = a.filter(x=>x.state==="open").length;
  const badge=$("#alertBadge");
  if(open){ badge.style.display="inline-block"; badge.textContent=open; }
  else badge.style.display="none";
  $("#alertTable tbody").innerHTML = a.map(x=>{
    const sev = x.severity==="critical"?"b-crit":x.severity==="warning"?"b-warn":"b-info";
    const st  = x.state==="open"?"b-crit":x.state==="acked"?"b-warn":"b-ok";
    return `<tr><td><span class="badge ${sev}">${x.severity}</span></td>
      <td><span class="badge ${st}">${x.state}</span></td>
      <td>${esc(x.node)}</td><td><b>${esc(x.title)}</b></td>
      <td class="muted">${esc(x.msg||'')}</td>
      <td class="muted">${when(x.ts)}</td>
      <td>${x.state==="open"?`<button class="btn small ghost" data-ack="${x.id}">descartar</button>`:''}</td></tr>`;
  }).join("") || `<tr><td colspan=7 class="muted">Sin alertas. Todo en orden ✅</td></tr>`;
  document.querySelectorAll("[data-ack]").forEach(b=> b.addEventListener("click", async ()=>{
    await post(`/api/alerts/${b.dataset.ack}/ack`); loadAlerts();
  }));
}

const TH_FIELDS=[["cpu_pct","CPU %"],["mem_pct","RAM %"],["swap_pct","Swap %"],
  ["gpu_temp","GPU °C"],["disk_pct","Disco %"],["anomaly_z","Z-score"],
  ["sustained_samples","Muestras sost."]];
async function loadThresholds(){
  const t = await api("/api/thresholds");
  $("#thForm").innerHTML = TH_FIELDS.map(([k,l])=>`
    <label>${l}<input type="number" step="any" id="th-${k}" style="width:96px"
      value="${t[k]?.override ?? ''}" placeholder="${t[k]?.config ?? ''}"></label>`).join("")
    + `<button class="btn" id="thSave">Guardar</button>`;
  $("#thSave").addEventListener("click", async ()=>{
    const body={};
    TH_FIELDS.forEach(([k])=>{ body[k] = $("#th-"+k).value===""? null : +$("#th-"+k).value; });
    await post("/api/thresholds", body);
    $("#thMsg").textContent="✓ Umbrales aplicados en runtime (persisten en la base). "+
      "Ej.: sube RAM % si tus nodos de inferencia mantienen ~100 GB residentes por diseño.";
    loadThresholds();
  });
}

/* ---------- agents ---------- */
async function loadAgents(){
  const ags = await api("/api/agents");
  $("#agentList").innerHTML = ags.map(a=>`
    <div class="card">
      <div class="nc-head"><div class="nc-name">🤖 ${esc(a.name)}</div>
        <div style="display:flex;gap:8px">
          <button class="btn small" data-run="${a.id}">▶ Ejecutar</button>
          <button class="btn small ghost" data-hist="${a.id}">historial</button>
          <button class="btn small danger" data-agdel="${a.id}">✕</button>
        </div></div>
      <p class="muted" style="font-size:12.5px;line-height:1.5">${esc(a.goal)}</p>
      <p class="muted" style="margin-top:8px">${a.last_run?`última corrida: ${when(a.last_run.ts)} · ${a.last_run.status}`:"nunca ejecutado"}</p>
    </div>`).join("") || `<p class="muted">Sin agentes. Crea el primero arriba — p. ej. «Vigía GPU» o «Auditor de capacidad».</p>`;
  document.querySelectorAll("[data-run]").forEach(b=>b.addEventListener("click",()=>runAgent(b.dataset.run)));
  document.querySelectorAll("[data-agdel]").forEach(b=>b.addEventListener("click",async()=>{
    await del(`/api/agents/${b.dataset.agdel}`); loadAgents(); }));
  document.querySelectorAll("[data-hist]").forEach(b=>b.addEventListener("click",async()=>{
    const runs = await api(`/api/agents/${b.dataset.hist}/runs`);
    const box=$("#agentReport"); box.style.display="block";
    $("#agentStream").style.display="none"; $("#streamTitle").style.display="none";
    box.innerHTML = runs.length? runs.map(r=>`
      <div style="border-bottom:1px solid var(--line);padding:10px 0">
        <div class="muted" style="margin-bottom:6px">${when(r.ts)} · ${r.status}</div>
        ${window.marked?marked.parse(r.report||""):`<pre style="white-space:pre-wrap">${esc(r.report)}</pre>`}
      </div>`).join("") : `<p class="muted">Sin corridas aún.</p>`;
  }));
}
$("#agCreate").addEventListener("click", async ()=>{
  const name=$("#agName").value.trim(), goal=$("#agGoal").value.trim();
  if(!name||!goal) return;
  await post("/api/agents",{name,goal});
  $("#agName").value=$("#agGoal").value="";
  loadAgents();
});

let CURRENT_ES=null;
function runAgent(id){
  if(CURRENT_ES) CURRENT_ES.close();
  const box=$("#agentStream"), rep=$("#agentReport");
  $("#streamTitle").style.display="flex";
  box.style.display="block"; rep.style.display="none";
  box.innerHTML=`<div class="st"><span class="ic">🛰</span><span class="pulse-dots">conectando con el agente</span></div>`;
  const es = new EventSource(`/api/agents/${id}/run`);
  CURRENT_ES=es;
  const add = html => { const d=document.createElement("div"); d.className="st"; d.innerHTML=html;
    box.appendChild(d); box.scrollTop=box.scrollHeight; };
  es.onmessage = e=>{
    const m = JSON.parse(e.data);
    if(m.type==="start"){ box.innerHTML="";
      add(`<span class="ic">🛰</span><span><b>${esc(m.agent)}</b> inicia misión: <span class="thought">${esc(m.goal)}</span></span>`); }
    if(m.type==="thought") add(`<span class="ic">💭</span><span class="thought">${esc(m.text)}</span>`);
    if(m.type==="tool") add(`<span class="ic">🔧</span><span class="tool">${esc(m.tool)}(${esc(JSON.stringify(m.args))})</span>`);
    if(m.type==="observation") add(`<span class="ic">📥</span><span class="obs">observación recibida (${m.size} bytes)</span>`);
    if(m.type==="error") add(`<span class="ic">⚠️</span><span class="err">${esc(m.msg)}</span>`);
    if(m.type==="final"){
      add(`<span class="ic">✅</span><span><b>Reporte final listo</b></span>`);
      rep.style.display="block";
      rep.innerHTML = window.marked? marked.parse(m.report||"") : `<pre style="white-space:pre-wrap">${esc(m.report)}</pre>`;
    }
    if(m.type==="end"){ es.close(); CURRENT_ES=null; loadAgents(); }
  };
  es.onerror = ()=>{ add(`<span class="ic">⚠️</span><span class="err">stream interrumpido</span>`); es.close(); CURRENT_ES=null; };
}

/* ---------- insights ---------- */
function loadInsightMeta(){
  $("#insightCards").innerHTML = INSIGHTS.map(x=>`
    <div class="card insight-card" data-k="${x.k}">
      <div class="ic-ico">${x.ico}</div>
      <div class="ic-t">${x.t}</div><div class="ic-d">${x.d}</div>
      <div class="muted" id="its-${x.k}" style="margin-top:10px">—</div>
    </div>`).join("");
  document.querySelectorAll(".insight-card").forEach(c=>{
    c.addEventListener("click",()=>runInsight(c.dataset.k));
    api(`/api/insight/${c.dataset.k}`).then(r=>{
      if(r.ts) $("#its-"+c.dataset.k).textContent = "último: "+when(r.ts);
    });
  });
}
async function loadInsightHistory(){
  const h = await api("/api/insights/history?limit=10");
  const names=Object.fromEntries(INSIGHTS.map(x=>[x.k,x.t]));
  $("#insightHistory").innerHTML = h.map(r=>`
    <div class="card" style="padding:12px 16px;cursor:pointer" data-hid="${r.id}">
      <div style="display:flex;gap:10px;align-items:center">
        <span class="badge b-info">${esc(names[r.kind]||r.kind)}</span>
        <span class="muted">${when(r.ts)}</span>
        <span class="muted" style="margin-left:auto">ver ↗</span></div>
    </div>`).join("") || `<p class="muted">Aún no hay reportes generados.</p>`;
  document.querySelectorAll("[data-hid]").forEach(c=>{
    const r = h.find(x=>x.id==+c.dataset.hid);
    c.addEventListener("click",()=>{
      $("#report").innerHTML = window.marked? marked.parse(r.report||"") :
        `<pre style="white-space:pre-wrap">${esc(r.report)}</pre>`;
      $("#report").scrollIntoView({behavior:"smooth"});
    });
  });
}
$("#llmPing").addEventListener("click", async ()=>{
  const st=$("#llmStatus");
  st.textContent="probando payload…";
  const r = await api("/api/llm/ping");
  st.innerHTML = r.ok
    ? `<span style="color:var(--ok)">● ${esc(r.model)} OK · ${r.latency_ms} ms</span>`
    : `<span style="color:var(--crit)">● falló (${esc(r.error||r.reply||"?")}) · ${esc(r.endpoint)}</span>`;
});
async function runInsight(kind){
  const rep=$("#report");
  rep.innerHTML=`<p class="muted">✨ NOVA está investigando la telemetría con la IA de la factory… (puede tardar ~10-30s)</p>`;
  try{
    const r = await fetch(`/api/insight/${kind}`,{method:"POST"}).then(x=>x.json());
    if(r.report){
      rep.innerHTML = window.marked ? marked.parse(r.report)
        : `<pre style="white-space:pre-wrap">${esc(r.report)}</pre>`;
      loadInsightMeta(); loadInsightHistory();
    } else rep.innerHTML=`<p class="muted">Sin reporte.</p>`;
  }catch(e){ rep.innerHTML=`<p style="color:var(--crit)">Error: ${esc(e)}. ¿LLM en :8003 arriba?</p>`; }
}

/* ---------- loop ---------- */
loadOverview(); setInterval(loadOverview, 5000);
setInterval(()=>{ const v=document.querySelector(".tab.active").dataset.v;
  if(v==="network") loadNetwork();
  if(v==="noc") loadDevices();
  if(v==="alerts") loadAlerts();
  if(v==="logs") loadLogs();
}, 5000);
