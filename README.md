<p align="center">
  <img src="docs/assets/gira-pucp.png" alt="Grupo de Investigación en Redes Avanzadas — GIRA PUCP" width="460">
</p>

<h1 align="center">gira-iso</h1>

<p align="center">
  De una descripción en lenguaje natural a una imagen <code>qcow2</code> reproducible,<br>
  verificable y lista para la nube.
</p>

---

## Tabla de contenido

1. [Qué es](#qué-es)
2. [Estado del proyecto](#estado-del-proyecto)
3. [Requisitos](#requisitos)
4. [Instalación](#instalación)
5. [Uso](#uso)
6. [Cómo funciona](#cómo-funciona)
7. [Modelo de seguridad](#modelo-de-seguridad)
8. [Configuración](#configuración)
9. [Documentación](#documentación)
10. [Estructura del repositorio](#estructura-del-repositorio)
11. [Desarrollo](#desarrollo)
12. [Alcance y límites](#alcance-y-límites)
13. [Licencia](#licencia)

---

## Qué es

`gira-iso` convierte una petición en lenguaje natural o una especificación YAML en una imagen
`qcow2` para Ubuntu, con procedencia completa y verificación opcional.

**Principio de diseño:** el modelo de lenguaje nunca genera scripts ni imágenes. Solo produce una
especificación declarativa que se valida contra un esquema y una política de seguridad. Un motor
determinista construye la imagen a partir de esa especificación.

```
lenguaje natural ──► intérprete (LLM) ──► spec ──► esquema + política ──► builder (Packer/QEMU)
                                                                                │
                                              verificación (QEMU + goss) ◄──────┤
                                                                                ▼
                                                        imagen qcow2 + manifest.json ──► push (OpenStack)
```

Ese límite es lo que hace el resultado auditable: lo que el modelo puede afirmar queda reducido a
campos validados, y todo lo que entra en la imagen pasa por la política.

| Garantía | Cómo se consigue |
|---|---|
| Reproducibilidad funcional | Misma especificación, mismas versiones, mismos paquetes y usuarios |
| Trazabilidad | `manifest.json` con hashes de especificación, política, base y artefacto |
| Rechazo temprano | La política corre antes de descargar o construir nada |
| Verificación real | Arranque en QEMU y aserciones `goss` generadas desde la especificación |
| Sin secretos en disco | La clave del modelo solo vive en el entorno |

---

## Estado del proyecto

| Fase | Contenido | Estado |
|---|---|---|
| F0–F4 | Esquema, política, construcción con Packer, manifest, CLI básica | Cerradas |
| F5 | Intérprete de lenguaje natural y arnés de evaluación | Código cerrado; **sin medir contra un modelo real** |
| F6 | Verificación en QEMU con `goss` | Cerrada, salvo un punto |
| F7 | Subida a OpenStack Glance | Implementada; **sin probar contra una nube real** |
| Extra | Paquetes añadidos localmente, snaps, repositorios oficiales | Implementados y revisados |

El detalle honesto de lo que funciona, lo que no se ha probado y lo que queda fuera está en
[`docs/alcance.md`](docs/alcance.md). Las tareas abiertas están en [`backlog.md`](backlog.md).

---

## Requisitos

- Linux con KVM (`/dev/kvm`).
- `qemu-system-x86`, `qemu-utils`, `cloud-image-utils`.
- Packer 1.16.2 y `goss` 0.4.10 (binarios oficiales, fijados por versión y checksum en
  `toolchain.lock`).
- Python 3.12 y [`uv`](https://docs.astral.sh/uv/).

---

## Instalación

```bash
# Linux o WSL2
./install.sh

# Windows: prepara WSL2 y ejecuta install.sh dentro
powershell -ExecutionPolicy Bypass -File .\install.ps1
```

Después, `gira-iso` abre el menú interactivo. En Windows:

```powershell
wsl -d Ubuntu -- gira-iso
```

La primera ejecución pide la clave del endpoint del modelo, salvo que `GIRA_ISO_LLM_API_KEY` ya
esté definida. El endpoint por defecto es `https://api.llmapi.ai/v1`.

<details>
<summary>Instalación manual, paso a paso</summary>

```bash
# 1. Paquetes del sistema
sudo apt-get install -y qemu-system-x86 qemu-utils cloud-image-utils

# 2. Toolchain fijado (Packer y goss, verificados por checksum)
./scripts/check-toolchain.sh

# 3. Dependencias de Python
uv sync

# 4. Primera imagen
uv run gira-iso create examples/nginx-web.yaml --name web
uv run gira-iso list
```

</details>

---

## Uso

### Menú interactivo

Ejecutar `gira-iso` sin argumentos abre el menú. La clave del modelo se pide primero, oculta y
validada, y nunca se escribe en disco.

| Opción | Qué hace |
|---|---|
| 1. Listar imágenes | Ver procedencia, exportar o eliminar una imagen |
| 2. Subir imagen base | Registrar una cloud image local (`*-cloudimg-amd64.img`, `qcow2`) |
| 3. Imágenes base | Las oficiales fijadas (Ubuntu 22.04 y 24.04) y las propias |
| 4. Crear una imagen | Elegir base, describir la imagen, revisar la spec interpretada y observar el build |
| 5. Paquetes permitidos | Ver la lista empaquetada, añadir o quitar paquetes locales |

El intérprete conoce el catálogo permitido (`src/gira_iso/policy/allowlist.yaml`, descrito en
`src/gira_iso/nl/catalog.yaml`), así que distingue un **paquete** (`docker` se traduce a
`docker.io`) de un **programa** que `apt` no proporciona (opencode, nvm, Docker CE oficial).

El menú muestra *qué entendió* y *qué no puede incluir*, con la razón y las alternativas permitidas.
Esas afirmaciones se contrastan contra la especificación y la política antes de mostrarlas. Si la
política rechaza la spec, las violaciones vuelven al modelo para que se corrija, y nada se construye
hasta que la especificación final pase.

### Comandos

La superficie guionizable refleja el menú:

```bash
gira-iso list                                          # imágenes creadas
gira-iso create examples/nginx-web.yaml --name web     # YAML; escribe imagen + manifest
gira-iso create "un servidor web con nginx" --name web # lenguaje natural (pide la clave)
gira-iso show web                                      # procedencia registrada
gira-iso download web --to ~/Downloads                 # copia imagen + manifest con checksum
gira-iso delete web                                    # pide confirmación; --yes la omite
gira-iso clean                                         # elimina builds que no terminaron
gira-iso clean --bases                                 # elimina bases que ningún manifest usa
```

`create` ejecuta el esquema y la política antes de descargar o construir nada: una especificación
rechazada no cuesta ancho de banda ni escrituras. Añadir `--verify` arranca la imagen y ejecuta las
aserciones `goss` inmediatamente después del build.

Los builds se escriben en `~/.local/share/gira-iso/builds`; la raíz se cambia con
`GIRA_ISO_HOME`.

Los comandos avanzados (`check`, `plan`, `build`, `verify`, `policy`, `push`, `eval`) siguen
disponibles, pero ocultos de la ayuda principal.

### Automatización

Para scripts, `--json` reserva `stdout` para un único objeto JSON y manda el progreso a `stderr`.
`--non-interactive` nunca pregunta: la clave debe venir de `GIRA_ISO_LLM_API_KEY`, `--yes` queda
implícito, y una petición que necesita más detalle sale con código `1` listando las preguntas.

```bash
export GIRA_ISO_LLM_API_KEY=...
gira-iso create "un servidor web con nginx" --name web --non-interactive --json | jq -r .image
gira-iso list --json | jq '.[].name'
gira-iso show web --json | jq .base
```

Códigos de salida:

| Código | Significado |
|---|---|
| `0` | Éxito |
| `1` | La política o la verificación rechazó algo, o el modelo necesita más detalle |
| `2` | Entrada inválida |
| `3` | El entorno no puede hacerlo (Packer, QEMU, descarga de la base, dependencia opcional) |
| `130` | Interrumpido con `Ctrl+C` |

---

## Cómo funciona

1. **Entrada.** Una petición en lenguaje natural o un YAML. El YAML se carga directamente; el texto
   pasa por el intérprete.
2. **Interpretación** (`src/gira_iso/nl/`). El cliente habla con cualquier endpoint compatible con
   OpenAI, pide JSON y reintenta una vez si la salida no valida. El prompt incluye el catálogo
   permitido.
3. **Validación** (`src/gira_iso/spec/`, `src/gira_iso/policy/`). Pydantic rechaza formas inválidas.
   La política devuelve *todas* las razones por las que no se puede construir: paquetes, snaps,
   servicios, rutas, repositorios y base. Si falla, las violaciones vuelven al modelo.
4. **Construcción** (`src/gira_iso/builder/`). Descarga o reutiliza la imagen base fijada por
   `sha256`, renderiza la plantilla de Packer con cloud-init y scripts, y ejecuta el build. El
   resultado queda en un directorio con su `manifest.json`.
5. **Verificación** (`src/gira_iso/verify/`, opcional con `--verify`). Arranca la imagen en QEMU,
   habla con el guest a través del agente de QEMU —sin SSH— y ejecuta las aserciones `goss`.
6. **Entrega.** `download` copia imagen y manifest con verificación de checksum; `push` sube la
   imagen a Glance.

### Construcción de una imagen

```bash
uv run gira-iso build examples/nginx-web.yaml
```

El build renderiza la especificación a una plantilla de Packer y un seed de cloud-init, ejecuta
Packer como subproceso y escribe `manifest.json` junto a la imagen. El manifest registra la
especificación, la base fijada, las versiones de Packer y sus plugins, el hash de la plantilla y el
hash del artefacto. Una imagen nunca es un blob anónimo.

### Verificación de una imagen

```bash
uv run gira-iso verify builds/<timestamp>-<spec-hash>
```

Lee el manifest y comprueba el checksum del artefacto antes de arrancar nada. Después deriva un
`goss.yaml` de la especificación y lo ejecuta en un arranque limpio, con un seed que la imagen nunca
vio. El código de salida es `0` solo si toda aserción pasa.

El canal es el agente de QEMU sobre un puerto virtio-serial, no SSH: el agente corre como root
dentro del guest, así que el seed de verificación no declara ningún usuario. Eso permite afirmar
algo sobre las cuentas en lugar de inventar la que de otro modo haría falta: ningún usuario con
shell real puede tener `sudo` sin contraseña salvo los que la especificación declara (y root).

Solo se afirma lo que la especificación declara: paquetes instalados, servicios habilitados y
corriendo, usuarios con sus claves, archivos con su modo y hash de contenido exacto. La
especificación no declara puertos, así que no se inventa ninguna aserción de puerto.

---

## Modelo de seguridad

La política es el único lugar que decide qué entra en una imagen. Es un artefacto de seguridad, no
un archivo de configuración.

| Fuente | Cómo se fija |
|---|---|
| Paquetes de Ubuntu | Lista curada por versión; las adiciones locales se comprueban contra Launchpad y las aprueba el operador |
| Snaps | Canal, confinamiento y revisión concreta vienen de la política |
| Repositorios de terceros | La especificación no los nombra; la clave se fija por `sha256` y se instala con `Signed-By:` |
| Imágenes base | Fijadas por versión y `sha256` |

Reglas que no se negocian:

- **La especificación nombra identificadores, nunca URLs.** Un repositorio es un detalle de
  implementación de un paquete; el modelo no puede apuntar un build a un origen elegido por él.
- **La clave de un repositorio es confianza root.** Firma todo lo que `apt` instale desde ese
  origen en el futuro. Por eso los repositorios se curan en la política y **no** se añaden con
  `gira-iso allow add`.
- **Un hash de clave distinto falla el build.** No hay ruta de advertencia que continúe.
- **Un snap se fija a una revisión, no a un canal.** `stable` es un puntero móvil; la aserción del
  build y la verificación comparan la revisión instalada con el pin.
- **Un snap es dueño de su servicio.** Nombrarlo también en `services.enable` falla el build con
  `service.snap_managed`.

### Ampliar la lista permitida

La lista empaquetada contiene 48 paquetes, 2 snaps, 2 repositorios con clave fijada, 11 servicios y
8 prefijos de rutas.

```bash
gira-iso allow add nodejs --base 24.04    # consulta el archivo, muestra lo encontrado y pregunta
gira-iso allow list                       # lo añadido en esta máquina
gira-iso allow remove nodejs
```

Solo se ofrecen `main` y `universe`; `multiverse`, `restricted` y una lista corta de exclusiones se
rechazan. Un paquete se permite **para la versión de Ubuntu contra la que se comprobó**. Sus
scripts de instalación corren como root y sus dependencias no se revisan.

Las adiciones se guardan en `~/.local/share/gira-iso/allowlist-additions.yaml` con un registro de
auditoría, forman parte del digest de la política y se listan en el manifest de cada imagen que las
usa.

- **Modo estricto:** `GIRA_ISO_STRICT=1` ignora y rechaza las adiciones. Se activa solo al apuntar
  `GIRA_ISO_ALLOWLIST` a la lista de la organización; `GIRA_ISO_ALLOW_LOCAL=1` lo anula.
- Con `--non-interactive` no se pregunta nada; `--json` lista los paquetes `addable` y el comando
  para añadirlos.

Para cambiar la política:

```bash
export GIRA_ISO_ALLOWLIST=/etc/gira-iso/allowlist.yaml
uv run gira-iso policy      # archivo activo, su hash y lo que permite
```

El hash de la política activa se registra en cada manifest, así que una imagen se puede rastrear
hasta las reglas que la permitieron.

---

## Configuración

| Variable | Uso | Valor por defecto |
|---|---|---|
| `GIRA_ISO_LLM_BASE_URL` | Endpoint compatible con OpenAI | `https://api.llmapi.ai/v1` |
| `GIRA_ISO_LLM_API_KEY` | Clave del endpoint; obligatoria para llamar al modelo | — |
| `GIRA_ISO_LLM_MODEL` | Identificador del modelo | `deepseek-v4.1-flash-1017` |
| `GIRA_ISO_HOME` | Raíz de datos: builds, caché y adiciones | `~/.local/share/gira-iso` |
| `GIRA_ISO_ALLOWLIST` | Política propia en lugar de la empaquetada | lista incluida |
| `GIRA_ISO_STRICT` | Ignora y rechaza adiciones locales | desactivado |
| `GIRA_ISO_ALLOW_LOCAL` | Permite adiciones aunque haya política propia | desactivado |

Como el cliente solo exige un endpoint compatible con OpenAI, un modelo local servido con
`llama.cpp` u Ollama puede usarse sin cambiar código. Ver
[`docs/laboratorio-ia-local.md`](docs/laboratorio-ia-local.md).

---

## Documentación

| Documento | Contenido |
|---|---|
| [`docs/README.md`](docs/README.md) | Índice de la documentación y orden de lectura |
| [`docs/alcance.md`](docs/alcance.md) | Qué funciona, qué falta probar y qué queda fuera |
| [`docs/arquitectura.md`](docs/arquitectura.md) | Módulos, flujo de una creación y fuentes de confianza |
| [`docs/especificacion.md`](docs/especificacion.md) | Campos de `spec.yaml`, límites y reglas de política |
| [`docs/plan-v1.md`](docs/plan-v1.md) | Plan de desarrollo por fases, criterios de aceptación y decisiones cerradas |
| [`docs/flujo-de-control.md`](docs/flujo-de-control.md) | Quién decide qué, dónde se puede parar y cómo se propagan los errores |
| [`docs/openstack.md`](docs/openstack.md) | Runbook para subir una imagen a Glance |
| [`docs/laboratorio-ia-local.md`](docs/laboratorio-ia-local.md) | Modelo local en CPU para probar el intérprete sin costo por token |
| [`backlog.md`](backlog.md) | Lo que queda abierto, ordenado por prioridad |

---

## Estructura del repositorio

```text
gira-iso/
├── src/gira_iso/
│   ├── cli/            Comandos de Typer y menú
│   ├── tui/            Interfaz interactiva
│   ├── workflow/       Orquestación de la creación con puertos de entrada/salida
│   ├── nl/             Intérprete: cliente OpenAI-compatible, prompt y esquema
│   ├── spec/           Modelo de la especificación y forma canónica
│   ├── policy/         Lista permitida y evaluación de seguridad
│   ├── builder/        Base, plantillas, Packer, manifest y exportación
│   ├── verify/         Arranque en QEMU, agente de invitado y aserciones goss
│   ├── push/           Subida a OpenStack Glance
│   ├── eval/           Arnés de evaluación del intérprete
│   └── templates/      Plantillas de Packer, cloud-init y scripts
├── tests/              623 pruebas
├── examples/           Especificaciones de ejemplo
├── docs/               Documentación y activos del grupo
├── scripts/            Comprobación de toolchain y arranque vanilla
├── install.sh          Instalador para Linux y WSL2
├── install.ps1         Instalador para Windows
├── toolchain.lock      Versiones y hashes de Packer y goss
└── backlog.md          Trabajo pendiente
```

---

## Desarrollo

```bash
uv sync
uv run gira-iso version
uv run pytest          # 623 pruebas
uv run ruff check .
uv run ruff format --check .
```

Este repositorio **no usa CI/CD**. Las comprobaciones se ejecutan en local y no hay workflows de
GitHub Actions: el proyecto no depende de un servicio externo para validar sus cambios.

La suite cubre esquema, política, builder con Packer simulado, CLI, menú, cancelación, limpieza,
OpenStack con conexión falsa y el canal del agente de invitado. `scripts/check-toolchain.sh`
compara el binario del plugin de Packer con el hash de `toolchain.lock`.

### Evaluación del intérprete

El intérprete es agnóstico al modelo: habla con cualquier endpoint compatible con OpenAI.

```bash
export GIRA_ISO_LLM_BASE_URL=https://api.example.com/v1
export GIRA_ISO_LLM_API_KEY=...
export GIRA_ISO_LLM_MODEL=deepseek-v4.1-flash-1017
uv run gira-iso eval
```

El modelo solo puede responder con tres formas: una especificación, una lista de preguntas de
aclaración o un rechazo. Cualquier otra cosa falla la validación y se reintenta una vez con el error
adjunto. El texto del usuario se delimita como dato y la política corre independientemente de lo que
el modelo haya producido.

```bash
uv run gira-iso eval --record eval/runs/2026-11-01.json   # llama al modelo
uv run gira-iso eval --replay eval/runs/2026-11-01.json   # re-puntúa, sin red
```

El dataset vive en `src/gira_iso/eval/cases.yaml`: 25 casos en categorías simple, compuesto,
ambiguo, fuera de alcance, adversario y bilingüe. Se escribió antes que el cliente, y una prueba
exige que toda especificación esperada sea válida bajo el esquema y la política.

### Subida a OpenStack

Fuera del MVP y opcional:

```bash
pip install 'gira-iso[openstack]'
uv run gira-iso push builds/<timestamp>-<hash> --cloud mycloud
uv run gira-iso push builds/<timestamp>-<hash> --disk-format raw   # backends Ceph/RBD
```

Las propiedades de Glance se derivan del manifest (`os_distro`, `os_version`,
`hw_qemu_guest_agent`, `hw_disk_bus`, y los hashes de especificación, política y base). El checksum
del artefacto se verifica antes de enviar un solo byte.

### Qué significa reproducibilidad aquí

Dos builds de la misma especificación producen manifests idénticos campo por campo, salvo
`created_at` y el hash del artefacto. **Los bytes del `.qcow2` difieren**, porque el sistema de
archivos registra fechas de instalación y metadatos de ext4.

`gira-iso` promete reproducibilidad **de comportamiento** —misma especificación, mismos paquetes,
mismos usuarios, misma configuración— no imágenes idénticas bit a bit. La reproducibilidad de bytes
está fuera de alcance y se documenta en [`backlog.md`](backlog.md).

### Depurar una imagen cruda

```bash
./scripts/boot-vanilla.sh --image ruta/a/imagen.qcow2
```

Arranca una imagen sin manifest e informa si cloud-init aplicó un seed. Existe para trabajo de fase
0 y depuración; la ruta soportada es `gira-iso verify`.

---

## Alcance y límites

- **Solo Ubuntu 22.04 y 24.04.** Otras distribuciones quedan fuera.
- **Solo cloud images.** Las imágenes de instalador (ISO) se rechazan.
- **La precisión del intérprete no está medida.** El arnés de 25 casos detecta regresiones groseras,
  pero no permite afirmar precisión ni comparar modelos.
- **Un manifest puede mentir.** El checksum ata los bytes, no las afirmaciones. Firmarlo queda para
  cuando alguien dependa de esa garantía.
- **La subida a OpenStack no se ha probado contra una nube real.**
- **Un build interrumpido se descarta entero.** No hay reanudación por pasos.

La lista completa y razonada está en [`docs/alcance.md`](docs/alcance.md); lo accionable, en
[`backlog.md`](backlog.md).

---

## Licencia

MIT. Ver [`LICENSE`](LICENSE).

---

<p align="center">
  <sub>Grupo de Investigación en Redes Avanzadas — GIRA PUCP</sub>
</p>
