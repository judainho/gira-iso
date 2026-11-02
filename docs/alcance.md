# Alcance del proyecto gira-iso

Estado a 1 de noviembre de 2026. Este documento separa lo que está implementado, lo que está
implementado pero sin probar en su entorno real, y lo que queda fuera. Para el detalle técnico,
ver [arquitectura.md](arquitectura.md); para el índice completo, [README.md](README.md). Para la
lista de tareas abiertas, ver [backlog.md](../backlog.md).

## Objetivo

Convertir una descripción en lenguaje natural (o una especificación YAML) en una imagen `qcow2`
reproducible y lista para la nube, con un proceso auditable: el modelo solo propone, y una política
decide qué se puede construir.

## Estado por fase

El plan original está en [plan-v1.md](plan-v1.md).

| Fase | Contenido | Estado |
|---|---|---|
| F0–F4 | Esquema, política, construcción con Packer, manifest, CLI básica | Cerradas |
| F5 | Intérprete de lenguaje natural y arnés de evaluación | Código cerrado; **sin medir contra un modelo real** |
| F6 | Verificación en QEMU con goss | Cerrada, salvo un punto |
| F7 | Subida a OpenStack Glance | Implementada; **sin probar contra una nube real** |
| Extra | Paquetes añadidos localmente, snaps, repositorios oficiales | Implementados y revisados |

## Lo que funciona hoy

**Creación de imágenes**
- Entrada en lenguaje natural o YAML, en español o inglés.
- Revisión de la especificación interpretada antes de construir (en el menú y en modo interactivo).
- Rechazo temprano: una especificación que la política no permite no descarga nada ni construye.
- Bases Ubuntu 22.04 y 24.04, pinadas por sha256. Se pueden descargar las oficiales o usar una imagen
  propia (cloud image qcow2).

**Política y paquetes**
- Lista curada de 48 paquetes (42 del archivo de Ubuntu y 6 de repositorios de terceros), 2 snaps,
  2 repositorios con clave fijada, 11 servicios y 8 prefijos de rutas.
- Adición de paquetes del archivo de Ubuntu bajo confirmación del operador, comprobada contra
  Launchpad, solo en `main` y `universe`, y con una lista de exclusiones.
- Modo estricto para entornos que no admiten adiciones locales.

**Verificación**
- Arranque en QEMU y aserciones goss generadas a partir de la especificación.
- Canal con el agente de QEMU; no requiere SSH.

**Operación**
- Menú interactivo y comandos `list`, `show`, `download` (con verificación de checksum), `delete`,
  `clean` (builds fallidos y bases sin uso).
- Modo `--json` y `--non-interactive` para automatización, con códigos de salida definidos.

**Calidad**
- Suite de 623 pruebas; pasa completa en local.
- Calidad de código con `ruff`.
- Revisiones de seguridad aplicadas en los últimos cambios: la verificación de repositorios ocurre
  después de que cloud-init termine, las validaciones usan anclas estrictas, y los fallos de la
  verificación independiente quedaron corregidos y comprobados.
- No hay CI/CD: las comprobaciones se ejecutan en local y la re-puntuación de grabaciones usa
  `gira-iso eval --replay`.

## Implementado pero sin probar en el entorno real

- **Subida a OpenStack.** Probada con una conexión falsa. Falta un `clouds.yaml`, un proyecto real, y
  la prueba de arranque de una instancia creada desde la imagen subida.
- **Intérprete contra un modelo real.** El código está, y la evaluación se ejecuta, pero no hay una
  clave de API válida para el endpoint por defecto (que responde `401` a las peticiones de chat).
  Sin esto no hay métricas comparables ni grabación de línea base.
- **Traducción de nombres de paquete con el modelo real** (`nl/suggest.py`): no probada.

## Límites conocidos

- **Precisión del intérprete sin medir.** El arnés tiene 25 casos que detectan regresiones groseras,
  pero no alcanzan para afirmar precisión ni comparar modelos.
- **Un manifest puede mentir.** El checksum liga los bytes, no las afirmaciones del manifest. Firmar el
  manifest queda para cuando alguien dependa de esa garantía.
- **Dependencias de paquetes añadidos.** Permitir un paquete permite todo su árbol de dependencias, y
  hoy solo se avisa; no se muestra el árbol completo.
- **Un build interrumpido se descarta entero.** No hay reanudación ni reintentos por paso.
- **Reproducibilidad a nivel de byte.** Los manifests coinciden entre builds, pero el `qcow2` no.
  Está fuera de alcance salvo que alguien necesite comparar imágenes por hash.
- **Pines que requieren revisión manual.** Revisiones de snaps (`docker=3618`, `certbot=5911`) y el
  hash del plugin de Packer. Cada cambio se hace con su verificación en el mismo commit.

## Fuera de alcance (por ahora)

- **Software fuera de apt, snaps y repositorios oficiales.** Por ejemplo, binarios de GitHub: un asset
  de release es un binario opaco que corre como root. Exigiría fijar tag, nombre y sha256 por entrada,
  y prohibir `latest`. Falta decidir cómo se revisa.
- **Otras distribuciones.** Solo Ubuntu 22.04 y 24.04.
- **Imágenes de instalador (ISO).** Se rechazan; solo se aceptan cloud images.
- **Firma de manifests y builds reproducibles a nivel de byte.**

## Riesgos

- **La lista de paquetes permitidos es una decisión de seguridad de la organización, no técnica.** La
  clave de un repositorio de terceros equivale a confianza root permanente para lo que apt instale de
  ese origen. Debe revisarse antes de usar el sistema en serio.
- **Dependencia de un proveedor de modelos.** El valor por defecto apunta a un endpoint externo que
  hoy no acepta la credencial disponible. Por eso el laboratorio local es importante (ver
  [laboratorio-ia-local.md](laboratorio-ia-local.md)).

## Próximos pasos

1. Obtener una credencial válida para medir el intérprete y elegir la línea base de comparación.
2. Ampliar el arnés con casos reales antes de comparar modelos.
3. Revisar la lista de paquetes permitidos con quien decida por la organización.
4. Preparar un `clouds.yaml` y probar la subida y el arranque en una nube de pruebas.
