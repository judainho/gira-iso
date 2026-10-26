# Flujo de control de gira-iso

Estado: **P0, P1 y P2 cerradas**. Las secciones 1 a 8 describen el código tal como estaba en P0 (commit `60e43e0`); donde P1 lo cambió, se indica. Este documento responde: *¿quién decide qué, en qué orden, dónde se puede parar todo y qué pasa cuando algo
falla?* La sección 9 resume los problemas encontrados; el plan para resolverlos está en la sección 10.
Cuando cada fase se complete, las secciones se actualizan al estado nuevo (ver sección 10).

Convención: el código, los identificadores y los comentarios están en inglés; este documento, en español.

---

## 1. Principio de confianza

Todo el diseño descansa en una idea: **el modelo de lenguaje nunca ejecuta nada ni decide qué es
seguro**. Solo propone un spec declarativo; lo que lo deja pasar es código determinista.

```mermaid
flowchart LR
    U[Persona] -->|texto libre| LLM[Modelo de lenguaje]
    LLM -->|JSON: spec + understood + unsupported| V1[Esquema Pydantic<br/>spec/models.py]
    V1 --> P1[Política<br/>policy/evaluate.py]
    P1 -->|rechazado| LLM
    P1 -->|aprobado| C[Confirmación humana]
    C --> B[build_image<br/>vuelve a evaluar la política]
    B --> PK[Packer + QEMU]
    PK --> M[manifest.json + qcow2]

    classDef untrusted fill:#fde2e2,stroke:#c0392b;
    classDef gate fill:#e2f0e2,stroke:#27ae60;
    class LLM untrusted;
    class V1,P1,B gate;
```

| Pieza | Puede | No puede |
|---|---|---|
| Modelo (`nl/`) | Proponer un spec, preguntas o un rechazo | Escribir scripts, URLs, repositorios; ampliar la lista de paquetes |
| Esquema (`spec/models.py`) | Rechazar formas inválidas (campos extra, tamaños, nombres) | Decidir qué paquetes son seguros |
| Política (`policy/`) | Rechazar paquetes, servicios, rutas y bases fuera de la lista | Nada con red ni con el sistema de archivos (función pura) |
| `build_image` | Volver a evaluar la política (segunda puerta, independiente del modelo) y construir | Aceptar un spec que no pasó |
| Packer/QEMU | Ejecutar la plantilla renderizada | Leer nada que no esté en la plantilla |

La política se evalúa **tres veces** en el camino de "crear" desde lenguaje natural: dentro de la
conversación (para que el modelo se corrija), en la vista previa y, de forma obligatoria, dentro de
`build_image`. Solo la última es una garantía; las otras mejoran la experiencia.

---

## 2. Mapa de módulos

Regla de dependencias (desde P2): **`cli` y `tui` → `workflow` → todo lo demás.** Nada del núcleo
conoce a las interfaces, y el orquestador no importa `typer` ni `rich` (hay un test que lo comprueba).

```mermaid
flowchart TB
    subgraph UI[Interfaces: solo presentan y responden]
        CLI["cli/ (create, images, advanced)"]
        TUI["tui/ (app, create, images, bases, views)"]
    end
    subgraph WF[Orden del flujo]
        WFC[workflow/create.py<br/>run_create]
        PORT[workflow/ports.py<br/>Io, Review, Decision]
        EVT[workflow/events.py]
    end
    subgraph NL[Lenguaje natural]
        SES[nl/session.py<br/>converse, policy_checker]
        CLN[nl/client.py]
        PRM[nl/prompt.py + catalog.yaml]
        EXP[nl/explain.py]
    end
    subgraph CORE[Núcleo determinista]
        POL[policy/evaluate.py]
        SPC[spec/models.py, canonical.py]
        BLD[builder/build.py]
        PKR[builder/packer.py]
        BAS[builder/base.py]
        EXPT[builder/export.py]
        CAT[builder/catalog.py]
        MAN[builder/manifest.py]
    end
    ERR[errors.py<br/>GiraError y códigos de salida]
    NAM[naming.py]
    VER[verify/verify.py, vm.py, goss.py]
    BSE[bases.py<br/>bases del usuario y rutas]
    SET[settings.py]
    PSH[push/openstack.py]

    CLI --> WFC
    TUI --> WFC
    CLI --> CAT & EXPT & PSH & NAM
    TUI --> CAT & EXPT & BSE & SET & NAM
    WFC --> PORT & EVT
    WFC --> SES & EXP & BLD & VER & EXPT & CAT
    SES --> CLN --> PRM
    SES --> POL
    EXP --> POL
    BLD --> POL & PKR & BAS & MAN
    BAS --> POL
    POL --> BSE
    VER --> CAT
    CAT --> MAN
    CLI -.usa.-> ERR
    BLD -.usa.-> ERR
```

Antes de P2 `cli.py` (629 líneas) y `tui.py` (678) importaban al núcleo y cada uno decidía el orden. Ahora
el código de interfaz se reparte por responsabilidad:

| Paquete | Módulos | Contenido |
|---|---|---|
| `cli/` | `_app`, `_shared`, `create`, `images`, `advanced` | la app Typer; guarda de errores y helpers; `create` y `build`; `list/download/show/delete`; comandos avanzados ocultos |
| `tui/` | `app`, `common`, `views`, `create`, `images`, `bases` | bucle del menú y clave; constantes y barra de descarga; tablas y paneles puros; crear; imágenes; bases |
| `workflow/` | `create`, `ports`, `events` | el orquestador, el puerto `Io` y los eventos tipados |

---

## 3. Flujo "crear" por el menú (`tui.create_image`)

```mermaid
sequenceDiagram
    actor P as Persona
    participant T as tui.create_image
    participant S as nl.session.converse
    participant M as Modelo
    participant Po as policy.evaluate
    participant B as builder.build_image
    participant V as verify_build
    participant E as export_image

    T->>P: elegir base (default 24.04)
    P-->>T: base
    T->>P: petición (o nº de ejemplo)
    P-->>T: texto
    loop hasta 3 rondas
        T->>S: converse(texto, contexto=base, check=política)
        S->>M: system(catálogo) + user
        M-->>S: spec | questions | reject
        alt questions
            S->>P: preguntas
            P-->>S: respuestas
        else spec
            S->>Po: evaluar
            Po-->>S: violaciones
            Note over S,M: si hay violaciones y quedan rondas,<br/>se las devuelve al modelo
        end
    end
    S-->>T: SessionResult
    T->>P: Lo que entendí / No puedo incluir + spec
    alt violaciones restantes
        T-->>P: "La política rechaza este spec" (FIN, nada se construyó)
    end
    T->>P: nombre, ¿construir?, ¿verificar?
    P-->>T: respuestas
    T->>B: build_image(progress, on_line, on_download)
    B->>Po: evaluar (puerta final)
    B-->>T: BuildResult | BuildRejected | BaseImageError | PackerError | RuntimeError
    opt verificar
        T->>V: verify_build
        V-->>T: checks | VerificationError
    end
    T->>P: ¿dónde guardo la imagen?
    T->>E: export_image
    E-->>T: rutas | ExportError
    T->>P: ruta final, sha256, tamaño
```

Puntos donde la persona puede detener el flujo: elegir base, petición vacía, preguntas del modelo (no
responder no es posible: hay que contestar o `Ctrl+C`), nombre inválido, "¿construir?", y `Ctrl+C` en
cualquier momento (vuelve al menú).

## 3b. El orquestador (desde P2)

El orden de "crear" vive en **un solo lugar**: `workflow/create.py::run_create`. El menú y el CLI
ya no deciden nada; implementan el puerto `Io` (`workflow/ports.py`) y reaccionan a eventos tipados
(`workflow/events.py`: `Step`, `Download`, `LogLine`, `Built`, `Verified`).

```mermaid
flowchart TD
    R[CreateRequest<br/>texto o spec] --> T{¿lenguaje natural?}
    T -- sí --> CV[converse con política<br/>io.answer, io.busy]
    CV -- el modelo rechaza --> S1([MODEL_REJECTED])
    CV -- pregunta y nadie responde --> S2([NEEDS_DETAIL])
    CV -- no produjo JSON válido --> S3([INTERPRET_ERROR])
    CV -- spec con violaciones --> S4([POLICY_REJECTED<br/>sin construir])
    CV -- spec --> EX[explain: verificar lo que dice el modelo]
    T -- no --> B0[base elegida por la persona]
    EX --> B0
    B0 --> RV{"¿request.review?"}
    RV -- sí --> IO[io.review<br/>nombre, confirmar, verificar]
    IO -- no procede --> S5([DECLINED])
    IO -- procede --> BU
    RV -- no --> BU[build_image<br/>vuelve a evaluar la política]
    BU -- BuildRejected --> S6([POLICY_REJECTED<br/>con verdict])
    BU -- GiraError / Ctrl+C --> PR[[se propaga al front end]]
    BU -- ok --> VF{"¿verificar?"}
    VF -- sí --> V[verify_build<br/>io.busy, evento Verified]
    VF -- no --> DS
    V --> DS{"io.destination"}
    DS -- ruta --> EXP[export_image]
    DS -- None --> OK
    EXP --> OK([BUILT])

    classDef stop fill:#fde2e2,stroke:#c0392b;
    classDef ok fill:#e2f0e2,stroke:#27ae60;
    class S1,S2,S3,S4,S5,S6 stop;
    class OK ok;
```

Reglas del orquestador:

- Un resultado normal (rechazo, "no", faltan datos) es un `CreateOutcome` con su `Status`. Un fallo
  inesperado es una `GiraError` y se **propaga**: el front end sabe cómo mostrarla (el CLI con su
  código de salida, el menú con un panel).
- `Ctrl+C` también se propaga; después de él no se verifica ni se exporta.
- La base la elige la persona (`request.base`); lo que el modelo escriba en `base` se reemplaza.
- `workflow/` no importa `typer`, `rich`, `cli` ni `tui` (hay un test que lo comprueba).

Qué cambia en cada front end: el **CLI** (`cli._CliIo`) escribe a stderr/stdout y pregunta con
`typer.prompt`; el **menú** (`tui._TuiIo`) usa tablas y paneles de `rich`. Los dos reciben el mismo
`Review`, así que el orden "mostrar → decidir → construir" ya no puede divergir.

---

## 4. Flujo "crear" por el CLI (`cli.create`)

Mismas fases, orden distinto y con otras decisiones:

```mermaid
flowchart TD
    A[create SOURCE] --> B{¿SOURCE es .yaml<br/>o archivo existente?}
    B -- sí --> C[_load_spec<br/>esquema]
    B -- no --> D{"¿hay --name?"}
    D -- no --> X2[exit 2]
    D -- sí --> E[_llm_config<br/>clave de entorno o prompt]
    E -->|"sin clave y --non-interactive"| X2
    E --> F[converse + check política]
    F -- reject --> X1[exit 1]
    F -- preguntas sin poder responder --> X1
    F -- error --> X2
    F -- violaciones --> X1
    F -- spec --> G[explain + imprimir]
    C --> H[resumen]
    G --> H
    H --> I{"¿NL, sin --yes<br/>e interactivo?"}
    I -- sí --> J{confirmar}
    J -- no --> X0[exit 0]
    I -- no --> K
    J -- sí --> K[build_image]
    K -- BuildRejected --> X1
    K -- BaseImageError / PackerError / RuntimeError --> TB[TRACEBACK sin capturar]
    K -- ok --> L{"¿--verify?"}
    L -- no --> O[JSON opcional + exit 0]
    L -- sí --> N[verify_build]
    N -- VerificationError --> TB
    N -- falla --> X1
    N -- ok --> O
```

### Diferencias entre los dos caminos

| Aspecto | Menú | CLI |
|---|---|---|
| Nombre de la imagen | se pide **después** de interpretar | `--name` obligatorio **antes** |
| Base | se elige en una lista; el modelo no decide | la decide el modelo (por defecto 24.04) |
| Vista previa del spec | siempre | solo con lenguaje natural y sin `--yes` |
| Exportar a una carpeta | sí, al final | no (hay `download` aparte) |
| Errores de build | capturados (`BaseImageError`, `PackerError`, `RuntimeError`, `KeyboardInterrupt`) | solo `BuildRejected`; el resto es **traceback** |
| Longitud máxima del nombre | 63 (`tui.NAME_RE`) | 64 (`cli._IMAGE_NAME`) |
| Spec desde archivo | no | sí |

---

## 5. Máquina de estados del menú

```mermaid
stateDiagram-v2
    [*] --> Banner
    Banner --> PedirClave
    PedirClave --> PedirClave: clave inválida (hasta 3)
    PedirClave --> [*]: 3 intentos fallidos
    PedirClave --> Menu: válida o no verificable
    Menu --> Listar: 1
    Menu --> SubirBase: 2
    Menu --> VerBases: 3
    Menu --> Crear: 4
    Menu --> [*]: 0, Ctrl+C o EOF
    Listar --> AccionesImagen: elegir nº
    AccionesImagen --> AccionesImagen: detalle / exportar
    AccionesImagen --> Menu: volver / eliminada
    Listar --> Menu
    SubirBase --> Menu
    VerBases --> Descarga: elegir base pendiente
    Descarga --> Menu
    VerBases --> Menu
    Crear --> Menu: fin, rechazo o error
    Listar --> Menu: Ctrl+C
    Crear --> Menu: Ctrl+C
```

La clave de API vive solo en memoria (`LlmConfig`); no se escribe en disco ni en logs. Lo único que se
recuerda entre ejecuciones es el último nombre y destino (`settings.py`).

---

## 6. Errores: quién los lanza y quién los captura

| Excepción | La lanza | `cli create` | Menú |
|---|---|---|---|
| `BuildRejected` | `builder/build.py` | exit 1 | tabla de violaciones |
| `BaseImageError` | `builder/base.py` | **traceback** | mensaje |
| `PackerError` | `builder/packer.py` | **traceback** | panel "Falló la construcción" |
| `RuntimeError` (directorio existe, Packer sin imagen) | `builder/build.py` | **traceback** | panel |
| `VerificationError` | `verify/verify.py` | **traceback** | mensaje |
| `VmError` | `verify/vm.py` | se convierte en un check fallido dentro de `verify_build` (parcial) | igual |
| `InterpretError` | `nl/client.py` | capturada en `converse` → exit 2 | mensaje |
| `ExportError` | `builder/export.py` | `download`/`delete`: exit 2 o 1 según `.invalid` | mensaje |
| `UserBaseError` | `bases.py` | (no aplica) | mensaje |
| `PushError` | `push/openstack.py` | `push`: exit 2 | (no aplica) |
| `KeyboardInterrupt` | — | **traceback** | vuelve al menú; Packer se termina |

Todas heredaban de `RuntimeError` **sin una raíz propia**, y el código de salida lo elegía cada comando a
mano (42 usos de `typer.Exit` en `cli.py`).

> **Desde P1:** todas heredan de `errors.GiraError`, que lleva su `exit_code`; cada comando del CLI
> está envuelto por `_guarded`, que convierte cualquier `GiraError` en `error: …` más su código y
> `Ctrl+C` en el código 130. Ya no hay trazas en `create`, `build` ni `verify`. Los códigos por clase:
> `BuildRejected` 1 · `ExportError` 2 si la entrada es inválida y 1 si se rechazó · `BaseImageError`,
> `PackerError`, `VmError`, `BuildError` 3 · el resto 2.

Contrato de salida documentado en el README: `0` éxito · `1` el motor o la verificación rechazaron algo
(o el modelo necesita más detalle) · `2` entrada inválida. El código `3` (fallos de entorno) y el `130` (interrumpido) se añadieron en P1; el chequeo previo que
anticipa esos fallos llega en P3 (C4).

---

## 7. Ciclo de vida de los artefactos

| Artefacto | Nace | Vive en | Se borra |
|---|---|---|---|
| API key | al pedirla / variable de entorno | memoria del proceso | al salir |
| Preferencias (último nombre/destino) | tras exportar en el menú | `~/.local/share/gira-iso/settings.yaml` | nunca |
| Base oficial descargada | primer uso o desde el menú | `~/.cache/gira-iso/images/` | nunca (hay que borrarla a mano) |
| Descarga parcial `*.part` | durante la descarga | junto a la base | se renombra al verificar; **queda huérfana si se interrumpe** |
| Base subida por el usuario | opción 2 | `~/.local/share/gira-iso/bases/` | nunca |
| Directorio de build | `build_image` | `~/.local/share/gira-iso/builds/<fecha>-<hash>/` | con `delete`; uno fallido queda marcado con `FAILED` y se borra con `clean` |
| Clave SSH del build `id_ed25519` | `build_image` | dentro del directorio de build | **siempre, al terminar el build** (éxito, fallo o `Ctrl+C`; control C2) |
| Plugin de Packer | `packer init` | `~/.packer.d/` | nunca |
| Imagen y manifest | build correcto | `builds/<id>/image/` y `manifest.json` | con `delete` |
| Copia exportada | menú o `download` | carpeta elegida | a mano |

Un directorio sin `manifest.json` no se lista (`scan_builds` lo ignora), por lo que los builds fallidos
son invisibles pero siguen ocupando disco y conservando la clave privada.

---

## 8. Controles que existen hoy

| Control | Dónde | Qué evita |
|---|---|---|
| Esquema cerrado (`extra="forbid"`) | `spec/models.py`, `nl/schema.py` | campos inventados por el modelo |
| Política como segunda puerta | `build_image` | spec que no pasó, aunque la UI lo permitiera |
| Verificación de sha256 de la base | `builder/base.py` | base alterada o descarga corrupta |
| Base registrada: solo qcow2, nunca ISO | `bases.py` | instalador usado como base |
| Autocorrección acotada (3 rondas) | `nl/session.py` | bucles infinitos con el modelo |
| Claims del modelo verificados | `nl/explain.py` | explicaciones que contradicen el spec |
| Borrado solo bajo `builds/`, sin symlinks | `builder/export.delete_build` | borrar fuera del área de trabajo |
| Terminar Packer con `Ctrl+C` | `builder/packer.py` | VM huérfana |
| Limpieza de la imagen (machine-id, claves de host, cloud-init) | `templates/scripts/cleanup.sh.j2` | imágenes que heredan estado del build |
| Validación de la clave de API | `nl/client.check_credentials` | descubrir una clave mala al final |

---

## 9. Hallazgos

Verificados leyendo el código; H7 requiere confirmarse con un build real.

| # | Hallazgo | Gravedad | Estado |
|---|---|---|---|
| H1 | El flujo "crear" está duplicado en `cli.create` y `tui.create_image`; cada uno decide el orden | alta (mantenimiento) | **resuelto en P2**: `workflow/create.py` |
| H2 | 10 clases de error sin raíz propia; el código de salida lo elige cada comando | media | **resuelto en P1**: `errors.GiraError` con `exit_code` |
| H3 | Dos reglas de nombre de imagen (63 vs 64 caracteres) | baja | **resuelto en P1**: `naming.py` (64 caracteres) |
| H4 | Progreso por tres callbacks sueltos en vez de eventos tipados | media | **resuelto en P2**: `workflow/events.py` (los callbacks de `build_image` se conservan por dentro) |
| H5 | `cli.py` (629) y `tui.py` (678) mezclan argumentos, presentación, flujo y comandos avanzados | media | **resuelto en P2**: paquetes `cli/` y `tui/` (ningún módulo pasa de 250 líneas) |
| H6 | Lectura de manifests triplicada (`cli._load_manifest`, `verify.load_manifest`, `export.scan_builds`) | baja | **resuelto en P1**: `builder/catalog.py` |
| H7 | El usuario de build `packer` (sudo NOPASSWD + clave pública del build) se crea por cloud-init y la limpieza no lo elimina; la clave privada queda en el directorio de build. Si se confirma, **toda imagen producida acepta esa clave** | **alta (seguridad)** | **control C1 implementado en P3**: `finalize.sh` elimina el usuario, verifica y recién apaga; goss lo comprueba. *Pendiente confirmar con un build real* |
| H8 | Un build fallido o cancelado deja el directorio con la clave privada; no hay forma de limpiar huérfanos | alta | **control C2 implementado en P3**: la clave se borra en `finally`; los fallidos llevan `FAILED`; `gira-iso clean` y la opción del menú |
| H9 | Sin candado: dos builds iguales en el mismo segundo chocan con `RuntimeError` | baja | **control C3 implementado en P3**: `builder/lock.py` (`flock`) y sufijo `-2`, `-3` si el nombre existe |
| H10 | Sin preflight: QEMU, KVM o espacio faltantes se descubren dentro de Packer | alta (experiencia) | **control C4 implementado en P3**: `workflow/preflight.py`; el menú muestra el estado al abrir |
| H11 | Sin timeout global del build; una descarga interrumpida deja un `.part` | media | pendiente (P3, C5 y C6) |
| H12 | `cli create` y el comando `build` solo capturan `BuildRejected`: un fallo de Packer, de checksum o de verificación termina en traceback | alta | **resuelto en P1**: guarda `_guarded` en todos los comandos |

---

## 10. Hacia dónde va

El plan aprobado ordena el trabajo en fases con un commit cada una:

1. **P0** este documento.
2. **P1** base común: `errors.py` (raíz `GiraError` con código de salida), `naming.py`, `builder/catalog.py`.
3. **P2** un solo orquestador `workflow/create.py` con eventos tipados y un puerto `Io`; `cli/` y `tui/`
   pasan a ser adaptadores que solo presentan y responden.
4. **P3** controles: C1 eliminar el usuario de build, C2 borrar la clave en `finally` y `gira-iso clean`,
   C3 candado, C4 preflight, C5 cancelación limpia, C6 timeout global, y código de salida `3` para
   fallos de entorno.
5. **P4** este documento actualizado al estado final.

Cada fase se registra en el log de [`plan-v1.md`](plan-v1.md) al cerrarse.
