# 🛰 NOVA Glass

**Single pane of glass agéntico para una AI Factory.** Monitoreo persistente de
CPU · GPU · red · almacenamiento · logs · updates, con **análisis por IA** usando
el propio LLM de la factory, alertas por **webhook**, detección de **anomalías** y
un modo **NOC** para vigilar más equipos (ping / TCP / HTTP / SNMP).

Pensado para superar a un dashboard efímero: la historia **no se pierde al recargar
el navegador ni al reiniciar** — todo se guarda en SQLite (WAL).

![arquitectura](media/architecture.svg)

---

## ✨ Qué lo hace diferente

| | Dashboard típico (`:8080`) | **NOVA Glass** |
|---|---|---|
| Historial | se pierde al recargar el browser | persistente en SQLite (48h raw + 90d rollup) |
| Multi-nodo | uno a la vez | fleet completo en un panel |
| Análisis | manual | **IA agéntica** (Resumen · Anomalías · Capacity · Performance) |
| Alertas | ninguna | umbral + anomalía z-score → **webhooks** (Telegram/genérico) |
| Red | básica | throughput por interfaz, por nodo |
| Almacenamiento | — | filesystems + **uso por usuario** |
| Updates | — | paquetes + **CVE (parches de seguridad) + firmware** del inventario propio |
| NOC | — | agrega equipos externos (ping/tcp/http/snmp) |

## 🧩 Arquitectura

```
  ┌─ nova_collector.py ─┐   (un agente por nodo, solo stdlib + psutil)
  │  cpu·mem·net·gpu    │──┐
  │  disk·mounts·users  │  │  HTTP POST /api/ingest
  │  logs·updates·cve   │  │
  └─────────────────────┘  ▼
                    ┌──────────────────┐    SQLite WAL (nova.db)
                    │  nova_server.py  │◄── historial persistente
                    │  hub :8080       │    anomalías · alertas
                    │  FastAPI         │──► webhooks (Telegram/genérico)
                    │  Insights (LLM)  │──► LLM factory :8003 (OpenAI-compatible)
                    └────────┬─────────┘
                             ▼  static/  (dashboard SPA, dark, CVD-safe)
```

## 🚀 Instalación rápida

```bash
git clone https://github.com/<tu-org>/nova-glass.git
cd nova-glass
pip install -r requirements.txt          # o usa el venv del sistema

# 1) configura secretos (NO se sube a git)
cp config.local.example.json config.local.json
#   edita: ingest_token, webhooks, devices...

# 2) arranca el hub (nodo central)
python3 -m uvicorn nova_server:app --host 0.0.0.0 --port 8080

# 3) arranca un collector en CADA nodo
NOVA_HUB=http://<ip-del-hub>:8080 NOVA_NODE=spark01 python3 nova_collector.py
```

Abre **http://\<ip-del-hub\>:8080** (o el puerto configurado)

### Como servicio systemd (persistente tras reboot)

```bash
# copia el repo a /home/<user>/nova-glass en cada nodo
sudo cp systemd/nova-hub.service       /etc/systemd/system/nova-hub@<user>.service
sudo cp systemd/nova-collector.service /etc/systemd/system/nova-collector@<user>.service
sudo systemctl daemon-reload
sudo systemctl enable --now nova-hub@<user>          # solo en el hub
sudo systemctl enable --now nova-collector@<user>   # en todos los nodos
```

## ⚙️ Configuración

`config.json` es **público** (valores por defecto, sin secretos).
`config.local.json` sobreescribe y **está en `.gitignore`** — ahí van tokens, IPs
internas, SNMP communities y webhooks.

### Añadir equipos al NOC

```json
"devices": [
  {"name": "gateway",   "host": "192.168.0.1",   "kind": "ping"},
  {"name": "switch-poe","host": "192.168.0.2",   "kind": "snmp", "community": "public"},
  {"name": "api-llm",   "host": "192.168.0.200", "kind": "http", "url": "http://192.168.0.200:8003/v1/models"},
  {"name": "spark02",   "host": "192.168.0.210", "kind": "tcp",  "port": 22}
]
```
`snmp` requiere `pip install pysnmp`; si no está, cae a `ping` automáticamente.

### Webhooks de alertas

```json
"webhooks": [
  {"type": "telegram", "bot_token": "...", "chat_id": "..."},
  {"type": "generic",  "url": "https://tu-endpoint/webhook"}
]
```

### Insights por IA

Apunta `llm.base_url` a cualquier endpoint OpenAI-compatible (el DeepSeek-V4-Flash
de la factory, vLLM, etc.). Los reportes se generan bajo demanda desde la pestaña
**✨ Insights** y se cachean en la BD.

## 📊 Métricas recolectadas

- **Rápido (5s):** CPU total + load, RAM, swap, red por interfaz (rx/tx MB/s),
  disco I/O, GPU util/temp/power.
- **Lento (5min):** filesystems, uso por usuario (`du` de `/home/*`), errores de
  journal (última hora), paquetes actualizables, **parches de seguridad (CVE del
  inventario) + firmware**, contenedores Docker, uptime, kernel.

## 🎨 Diseño

Tema oscuro flat. Paleta de series **validada CVD-safe** (colorblind) contra la
superficie `#151c28`: CPU `#5b8def` · RAM `#c07617` · Red `#0f9e8c` · GPU `#a371f7`.
El acento teal es solo UI. Todo el layout es de cards reordenables, fácil de
extender con nuevos paneles.

## 🔒 Seguridad y privacidad

- Este proyecto **no hace pentest ni escaneo de vulnerabilidades de red** — solo
  reporta el inventario propio (paquetes/firmware pendientes vía `apt`/`fwupd`).
- `config.local.json` y `nova.db` están en `.gitignore`: **el repo público nunca
  contiene tokens, IPs internas ni telemetría real**.
- El endpoint de ingesta valida `X-Nova-Token` si configuras `ingest_token`.

## 📄 Licencia

MIT — ver [LICENSE](LICENSE).
