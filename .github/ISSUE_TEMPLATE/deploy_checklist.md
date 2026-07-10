---
name: Deploy checklist
about: Checklist para promover código entre ambientes
title: '[DEPLOY] '
labels: 'deploy:coolify'
assignees: ''
---

**Origen → Destino**
- [ ] dev → QA
- [ ] QA → prod
- [ ] hotfix → prod

**Pre-requisitos**
- [ ] Pruebas manuales en ambiente origen
- [ ] Nodos reportando datos (spark01 + spark02)
- [ ] NOVA Insights funcionando (LLM reachable)
- [ ] Dispositivos NOC verdes en dashboard

**Pasos**
- [ ] Reconstruir imagen Docker
- [ ] Push a registro
- [ ] Actualizar stack en Portainer
- [ ] Verificar healthcheck
- [ ] Verificar recolección de datos

**Rollback plan**
- [ ] Stack anterior identificado en Portainer
- [ ] Comando de rollback documentado
