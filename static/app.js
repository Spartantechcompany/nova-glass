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
const api = p => fetch(p).then(r=>r.json());

/* ---------- tabs ---------- */
document.getElementById("tabs").addEventListener("click", e=>{
  const t = e.target.closest(".tab"); if(!t) return;
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".view").forEach(x=>x.classList.remove("active"));
  t.classList.add("active");
  $("#v-"+t.dataset.v).classList.add("active");
  if(t.dataset.v==="storage") loadStorage();
  if(t.dataset.v==="noc") loadDevices();
  if(t.dataset.v==="updates") loadUpdates();
  if(t.dataset.v==="alerts") loadAlerts();
  if(t.dataset.v==="nodes") loadNodeDetail();
  if(t.dataset.v==="insights") loadInsightMeta();
});

/* ---------- clock + hub health ---------- */
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

/* ---------- overview ---------- */
const HIST = {};                                  // metric -> [values] client cache
function pushHist(key,val,cap=60){ (HIST[key]=HIST[key]||[]).push(val);
  if(HIST[key].length>cap) HIST[key].shift(); }

async function loadOverview(){
  let nodes;
  try{ nodes = await api("/api/nodes"); $("#hubDot").classList.remove("off");
       $("#hubTxt").textContent="hub online"; }
  catch{ $("#hubDot").classList.add("off"); $("#hubTxt").textContent="hub offline"; return; }

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
    {t:"Red total", v:fmt(sum(net),2), u:"MB/s", s:SERIES.net, hk:"f.net", max:null,
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
    return `<div class="card node-card">
      <div class="nc-head"><div class="nc-name">
        <span class="dot ${n.online?'':'off'}"></span>${name}</div>
        <span class="chip">${n.online?'online':'offline'}</span></div>
      <div class="bars">
        ${bar("CPU",m["cpu.total"],SERIES.cpu.c)}
        ${bar("RAM",m["mem.used_pct"],SERIES.ram.c)}
        ${bar("GPU",m["gpu.util"],SERIES.gpu.c)}
        ${bar("Swap",m["swap.used_pct"],"#8a94a6")}
      </div>
      <div class="sub" style="margin-top:12px">
        <span>↓ ${fmt(m["net.total.rx"],2)} MB/s</span>
        <span>↑ ${fmt(m["net.total.tx"],2)} MB/s</span>
        <span>load ${fmt(m["cpu.load1"],2)}</span>
        ${m["gpu.temp"]!=null?`<span>${fmt(m["gpu.temp"],0)}°C</span>`:''}
      </div></div>`;
  }).join("") || `<p class="muted">Sin nodos aún. Inicia <code>nova_collector.py</code> en cada equipo.</p>`;
}

/* ---------- node detail (real history from server) ---------- */
async function loadNodeDetail(){
  const nodes = await api("/api/nodes");
  const names = Object.keys(nodes);
  $("#nodeDetail").innerHTML = names.map(n=>`
    <div class="card"><h3>${n} · historial (60 min)</h3>
      <div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(240px,1fr))" id="nd-${cssId(n)}"></div>
    </div>`).join("") || `<p class="muted">Sin nodos.</p>`;
  for(const n of names){
    const wrap = $("#nd-"+cssId(n));
    for(const [metric,s] of [["cpu.total",SERIES.cpu],["mem.used_pct",SERIES.ram],
                             ["net.total.rx",SERIES.net],["gpu.util",SERIES.gpu]]){
      const d = await api(`/api/series?node=${encodeURIComponent(n)}&metric=${metric}&mins=60`);
      const pts = d.points.map(p=>p.v);
      const div=document.createElement("div"); div.className="metric-row";
      div.innerHTML=`<div class="ml"><span>${s.label} · ${metric}</span>
        <b>${pts.length?fmt(pts[pts.length-1]):"—"}</b></div>
        <div class="spark"></div>`;
      wrap.appendChild(div);
      spark(div.querySelector(".spark"), pts, s.c,
        metric.includes("net")?null:100);
    }
  }
}
const cssId = s => s.replace(/[^a-z0-9]/gi,"_");

/* ---------- network ---------- */
async function loadNetwork(){
  const nodes = await api("/api/nodes");
  $("#netCards").innerHTML = Object.entries(nodes).map(([name,n])=>{
    const ifaces={};
    for(const [k,v] of Object.entries(n.metrics)){
      const mm = k.match(/^net\.(.+)\.(rx|tx)$/);
      if(mm && mm[1]!=="total"){ (ifaces[mm[1]]=ifaces[mm[1]]||{})[mm[2]]=v; }
    }
    const rows = Object.entries(ifaces).map(([ifn,d])=>`
      <div class="metric-row" style="margin-bottom:10px">
        <div class="ml"><span>${ifn}</span>
          <b style="color:${SERIES.net.c}">↓${fmt(d.rx,2)} ↑${fmt(d.tx,2)} MB/s</b></div>
        <div class="track"><div class="fill" style="width:${Math.min((d.rx+d.tx)*4,100)}%;background:${SERIES.net.c}"></div></div>
      </div>`).join("") || `<p class="muted">sin interfaces</p>`;
    return `<div class="card"><div class="nc-head"><div class="nc-name">
      <span class="dot ${n.online?'':'off'}"></span>${name}</div></div>${rows}</div>`;
  }).join("") || `<p class="muted">Sin datos de red.</p>`;
}

/* ---------- storage (movible: cada bloque es una card reordenable) ---------- */
async function loadStorage(){
  const nodes = await api("/api/nodes");
  let html="";
  for(const name of Object.keys(nodes)){
    const mounts = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=mounts`)).data||[];
    const users  = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=users_storage`)).data||[];
    const mrows = mounts.map(m=>{
      const cls = m.pct>=90?"b-crit":m.pct>=75?"b-warn":"b-ok";
      return `<tr><td><code>${m.mount}</code></td><td>${m.fs}</td>
        <td>${fmt(m.used_gb,0)}/${fmt(m.total_gb,0)} GB</td>
        <td><div class="track" style="width:120px"><div class="fill"
          style="width:${m.pct}%;background:var(--${m.pct>=90?'crit':m.pct>=75?'warn':'ok'})"></div></div></td>
        <td><span class="badge ${cls}">${m.pct}%</span></td></tr>`;
    }).join("");
    const urows = users.map(u=>`<tr><td>${u.user}</td>
      <td style="color:${SERIES.ram.c};font-weight:700">${u.gb} GB</td></tr>`).join("")
      || `<tr><td colspan=2 class="muted">sin datos (revisa user_storage_root)</td></tr>`;
    html += `<div class="card"><h3>💽 ${name}</h3>
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

/* ---------- NOC devices ---------- */
async function loadDevices(){
  const d = await api("/api/devices");
  const tb = $("#devTable tbody");
  tb.innerHTML = d.map(x=>{
    const cls = x.ok?"b-ok":"b-crit";
    const ago = x.ts? Math.round(Date.now()/1000-x.ts)+"s":"—";
    return `<tr><td><span class="badge ${cls}">${x.ok?'UP':'DOWN'}</span></td>
      <td><b>${x.name}</b></td><td><code>${x.host}</code></td>
      <td>${x.kind}</td><td>${x.ok?fmt(x.latency_ms,1)+' ms':'—'}</td>
      <td class="muted">${x.detail||''}</td><td class="muted">${ago}</td></tr>`;
  }).join("") || `<tr><td colspan=7 class="muted">Sin dispositivos configurados.</td></tr>`;
}

/* ---------- updates / CVE inventario propio ---------- */
async function loadUpdates(){
  const nodes = await api("/api/nodes");
  let html="";
  for(const name of Object.keys(nodes)){
    const up  = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=updates`)).data||{};
    const sec = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=security_updates`)).data||{};
    const fw  = (await api(`/api/meta?node=${encodeURIComponent(name)}&key=firmware_updates`)).data||[];
    const secN = sec.count||0, upN = up.count||0, fwN = fw.length;
    const secCls = secN>0?"b-crit":"b-ok";
    html += `<div class="card"><h3>📦 ${name}</h3>
      <div class="grid" style="grid-template-columns:repeat(3,1fr);gap:14px;margin-bottom:14px">
        <div class="card" style="background:var(--panel2)"><h3>Seguridad (CVE)</h3>
          <div class="big" style="font-size:30px">${secN}<span class="unit">pkgs</span></div>
          <span class="badge ${secCls}" style="margin-top:8px;display:inline-block">${secN>0?'atender':'al día'}</span></div>
        <div class="card" style="background:var(--panel2)"><h3>Paquetes</h3>
          <div class="big" style="font-size:30px">${upN}<span class="unit">pkgs</span></div></div>
        <div class="card" style="background:var(--panel2)"><h3>Firmware</h3>
          <div class="big" style="font-size:30px">${fwN}<span class="unit">upd</span></div></div>
      </div>
      ${secN? `<div class="sec-title" style="margin:6px 0 8px">🔒 Parches de seguridad</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${(sec.packages||[]).map(p=>`<span class="chip" style="color:var(--crit)">${p}</span>`).join("")}</div>`:''}
      ${upN? `<div class="sec-title" style="margin:14px 0 8px">Otros paquetes</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${(up.packages||[]).map(p=>`<span class="chip">${p}</span>`).join("")}</div>`:''}
      ${fwN? `<div class="sec-title" style="margin:14px 0 8px">Firmware</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${fw.map(p=>`<span class="chip" style="color:var(--warn)">${p}</span>`).join("")}</div>`:''}
      ${(!secN&&!upN&&!fwN)?'<p class="muted">Sistema al día ✅ (o collector aún recopilando — ciclo lento cada 5 min).</p>':''}
    </div>`;
  }
  $("#updatesWrap").innerHTML = html || `<p class="muted">Sin datos.</p>`;
}

/* ---------- alerts ---------- */
async function loadAlerts(){
  const a = await api("/api/alerts?limit=120");
  const open = a.filter(x=>x.state==="open").length;
  const badge=$("#alertBadge");
  if(open){ badge.style.display="inline-block"; badge.textContent=open; }
  else badge.style.display="none";
  $("#alertTable tbody").innerHTML = a.map(x=>{
    const sev = x.severity==="critical"?"b-crit":x.severity==="warning"?"b-warn":"b-info";
    const st  = x.state==="open"?"b-crit":"b-ok";
    return `<tr><td><span class="badge ${sev}">${x.severity}</span></td>
      <td><span class="badge ${st}">${x.state}</span></td>
      <td>${x.node}</td><td><b>${x.title}</b></td>
      <td class="muted">${x.msg||''}</td>
      <td class="muted">${new Date(x.ts*1000).toLocaleString("es-MX",{hour12:false})}</td></tr>`;
  }).join("") || `<tr><td colspan=6 class="muted">Sin alertas. Todo en orden ✅</td></tr>`;
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
      if(r.ts) $("#its-"+c.dataset.k).textContent =
        "último: "+new Date(r.ts*1000).toLocaleString("es-MX",{hour12:false});
    });
  });
}
async function runInsight(kind){
  const rep=$("#report");
  rep.innerHTML=`<p class="muted">✨ NOVA está investigando la telemetría con la IA de la factory… (puede tardar ~10-30s)</p>`;
  try{
    const r = await fetch(`/api/insight/${kind}`,{method:"POST"}).then(x=>x.json());
    if(r.report){
      rep.innerHTML = window.marked ? marked.parse(r.report)
        : `<pre style="white-space:pre-wrap">${r.report}</pre>`;
      loadInsightMeta();
    } else rep.innerHTML=`<p class="muted">Sin reporte.</p>`;
  }catch(e){ rep.innerHTML=`<p style="color:var(--crit)">Error: ${e}. ¿LLM en :8003 arriba?</p>`; }
}

/* ---------- loop ---------- */
loadOverview(); setInterval(loadOverview, 5000);
setInterval(()=>{ const v=document.querySelector(".tab.active").dataset.v;
  if(v==="network") loadNetwork();
  if(v==="noc") loadDevices();
  if(v==="alerts") loadAlerts();
}, 5000);
