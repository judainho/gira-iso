# Backlog

Trabajo abierto de `gira-iso`. Las fases cerradas y sus pruebas viven en
[`docs/plan-v1.md`](docs/plan-v1.md); el estado honesto de lo implementado, en
[`docs/alcance.md`](docs/alcance.md). Este archivo solo contiene **lo que falta**, ordenado por
prioridad.

Convención: `P0` bloquea el uso real del proyecto, `P1` endurece una garantía que hoy es parcial,
`P2` mejora la evidencia disponible, `P3` es trabajo diferible y `P4` es cosmético o de
mantenimiento.

| Prioridad | Significado |
|---|---|
| P0 | Bloquea el criterio de éxito de una fase o el uso en producción |
| P1 | Endurecimiento pendiente de una garantía ya prometida |
| P2 | Deuda del arnés de evaluación |
| P3 | Trabajo decidido fuera de alcance, no resuelto |
| P4 | Mantenimiento y limpieza |

---

## Estado por fase

| Fase | Estado |
|---|---|
| F0–F4 | Cerradas |
| F5 | Código cerrado, **sin medir contra un modelo real** |
| F6 | Cerrada, salvo un punto |
| F7 | Implementada, **sin probar contra una nube** |
| Extra | Snaps y repositorios oficiales integrados (2026-11-01) |

---

## P0 — Bloqueantes

### B-01 · Medir F5 contra un modelo real

Sin esto no hay métricas ni comparación de modelos, y el criterio de éxito de la fase no se cumple.

**Situación actual.** El endpoint por defecto es `https://api.llmapi.ai/v1`. Su `/v1/models` lista
396 modelos **sin autenticación**: responde `200` pero no prueba ninguna credencial.
`/v1/chat/completions` rechaza la clave disponible en este entorno con `401 invalid_api_key`, tanto
con `Authorization: Bearer` como con `x-api-key`.

**Falta:**

1. Una clave que el endpoint de chat acepte.
2. Elegir el **modelo de línea base** para la comparación. Candidatos ya listados por el proveedor:
   `deepseek-v4.1-flash-1017` (modelo por defecto del código) y `claude-haiku-4-5` como línea base
   barata.

**Criterio de cierre.** Existen dos grabaciones reproducibles y una comparación legible:

```bash
GIRA_ISO_LLM_API_KEY=... GIRA_ISO_LLM_MODEL=deepseek-v4.1-flash-1017 \
  uv run gira-iso eval --record eval/runs/current.json
GIRA_ISO_LLM_API_KEY=... GIRA_ISO_LLM_MODEL=claude-haiku-4-5 \
  uv run gira-iso eval --record eval/runs/baseline.json
uv run gira-iso eval --replay eval/runs/current.json     # re-puntuar, sin red
```

La infraestructura está lista: `eval/runs/` es local, y una grabación se puede promover a
`eval/baselines/` para re-puntuarla sin red.

### B-02 · Revisar la política como artefacto de seguridad

`src/gira_iso/policy/allowlist.yaml` decide qué paquetes, servicios, rutas, snaps y repositorios se
permiten. Es una decisión de la organización, no técnica.

Hoy contiene 48 paquetes (42 del archivo de Ubuntu y 6 de repositorios de terceros), 2 snaps, 2
repositorios con clave fijada por `sha256`, 11 servicios y 8 prefijos de rutas.

**Falta:** canjear la lista curada por la política real de la organización antes de usar esto en
serio. La clave de un repositorio es confianza root permanente para todo lo que `apt` instale desde
ese origen.

**Criterio de cierre.** La política activa (`GIRA_ISO_ALLOWLIST`) fue revisada por quien decide, y
el hash registrado en los manifests corresponde a esa revisión.

### B-03 · Probar la subida a OpenStack

`src/gira_iso/push/openstack.py` está implementada y testeada contra una conexión falsa:
propiedades, verificación de checksum y formatos. Nunca se ejecutó contra una nube real.
[`docs/openstack.md`](docs/openstack.md) es el runbook (`clouds.yaml`, nombre en Glance,
comprobación manual de arranque).

**Falta:**

- Un `clouds.yaml` y un proyecto OpenStack de pruebas.
- La prueba de arranque real de una instancia creada desde la imagen subida (el criterio que el plan
  pone para F7).
- Decidir si el nombre por defecto (`ubuntu-24.04-<hash>`) es el que se quiere en Glance.

---

## P1 — Endurecimiento pendiente

### H-01 · Un manifest puede mentir

El checksum ata los bytes del artefacto, no las afirmaciones del manifest. Firmar el manifest queda
para cuando alguien dependa de esa garantía.

**Criterio de cierre.** `verify` rechaza un manifest cuya firma no valide contra una clave conocida.

### H-02 · Mostrar las dependencias de un paquete añadido

Permitir `nodejs` permite todo su árbol de dependencias, y Launchpad no lo entrega. Hoy solo se
avisa; no se muestra el árbol completo. Haría falta consultar otra fuente antes de aprobar.

**Criterio de cierre.** `gira-iso allow add` muestra el árbol de dependencias y advierte el impacto
antes de pedir confirmación.

### H-03 · Probar la traducción de nombres con el modelo real

`src/gira_iso/nl/suggest.py` traduce un término del usuario al paquete de Ubuntu que le corresponde
(`node` → `nodejs`). No se ha probado con un modelo real. Caso mínimo: pedir `nginx y node`.

**Criterio de cierre.** El caso bilingüe se resuelve sin intervención y queda registrado en una
grabación.

### H-04 · Permitir ampliar servicios habilitables

Hoy son 11 servicios curados. Si el uso real necesita más, conviene un mecanismo equivalente al de
las adiciones de paquetes, con revisión y auditoría.

### H-05 · Reintentos y puntos de reanudación del build

Un build interrumpido se descarta entero. Haría falta reanudar por paso (descarga, render, Packer,
provisión) sin reconstruir lo ya hecho.

**Criterio de cierre.** Un build interrumpido en la fase de provisión se reanuda sin volver a
descargar la base ni repetir el render.

### H-06 · Recetas aprobadas para software fuera de `apt`

Los snaps y los repositorios oficiales ya están soportados. Queda **GitHub**: un asset de release es
un binario opaco corriendo como root, sin `dpkg` que lo respalde.

**Falta:** exigir tag, nombre de asset y `sha256` por entrada, prohibir `latest` (es un puntero
mutable) y decidir cómo se revisa. No se implementa hasta cerrar esa decisión.

---

## P2 — Deuda del arnés de evaluación

- **Cobertura insuficiente.** Los 25 casos detectan regresiones groseras; no permiten afirmar
  precisión con confianza. Ampliar con casos reales antes de comparar modelos.
- **Sin grabación de línea base.** No hay una grabación comprometida en `eval/baselines/` porque
  depende de la credencial de B-01. La reproducción local con `--replay` ya funciona.
- **Tiempo de ejecución.** Medir la latencia real por caso antes de fijar expectativas de calidad.

---

## P3 — Reproducibilidad de bytes

Decidida fuera de alcance, no resuelta. Los manifests coinciden entre builds; el `.qcow2` no.

Haría falta `SOURCE_DATE_EPOCH`, ext4 determinista y `mkfs` reproducible. Solo importa si alguien
necesita comparar imágenes por hash.

---

## P4 — Menor y mantenimiento

- `scripts/boot-vanilla.sh` duplica parte de `src/gira_iso/verify/vm.py`. Si vuelve a divergir,
  borrarlo y darle un modo `--no-goss` a `gira-iso verify`.
- `goss` se sube al guest en cada verificación (~16 MB por el canal del agente, en trozos).
  Funciona, pero sigue siendo el objeto más grande que cruza el canal.
- Revisar que la documentación siga reflejando el código tras cada fase cerrada.

### Mantenimiento de pines

No es deuda, es disciplina. Cada cambio se hace con su verificación en el mismo commit.

| Pin | Valor actual | Procedimiento |
|---|---|---|
| Snap `docker` | revisión `3618`, canal `stable`, `classic` | Verificar que siga en la tienda y que el build la acepte en 22.04 y 24.04 |
| Snap `certbot` | revisión `5911`, canal `stable` | Igual que el anterior |
| Plugin de Packer | versión y `sha256` en `toolchain.lock` | Cambiar versión y hash en el mismo commit; `scripts/check-toolchain.sh` lo comprueba |
| Imágenes base | `sha256` en la política | Actualizar solo con revisión explícita |

---

## Decisiones de proyecto

Estas decisiones están cerradas y no se reabren sin una petición explícita.

- **Sin CI/CD.** El repositorio no usa GitHub Actions ni ningún servicio externo de integración.
  Lint, pruebas y validación se ejecutan en local. No se añaden workflows.
- **Idioma.** El código, los identificadores y los comentarios están en inglés; la documentación y
  el backlog, en español.
- **Sin emojis en la documentación.** La presentación es técnica y legible sin decoración.
- **El modelo nunca ejecuta nada.** Solo produce una especificación validada por esquema y política.

---

## Trampas aprendidas (no repetir)

- **Packer `disk_size` vale 40960 MB por defecto incluso con `disk_image = true`.** Declararlo
  siempre.
- **Un overlay más pequeño que su backing file** deja al guest en initramfs con
  `LABEL=cloudimg-rootfs does not exist`.
- **El plugin `qemu` 1.1.7 no expone `cpu_type`:** `-cpu host` va por `qemuargs`.
- **`virtserialport` sin `virtio-serial-pci`** hace que QEMU se niegue a arrancar.
- **La cloud image 24.04 no trae `qemu-guest-agent`.** Está en `mandatory_packages`.
- **`goss` reporta `test-count`, no `tested-count`.**
- **`$` en una regex acepta un salto de línea final; `\Z` no.** Los valores de un repositorio se
  validan con `\Z`: un `url` con salto de línea podría cerrar la línea del script root.
