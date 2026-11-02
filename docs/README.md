# Documentación de gira-iso

Este directorio reúne la documentación del proyecto. Cada archivo responde una pregunta distinta;
si buscas algo concreto, empieza por la tabla.

| Documento | Responde | Cuándo leerlo |
|---|---|---|
| [`alcance.md`](alcance.md) | Qué funciona, qué no se ha probado y qué queda fuera | Antes de usar el proyecto o prometer una capacidad |
| [`arquitectura.md`](arquitectura.md) | Cómo está organizado el código y cuál es el flujo de una creación | Antes de tocar el núcleo o revisar un cambio |
| [`especificacion.md`](especificacion.md) | Los campos de `spec.yaml`, sus límites y las reglas de política | Al escribir o depurar una especificación |
| [`flujo-de-control.md`](flujo-de-control.md) | Quién decide qué, dónde se puede parar y cómo se propagan los errores | Antes de cambiar el orquestador, la política o la verificación |
| [`openstack.md`](openstack.md) | Cómo subir una imagen a Glance y comprobar que arranca | Al cerrar F7 o depurar una subida |
| [`laboratorio-ia-local.md`](laboratorio-ia-local.md) | Cómo probar el intérprete con un modelo local en CPU | Mientras no haya una credencial del proveedor externo |
| [`plan-v1.md`](plan-v1.md) | El plan por fases, las decisiones cerradas y la bitácora | Para entender por qué el proyecto llegó a este diseño |
| [`../backlog.md`](../backlog.md) | Lo que falta, ordenado por prioridad | Al retomar el trabajo |

## Orden de lectura recomendado

1. [`../README.md`](../README.md) — qué es el proyecto y cómo se usa.
2. [`alcance.md`](alcance.md) — el estado real, sin promesas de más.
3. [`arquitectura.md`](arquitectura.md) — el mapa del código.
4. [`especificacion.md`](especificacion.md) — el contrato que el usuario escribe o el modelo produce.
5. [`flujo-de-control.md`](flujo-de-control.md) — el detalle de control y seguridad.

## Convenciones

- **Idioma.** El código, los identificadores y los comentarios están en inglés; la documentación y
  el backlog, en español.
- **Sin emojis.** La presentación es técnica y legible sin decoración.
- **Estado, no aspiración.** Un documento describe lo que el código hace hoy. Lo que falta va en
  [`../backlog.md`](../backlog.md), no disfrazado de presente.
- **Enlaces relativos.** Los documentos se leen igual en GitHub y en un clon local.
- **Sin CI/CD.** No hay workflows que validen estos documentos; la revisión es humana.

## Activos

- `assets/gira-pucp.png` — logo del Grupo de Investigación en Redes Avanzadas, usado en el README.

## Mantenimiento

Cuando una fase cierre o cambie un contrato:

1. Actualiza el documento afectado (no el que sea más cómodo).
2. Mueve el punto cerrado de [`../backlog.md`](../backlog.md) a [`plan-v1.md`](plan-v1.md) si es
   una decisión o a [`alcance.md`](alcance.md) si es una capacidad.
3. Verifica que los enlaces relativos sigan resolviendo.
