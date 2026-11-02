# Especificación (`spec.yaml`)

La especificación es el **único contrato** entre el intérprete y el motor de construcción. Es
declarativa: no contiene scripts, URLs ni comandos. El esquema decide la forma; la política decide
qué valores son aceptables.

```bash
gira-iso check examples/nginx-web.yaml    # valida esquema y política, sin construir
```

## Forma

```yaml
spec_version: "1"

base:
  os: ubuntu
  version: "24.04"

disk_size: 10G

packages:
  - nginx
  - qemu-guest-agent
  - docker-ce          # viene del repositorio oficial de Docker, por política

snaps:
  - certbot            # canal, confinamiento y revisión vienen de la política

services:
  enable:
    - nginx
  disable: []

users:
  - name: deploy
    sudo: true
    ssh_authorized_keys:
      - ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA...

files:
  - path: /etc/nginx/conf.d/health.conf
    content: |
      server {
          listen 8080;
          location /health { return 200 "ok\n"; }
      }
    mode: "0644"
    owner: root:root
```

## Campos

| Campo | Tipo | Obligatorio | Límite | Notas |
|---|---|---|---|---|
| `spec_version` | `"0"` \| `"1"` | no | — | Por defecto `"1"`. `"0"` no admite `snaps`. |
| `base` | objeto | no | — | `os` solo `ubuntu`; `version` con forma `NN.NN`. Por defecto 24.04. |
| `disk_size` | string | no | `6G`–`256G` | Por defecto `8G`. Debe declararse: Packer usa 40960 MB si se omite. |
| `packages` | lista | no | máx. 40 | Nombres de paquete, sin duplicados. Deben estar en la política. |
| `snaps` | lista | no | máx. 10 | Identificadores; el canal y la revisión los decide la política. |
| `services.enable` | lista | no | máx. 20 | Deben estar en la política; un snap es dueño de su servicio. |
| `services.disable` | lista | no | máx. 20 | `ssh` y `qemu-guest-agent` no se pueden deshabilitar. |
| `users` | lista | no | máx. 10 | Nombres únicos. `sudo` es booleano. |
| `users[].ssh_authorized_keys` | lista | sí, por usuario | 1–5 claves | Solo `ssh-ed25519` desnuda: sin opciones ni comentario. |
| `files` | lista | no | máx. 20 | Rutas únicas y absolutas, sin `.` ni `..`. |
| `files[].content` | string | sí | 32 KiB por archivo | Sin bytes NUL. Total de todos los archivos: 128 KiB. |
| `files[].mode` | string | no | `0[0-7]{3}` | Por defecto `"0644"`. |
| `files[].owner` | string | no | `usuario[:grupo]` | Se valida la forma; el propietario debe existir en la imagen. |

El esquema es cerrado (`extra="forbid"`): un campo inventado por el modelo se rechaza, no se ignora.

## Reglas que aplica la política

El esquema no decide qué es seguro. Eso lo hace `src/gira_iso/policy/allowlist.yaml`:

| Regla | Consecuencia |
|---|---|
| Paquete fuera de la lista | Rechazo; el mensaje vuelve al modelo para que corrija |
| Snap fuera de la lista | `snap.not_allowed` |
| Servicio fuera de la lista | `service.not_allowed` |
| Deshabilitar `ssh` o `qemu-guest-agent` | `service.required` |
| Servicio de un snap declarado también en `enable` | `service.snap_managed` |
| Ruta fuera de los prefijos permitidos | Rechazo; los prefijos de autorización, sudoers, unidades y `/home/` están prohibidos |
| Usuario `root`, `packer` o `gira-iso` | `user.reserved` |
| Base no fijada en la política | Rechazo |

La política se evalúa antes de descargar o construir nada. Un spec rechazado cuesta cero bytes y
cero minutos.

## Prefijos de archivo permitidos hoy

`/etc/default/`, `/etc/logrotate.d/`, `/etc/motd`, `/etc/nginx/`, `/etc/profile.d/`,
`/etc/security/limits.d/`, `/etc/sysctl.d/`, `/etc/systemd/journald.conf`.

Están prohibidos por defensa en profundidad: `/etc/cron`, `/etc/ssh/`, `/etc/sudoers`,
`/etc/sudoers.d/`, `/home/`, `/lib/systemd/system/`, `/root/`, `/usr/lib/systemd/system/`.

## Ejemplos

- [`examples/nginx-web.yaml`](../examples/nginx-web.yaml) — servidor web con nginx, usuario con sudo
  y un archivo de configuración.

## Errores frecuentes

| Mensaje | Causa | Corrección |
|---|---|---|
| `disk_size must be at least 6G` | Disco menor que la cloud image | Subir `disk_size` |
| `duplicate packages` | Paquete repetido | Quitar la repetición |
| `must be exactly '<type> <base64>'` | Clave SSH con comentario u opciones | Dejar solo `ssh-ed25519 <base64>` |
| `path must not contain '.' or '..'` | Ruta con segmentos relativos | Usar una ruta absoluta normalizada |
| `service.required` | Intento de deshabilitar `ssh` o `qemu-guest-agent` | Quitarlo de `disable` |
| `user.reserved` | Nombre reservado por la herramienta | Elegir otro nombre |

Los códigos de salida están en el [README](../README.md#automatización): `1` es rechazo de política
o verificación, `2` es entrada inválida y `3` es entorno incompleto.
