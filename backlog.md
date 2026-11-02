# Backlog

Lo que queda. Las fases cerradas y sus pruebas están en `docs/plan-v1.md`; aquí solo lo abierto.

Estado por fase: F0–F4 cerradas. F5 código cerrado, **sin medir**. F6 cerrada salvo un punto.
F7 implementada, **sin probar contra una nube**. Snaps y repositorios oficiales integrados
(2026-11-01, ver `odd/tasks/snaps-and-repositories.md`).

---

## 1. Bloqueantes

### 1.1 Medir F5 contra un modelo real

Sin esto el criterio de éxito de la fase no está cumplido: no hay métricas ni comparación.

- El endpoint por defecto es `https://api.llmapi.ai/v1` y `/v1/models` lista 396 modelos **sin
  autenticación**, así que responde 200 pero no prueba ninguna credencial. `/v1/chat/completions`
  rechaza la clave de este entorno con `401 invalid_api_key`, tanto con `Authorization: Bearer` como
  con `x-api-key`.
- Falta una clave que el endpoint de chat acepte y elegir el **modelo de línea base** para la
  comparación. Los candidatos ya están listados por el proveedor: `deepseek-v4.1-flash-1017` (el
  modelo por defecto del código) y, como línea base barata, `claude-haiku-4-5`.

Con las dos cosas:

```bash
GIRA_ISO_LLM_API_KEY=... GIRA_ISO_LLM_MODEL=deepseek-v4.1-flash-1017 \
  uv run gira-iso eval --record eval/runs/current.json
GIRA_ISO_LLM_API_KEY=... GIRA_ISO_LLM_MODEL=claude-haiku-4-5 \
  uv run gira-iso eval --record eval/runs/baseline.json
uv run gira-iso eval --replay eval/runs/current.json     # re-puntuar, sin red
```

La infraestructura ya está: `eval/baselines/` es donde se compromete una grabación para que el CI la
repita sin llamar al modelo, y la documentación de `--replay` está en el README.

### 1.2 Revisar la política como artefacto de seguridad

`src/gira_iso/policy/allowlist.yaml` decide qué paquetes, servicios, rutas, snaps y repositorios se
permiten. Es una decisión de la organización, no técnica. Ya lo usan el builder, el dataset y
`gira-iso push`. Hoy son 48 entradas de paquete (42 del archivo de Ubuntu y 6 que llegan por
repositorio de tercero), 2 snaps, 2 repositorios con su clave fijada por sha256, 11 servicios y 8
prefijos de rutas. Canjear la lista curada por la política real es lo primero que debería pasar antes
de usar esto en serio; la clave de un repositorio es confianza root permanente para todo lo que apt
instale desde ese origen.

### 1.3 Probar la subida a OpenStack

`src/gira_iso/push/openstack.py` está implementada y testeada contra una conexión falsa: propiedades,
verificación de checksum y formatos. Nunca se ejecutó contra una nube real. `docs/openstack.md` es el
runbook (`clouds.yaml`, nombre en Glance, comprobación manual de arranque).

- Falta un `clouds.yaml` y un proyecto OpenStack.
- Falta la prueba de arranque real de la instancia (el criterio que el plan pone para F7).
- Pendiente decidir si el nombre por defecto (`ubuntu-24.04-<hash>`) es el que quieres en Glance.

---

## 2. Endurecimiento pendiente

- **Verificación por `qemu-guest-agent` en vez de SSH, y el seed con sudo sin contraseña.** Son el
  mismo cambio: hoy el verificador entra por SSH con un usuario que crea él mismo con sudo NOPASSWD,
  así que ninguna verificación puede afirmar "esta imagen no tiene cuentas con sudo". El canal
  virtio-serial ya se usa en la VM de verificación para que el agente arranque; falta usarlo como
  transporte. El plan lo deja como alternativa opcional (D1) y `ssh` sigue siendo obligatorio.
- **Un manifest puede mentir.** El checksum ata los bytes, no las afirmaciones. Firmar el manifest
  queda para cuando alguien dependa de esa garantía.
- **Mostrar las dependencias de un paquete que se va a agregar.** Permitir `nodejs` permite todo su
  árbol, y Launchpad no lo da; hoy solo se avisa. Haría falta una consulta a otra fuente.
- **Probar la traducción de nombres con el modelo real** (`nl/suggest.py`): pedir `nginx y node`.
- **Ampliar servicios habilitables** como las adiciones de paquetes, si hace falta (hoy son 11, curados).
- **Reintentos y puntos de reanudación del build.** Hoy un build interrumpido se descarta entero.
- **Recetas aprobadas para software fuera de apt.** Los snaps y los repositorios oficiales ya están
  soportados (ver README). Queda **GitHub**: un asset de release es un binario opaco corriendo como
  root, sin `dpkg` que lo respalde. Exigiría fijar tag, nombre de asset y sha256 por entrada, y
  prohibir `latest` (es un puntero mutable). Falta decidir cómo se revisa.

### Mantenimiento de pines (no es deuda, es disciplina)

- **Snaps.** `docker=3618` y `certbot=5911` (stable, amd64) están fijados y el build comprueba que la
  revisión instalada coincide. Subir una revisión es un cambio revisado: verificar que siga en la
  tienda y que el build la acepte en 22.04 y en 24.04 antes de mover el número.
- **Plugin de Packer.** `toolchain.lock` fija el sha256 del binario instalado y
  `scripts/check-toolchain.sh` lo compara. Actualizar el plugin es cambiar versión y hash en el mismo
  commit.

## 3. Deuda del arnés de evaluación

- 25 casos detectan regresiones groseras; no permiten afirmar precisión con confianza. Ampliar con
  casos reales antes de comparar modelos.
- El job de CI que repite una grabación con `--replay` está listo, pero no hay grabación comprometida
  todavía: depende de la credencial de 1.1.

---

## 4. Reproducibilidad de bytes

Decidida fuera de alcance, no resuelta. Los manifests coinciden entre builds; el `.qcow2` no.
Haría falta `SOURCE_DATE_EPOCH`, ext4 determinista y `mkfs` reproducible. Solo importa si alguien
necesita comparar imágenes por hash.

---

## 5. Menor

- `scripts/boot-vanilla.sh` duplica parte de `verify/vm.py`. Si vuelve a divergir, borrarlo y darle
  un modo `--no-goss` a `gira-iso verify`.
- goss se sube al guest en cada verificación (~20 MB por SSH). Funciona.

---

## Trampas aprendidas (no repetir)

- **Packer `disk_size` vale 40960 MB por defecto incluso con `disk_image = true`.** Declararlo
  siempre.
- **Un overlay más pequeño que su backing file** deja al guest en initramfs con
  `LABEL=cloudimg-rootfs does not exist`.
- **El plugin `qemu` 1.1.7 no expone `cpu_type`:** `-cpu host` va por `qemuargs`.
- **`virtserialport` sin `virtio-serial-pci`** hace que QEMU se niegue a arrancar.
- **La cloud image 24.04 no trae `qemu-guest-agent`.** Está en `mandatory_packages`.
- **goss reporta `test-count`, no `tested-count`.**
- **`$` en una regex acepta un salto de línea final; `\Z` no.** Los valores de un repositorio se
  validan con `\Z`: un `url` con salto de línea podría cerrar la línea del script root.
