# Arquitectura actual de gira-iso

Este documento describe cómo está organizado el proyecto hoy. Para el alcance y lo pendiente, ver
[alcance.md](alcance.md).

## Principio de diseño

El modelo de lenguaje **nunca genera scripts ni imágenes**. Solo produce una especificación
declarativa (`spec.yaml`), que se valida contra un esquema y una política de seguridad. Un motor
determinista construye la imagen a partir de esa especificación.

```
lenguaje natural ──► intérprete (LLM) ──► spec ──► esquema + política ──► builder (Packer/QEMU)
                                                                              │
                                              verificación (QEMU + goss) ◄────┤
                                                                              ▼
                                                        imagen qcow2 + manifest.json ──► push (OpenStack)
```

Ese límite es lo que hace el resultado auditable: lo que el modelo puede afirmar queda reducido a
campos validados, y todo lo que entra en la imagen pasa por la política.

## Flujo de una creación

1. **Entrada.** Una petición en lenguaje natural o un YAML. Un YAML se carga directamente; un texto
   pasa por el intérprete.
2. **Interpretación** (`nl/`). El cliente (`nl/client.py`) habla con cualquier endpoint compatible
   con OpenAI. Pide respuesta en JSON y reintenta una vez si la salida no valida. El prompt incluye el
   catálogo permitido, para que el modelo distinga un paquete (`docker` → `docker.io`) de un programa
   que apt no ofrece.
3. **Validación** (`spec/`, `policy/`). El esquema (Pydantic) rechaza formas inválidas. La política
   (`policy/evaluate.py`) devuelve *todas* las razones por las que no se puede construir: paquetes,
   snaps, servicios, rutas, repositorios y base. Si falla, las violaciones vuelven al modelo para que
   corrija su propuesta. Nada se construye hasta que la especificación final pasa.
4. **Construcción** (`builder/`). `build_image` descarga o reutiliza la imagen base fijada por
   sha256, renderiza la plantilla de Packer con cloud-init y scripts, y ejecuta el build. El resultado
   se escribe en un directorio de build con su `manifest.json`.
5. **Verificación** (`verify/`, opcional con `--verify`). Arranca la imagen con QEMU, habla con el
   guest a través del agente de QEMU (sin SSH) y ejecuta las aserciones goss generadas.
6. **Entrega.** El build queda en `~/.local/share/gira-iso/builds`. `download` copia imagen y
   manifest con comprobación de checksum; `push` sube la imagen a Glance.

## Módulos

| Ruta | Responsabilidad |
|---|---|
| `src/gira_iso/cli/` | Comandos de Typer: menú, `create`, `list`, `show`, `download`, `delete`, `clean`, `allow`, y los avanzados (`check`, `plan`, `build`, `verify`, `eval`, `policy`, `push`). |
| `src/gira_iso/tui/` | Menú interactivo. |
| `src/gira_iso/workflow/` | Orquestación de la creación (`create.py`, `run_create`) con puertos de entrada/salida, para que CLI y menú compartan el mismo flujo. |
| `src/gira_iso/nl/` | Intérprete: cliente OpenAI-compatible, prompt, esquema de respuesta, sugerencias de nombres de paquete y explicaciones. |
| `src/gira_iso/spec/` | Modelo de la especificación y forma canónica (hash `spec_hash`). |
| `src/gira_iso/policy/` | Lista permitida (`allowlist.yaml`) y evaluación. |
| `src/gira_iso/builder/` | Imagen base, renderizado de plantillas, Packer, manifest, exportación y bloqueo de builds. |
| `src/gira_iso/verify/` | Arranque en QEMU, canal del agente de QEMU, aserciones goss. |
| `src/gira_iso/push/` | Subida a OpenStack Glance (`openstack.py`). |
| `src/gira_iso/eval/` | Arnés de evaluación del intérprete: casos dorados, puntuación, grabación y reproducción. |
| `src/gira_iso/additions.py`, `proposals.py`, `releases.py`, `archive.py` | Paquetes añadidos localmente (con auditoría), propuestas, resolución de versiones y consulta al archivo de Ubuntu (Launchpad). |
| `src/gira_iso/templates/` | Plantillas de Packer, cloud-init y scripts de aserción, limpieza, finalización y repositorios. |
| `src/gira_iso/bases.py`, `settings.py`, `naming.py`, `errors.py` | Bases del usuario y rutas de datos, preferencias recordadas, reglas de nombre de imagen y errores con código de salida. |
| `src/gira_iso/cli/_app.py`, `_shared.py` | Definición de la app Typer y la guarda que convierte un `GiraError` en salida con código, sin traceback. |

La regla de dependencias es **`cli` y `tui` → `workflow` → núcleo**: el núcleo no conoce a las
interfaces y el orquestador no importa `typer` ni `rich`. Para el detalle de quién decide qué y
dónde se puede detener el flujo, ver [`flujo-de-control.md`](flujo-de-control.md).

## Política y fuentes de confianza

La política es el único lugar que decide qué entra en una imagen:

- **Paquetes de Ubuntu:** lista curada por versión de Ubuntu. Una adición local se permite solo para
  la versión contra la que se comprobó, y se registra en el manifest de cada imagen que la usa.
- **Snaps:** canal y confinamiento vienen de la política, no de la especificación. Revisiones fijadas.
- **Repositorios oficiales de terceros:** la especificación no los nombra; la política decide de qué
  repositorio viene cada paquete. La clave se instala como `/usr/share/keyrings/<id>.asc` y se fija
  por sha256.
- **Imágenes base:** pinadas por sha256.
- **Modo estricto** (`GIRA_ISO_STRICT=1`, o automático al apuntar `GIRA_ISO_ALLOWLIST` a una lista
  propia): ignora y rechaza las adiciones locales.

## Trazabilidad

Cada build escribe un `manifest.json` con: especificación original y su hash, versión del motor,
`PolicyRef` (fuente y sha256 de la política, y adiciones usadas), base, paquetes, servicios, snaps,
repositorios y checksum del artefacto. `show` lo presenta y `download` lo verifica.

## Modo automatizado

`--json` reserva stdout para un único objeto JSON; el progreso va a stderr. `--non-interactive` nunca
pregunta: la clave viene de `GIRA_ISO_LLM_API_KEY` y `--yes` queda implícito. Los códigos de salida
distinguen rechazo de política (1), entrada inválida (2), entorno incompleto (3) e interrupción (130).

## Configuración del intérprete

| Variable | Uso |
|---|---|
| `GIRA_ISO_LLM_BASE_URL` | Endpoint compatible con OpenAI. Por defecto, `https://api.llmapi.ai/v1`. |
| `GIRA_ISO_LLM_API_KEY` | Clave del endpoint. Obligatoria para llamar al modelo. |
| `GIRA_ISO_LLM_MODEL` | Identificador del modelo. |
| `GIRA_ISO_HOME` | Raíz de datos (builds, caché, adiciones). |

Como el cliente solo exige un endpoint compatible con OpenAI, un modelo local servido con llama.cpp
u Ollama puede usarse sin cambiar código. Ver [laboratorio-ia-local.md](laboratorio-ia-local.md).

## Pruebas

- Suite con `uv run pytest` (623 pruebas). Cubren esquema, política, builder con Packer simulado,
  CLI, menú, cancelación, limpieza, OpenStack con conexión falsa, y el canal del agente.
- Calidad con `ruff`.
- `scripts/check-toolchain.sh` compara el binario del plugin de Packer con el hash de
  `toolchain.lock`.
- No hay CI/CD: lint, pruebas y re-puntuación de grabaciones se ejecutan en local. El arnés se puede
  reproducir sin red con `gira-iso eval --replay <grabación>`.
