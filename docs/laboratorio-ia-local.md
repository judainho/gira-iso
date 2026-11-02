# Laboratorio de IA local en CPU

Objetivo: tener un modelo de lenguaje corriendo en el laboratorio, sin costo por token y sin enviar
datos fuera, para probar el intérprete de gira-iso y el arnés de evaluación mientras se resuelve la
credencial del proveedor externo. Ver [alcance.md](alcance.md) para el contexto.

## Por qué sirve para este proyecto

El intérprete de gira-iso solo necesita un endpoint compatible con la API de OpenAI
(`/v1/chat/completions`). El cliente (`src/gira_iso/nl/client.py`) lo llama con
`response_format` de tipo `json_object`. Por eso un servidor local puede sustituir al proveedor sin
cambiar código: basta con apuntar `GIRA_ISO_LLM_BASE_URL` a él.

```bash
export GIRA_ISO_LLM_BASE_URL=http://localhost:8080/v1
export GIRA_ISO_LLM_API_KEY=local            # el servidor local no valida la clave; el cliente la exige
export GIRA_ISO_LLM_MODEL=<nombre-del-modelo>
uv run gira-iso eval --record eval/runs/local.json
```

## Requisitos de hardware

El cuello de botella en CPU es la **memoria**, no los núcleos. Un modelo cuantizado (formato GGUF,
por ejemplo Q4_K_M) ocupa aproximadamente su número de parámetros en gigabytes, más el contexto.

| Tamaño del modelo (cuantizado) | RAM aproximada | Uso sugerido |
|---|---|---|
| 1–3 B | 2–4 GB | Primeras pruebas del flujo y del formato JSON |
| 7–8 B | 5–7 GB | Pruebas de calidad razonable |
| 13 B o más | 10 GB o más | Solo si el laboratorio tiene margen de sobra |

Recomendación para el laboratorio: **16 GB de RAM** o más, para dejar espacio a la construcción de
imágenes. Packer y QEMU consumen memoria durante un build; no conviene correr un modelo de 7 B y un
build al mismo tiempo en 8 GB.

La velocidad en CPU será lenta (tokens por segundo de un dígito en modelos grandes). Para las
pruebas del intérprete basta, porque la respuesta es un JSON corto. Medir el tiempo real en el
laboratorio antes de decidir.

**Nota sobre la máquina de desarrollo actual:** tiene 16 núcleos y 14 GB de RAM, pero en el momento de
esta revisión solo había unos 3,4 GB disponibles. Para un modelo local conviene cerrar otros procesos
o usar la máquina del laboratorio.

## Opciones de servidor

Las dos opciones principales son estas. Ninguna se ha instalado todavía; antes de depender de una,
hay que comprobar los puntos de la lista de verificación.

**llama.cpp (`llama-server`)**
- Compilado para CPU; soporta modelos GGUF de muchas familias.
- Expone `/v1/chat/completions`. Comando típico:
  `llama-server -m modelo.gguf --port 8080 -c 4096`.

**Ollama**
- Instalación sencilla; descarga los modelos con un nombre (`ollama pull <modelo>`).
- Expone `/v1/chat/completions` en `http://localhost:11434/v1`.

## Lista de verificación antes de usarlo

1. **Responde en JSON.** Con `response_format: {"type": "json_object"}` el servidor debe devolver un
   JSON válido. Probar con una petición mínima con `curl` antes de conectarlo a gira-iso.
2. **Respeta el modelo elegido.** Confirmar el nombre que el servidor devuelve en `/v1/models` y
   ponerlo en `GIRA_ISO_LLM_MODEL`.
3. **Sigue el esquema.** Ejecutar `gira-iso eval --limit 5` y revisar que las respuestas se
   validan. Los fallos de formato los reintenta el cliente una vez.
4. **Mide el tiempo.** Anotar la latencia por caso; el arnés ya la registra en la grabación.

## Cómo usarlo para las pruebas

- **Flujo completo sin gastar tokens:** `gira-iso create "un servidor web con nginx" --name prueba`
  con la variable apuntando al servidor local.
- **Evaluación:** `gira-iso eval --record eval/runs/local.json` guarda la respuesta cruda para
  re-puntuar sin red con `gira-iso eval --replay`.
- **Comparación:** la línea base del proyecto (`claude-haiku-4-5` en el proveedor externo) se compara
  contra el modelo local. Las diferencias de calidad se leen en la grabación, no en la salida de
  consola.

## Límites de un modelo local pequeño

- Puede no respetar bien el esquema con peticiones largas o ambiguas. Eso se detecta como fallo en
  la evaluación, no como una imagen mal construida: la política y el esquema siguen validando.
- Su precisión no es comparable con la del modelo de referencia. Sirve para probar el flujo, no para
  elegir el modelo de producción.
- Los 25 casos del arnés detectan regresiones groseras. No bastan para afirmar precisión.

## Pasos propuestos para el laboratorio

1. Preparar la máquina (16 GB de RAM recomendados, almacenamiento para varios modelos).
2. Instalar llama.cpp u Ollama y descargar un modelo de 1–3 B para la primera prueba.
3. Verificar los cuatro puntos de la lista de verificación.
4. Ejecutar `gira-iso create` y `gira-iso eval` con el servidor local.
5. Probar después un modelo de 7–8 B y comparar la latencia y la tasa de respuestas válidas.
6. Cuando haya credencial del proveedor externo, repetir la evaluación y comparar.
