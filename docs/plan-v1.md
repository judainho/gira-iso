# gira-iso — Plan de desarrollo (v1)

Estado: revisión del plan v0. Objetivo del documento: ordenar el trabajo en fases con
hitos verificables, corrigiendo los huecos de seguridad y los acoplamientos que el v0
dejaba implícitos.

---

## 0. Qué cambia respecto al v0 (y por qué)

| v0 | v1 | Motivo |
|---|---|---|
| Se empieza con Packer sobre la cloud image | Se empieza con **un arranque vanilla** de la cloud image + seed | Antes de automatizar hay que demostrar que sabes arrancar y entrar a la VM. Si esto no funciona, ningún paso posterior es depurable. |
| `files: []` sin restricciones | `files` con **prefijo de ruta permitido**, `mode`, `owner` | Contenido arbitrario en ruta arbitraria = root. La lista blanca de paquetes no cubre esto. |
| Políticas de seguridad en el paso 6 | Políticas **junto al esquema** (paso 1) | Las reglas de seguridad determinan la forma del esquema. Diseñarlas después obliga a rehacer el contrato. |
| `goss` **o** `testinfra` | Solo `goss` | `testinfra` está sin mantenimiento desde 2021 e incompatible con pytest 8. |
| Canal de verificación sin definir | Decidido y probado en la fase 0 | Si el spec puede deshabilitar `ssh`, el verificador no puede entrar. Es una decisión de arquitectura, no un detalle. |
| Dataset dorado en el paso 5 junto al cliente | Dataset **antes** del cliente | El dataset es la verdad de referencia. Si se escribe después de ver las salidas del modelo, se contamina. |
| Salida = `.qcow2` | Salida = `.qcow2` + `manifest.json` | Procedencia (spec, hash del spec, hash de la imagen base, hash de la imagen final, veredicto de políticas, resultados de pruebas) desde el día uno. Barato ahora, imposible de reconstruir después. |
| Base sin verificar | Base **fijada por versión + sha256** en la política | La URL de la imagen base es un vector de cadena de suministro igual que un repositorio apt. |

---

## 1. Principios operativos

1. **El LLM no ejecuta nada.** Su única salida válida es un `spec` que pasa Pydantic y políticas.
2. **Todo lo determinista es determinista de verdad.** Misma spec + misma base + mismas versiones
   fijadas ⇒ mismo comportamiento observable. Se prueba, no se asume.
3. **Ninguna fase avanza sin su hito.** El hito se demuestra con un comando reproducible, no con
   "funcionó en mi terminal".
4. **Cada fase retira un riesgo concreto.** Si una fase no retira ninguno, sobra o está mal ubicada.
5. **La cadena de herramientas cumple las reglas que impone al usuario.** Nada de `curl | bash`
   para instalar Packer, goss o cloud-localds: binarios fijados por versión y verificados por sha256.

---

## 2. Decisiones cerradas

Resueltas antes de la fase 1. No se reabren sin una razón nueva y documentada en `## Log`.

**D1 — `ssh` y `qemu-guest-agent` no se pueden deshabilitar en v0.** *(cerrada)*
La política rechaza cualquier spec que los deshabilite. El verificador necesita un canal de
entrada garantizado. La consola serie se usa para diagnóstico de arranque, no como canal de pruebas.
Marshal por guest-agent vía virtio-serial queda como endurecimiento opcional posterior, no bloquea
el MVP.

**D2 — `files` solo escribe bajo prefijos permitidos.** *(cerrada)*
`policy/allowlist.yaml` declara prefijos permitidos (`/etc/nginx/`, `/etc/motd`, …) y la política
exige que cada `path` (a) sea absoluto, (b) no contenga `..`, (c) caiga dentro de un prefijo
permitido, (d) no resuelva por symlink fuera de él, (e) declare `mode` explícito.
Rutas de autorización, sudoers, unidades systemd fuera del prefijo y cron quedan fuera por
construcción — no por una lista negra.

**D3 — La imagen base se selecciona por identificador, no por URL.** *(cerrada)*
El spec dice `base: {os: ubuntu, version: "24.04"}`. El mapeo versión → URL + sha256 vive en la
política. Una URL arbitraria en el spec es un rechazo.

**D4 — Catálogo de paquetes curado.** *(cerrada)*
`policy/allowlist.yaml` contiene una lista explícita de paquetes de servidor, ampliable por PR.
No se acepta el componente `main` completo de Ubuntu, y no hay vía de escape por spec.

**D5 — `services` es una lista cerrada.** *(cerrada)*
Solo servicios declarados en el allowlist pueden habilitarse. Evita que el spec habilite unidades
arbitrarias presentes en el sistema.

**D6 — Nombre del proyecto.** *(cerrada, revisada 2026-10-26)*
El proyecto, el repositorio y el comando se llaman `gira-iso`. El paquete Python es `gira_iso`
porque los guiones no son válidos en un nombre de módulo.

**Defaults operativos:** x86_64 + BIOS sin UEFI en v0 · Ubuntu 24.04 amd64 cloud image ·
Python 3.12 con `uv` + Pydantic v2 · CLI con Typer · goss fijado por release con sha256 ·
docs en español; código, identificadores y comentarios en inglés · `manifest.json` desde F3 ·
licencia MIT.

---

## 3. Fases

### F0 — Entorno y línea base

**Objetivo:** probar que el hardware, el hipervisor y cloud-init funcionan, sin Packer y sin código propio.

**Trabajo**
- Instalar con versiones fijadas y sha256 verificado: Packer, `cloud-image-utils`, `goss`.
  Entorno Python con venv + Pydantic.
- Descargar la cloud image de Ubuntu 24.04, verificar checksum, fijar su sha256 en la política.
- A mano: overlay `qcow2` sobre la base, resize de disco, seed ISO con `cloud-localds`
  (usuario, clave SSH, hostname), arranque con `-enable-kvm`, `-nographic`/serie.
- Entrar por SSH, comprobar que `cloud-init status --wait` termina en `done` y que el disco
  creció hasta el tamaño pedido.

**Hito verificable:** `scripts/boot-vanilla.sh` arranca la imagen base sin modificar, cloud-init
aplica el seed (usuario y clave creados, hostname correcto) y el rootfs ocupa el disco entero.

**Retira el riesgo:** "no sé si KVM, la red de QEMU o cloud-init funcionan en esta máquina".
Es el riesgo que en v0 se descubría dentro de Packer, donde es mucho más caro de diagnosticar.

**Notas de entorno de esta máquina:** host Ubuntu 26.04, QEMU 10.2.1, KVM accesible vía ACL sobre
`/dev/kvm` (no hace falta el grupo `kvm`). `packer` y `goss` instalados en `~/.local/bin` fijados
por versión y sha256 verificado. Único paquete del sistema necesario: `cloud-image-utils`.

**Hallazgo de F0:** la cloud image oficial de Ubuntu 24.04 **no** trae `qemu-guest-agent`. De ahí
que esté en `mandatory_packages` del allowlist: el builder debe instalarlo siempre.

---

### F1 — Esquema `spec` v0 + contrato de políticas

**Objetivo:** definir el contrato entre el LLM y el motor, y la capa que lo puede rechazar.
Sin LLM, sin build.

**Trabajo**
- Modelos Pydantic: `base`, `disk_size`, `packages`, `services{enable,disable}`, `users`
  (nombre, sudo, claves), `files` (ruta restringida, contenido, modo, dueño).
- Reglas de validación de forma: nombres de usuario, formato de claves SSH, unidades de tamaño de
  disco, límites (número de paquetes, tamaño de contenido por archivo, tamaño total).
- `policy/allowlist.yaml`: paquetes permitidos, prefijos de rutas, servicios habilitables,
  versiones de base mapeadas a (URL, sha256).
- `policy.evaluate(spec) -> Verdict(passed | rejected, reasons[])`. Función pura: sin red, sin
  sistema de archivos, sin LLM.
- Suite de casos adversariales del propio veredicto: spec con `..`, con ruta de sudoers, con paquete
  fuera de lista, con base no reconocida, con servicio prohibido, con contenido gigante.
- Serialización canónica del spec a YAML/JSON (orden estable) para poder hashearlo.

**Hito verificable:** `gira-iso check spec.yaml` valida esquema y políticas, imprime el veredicto con
motivos, y la suite adversarial pasa entera sin build.

**Retira el riesgo:** "el contrato no expresa lo que el motor necesita, o expresa más de lo que la
política puede contener". Es el cambio más caro de hacer tarde.

---

### F2 — Build a mano con Packer

**Objetivo:** entender el build real antes de plantearlo.

**Trabajo**
- `templates/packer/ubuntu-24.04.pkr.hcl` escrito a mano: builder `qemu`, comunicador SSH,
  plugins fijados por versión (`packer init`).
- Provisioning mínimo: esperar a que cloud-init termine, instalar paquetes con
  `DEBIAN_FRONTEND=noninteractive` y reintento frente al lock de `unattended-upgrades`.
- Limpieza obligatoria: vaciar `/etc/machine-id`, limpiar `/var/lib/dbus/machine-id`,
  `cloud-init clean --logs`, `apt-get clean`, borrar claves de host y rastro de shell.
- Build y arranque del resultado con el mismo script de F0.

**Hito verificable:** un `.qcow2` con dos paquetes extra y un usuario creado arranca, cloud-init
corre de nuevo (no cree que ya está configurado) y `/etc/machine-id` está vacío. *(cumplido)*

**Retira el riesgo:** "las imágenes heredan estado del build". Aquí se descubre qué hay que limpiar
de verdad, no lo que la documentación dice que hay que limpiar.

---

### F3 — Builder determinista

**Objetivo:** `spec` → plantilla → imagen, sin intervención manual.

**Trabajo**
- Plantillas Jinja2: `spec` → `.pkr.hcl` y fichero cloud-init de provisionamiento.
- Renderizado a un directorio de build propio por ejecución (`builds/<timestamp>-<hash-spec>/`),
  nunca sobrescribiendo el anterior.
- Ejecución de Packer como subproceso con captura de log, código de salida y ruta del artefacto.
- `manifest.json`: hash del spec, hash del spec canónico, versión y sha256 de la base, versión de
  Packer, hash del `.qcow2` resultante, fecha.
- Prueba de determinismo: dos builds consecutivos de la misma spec → mismo comportamiento observable
  y mismo hash de spec canónico (el hash del `.qcow2` no tiene por qué coincidir; se documenta por qué).

**Hito verificable:** `gira-iso build spec.yaml` produce imagen + manifest, y dos ejecuciones seguidas
dan un manifest con el mismo hash de spec y artefactos funcionalmente equivalentes. *(cumplido)*

**Retira el riesgo:** "la automatización no reproduce lo que hice a mano".

---

### F4 — Verificación automática

**Objetivo:** probar la imagen arrancándola, no confiando en el log de Packer.

**Trabajo**
- Generación de un `goss.yaml` **derivado del spec**: paquete instalado, servicio activo, puerto
  escuchando (solo si el spec lo implica), usuario existente, clave presente, archivo con modo y
  contenido esperados.
- Overlay sobre la imagen, seed ISO generado, arranque con KVM, espera a `cloud-init status --wait`,
  ejecución de goss por SSH, apagado y limpieza garantizada (sin VMs huérfanas ni overlays sueltos
  incluso si el proceso falla).
- Diagnóstico: si no arranca, volcar la consola serie y los logs de cloud-init antes de fallar.

**Hito verificable:** `gira-iso verify <build>` sale con 0 en una imagen correcta y con código distinto
de cero, con diagnóstico legible, en una imagen a la que le falta algo del manifest. *(cumplido)*

**Retira el riesgo:** "la imagen pasa el build pero no sirve".

---

### F5 — Dataset dorado y cliente LLM

**Objetivo:** medir la interpretación antes de optimizar prompts.

**Trabajo, en este orden**
1. Dataset dorado primero: pares prompt → spec esperado, en las seis categorías del v0
   (simples, compuestos, ambiguos, fuera de alcance, adversariales, es/en). Cada caso declara su
   expectativa: spec exacto, pregunta aclaratoria, o rechazo.
2. Cliente independiente del modelo (compatible con SDK de OpenAI, `base_url` configurable), salida
   JSON, validación Pydantic, **un** reintento con el error de validación.
3. Ambigüedad: el esquema incluye una salida alternativa `questions[]`; el cliente la devuelve en
   lugar de inventar valores.
4. Defensa de inyección: el texto del usuario va delimitado explícitamente como dato, el sistema no
   acepta instrucciones del contenido, y la política se evalúa aunque el modelo "obedezca".
5. Arnés `pytest` de métricas: validez sin reintento, precisión/recall por campo, tasa de
   alucinación, acierto en ambigüedad, bloqueo de adversariales, latencia y costo.
6. Comparación con un modelo de línea base sobre el mismo dataset.

**Hito verificable:** el dataset y el arnés producen una tabla de métricas reproducible para dos
modelos. *(código cerrado; medición pendiente de endpoint)*

**Retira el riesgo:** "no sabemos si un modelo barato alcanza".

---

### F6 — Endurecimiento y cierre del MVP

**Objetivo:** lo que falta para que esto sea usable por alguien más que tú.

- Política configurable (archivo por equipo/organización) sin tocar código.
- Documentación de uso por fase; `--help` real en cada comando.
- CI: la suite de políticas, la suite de evaluación con grabaciones de respuestas cacheadas
  (sin llamar al modelo en cada push), y lint.
- Endurecimiento final de la imagen: sin cuentas con contraseña, sin `sudo` sin contraseña salvo
  lo que pida el spec, cloud-init limpio.

**Hito verificable:** un tercero clona el repo y llega de `spec.yaml` a imagen verificada siguiendo
solo la documentación. *(cumplido salvo el punto de guest-agent, movido a backlog)*

---

### F7 — OpenStack (fuera del MVP)

Subida a Glance con `openstacksdk`, metadatos `os_distro`, `os_version`, `hw_qemu_guest_agent`,
`hw_disk_bus`, formato `raw` si el backend es Ceph, y prueba de arranque real.

Estado: implementada y testeada contra una conexión falsa; la prueba de arranque real requiere una
nube y queda en backlog.

---

## 4. Estructura del repositorio (revisada)

```
gira-iso/                    # project and repository are `gira-iso` (see D6)
├── src/gira_iso/
│   ├── cli.py               # Typer: list, create, download (+ advanced commands)
│   ├── spec/                # Pydantic models + canonical form
│   ├── policy/              # allowlist.yaml + evaluate() pura
│   ├── builder/             # spec -> HCL/cloud-init, runner de Packer, manifest
│   ├── verify/              # overlay + seed + qemu + goss generado
│   ├── nl/                  # cliente LLM + prompts
│   └── eval/                # dataset dorado + métricas
│   └── templates/           # plantillas Jinja dentro del paquete
│       ├── packer/          # image.pkr.hcl.j2
│       └── scripts/         # assert.sh.j2, cleanup.sh.j2
├── scripts/                 # boot-vanilla.sh (verificación manual, previa a F4)
├── examples/                # specs de ejemplo que deben seguir siendo válidos
├── docs/                    # este plan, notas por fase
└── tests/                   # unitarias + adversariales de política
```

Cambios respecto al v0: `src/` layout (evita que `spec`, `policy` o `builder` colisionen como
módulos de nivel superior al instalar el paquete), `scripts/` para las herramientas manuales de
F0, `examples/` con un spec que la suite verifica, y `docs/`.

---

## 5. Matriz de riesgos (revisada)

| Riesgo | Mitigación | Fase que lo retira |
|---|---|---|
| `files` como vía de escalada a root | Prefijos permitidos, sin `..`, sin symlink fuera, modo explícito | F1 |
| Cadena de suministro de la base | Versión → (URL, sha256) en política; URL arbitraria rechazada | F1 |
| El spec no deshabilita la verificación | `ssh` y `qemu-guest-agent` obligatorios en v0 | F1 |
| Inyección de instrucciones | Texto delimitado como dato + política independiente del modelo | F5 |
| El LLM alucina paquetes | Lista blanca | F1 / F5 |
| Imagen con estado heredado | Pasos de limpieza obligatorios + arranque limpio con cloud-init | F2 / F4 |
| Build no reproducible | Spec canónico hasheado + versiones fijadas + manifest | F3 |
| El verificador no distingue fallo de imagen de fallo de entorno | Consola serie y logs de cloud-init como diagnóstico obligatorio | F4 |
| Modelo barato insuficiente | Dataset antes que prompts; comparación con línea base | F5 |
| Trampa del propio tooling (`curl \| bash`) | Binarios fijados por versión y sha256 | F0 |

---

## 6. Definition of Done global

El MVP está cerrado cuando, desde una descripción en español o inglés:

1. Se obtiene un `spec` válido o una pregunta aclaratoria, nunca un valor inventado.
2. Un spec peligroso es rechazado con motivo, sin build.
3. El build produce `.qcow2` + `manifest.json` reproducibles.
4. La verificación arranca la imagen y comprueba el spec campo por campo.
5. Las métricas de evaluación son reproducibles para al menos dos modelos.

---

## 7. Bitácora

**2026-10-26 — F0 cerrado.** `scripts/boot-vanilla.sh` arranca la cloud image 24.04 sin modificar
sobre un overlay de 8G, con seed NoCloud. Comprobado en dos ejecuciones consecutivas: hostname
`gira-iso-f0`, `cloud-init status: done`, clave ed25519 del seed instalada, `PasswordAuthentication`
en `no`, `/dev/vda` expandido a 8.0G. Artefactos en `builds/vanilla-<ts>/`.
Defecto propio corregido durante la fase: el directorio de trabajo por defecto resolvía relativo al
cache de imágenes y no al repositorio, porque el script hace `cd` al cache antes de crearlo.

**2026-10-26 — Hallazgo.** La cloud image oficial de Ubuntu 24.04 no incluye `qemu-guest-agent`.
Consecuencia directa: pasa a `mandatory_packages` y el builder lo instala siempre.

**2026-10-26 — F1 cerrado.** `spec/models.py` (reglas de forma), `spec/canonical.py` (hash estable),
`policy/allowlist.yaml` y `policy/evaluate.py` (función pura). `gira-iso check` y `gira-iso plan`
implementados con códigos de salida 0/1/2. 63 tests en verde, entre ellos 6 casos adversariales de
clave SSH, 5 de rutas de archivo y la invariante de que `mandatory_packages ⊆ packages`.
Decisión tomada al escribir el esquema: en v0 una clave SSH debe ser `ssh-ed25519` desnuda, sin
opciones ni comentario, porque las opciones son la vía clásica de meter un `command=` en
`authorized_keys`.
Desviación del plan registrada: el repositorio usa `src/` layout para que `spec`, `policy` y
`builder` no colisionen como módulos de nivel superior.

**2026-10-26 — F2 cerrado.** `templates/packer/ubuntu-24.04.pkr.hcl` con plugin `qemu` fijado a
`1.1.7`, seed de build por `cd_files` + `cd_label "cidata"`, retry frente al lock de apt, limpieza
(`machine-id`, `cloud-init clean --logs --seed`, claves de host, historial) y aserciones dentro del
guest que abortan el build si algo quedó sucio. Build de 48 s. Artefacto de 8G virtual.
Verificado arrancando el artefacto con un seed nuevo: el hostname del seed se aplica (prueba de que
cloud-init no cree que ya está configurada), `nginx` y `qemu-guest-agent` presentes.
Tres trampas encontradas y ya reflejadas en `backlog.md`: `disk_size` de Packer vale 40960 MB por
defecto incluso sobre un `disk_image` y produjo un artefacto de 40G; el plugin 1.1.7 no expone
`cpu_type` y `-cpu host` hay que pasarlo por `qemuargs`; y un overlay menor que su backing file no
da error, deja al guest en initramfs con `LABEL=cloudimg-rootfs does not exist`.


**2026-10-26 — F3 cerrado.** `gira-iso build` renderiza el spec a plantilla Packer y a seed de
cloud-init, ejecuta Packer como subproceso con log en `builds/<ts>-<hash>/logs/` y escribe
`manifest.json` con hash del spec, base fijada, versión de Packer y del plugin, hash de la
plantilla y hash del artefacto. Build en 42 s, 10G virtual.

Determinismo comprobado así: dos builds consecutivos del mismo spec producen manifests idénticos
campo a campo salvo `created_at` y el hash del artefacto. **Los bytes del `.qcow2` no coinciden**,
y es esperado: el sistema de archivos guarda marcas de tiempo de instalación y metadatos ext4 que
no controlamos. La reproducibilidad que se promete es de comportamiento (mismo spec, mismos
paquetes, mismos usuarios, misma configuración), no de bits. Un build bit a bit idéntico requeriría
`SOURCE_DATE_EPOCH`, ext4 determinista y `mkfs` reproducible: fuera del MVP, anotado en el backlog.

Decisiones de esta fase: el seed de cloud-init se genera como diccionario de Python y se serializa
con `yaml.safe_dump`, no con Jinja, porque el contenido de `files` es texto arbitrario y la
indentación de una plantilla lo altera en silencio (espacios finales dentro de un bloque escalar).
Solo el HCL y los scripts de shell pasan por Jinja. La política se evalúa **antes** de descargar la
base, así que un spec rechazado no cuesta ancho de banda ni escrituras.

Absorbido y eliminado: `scripts/build-f2.sh` y la plantilla escrita a mano. Dejarlos habría creado
dos caminos divergentes. `scripts/boot-vanilla.sh` se queda como verificador manual hasta que F4
lo reemplace con goss.


**2026-10-26 — F4 cerrado.** `gira-iso verify <build-dir|manifest.json>` lee el manifest (no el spec
del disco), comprueba el checksum del artefacto **antes de arrancar**, genera `goss.yaml` desde el
spec, arranca un overlay con seed nuevo, espera a cloud-init y ejecuta goss dentro del guest.
Cleanup garantizado por context manager: no quedan procesos ni overlays.

Verde: 20 aserciones. Prueba negativa: un manifest que declara `redis-server` sobre una imagen que
no lo tiene falla con **3 de 23** aserciones y código de salida 1. Un verificador que solo sabe
aprobar no verifica nada.

Tres hallazgos de la fase:
1. `qemu-guest-agent` salía *enabled* pero **no** *running*. La unidad se ata al dispositivo del
   puerto virtio-serial, y la VM de prueba no lo exponía. Se arregló la VM, no la aserción: una nube
   real expone ese canal, y relajar la comprobación habría ocultado un fallo real. Además
   `virtserialport` sin `virtio-serial-pci` hace que QEMU se niegue a arrancar.
2. El campo de goss es `test-count`, no `tested-count`; el error se veía como "1 of 0".
3. El contenido de `files` se verifica por **sha256**, no por regex: el contenido es texto
   arbitrario lleno de llaves y barras.

No se inventan aserciones de puerto: el spec no declara puertos, así que "nginx escucha en 80" no
es una promesa del spec. Verificar lo que no se prometió es otra forma de mentir.


**2026-10-26 — F5: código cerrado, medición pendiente.** Entregado: dataset dorado de 25 casos en
seis categorías (13 es / 12 en), esquema de salida cerrado (`spec` / `questions` / `reject`),
cliente compatible con OpenAI con reintento único que devuelve el error de validación al modelo,
defensa de inyección por delimitadores, scoring puro, agregación de métricas y `gira-iso eval` con
`--record` y `--replay`.

El orden importó: el dataset se escribió **antes** del cliente. Un dataset autorado después de ver
salidas del modelo mide la concordancia con el modelo, no la interpretación. Hay un test que exige
que **toda expectativa de tipo `spec` pase esquema y política**; si el dataset pidiera algo que la
política rechaza, estaría roto por definición.

El arnés se probó con un completer oráculo (responde desde las expectativas): 25/25 y todas las
métricas a 1.0. Eso demuestra la plomería, **no** el desempeño del modelo; el oráculo no es un
modelo y no se guarda como resultado.

**No se ejecutó contra un modelo real.** En esta máquina existe `LLMAPI_API_KEY` pero no hay
`base_url` ni modelo declarados, y adivinar un endpoint para gastar una credencial ajena no es una
decisión que me corresponda. Falta que el usuario aporte `GIRA_ISO_LLM_BASE_URL` y
`GIRA_ISO_LLM_MODEL`. Criterio de éxito de la fase (comparar dos modelos) sigue abierto.


**2026-10-26 — F6 cerrado.** Política configurable por entorno (`GIRA_ISO_ALLOWLIST`) con comando
`gira-iso policy` que muestra el archivo activo y su hash; ese hash se registra ahora en el manifest,
de modo que una imagen se puede rastrear hasta las reglas que la permitieron. `toolchain.lock` fija
packer y goss por versión y sha256, y `scripts/check-toolchain.sh` verifica una máquina contra el
lock; QEMU se comprueba por versión mínima porque sus parches no cambian el artefacto. Añadidos
`LICENSE` (MIT), dos workflows de CI: `ci.yml` (lint, tests, ejemplo válido, y un job que instala el
toolchain fijado en una máquina limpia para probar que el lock sirve) y `eval.yml` (manual, con
secretos, guarda la grabación como artefacto). README reescrito con un camino "From zero" y una
sección de depuración para imágenes sin manifest.

Verificado de punta a punta tras los cambios: `gira-iso build examples/nginx-web.yaml` (45 s) produce
manifest con `policy.sha256`, y `gira-iso verify` sobre ese build da 20 aserciones en verde.

Punto de F6 no hecho: verificación por `qemu-guest-agent` como alternativa a SSH. El plan lo marca
como endurecimiento opcional y queda en backlog con el resto.

**2026-10-26 — F7 implementada, no probada.** `src/gira_iso/push/openstack.py` sube a Glance con
propiedades derivadas del manifest, verifica el checksum del artefacto antes de enviar un byte,
acepta `qcow2` y `raw`, y rechaza un formato desconocido. `openstacksdk` es dependencia opcional y
la conexión es inyectable, que es como está testeada: siete tests con una conexión falsa. La prueba
de arranque real en OpenStack no se ejecutó porque no hay nube disponible, y no se va a simular.

**2026-10-26 — Renombrado a `gira-iso`.** D6 revisada: el nombre oficial es `gira-iso`. Se
renombraron el paquete (`src/gira_iso`), el comando (`gira-iso`), las variables de entorno
(`GIRA_ISO_*`), el caché (`~/.cache/gira-iso`), el artefacto
(`gira-iso-ubuntu-24.04.qcow2`), CI, scripts y documentación. Motivo: `vmforge` se había
registrado en D6 como decisión del plan, pero no era el nombre del producto. No había builds
reales todavía, así que no hubo manifests ni imágenes que migrar; solo se movió el caché de la
imagen base.

**2026-10-26 — Flujo de control, P0: estado actual documentado.** Nuevo
[`flujo-de-control.md`](flujo-de-control.md) con cinco diagramas Mermaid (confianza, módulos, flujo
"crear" por menú y por CLI, estados del menú), la tabla de errores, el ciclo de vida de artefactos y
doce hallazgos. Los más importantes: el flujo de "crear" está duplicado en `cli.py` y `tui.py`; el
CLI solo captura `BuildRejected` y deja que un fallo de Packer termine en traceback; no hay preflight
ni limpieza de builds fallidos; y, por lectura de código, el usuario de build `packer` (sudo NOPASSWD
y la clave pública del build) no se elimina de la imagen final. Esto último **no está confirmado con
un build real** y es el primer control a cerrar en P3. Fases siguientes: P1 base común (errores,
nombres, catálogo de builds), P2 orquestador único, P3 controles, P4 documento final.

**2026-10-26 — Flujo de control, P1: base común.** Todas las excepciones esperadas heredan de
`errors.GiraError`, que lleva su código de salida (`0` éxito, `1` rechazado, `2` entrada inválida,
`3` entorno nuevo, `130` interrumpido). Cada comando del CLI está envuelto por `_guarded`: un fallo de
Packer, de checksum de la base o de verificación ya no termina en traceback (H12). `naming.py` es la
única regla de nombres (64 caracteres; el menú aceptaba 63) y `builder/catalog.py` el único lugar que
lee manifests y resuelve referencias (antes triplicado). 26 tests nuevos; 258 en total.

**2026-10-26 — Flujo de control, P2a: un solo orquestador.** `workflow/create.py::run_create` fija el
orden de "crear" (interpretar con política, explicar, rechazar, revisar, construir, verificar,
exportar) y devuelve un `CreateOutcome` tipado. El menú (`tui._TuiIo`) y el CLI (`cli._CliIo`)
implementan el puerto `Io` y reaccionan a eventos (`Step`, `Download`, `LogLine`, `Built`,
`Verified`); ya no deciden el orden. El comando oculto `build` también pasa por el orquestador.
`workflow/` no importa `typer`, `rich`, `cli` ni `tui`. 23 tests nuevos del orquestador con un
front end falso; 281 en total. Los tests de CLI y menú ahora parchean
`gira_iso.workflow.create.build_image`. Queda para P2b dividir `cli.py` y `tui.py` en paquetes.

**2026-10-26 — Flujo de control, P2b: `cli/` y `tui/` en paquetes.** `cli.py` y `tui.py` se dividieron
por responsabilidad con un script basado en `ast` (sin tocar la lógica): `cli/{_app,_shared,create,
images,advanced}` y `tui/{app,common,views,create,images,bases}`. Ningún módulo pasa de 250 líneas. El
punto de entrada `gira_iso.cli:app` no cambió y `python -m gira_iso.cli` funciona. Cambio cosmético:
en la ayuda, `create` aparece antes que `list`. Los tests ahora parchean el módulo que usa cada
nombre (`gira_iso.tui.app.check_credentials`, `gira_iso.cli.create.OpenAiCompatible`, ...). 281 tests.

**2026-10-26 — Flujo de control, P3 / C1: el usuario de build ya no queda en la imagen.** Se confirmó
por lectura de código que `cleanup.sh` solo borraba el historial del usuario `packer`, que tiene sudo
sin contraseña y cuya clave privada queda en el directorio de build. Ahora el `shutdown_command` de
Packer ejecuta `finalize.sh` (nuevo): elimina el usuario y solo su línea del archivo sudoers
compartido, verifica que no queda ni usuario, ni grupo, ni home, ni regla sudo, ni la clave
(marcador `gira-iso-build`), y recién entonces apaga. Si algo falla, no apaga y el build falla sin
manifest. La verificación (goss) comprueba lo mismo en la imagen final. La política reserva los
nombres `root`, `packer` y `gira-iso` para usuarios del spec. 14 tests nuevos, entre ellos uno que
ejecuta el script contra un sistema simulado (éxito y cuatro modos de fallo). **No probado con un
build real:** esta máquina no tiene QEMU. Hay que ejecutar `gira-iso create examples/nginx-web.yaml
--name web --verify` y comprobar que aparece el check de usuario ausente.

**2026-10-26 — Flujo de control, P3 / C2: nada sensible sobrevive a un build fallido.** `build_image`
borra la clave SSH del build en un `finally` (éxito, fallo o `Ctrl+C`) y, si el build no termina,
escribe un archivo `FAILED` con la causa. `catalog.find_incomplete` reconoce los directorios que la
herramienta creó y no llegaron a tener manifest (marcador o subdirectorios propios); nunca toca
carpetas ajenas ni sigue enlaces simbólicos. Nuevo comando `gira-iso clean` (pide confirmación; `--yes`
la omite) y, en la pantalla de imágenes del menú, la opción `limpiar`. 15 tests nuevos.

**2026-10-26 — Flujo de control, P3 / C3: un build a la vez.** `builder/lock.py` toma un `flock`
exclusivo sobre `builds/.lock` durante todo `build_image`; el segundo intento falla con `BuildBusy`
(código 3) y nombra el proceso que lo tiene. El kernel suelta el candado si el proceso muere, así que
un cierre brusco no lo deja trabado. `gira-iso clean` toma el mismo candado: un build en curso parece
incompleto y no debe borrarse. Si el directorio ya existe (mismo spec en el mismo segundo) se añade
`-2`, `-3` en lugar de fallar. 8 tests nuevos.

**2026-10-26 — Flujo de control, P3 / C4: preflight.** `workflow/preflight.py` comprueba, antes de
tocar el modelo, la red o el disco, que existan `qemu-system-x86_64`, `qemu-img`, `cloud-localds`,
`ssh-keygen`, `packer` y `goss`, que `/dev/kvm` sea legible y escribible (nombra el grupo real del
dispositivo) y, con el tamaño ya conocido del spec, que haya espacio libre (`disk_size` + 2 GB). Cada
problema trae el comando exacto que lo arregla (`apt-get install` solo de lo que falta;
`./install.sh` para packer y goss; `usermod -aG <grupo>`). Todos se juntan en un solo
`EnvironmentProblem` (código 3). El menú muestra "Entorno listo" o el panel de problemas al abrir y
sigue siendo utilizable. Los tests desactivan el preflight real con una fixture automática, para que
la suite pase en una máquina sin QEMU; 20 tests lo prueban por separado. Primera comprobación real: en
la máquina de desarrollo detecta justo el permiso de `/dev/kvm` que faltaba.

**2026-10-26 — Flujo de control, P3 / C5 y C6: cancelar o agotar el tiempo no deja nada vivo.**
Al revisar la cancelación apareció un fallo real: si `Ctrl+C` llegaba mientras la VM de verificación
arrancaba (dentro de `__enter__`), Python no llamaba a `__exit__` y el proceso QEMU quedaba
huérfano con la imagen abierta; ahora `__enter__` detiene el invitado ante cualquier fallo. La
descarga de la base borra el `.part` ante cualquier causa (checksum, red, `Ctrl+C`), convierte los
errores de red en `BaseImageError` (código 3, antes eran un traceback) y tiene timeout por operación
de socket (60 s). Packer tiene un timeout global de 45 minutos (`GIRA_ISO_BUILD_TIMEOUT_MINUTES` lo
cambia; 10 minutos para `packer init`): al agotarse se termina el proceso, se mata si no sale en
20 s y se informa con `PackerError`. 16 tests nuevos, entre ellos uno con un proceso real detenido por
el temporizador. Con esto se cierran C1 a C6; queda P4 (documento final).

**2026-10-26 — Flujo de control, P4: documento final y cierre.** `docs/flujo-de-control.md` se reescribió
al estado real: confianza, mapa de módulos con la regla de dependencias, el flujo de `run_create` con el
preflight, la máquina de estados del menú, la tabla de errores y códigos (0, 1, 2, 3 y 130), el ciclo de
vida de los artefactos, los controles C1 a C6 con su prueba, el cierre de los doce hallazgos y, aparte, lo
que **no** está hecho. El backlog recoge lo pendiente: confirmar C1 con un build real (nuevo punto
bloqueante), reintentos y reanudación, limpieza de la caché de bases y recetas para software fuera de apt.
Balance del trabajo: 232 tests al empezar, 352 al terminar; `cli.py` y `tui.py` (1307 líneas) pasaron a
dos paquetes sin módulos de más de 250 líneas.

