# Backlog

Lo que queda. Las fases cerradas y sus pruebas están en `docs/plan-v1.md`; aquí solo lo abierto.

Estado por fase: F0–F4 cerradas. F5 código cerrado, **sin medir**. F6 cerrada salvo un punto.
F7 implementada, **sin probar contra una nube**.

---

## 1. Bloqueantes

### 1.1 Medir F5 contra un modelo real

Sin esto el criterio de éxito de la fase no está cumplido: no hay métricas ni comparación.

- Falta `VMFORGE_LLM_BASE_URL` y `VMFORGE_LLM_MODEL`. En la máquina de desarrollo solo existe
  `LLMAPI_API_KEY`, sin endpoint declarado. No se adivinó un endpoint para gastar una credencial.
- Falta elegir el **modelo de línea base** para la comparación.

Con las dos cosas:

```bash
VMFORGE_LLM_BASE_URL=... VMFORGE_LLM_API_KEY=... VMFORGE_LLM_MODEL=... \
  uv run vmforge eval --record eval/runs/baseline.json
```

### 1.2 Revisar la política como artefacto de seguridad

`src/vmforge/policy/allowlist.yaml` decide qué paquetes, servicios y rutas se permiten. Es una
decisión de la organización, no técnica. Ya lo usan el builder, el dataset y `vmforge push`.
Canjear la lista curada por la política real es lo primero que debería pasar antes de usar esto
en serio.

### 1.3 Probar la subida a OpenStack

`src/vmforge/push/openstack.py` está implementada y testeada contra una conexión falsa: propiedades,
verificación de checksum y formatos. Nunca se ejecutó contra una nube real.

- Falta un `clouds.yaml` y un proyecto OpenStack.
- Falta la prueba de arranque real de la instancia (el criterio que el plan pone para F7).
- Pendiente decidir si el nombre por defecto (`ubuntu-24.04-<hash>`) es el que quieres en Glance.

---

## 2. Endurecimiento pendiente

- **Verificación por `qemu-guest-agent` en vez de SSH.** El plan lo deja como alternativa opcional.
  Hoy `ssh` es obligatorio (D1) y una imagen no puede deshabilitarlo. El canal virtio-serial ya se
  usa en la VM de verificación para que el agente arranque; falta usarlo como transporte.
- **El seed de verificación crea un usuario con sudo NOPASSWD.** Consecuencia: ninguna verificación
  puede afirmar "esta imagen no tiene cuentas con sudo", porque el verificador añade una.
- **`packer init` instala el plugin en `~/.packer.d`**, fuera del build. `toolchain.lock` fija la
  versión del plugin, pero no verifica el binario descargado. Dos máquinas pueden estar usando
  binarios distintos con la misma versión declarada.
- **Un manifest puede mentir.** El checksum ata los bytes, no las afirmaciones. Firmar el manifest
  queda para cuando alguien dependa de esa garantía.

---

## 3. Deuda del arnés de evaluación

- El scoring compara conjuntos por campo, no el contenido de los archivos: dos `files` con el mismo
  path y contenido distinto puntúan igual.
- 25 casos detectan regresiones groseras; no permiten afirmar precisión con confianza. Ampliar con
  casos reales antes de comparar modelos.
- `mean_latency_ms` sale `None` cuando todas las latencias son 0 (stub). Debería distinguir "sin
  datos" de "cero".
- CI ejecuta el scoring sobre fixtures, no sobre una grabación real comprometida. Cuando exista una
  corrida real, guardarla y hacer que CI la repita con `--replay`.

---

## 4. Reproducibilidad de bytes

Decidida fuera de alcance, no resuelta. Los manifests coinciden entre builds; el `.qcow2` no.
Haría falta `SOURCE_DATE_EPOCH`, ext4 determinista y `mkfs` reproducible. Solo importa si alguien
necesita comparar imágenes por hash.

---

## 5. Menor

- `scripts/boot-vanilla.sh` duplica parte de `verify/vm.py`. Si vuelve a divergir, borrarlo y dar un
  modo `--no-goss` a `vmforge verify`.
- goss se sube al guest en cada verificación (~20 MB por SSH). Funciona.
- El nombre del directorio (`gira-iso`) no coincide con el del proyecto (`vmforge`). Aceptado en D6.

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
