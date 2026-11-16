# Classmer server — contratos internos (v0.1)

Complementa `ARCHITECTURE.md` (fuente de verdad del diseño). Acá están los contratos entre módulos
del server: qué archivo escribe cada etapa, con qué forma, y qué interfaces de Python usan.

## Módulos

| Módulo | Qué hace |
|---|---|
| `classmer/config.py` | `Config` (pydantic) desde `Classmer-data/config.yaml` + `.env`. `cfg.materia(id)`, `cfg.sesion_dir(id)`, `cfg.jobs_dir`, `Materia.ruta_perfil()`, `Materia.ruta_clases()`. `PROMPTS_DIR`, `PLANTILLAS_DIR`. |
| `classmer/db.py` | Esquema SQLite (§10), `Base(ruta).conexion()` (conexión nueva por uso, autocommit), `transaccion(conn)` (BEGIN IMMEDIATE), `abrir(cfg)`. |
| `classmer/cola.py` | Cola de jobs: `encolar`, `tomar(..., sesion_id=None)`, `completar`, `fallar`, `posponer`, `renovar`, `liberar`, `recuperar_vencidos`. |
| `classmer/worker.py` | `Worker(ctx, solo_sesion=None)`: pools (transcribir ×N, apunte ×1, general ×2), lease, encadenamiento. Un solo proceso por carpeta de datos procesa jobs: lock `Classmer-data/worker.lock`, que se suelta recién cuando el proceso termina. `importar --procesar` con el server andando sólo espera. |
| `classmer/pipeline/__init__.py` | `Contexto`, `Servicios`, `Resultado`, `ErrorEtapa`, `CuotaAgotada`, `EsperandoUsuario`, `actualizar_sesion`, `calcular_progreso`. |
| `classmer/pipeline/<etapa>.py` | Una etapa: `def ejecutar(ctx: Contexto, job: Job) -> Resultado`. |
| `classmer/sesiones.py` | Lógica de la API sin HTTP: crear, subir audio por offset, eventos, finalizar. `ruta_audio` (el original, por `assets.ruta`) y `ruta_audio_panel` (la copia `audio.panel.ogg` si existe; si no, el original); `MAX_AUDIO_IMPORTADO`, `mensaje_audio_grande`, `nombre_audio_importado`. |
| `classmer/adjuntos.py` | Material adjunto de una clase (PDF de slides o notas, imágenes): `guardar` (tipo por contenido, nombre saneado, idempotente por id; en cualquier estado de la sesión, también en `error`, para sumar material antes de regenerar), `listar`, `estados` / `resumen_api` (`pendiente`/`usado`/`sin_usar`), `nombre_latex`, `preparar_en` (PDF tal cual, imágenes como JPEG con la regla de las fotos), `escribir_manifiesto`, `linea_timeline`, `refrescar_timeline`, `describir_tipo`, `info_pdf`. |
| `classmer/api/app.py` | `crear_app(cfg, iniciar_worker=True, servicios=None)`: rutas §5.1. |
| `classmer/audio.py` | `duracion_ms`, `sondear`, `ejecutar` (ffmpeg/ffprobe sin consola), `fecha_creacion`. Audio importado: `EXTENSIONES_AUDIO` (lista blanca), `extension_valida`, `extension_por_formato`, `formato`, `codec_audio`, `MIME_AUDIO` y `reproducible_en_navegador`, `convertir_opus`. |
| `classmer/latex.py` | `compilar(carpeta, archivo, motor, pasadas=2)`; `lecturas_externas[_en]` (filtro de lo que lee archivos de afuera). |
| `classmer/ia/transcriptor.py` | Interfaz `Transcriptor` + Gemini (ASR y LLM) + stub de Whisper. |
| `classmer/ia/clasificador.py` | Clasificar materia: `ClasificadorClaude` (API con JSON estructurado o plan Max por el Agent SDK) y `ClasificadorGemini`; `crear_clasificador(cfg)` elige por `clasificar.motor`. |
| `classmer/ia/agente_apunte.py` | Runner del Claude Agent SDK con política de herramientas (también `WebSearch`/`WebFetch` con `apunte.web`); `--effort`, API key sólo en el `env` de la corrida y errores separados en pasajeros y de facturación (`clasificar_error`). |
| `classmer/ia/facturacion.py` | Facturación de Claude (§7): `Facturacion(cfg, base)` decide el medio (`api`/`max`) según `claude.facturacion` y la memoria (tabla `facturacion`); `ejecutar(tarea, correr)` rehace con el otro medio ante `SinCreditos`; `costo_api`, `texto_costo`. |
| `classmer/ia/modelos.py` | Catálogo de modelos (tablas `modelos` y `descubrimientos`): semillas, `opciones_tarea`, `redescubrir` (APIs de Anthropic y Google), `esfuerzo_para`. CLI `classmer modelos [--redescubrir]`. |
| `classmer/ia/meta.py` | Esquema JSON de `meta.json` y validación. |
| `classmer/importador.py` | Importar clase (ver "Importar clase"): `importar_clase` / `importar_archivos` (CLI, `POST /api/importar` y panel), `analizar_archivos` (paso "Analizar" del panel), `completar_importada` y `duracion_declarada` (los llama `verificar`), `candidatos_inicio` y el área temporal (`dir_temporal`, `crear_temporal`, `ruta_temporal`, `token_valido`, `ArchivoTemporal`, `guardar_temporal`, `borrar_temporal`, `limpiar_temporales`). |
| `classmer/panel/importar.py` | "Importar clase" en el panel: `recibir` (el multipart del navegador directo al área temporal, con los topes mientras llega), `vista_analisis`, `datos_importacion` (valida el formulario contra `analisis.json`), `texto_confirmacion` (el costo) y `resumen_importacion` (la página de la clase). |
| `classmer/pipeline/publicar.py` | Publicar con reemplazo: archiva la versión anterior en `apuntes-anteriores/`, tabla `publicaciones`, `INDICE-CLASES.md`; copia `fotos/` y `adjuntos/`. |
| `classmer/busqueda.py` | Índice FTS5 `busqueda`: `indexar_sesion`, `indexar_publicada` (la llama `publicar`), `reindexar` (`classmer reindexar`), `borrar_sesion`, `buscar`. |
| `classmer/mcp/` | MCP en `/mcp` (§11): `servidor.py` (JSON-RPC, `crear_router(cfg)`), `herramientas.py` (las 8 herramientas), `puente.py` (stdio → HTTP, sólo librería estándar, para Claude Desktop y Codex). |

## Reglas generales de una etapa

- `ejecutar(ctx, job)` corre en un hilo del worker. Puede bloquear (subprocess, HTTP).
- Lee y escribe en `ctx.sesion_dir(job.sesion_id)`. Es **re-ejecutable**: pisa sus propias salidas
  (escritura atómica con `util.escribir_atomico` / `util.escribir_json`) y borra/reemplaza sus filas
  de la base dentro de una transacción.
- Devuelve `Resultado(siguientes=[(tipo, args)], log=..., motor=..., costo=...)`. El worker encola
  los siguientes en la misma transacción en la que marca el job como hecho.
- Errores: `ErrorEtapa(msg, reintentable=True|False)`; `CuotaAgotada(msg, espera_s=...)` para cuota,
  rate limit o falta de un secreto (no cuenta como intento); `SinCreditos` (subclase de
  `CuotaAgotada`, ver "Modelos y facturación") si el medio de facturación de Claude no sirve;
  `EsperandoUsuario(msg, motor=, costo=, facturacion=)` deja la sesión en `esperando_usuario` (lo
  opcional queda en el job). Cualquier otra excepción cuenta como error reintentable.
- El worker ya pone `estado='procesando'` y `etapa=<tipo>` al tomar el job. Sólo `publicar` pone
  `estado='lista'` (o `'error'` si el apunte no compiló).
- Logs para el usuario: `ctx.log_sesion(sesion_id, "mensaje")` (va a `sesiones/<id>/pipeline.log`).

## Encadenamiento

```
verificar → segmentar → transcribir (1 job por bloque, args {"n": k}) → linea_de_tiempo
          → clasificar (no-op si la sesión ya tiene materia) → apunte → publicar → notificar
```

- `segmentar` devuelve `[("transcribir", {"n": k}) for k in bloques]`.
- `transcribir` devuelve `[("linea_de_tiempo", {})]` sólo si, después de guardar su bloque, **todos**
  los bloques de la sesión están en `transcripto` o `dudoso` (la clave de la cola evita duplicados).
- `clasificar`: si `sesiones.materia_id` no es null → `Resultado(siguientes=[("apunte", {})])` sin
  llamar a la IA. Si no, clasifica; confianza ≥ umbral → asigna la materia y sigue; si no → lanza
  `EsperandoUsuario` (se resuelve con `classmer asignar <sesion> --materia <id>`).

## Archivos de una sesión (`Classmer-data/sesiones/<id>/`)

| Archivo | Lo escribe | Forma |
|---|---|---|
| `audio.ogg` (o `audio.<ext>` si es importado) | API / importador | El nombre real está en `assets.ruta` del asset `audio:<id>`; usar `sesiones.ruta_audio(cfg, conn, id)`. Una importada desde la app puede llegar como `audio.bin` (extensión fuera de la lista blanca): `verificar` la renombra. |
| `audio.panel.ogg` | verificar (importadas) | Opus mono 32 kbps, sólo si el navegador no reproduce el original (amr, wma, alac, 3gp…). Para el reproductor del panel: `sesiones.ruta_audio_panel`. El pipeline usa siempre el original. |
| `audio.descartado-<ts>.ogg` | finalizar (hash distinto) | — |
| `fotos/<uuid>.jpg` | API / importador | Originales tal cual llegaron. Fila en `assets` (tipo `foto`, `ruta` relativa). |
| `eventos.jsonl` | API / importador | Una línea JSON por evento recibido. |
| `adjuntos/<nombre>` | API / panel | Material adjunto, tal cual llegó. `nombre` = el original en ASCII (sin tildes; espacios y símbolos → `_`; sin puntos internos; ≤ 60) + la extensión del tipo detectado por el contenido (`.pdf .jpg .png .webp .heic`); la raíz es única por sesión sin distinguir mayúsculas (`Slides-2.jpg`). Fila en `assets` (tipo `adjunto`, no cuenta como evento) con `datos` `{"nombre_original", "tipo", "mime", "creado", "origen": "app"|"panel", "paginas"?, "cifrado"?, "estado_sesion"}`. |
| `adjuntos.jsonl` | API / panel | Una línea JSON por adjunto recibido (`id`, `tipo`, `ruta`, `offset_ms`, `bytes`, `sha256`, `recibido` y los `datos`). |
| `sesion.json` | API al crear | Copia de la fila de la sesión al crearla. |
| `verificacion.json` | verificar | `{"sha256", "bytes", "duracion_ms", "duracion_declarada_ms", "fotos_faltantes": [...], "eventos": N, "avisos": [...]}` |
| `silencios.json` | segmentar | `{"duracion_ms", "silencios": [{"inicio_ms","fin_ms"}], "descartados": [{"inicio_ms","fin_ms"}], "habla": [{"inicio_ms","fin_ms"}]}` (todo en ms absolutos del audio original). `descartados` son los silencios largos **confirmados** también con el umbral estricto (`silencio_db − 12 dB`); `habla` incluye la voz baja. |
| `bloques/bloque_NN.mp3` | segmentar | Mono, 16 kHz, MP3 48 kbps. `NN` con 2 dígitos desde 01. |
| `bloques.json` | segmentar | `[{"n": 1, "inicio_ms": 0, "fin_ms": 600000, "ruta": "bloques/bloque_01.mp3"}]` |
| `transcript/bloque_NN.json` | transcribir | `{"n", "inicio_ms", "fin_ms", "motor", "modelo", "estado": "transcripto"|"dudoso", "avisos": [...], "segmentos": [{"inicio_ms","fin_ms","hablante","texto"}]}` (ms **absolutos**). |
| `transcript.json` | linea_de_tiempo | `[{"bloque", "inicio_ms", "fin_ms", "hablante", "texto"}]` ordenado. |
| `transcript.md` | linea_de_tiempo | Transcript legible: `[hh:mm:ss] PROFE: texto`. |
| `fotos_pdf/pizarra_NN.jpg` | linea_de_tiempo | Fotos ordenadas por `offset_ms` (las sin hora al final). **Nativas (§7):** la original byte a byte; sólo se recodifica (JPEG calidad 95, una vez) si tiene orientación EXIF ≠ 1 (se rota de verdad), si supera 8000 px o 7,5 MB (se lleva a 4000 px) o si no es JPEG. Son las que ve el agente y las que van al PDF y a la carpeta publicada. |
| `timeline.json` | linea_de_tiempo | `{"fotos": [{"id", "original", "nombre": "pizarra_01.jpg", "offset_ms", "tamano": [ancho, alto], "tratamiento": "original"|"rotada"|"reducida"|"convertida"}], "eventos": [{"tipo","offset_ms","texto"?,"marca"?,"foto"?}], "adjuntos": [{"id", "nombre": "Slides_clase_5.pdf", "nombre_original", "tipo", "offset_ms", "paginas"}]}` (`nombre` es el de LaTeX: `<raíz>.pdf` o `<raíz>.jpg`). |
| `timeline.md` | linea_de_tiempo | Lo que lee el agente (§6), una línea por cosa, en orden de tiempo. |
| `clasificacion.json` | clasificar | `{"materia_id", "confianza", "motivo", "modelo", "motor", "esfuerzo"?, "facturacion", "costo", "uso"}` (`esfuerzo` sólo con Claude; `facturacion` `api`/`max`; `costo` en US$, real con la API o equivalente con el plan Max). |
| `apunte/vN/` | apunte | `apunte.tex`, `apunte.pdf` (si compiló), `meta.json`, `compilacion.log`, `classmer.cls`, `agente.log`. La fila de `apuntes` guarda `modelo`, `esfuerzo`, `facturacion`, `costo` y `adjuntos` (JSON con los ids de los adjuntos que tomó la corrida que quedó; NULL en las versiones de antes = ninguno). |
| `importacion.json` | importador / verificar | Clases importadas: de dónde salió el inicio, fotos ubicadas o sin hora, material y avisos (forma en "Importar clase"). Se escribe una vez: el de la CLI o el panel no lo pisa `verificar`. |
| `apunte/vN/meta.agente.json` | apunte | Lo que escribió el agente, sólo si hubo que reparar `meta.json`. |
| `pipeline.log` | todas | Líneas de log legibles. |

`verificacion.json` también trae `eventos_totales` (y `error` + `sha256_esperado` si el hash no
coincide); `timeline.json` trae `avisos[]` y el `id` de cada evento. Los `datos` de las fotos
importadas guardan `origen_hora`, `hora` (ISO o null), `fuera_de_rango` y `offset_original_ms`; los
del audio importado, `nombre_original` y, según el camino, `bytes_declarados` (app), `origen_inicio` e
`importado` (importador) y `duracion_declarada_ms` (la que guardó `verificar` la primera vez).

### Formato de `timeline.md`

```
# Clase: Análisis Matemático II — 2026-10-15 — teórica
[00:34:10] PROFE: ...la derivada direccional se define como el límite...
[00:34:11] ALUMNO: ¿y si no es unitario?
[00:34:52] FOTO fotos/pizarra_07.jpg
[00:34:52] ADJUNTO adjuntos/Slides_clase_5.pdf (PDF, 23 páginas): "Slides clase 5.pdf"
[00:35:20] MARCA importante
[00:41:03] NOTA "preguntar por qué el gradiente es perpendicular"
[00:41:05] MARCA no_entendi
[00:50:00] PAUSA
[00:50:00] REANUDAR
[00:52:10] SILENCIO hasta 00:53:00 (no se transcribió)
[01:02:30] AVISO: tramo con habla sin transcribir hasta 01:03:10
# Fotos sin hora
FOTO fotos/pizarra_08.jpg
# Material adjunto
ADJUNTO adjuntos/Apunte_del_profe.pdf (PDF cifrado): "Apunte del profe.pdf"
ADJUNTO adjuntos/captura.jpg (imagen PNG): "captura.png" [no se pudo abrir: no está en adjuntos/]
```
Un segmento del transcript por línea (hablante en mayúsculas: `PROFE`, `ALUMNO`, `DESCONOCIDO`).
`SILENCIO` marca un silencio largo descartado; `AVISO`, un hueco con habla en un bloque `dudoso`.
Las fotos sin hora van al final, bajo `# Fotos sin hora`.

`ADJUNTO` es material adjunto (`adjuntos.linea_timeline`): la ruta es la de LaTeX (`<raíz>.pdf` o
`<raíz>.jpg`, relativa a la carpeta del `.tex` publicado y a `input/` en la de trabajo), entre
paréntesis el tipo (`PDF, N páginas`, `PDF cifrado`, `imagen PNG`…) y entre comillas el nombre
original. Con `offset_ms` va en su lugar (después de lo que pasó en ese mismo segundo); sin, al final,
bajo `# Material adjunto` (después de `# Fotos sin hora`). Una sola regla, `adjuntos.refrescar_timeline`:
la aplica `linea_de_tiempo` y la vuelve a aplicar `apunte` sobre `input/timeline.md` (pudo llegar uno
después), con `[<aviso>: no está en adjuntos/]` en los que no pudo preparar.

## Importar clase

Una clase que el usuario no grabó (el audio de un amigo por WhatsApp, fotos y PDFs) entra por dos caminos
con una sola lógica (`importador.py`). Las dos dejan `sesiones.importada = 1` (esquema 4; la
migración marca así las viejas con `dispositivo = 'importador'`), en `GET /api/sesiones[/{id}]`
como `"importada": true`.

**App: sesión de siempre.** `POST /api/sesiones` con `importada: true`, `audio_nombre` (el nombre
original) y `audio_bytes` (el tamaño total), que sin `importada` se ignoran:
- el audio se guarda como `audio.<ext>` si la extensión de `audio_nombre` está en
  `audio.EXTENSIONES_AUDIO`; si no, `audio.bin`;
- `audio_bytes` > `MAX_AUDIO_IMPORTADO` (2 GiB) → `413 {"detail": "el audio pesa 2,3 GB: el máximo
  para importar es 2 GB"}` sin crear nada. Una sesión que ya existe da `200` sin revalidar;
- `PUT /audio` en una importada: si `offset + largo` pasa los 2 GiB, `413 {"detail": "el audio pesa
  más de 2 GB: …"}` sin escribir nada (un PUT vacío nunca);
- las fotos van como eventos `foto` sin `offset_ms` y el material como adjuntos; `/finalizar` puede
  traer `duracion_ms: null`.

Una sesión sin `importada` queda exactamente como antes (`audio.ogg`, `importada: false`).

**`verificar` de una importada** (`importador.completar_importada`, idempotente; una sesión no
importada no pasa por ahí):
- `sesiones.duracion_ms` pasa a ser la medida. La declarada se guarda la primera vez en los datos
  del audio (`duracion_declarada_ms`, `importador.duracion_declarada`) y va en `verificacion.json`
  como `duracion_declarada_ms`, también en un reproceso. Una diferencia es sólo un aviso.
- Sin pista de audio (un video sin sonido: ffprobe igual le mide la duración) → `ErrorEtapa` no
  reintentable "audio ilegible: … no tiene pista de audio", como el importador de la PC.
- `audio.bin` → `audio.<ext>` según el `format_name` de ffprobe (`audio.extension_por_formato`), y
  `assets.ruta`. Si ese rename se cortó entre la base y el disco, `importador.reconciliar_audio` (antes
  de mirar el audio) pone en la base el nombre que sí está.
- Las fotos con `offset_ms` NULL cuyos datos no tienen `origen_hora` (no se miraron): `hora_exif` y
  `ubicar_offset` respecto de `sesiones.inicio` y la duración medida. Se actualizan `offset_ms` y los
  datos (`hora`, `origen_hora`, y `fuera_de_rango` + `offset_original_ms` si cae a más de 5 min del
  rango). Sin EXIF, o una imagen que Pillow no abre (una HEIC rota), quedan sin hora con
  `origen_hora: null` (miradas; la que no abre, con el motivo en `ilegible`). En el server no se usa el
  mtime. Una fecha EXIF fuera de 2000–2100 (`importador.ANIOS_CREIBLES`: un reloj sin configurar) es
  "sin hora"; un offset imposible (`+25:00`) se ignora (zona local).
- `audio.panel.ogg` una vez si `audio.reproducible_en_navegador` dice que no: el contenedor por la
  extensión (`MIME_AUDIO`: .ogg .opus .oga .m4a .mp4 .mp3 .wav .webm .aac .flac) y el códec de
  ffprobe (opus, vorbis, aac, mp3, flac, pcm_*). Si ffmpeg falla, sólo un aviso.
- `importacion.json` si no existe, con `via: "app"` y `origen_inicio` = `calendario` o `manual` (el
  inicio lo dio el cliente). Los avisos de fotos ("N de M fotos quedaron fuera de la grabación: revisá
  el inicio", "N de M fotos no tienen hora EXIF…") van también a `verificacion.json` y a
  `pipeline.log`. Salen de lo que quedó en la base (y las ilegibles, de `ilegible`), no de lo que miró
  esa corrida: un verificar que se cortó después de ubicar las fotos los vuelve a dar al reintentar.

**PC: archivos sueltos.** `importar_clase` (CLI `classmer importar`, `POST /api/importar` y el panel)
copia el audio (el panel, con `mover_audio`, lo mueve desde el área temporal: mismo disco, no ocupa el
doble), ubica las fotos, guarda el material con `adjuntos.guardar` (origen `importador`, o el que se
pase) **antes** de finalizar y finaliza. Si algo falla antes de finalizar (un `OSError`, el disco
lleno…), la sesión a medio crear se borra (`_deshacer`: filas y carpeta; un audio movido vuelve a su
lugar): no queda una clase en error sin audio ni se gasta su número. Sólo si no se puede borrar queda
en error. Un material rechazado (tipo, tamaño, vacío, no está) no frena la importación. Un audio de
más de 2 GiB da `AudioGrande` (una `ErrorImportacion` y una `ArchivoGrande`: la API responde `413`;
`POST /api/importar` lo corta mientras llega, sin guardarlo entero en la carpeta temporal del
sistema). `POST /api/importar` acepta además `numero_clase` y `material[]`/`material`; si algo del
material no entró, la respuesta suma `material_rechazado`.

**`importacion.json`** (la misma forma en los dos caminos):

```json
{"audio": {"nombre_original": "Audio de WhatsApp.m4a", "archivo": "audio.m4a", "bytes": 31457280,
           "sha256": "…", "duracion_ms": 5400000},
 "inicio": "2026-10-15T14:00:00-03:00",
 "origen_inicio": "parametro | mtime_audio | creation_time | creation_time_como_fin | exif_fotos | ahora_menos_duracion | calendario | manual",
 "candidatos_inicio": [{"origen": "creation_time", "inicio": "…", "fotos_dentro": 3, "etiqueta"?: "…", "elegido"?: false}],
 "fotos": [{"id", "nombre_original", "hora", "origen_hora", "offset_ms", "fuera_de_rango"?, "offset_original_ms"?}],
 "fotos_ilegibles": [{"nombre", "motivo"}],
 "avisos": ["…"],
 "importado": "2026-10-15T16:00:00-03:00",
 "via": "importador | app"}
```

- El del importador suma `material` (`[{"id", "nombre", "nombre_original", "tipo", "bytes"}]`) y
  `material_rechazado` (`[{"nombre", "motivo"}]`); sus `candidatos_inicio` son los de siempre
  (`origen`, `inicio` y, con fotos, `fotos_dentro`).
- El del panel (dispositivo `panel`, `origen_inicio: "parametro"`) suma `inicio_elegido`: el `origen`
  del candidato que tocó el usuario o `"manual"` si lo escribió; sus `candidatos_inicio` son los que mostró
  Analizar (con `etiqueta` y `elegido`, el que proponía el server).
- El de la app suma `audio.duracion_declarada_ms`, `audio.formato` y `audio.codec`; sus candidatos
  (sólo informativos, sin el mtime) tienen `etiqueta` y `elegido: false`. En `fotos`, `origen_hora:
  "cliente"` es una foto que ya vino con minuto.
- Se lee con `util.leer_json(cfg.sesion_dir(id) / "importacion.json")` (None si no hay); lo que no se
  conoce, se ignora.

**Inicio y candidatos** (condición 4 del diseño). `_inferir_inicio` decide; `candidatos_inicio` los
ordena y les pone etiqueta para el panel:
1. lo que da el usuario (`inicio`, `parametro`; en la app, el evento del calendario o el inicio a mano);
2. las fotos: `exif_fotos` (1 min antes de la primera; si hay una suelta lejos, de la primera desde la
   que entran más) y los metadatos del audio que las fotos confirman (dejan adentro a la mayoría);
3. los metadatos del audio solos: `mtime_audio` (la fecha del archivo − duración: el `mtime_audio`
   que manda el navegador o, en la CLI, el mtime del archivo), `creation_time` y
   `creation_time_como_fin`, con la advertencia "ojo, en WhatsApp suele ser la hora del envío".

Esa prioridad es la que ya aplicaba `_inferir_inicio`: con fotos, gana el candidato del audio que
deja más fotos adentro si deja a la mayoría y, si no, las fotos; sin fotos, el primero de los del
audio. La CLI no cambia. Lo nuevo es el orden y las etiquetas que ve el panel (el elegido primero) y
`mtime_audio` como parámetro.

**`analizar_archivos(cfg, audio, fotos, *, nombre_original=None, mtime_audio=None)`** no crea nada y
no usa mtimes del server:

```json
{"audio": {"nombre", "bytes", "duracion_ms", "formato", "codec", "extension": ".m4a", "reproducible": true},
 "inicio": "…", "origen_inicio": "creation_time",
 "candidatos_inicio": [{"origen", "inicio", "etiqueta", "elegido", "fotos_dentro"?}],
 "fotos": [{"indice", "nombre", "hora", "origen_hora", "offset_ms", "fuera_de_rango", "offset_original_ms"?}],
 "fotos_ilegibles": [{"nombre", "motivo"}],
 "avisos": ["…"]}
```

`indice` es la posición de la foto en la lista que se pasó, contando las ilegibles (las fotos vuelven
ordenadas por minuto, las sin hora al final). Errores: `ErrorImportacion` (audio ilegible) y `AudioGrande`.

**Área temporal del panel** (`Classmer-data/importaciones/<token>/`, nunca en el repo): `crear_temporal(cfg)
→ (token, carpeta)`; `guardar_temporal(cfg, token, tipo, archivo, nombre) → Path` guarda en
`<tipo>/NNN/<nombre saneado>` con el tope del tipo (`audio` 2 GiB → `AudioGrande`; `fotos` 50 MB y
`material` 100 MB → `ArchivoGrande`); `ruta_temporal(cfg, token)` (token de 32 hex;
`ErrorImportacion` si no es válido o ya no está); `borrar_temporal(cfg, token)` al cancelar o después
de importar; `limpiar_temporales(cfg, vencimiento_h=24)` borra lo que no se tocó en 24 h (y las
`importar-*` que dejaba un `POST /api/importar` cortado). Lo llaman el arranque del server
(lifespan) y el worker cada hora. `POST /api/importar` también usa el área temporal para su subida.
`ArchivoTemporal(cfg, token, tipo, nombre)` es lo mismo de a pedazos (`escribir`, `cerrar`, `descartar`):
lo usan `guardar_temporal` y el panel, que escribe el multipart mientras llega (así nada pasa por la
carpeta temporal del sistema y un audio de más de 2 GiB se corta al pasar el tope). Un multipart sin el
cierre (`--límite--`) es `ErrorRecepcion` (python-multipart no lo mira en `finalize()`). El panel deja
en `<token>/analisis.json` el audio (`archivo` relativo al token, `nombre` original, `modificado_ms`),
las fotos (`archivo`, `nombre`) y la última respuesta de Analizar (`resultado`). Al volver a analizar
un token se marca `reanalizando: true` (las fotos de antes se borran apenas llega la primera nueva) y el
análisis nuevo la saca: si quedó, o falta alguna foto del análisis, "Importar y procesar" responde 422
con `campo: "fotos"` (y el JS las vuelve a analizar) en vez de importar con menos fotos.

## Carpeta de trabajo del agente (`Classmer-data/jobs/<sesion>/apunte/`)

Ruta ASCII sin espacios. La etapa `apunte` la borra y la rearma en cada ejecución.

```
input/     timeline.md  transcript.json  fotos/pizarra_NN.jpg  adjuntos/<raíz>.pdf|.jpg  perfil.md
           estilo.md  clase_anterior.md  contexto.json  adjuntos.json
plantilla/ classmer.cls  ejemplo.tex
output/    classmer.cls   (copia para compilar ahí)   <- el agente escribe apunte.tex y meta.json
```

`contexto.json`: `{"materia_id", "materia", "fecha": "2026-10-15", "fecha_larga": "15 de octubre de 2026",
"numero_clase": 7, "tipo_clase": "teorica"|"practica"|null, "duracion_ms", "inicio": ISO}`.

Adjuntos (`apunte.preparar_carpeta`, con la carpeta ya limpia): se leen de la base en ese momento y
van a `input/adjuntos/` con el nombre para LaTeX (`adjuntos.preparar_en`: los PDF tal cual, las
imágenes como JPEG con la regla de las fotos; un HEIC sin `pillow-heif` o una imagen rota quedan
afuera con aviso en `pipeline.log` y en su línea `ADJUNTO`). `input/adjuntos.json` =
`{"job": <id del job>, "adjuntos": [ids…]}` con **todos** los que se consideraron, también los que no
se pudieron preparar (regenerar no los arreglaría); se escribe siempre, aunque no haya ninguno. De ahí
sale el estado mientras corre (`adjuntos.estados`): lo que llegó después es `sin_usar`. Mientras
corre o espera en cola una etapa anterior al apunte, todos son `pendiente` (cada etapa encola la
siguiente recién al terminar: con una corriendo puede no haber nada en cola). Al guardar
vN, esos ids (los del intento que quedó, también si se rehizo con el otro medio de facturación) van
a `apuntes.adjuntos`. El mensaje del prompt los nombra en `{adjuntos}`.

Se compila en `output/` con `pdflatex -interaction=nonstopmode -halt-on-error -file-line-error
-no-shell-escape apunte.tex` (dos pasadas). Antes de compilar, el server (y la política de Bash del
agente) rechaza un `.tex` que lea archivos de afuera (`\openin`, `\input` de rutas absolutas, con
`..` o con `$` (variables de entorno), `\directlua`, etc.: `latex.lecturas_externas`; los opcionales
`[…]` se saltean como TeX: un `]` entre llaves o escapado no los cierra, y se prueban las lecturas de
LaTeX2e y de xparse). `classmer.cls` pone
`\graphicspath{{fotos/}{../input/fotos/}{../input/}{adjuntos/}{../input/adjuntos/}}`, así el mismo
`.tex` compila en la carpeta de trabajo y en la carpeta publicada (que lleva `fotos/` y `adjuntos/`):
`\includegraphics[page=2,width=\linewidth]{adjuntos/Slides_clase_5.pdf}` (o `{Slides_clase_5.pdf}`) y
`\pizarra{adjuntos/captura.jpg}{…}{…}` andan en las dos.

## Política de herramientas del agente (`ia.agente_apunte`)

`_opciones` arma `ClaudeAgentOptions` con `tools` explícito, `allowed_tools=[]`,
`permission_mode="dontAsk"`, `setting_sources=[]`, `skills=[]` y `strict_mcp_config`. El hook
`PreToolUse` (sin matcher: todas las herramientas) llama a `decidir(nombre, entrada, estado)`; lo que no
permite explícitamente se deniega, y ante un error de la política también. Un `allow` del hook no le gana
a una regla deny: lo que se habilita tiene que salir de `disallowed_tools`.

| Herramienta | Regla |
|---|---|
| `Read`, `Glob`, `Grep` | Sólo dentro de la carpeta de trabajo, la de la materia y la `tool-results` de la sesión del CLI. |
| `Write`, `Edit` | Sólo dentro de `output/`, nunca `classmer.cls`. |
| `Bash` | Sólo `[cd output &&] pdflatex [flags] apunte.tex` (o `lualatex`; una o dos pasadas), con tope de intentos y de tiempo; si el `.tex` lee archivos de afuera se rechaza sin gastar intento. |
| `WebSearch` | Con `apunte.web.activa`: hasta `max_busquedas` llamadas por corrida, consulta de 300 caracteres como mucho. |
| `WebFetch` | Con `apunte.web.activa`: hasta `max_paginas` por corrida, sólo URLs que salieron en una búsqueda de la corrida y que pasan el cinturón (abajo). |
| Todo lo demás | Denegado (`PROHIBIDAS`: `Task`, `Agent`, MCP, `ToolSearch`, `PowerShell`, …). |

`PROHIBIDAS` incluye siempre `WebSearch` y `WebFetch`: la usan tal cual `clasificador.py` y
`claude_json.py` (clasificar y revisar el transcript, sin web). El apunte usa `herramientas(web)` y
`prohibidas(web)`, que con la web activa las suman a `tools` y las sacan de `disallowed_tools`.

**Búsqueda en internet** (`TrabajoApunte.web: WebCorrida | None`, de `WebCorrida.desde_config`; la etapa la
calcula una vez por intento y la usa para el prompt y para la política):

- **Las URLs permitidas** se juntan de los resultados de cada `WebSearch`, sólo del `tool_response` del
  hook `PostToolUse` (matcher `WebSearch`): las de los resultados del buscador (`results[*].content[*].url`),
  nunca las que aparecen en el texto que redacta el modelo que hace la búsqueda (ver "De dónde salen las
  URLs" más abajo). Se guardan normalizadas (`clave_url`: esquema y host en minúscula, el host en punycode
  si no es ASCII, http → https, sin `:443` ni `#fragmento`, camino `/` si no tiene; camino y consulta tal
  cual).
- **El orden de `WebFetch`:** (1) el cinturón sin red (`https`, puerto 443, sin usuario ni contraseña, sin
  espacios ni `\`; ni una IP —tampoco las formas que el parser WHATWG del CLI lee como IPv4: `2130706433`,
  `0x7f.1`, `127.1`—, ni `localhost`, ni un host que no sea `[a-z0-9.-]` con un punto como mínimo, ni
  `panel.dominio_publico` o sus subdominios); (2) que la URL haya salido en una búsqueda; (3) el tope;
  (4) que el host resuelva sólo a IPs globales (`ipaddress.is_global`, también la IPv4 mapeada). El DNS
  va último: un nombre armado por el agente no llega ni a una consulta DNS. Un `http://` de un resultado
  se deniega diciendo que lo pida con `https://` (sólo si con https pasaría).
- **Topes:** con el tope alcanzado, el motivo dice que siga con lo que tiene (no corta el apunte ni gasta
  intentos de compilación). Además, `CLAUDE_CODE_MAX_WEB_SEARCHES_PER_SESSION` = `max_busquedas` en el
  `env` del CLI, y `max_turns` = 80 + `max_busquedas` + `max_paginas`.
- **Prompt:** el bloque entre `===== WEB =====` y `===== FIN WEB =====` de `prompts/apunte.md` va sólo
  con la web activa (`apunte.con_bloque_web`), con `{max_busquedas}` y `{max_paginas}`; apagada, el
  sistema queda como antes del bloque (misma versión).
- **Registro:** `WebSearch '<consulta>' (búsqueda N de M)`, `→ WebSearch '<consulta>': K resultados`
  (K, las URLs que vio el hook PostToolUse; si el hook no las vio, lo dice), `WebFetch <url> (página N
  de M)`, `DENEGADO … : <motivo>` y los errores de la herramienta. La línea `Web:` del resumen cuenta lo
  permitido, las denegadas y las consultas reales al buscador (`webSearchRequests` del `model_usage` del
  `ResultMessage`, que es donde lo informa el CLI: el `usage` principal trae 0 porque la búsqueda va en
  una consulta aparte). El `total_cost_usd` del CLI ya incluye esas consultas (USD 0,01 cada una): si el
  CLI no informa la cantidad, la línea lo dice y el costo queda igual (no se suma nada).
- **De dónde salen las URLs que WebFetch puede abrir:** sólo de `results[*].content[*].url` en el
  `tool_response` del hook PostToolUse (`urls_de_resultado`). El texto del `ToolResultBlock` no cuenta:
  repite la consulta del agente y el resumen del modelo que busca, que escribe lo que dicen las páginas,
  así que una línea `Links: […]` ahí la puede armar una inyección. Por lo mismo, una consulta con saltos
  de línea o caracteres de control se deniega.
- **El hook decide en un hilo** (`asyncio.to_thread`) y de a una decisión por vez (un `asyncio.Lock`):
  el chequeo de DNS de WebFetch bloquea y el loop tiene que seguir leyendo al CLI, y los hooks de
  herramientas en paralelo no pueden pisarse los contadores.
- **Nombres con caracteres no ASCII** (`münchen.de`): `clave_url` los guarda en punycode y WebFetch los
  acepta sólo así (la política le sugiere la URL en punycode al agente). Así el nombre que se chequea es
  el mismo al que se conecta el CLI.
- **Errores:** una búsqueda o una página que falla llega como `ToolResultBlock` con `is_error` (el agente
  lo ve y sigue). Nunca pasa por `clasificar_error`: un resultado web que dice "rate limit" no rehace el
  apunte ni cambia de medio.

## Plantilla `classmer.cls`

Preámbulo obligatorio del apunte (el resto es libre: cualquier paquete, cualquier estructura):

```latex
\documentclass{classmer}
\materia{Análisis Matemático II}
\fechaclase{15 de octubre de 2026}
\numeroclase{7}
\tipoclase{Teórica}            % opcional
\titulo{Derivada direccional y gradiente}
\begin{document}               % el encabezado se imprime solo
...
\end{document}
```

Entornos tcolorbox con título opcional `[...]`: `definicion`, `teorema`, `ejemplo`, `intuicion`,
`agregadoIA`, `duda`. Con origen (§8): `importante` y `parcial`, con `[clase]` ("⭐ marcado en
clase", la marca del usuario) o `[ia]` ("detectado por IA") como primer opcional y el título como
segundo: `\begin{importante}[ia][Regla de la cadena]`. Si el primer opcional no es `clase`/`ia`,
se toma como título (compatibilidad). Macros: `\ts{hh:mm:ss}`,
`\pizarra{archivo}{hh:mm:ss}{epígrafe}`, `\nota{...}`. Funciona con pdflatex y lualatex.

## `meta.json` (lo escribe el agente, lo valida el server con `classmer.ia.meta`)

```json
{
  "titulo": "Derivada direccional y gradiente",
  "resumen": "Dos o tres oraciones.",
  "secciones": [
    {"titulo": "Derivada direccional", "inicio_ms": 2040000, "fin_ms": 2460000,
     "temas": ["derivada direccional"], "texto_plano": "opcional"}
  ],
  "items": [
    {"tipo": "parcial", "texto": "Entra la regla de la cadena", "offset_ms": 2100000,
     "fecha_limite": null}
  ]
}
```
`tipo` de item ∈ {`parcial`, `tarea`, `fecha`, `duda`, `aviso`}; `fecha_limite` es `YYYY-MM-DD` o null.

## Servicios inyectables (`Contexto.servicios`)

- `transcriptor(cfg) -> Transcriptor | list[Transcriptor]` (principal y respaldos en orden). Si es
  None: `classmer.ia.transcriptor.crear_transcriptores(cfg)` → `[TranscriptorGeminiASR,
  TranscriptorGeminiLLM]`. Interfaz: `.nombre`, `.transcribir(audio: Path, contexto:
  ContextoTranscripcion) -> ResultadoTranscripcion` (segmentos con ms **relativos** al bloque).
  `validar(resultado, contexto) -> list[str]`. `TranscriptorWhisper` es el stub de v0.2.
- `clasificador(cfg)` → objeto con `.clasificar(transcript: str, materias: list[MateriaCandidata],
  *, duracion_ms=None) -> ResultadoClasificacion` (si es None: `crear_clasificador(cfg)`, o sea
  `ClasificadorClaude` o `ClasificadorGemini` según `clasificar.motor`). El resultado trae además
  `costo`, `facturacion` y `avisos` (con default, así los clasificadores viejos siguen andando).
- `agente_apunte(cfg)` → objeto con `.ejecutar(trabajo: TrabajoApunte) -> ResultadoAgente` (si es
  None: `classmer.ia.agente_apunte.AgenteClaude`). `TrabajoApunte` trae `modelo`, `esfuerzo` y
  `facturacion` (`api`/`max`, la decide `Facturacion`). Lanza `SinCreditos` (subclase de
  `CuotaAgotada`) si el medio no sirve y `CuotaAgotada` si es algo pasajero.
- `notificador(cfg, sesion_id, ruta_pdf)` (un bot de mensajería).

Los tests pasan versiones falsas para no llamar a Gemini ni a Claude.

## Otros detalles

- `classmer/latex.py`: `compilar(carpeta, archivo="apunte.tex", *, motor="pdflatex", pasadas=2,
  timeout_s=600) -> ResultadoCompilacion(ok, pdf, log, errores, ruta_log)`; `comando(motor,
  archivo)`; `copiar_plantilla(destino, *, ejemplo=False)`; `motor_disponible(motor)`.
- Plantilla: además de lo de arriba, `\graphicspath` incluye `{../input/}` (así `\pizarra{fotos/x.jpg}`
  y `\includegraphics{adjuntos/x.pdf}` también andan en la carpeta de trabajo), opción `[sinencabezado]`, `\nuevacaja{nombre}{Rótulo}{estilo}` para cajas propias,
  `\fechaclase` acepta ISO y `\tipoclase` acepta `teorica`/`practica`. Reglas para el agente: no
  redefinir los 7 entornos ni `\pagestyle`; `{}` antes de un contenido de caja que empiece con `[`;
  el `\label` de una foto va dentro del epígrafe de `\pizarra`; no hay shell-escape.
- `prompts/apunte.md` tiene dos partes separadas por `===== MENSAJE =====` (system prompt y primer
  mensaje). Su versión (`apunte@<sha8>`) queda en la tabla `prompts`.
- La política del agente (`agente_apunte.decidir`) permite Read/Glob/Grep dentro de la carpeta de
  trabajo y de la materia, Write/Edit sólo en `output/` (nunca `classmer.cls`) y Bash sólo para
  `[cd output &&] pdflatex|lualatex … apunte.tex` (hasta 2 veces encadenado). Cada Bash permitido es
  un intento de compilación; se deniega sin gastar intento si `output/` lee archivos de afuera
  (`latex.lecturas_externas`, filtro léxico: mira el texto sin comentarios y también entero, porque un
  `%` no siempre es comentario —`\url{…%20…}`, `\verb|%|`— y TeX ejecuta lo que sigue; así que una
  lectura de afuera comentada también se rechaza). Con `apunte.web.activa`, también `WebSearch` y
  `WebFetch` (ver "Política de herramientas del agente").
- `prompts/apunte.md` tiene además el bloque `===== WEB =====` … `===== FIN WEB =====`, que va sólo con la
  búsqueda en internet activa.
- SDK del agente: `max_buffer_size = 1 GiB` (un Read de una foto vuelve en base64 dentro de UN
  mensaje del CLI; con el tope por defecto de 1 MiB una foto real del celu lo rompía). Caídas de la
  API (5xx, sin conexión) y límites del plan → `CuotaAgotada` (no se compila ni publica nada a medias).
- Número de clase: `materia.numero_clase_inicial − 1 +` sesiones de la materia con `inicio` ≤ el de
  ésta (sin contar las que están `grabando` ni las que están en `error` sin un apunte que compiló; a
  igual inicio desempata el id). Lo calcula `apunte` (`calcular_numero_clase`) y lo guarda en
  `sesiones.numero_clase`; si la sesión ya se publicó, conserva su número. `publicar` usa ese valor.
- `apunte` valida `meta.json` contra la duración medida (`verificacion.json`), no la declarada.
- Errores de Gemini: 429 y 503/UNAVAILABLE → `CuotaAgotada` (3 min; cuota diaria, 1 h); 5xx y
  timeouts → reintentable; en el último intento de un bloque, se degrada a `dudoso` (inaudible).
- Logs: `logs/classmer.log` (serve) y `logs/classmer-cli.log` (importar, estado, asignar,
  reprocesar). `serve` sale con código 4 si el puerto está ocupado (no arranca un worker zombie).
- `INDICE-CLASES.md` en `carpeta_clases` es de Classmer y se regenera en cada publicación con la
  versión vigente de cada clase (títulos limpios de LaTeX con `util.limpiar_titulo`).
- **Publicar: adjuntos.** Si la clase tiene adjuntos, la carpeta publicada lleva `adjuntos/` con
  **todos** los de la sesión (también los que llegaron después del apunte) y el mismo nombre para LaTeX
  que vio el agente (`adjuntos.preparar_en(..., exclusivo=True)`: nunca pisa nada). Los que no se
  pudieron copiar quedan como aviso en `pipeline.log`; sin adjuntos (o si ninguno se pudo copiar) no
  hay carpeta `adjuntos/`.
- **Adjuntos: validación y topes.** El `id` tiene que ser un UUID con guiones (se guarda en
  minúsculas: la misma UUID en mayúsculas es el mismo adjunto); `offset_ms` entre 0 y una semana
  (`MAX_OFFSET_MS`); un id que ya es de un adjunto da 422 también en `POST /eventos`. Si falla el
  INSERT, se borra el archivo ya movido (no queda huérfano con la raíz tomada). Imágenes:
  `procesar_foto` corta en `LIMITE_PIXELES` (300 MP) antes de decodificar, y `preparar_en` no prepara
  una imagen adjunta que no sea JPEG de más de `MAX_PIXELES_IMAGEN` (100 MP; sólo el JPEG se achica al
  leerlo): queda el aviso.
- **Panel: adjuntos.** `GET /panel/clases/{id}/adjuntos/{nombre}` abre un adjunto (inline, con su
  `mime` y `nosniff`; `?descargar=1` lo baja). Se busca por la ruta guardada en la base, nunca con lo que
  llega en la URL. Al bajarlo, el nombre es el original pero con una extensión de su tipo detectado
  (`notas.hta` que empieza con `%PDF-` baja como `notas.pdf`). `POST /panel/clases/{id}/adjuntos`
  (multipart, hasta 20 archivos) los guarda con `adjuntos.guardar` (`origen: panel`, id nuevo) y vuelve
  a la clase con lo que entró y lo que no. Los orígenes de un adjunto son `app`, `panel` e
  `importador` (el material de `classmer importar` y `POST /api/importar`).
- **Panel: importar clase** (`/panel/importar`, sólo desde la PC como todo el panel). Responden JSON;
  los errores, `{"detail", "campo"}` (`campo`: `audio`, `fotos`, `token`, `materia_id`, `tipo_clase`,
  `numero_clase`, `inicio`, `material` o null) con 422 (413 si el audio pasa los 2 GiB; 503 con un
  error de disco, como el disco lleno, sin rutas en el texto).
  - `POST /panel/importar/analizar` (multipart: `token`?, `audio`, `audio_modificado` = File.lastModified
    en ms, `fotos`): guarda en el área temporal y devuelve `{"token", …analizar_archivos}` más
    `inicio_local` (para el datetime-local), `inicio_texto`, `offset_zona`, `margen_ms`, el `texto` y el
    `inicio_local` de cada candidato, `minuto` y `hora_texto` de cada foto (con su nombre original) y
    `resumen_fotos` `{total, con_hora, sin_hora, fuera_de_rango, ilegibles}`. Con `token` (antes que los
    archivos) y sin audio, analiza el mismo audio con las fotos que vengan, que reemplazan a las anteriores.
    Una foto de más de 50 MB va a `fotos_ilegibles`. Hasta `MAX_FOTOS_FORM` (300) fotos más el audio.
  - `POST /panel/importar` (multipart: `token`, `materia_id` (`auto` = Auto), `tipo_clase`, `numero_clase`
    (no cuenta en Auto), `inicio`, `material`): `importar_clase` con dispositivo `panel`, el inicio del
    formulario, el `modificado_ms` del audio como `mtime_audio`, `origen_material="panel"`,
    `inicio_elegido` (el origen del candidato del mismo minuto, o `manual`) y `candidatos_panel`; borra el
    análisis y responde 201 `{"id", "url" (la clase, con el mensaje), "estado", "material",
    "material_rechazado", "avisos"}`. Un error deja el análisis para corregir y volver a mandar (el audio
    se mueve a la sesión con `mover_audio` y, si falla, vuelve). En la página, Cancelar se apaga cuando
    el server ya tiene el pedido (sin material, enseguida): cortar el xhr ya no frena la importación.
  - `POST /panel/importar/{token}/cancelar` → `{"ok": true, "borrado": bool}` (404 si el token no tiene
    forma de token). Un pedido a la vez por token (lock): volver a analizar, importar y cancelar esperan
    al que está en curso.
  - La página de la clase de una importada muestra el resumen de `importacion.json` (`inicio_elegido` y
    los candidatos del panel dicen de dónde salió el inicio) y una importada en `grabando` figura
    *subiendo*. `/panel/clases/{id}/audio` sirve `sesiones.ruta_audio_panel` con `audio.MIME_AUDIO`.
- **Publicar con reemplazo** (tabla `publicaciones`: `vigente | sin_archivar | archivada | no_esta`):
  - las carpetas anteriores de la sesión (`vigente` y `sin_archivar`, o `ruta_publicada` en una
    sesión de antes de la tabla) se mueven enteras a `<datos>/apuntes-anteriores/<materia>/<AAAA-MM-DD
    HH.MM.SS> <nombre>/` y la nueva se publica con el nombre normal;
  - sólo se mueve un directorio de verdad, dentro de la `carpeta_clases` de alguna materia, que
    publicó esta sesión (si otra sesión publicó después en la misma ruta, manda la más nueva);
  - `(v2)` sólo ante una carpeta ajena con ese nombre;
  - si la nueva no compiló y la vigente (en la carpeta de clases de la materia de ahora) tiene
    `apunte.pdf`, no se mueve nada: la sesión queda en error, `ruta_publicada` sigue en la vieja y esa
    versión vuelve a ser la `actual` (la que leen el índice, la búsqueda y el MCP);
  - en uso (Windows): `ESPERAS_ARCHIVAR` (~10 s) y, si sigue, la nueva va al lado y la vieja queda
    `sin_archivar` con su `aviso` (panel: clase y Estado), para archivarla en la próxima publicación;
  - si falla la copia o la base, se borra la nueva y se restaura la vieja; todo en `pipeline.log`.

## Búsqueda y MCP

- **Filas de `busqueda`** (FTS5, `unicode61 remove_diacritics 2`): `texto`, `materia` (id), `tipo`
  (`apunte` | `transcript`), `ref` (id de la sesión), `inicio_ms`. Por clase con apunte actual:
  una fila por sección de `meta.json` ("Sección: título. Temas: …"), el `apunte.tex` en texto plano
  partido en tramos de hasta ~700 caracteres sin cruzar `\section` (minuto: el primer `\ts{}` del
  tramo, si no el último anterior, si no el inicio de la sección en `meta.json`) y los segmentos del
  transcript unidos en tramos de hasta ~45 s. Las consultas filtran por la columna `texto`
  (`texto : (…)`): FTS5 indexa también las otras.
- **Consulta:** palabras sin las vacías ("qué dijo sobre…"), cada una como prefijo sin el plural
  (`"derivada"*`); primero todas (AND) y, si no hay nada, alguna (OR). `"frase"` busca exacto.
- `publicar` llama a `indexar_publicada` después de dejar la sesión en `lista`/`error`; un error
  ahí sólo queda como aviso en `pipeline.log`. `classmer reindexar` rearma todo; la herramienta
  `buscar` lo hace sola si el índice está vacío y hay apuntes. Al borrar una sesión:
  `busqueda.borrar_sesion(conn, id)` (si no, `buscar` igual ignora las sesiones que no existen).
- **MCP:** `POST /mcp` JSON-RPC 2.0 (un mensaje o un lote; notificaciones → `202`), `GET`/`DELETE`
  → `405`. Versiones del protocolo: 2025-11-25, 2025-06-18, 2025-03-26 y 2024-11-05 (si el cliente
  pide otra, se ofrece la primera). Token Bearer como la API; `Origin` que no sea localhost → `403`.
  Errores de uso de una herramienta → resultado con `isError: true` (el modelo lo ve y corrige);
  herramienta desconocida → error JSON-RPC `-32602`.
- **`archivos` (MCP: `leer_apunte`, `listar_clases`, `buscar`)** suma `"adjuntos": [rutas absolutas]`
  (`herramientas._rutas_adjuntos`): por cada adjunto de la clase, la copia de la carpeta publicada
  (`adjuntos/<nombre para LaTeX>`) si está; si no (no se publicó afuera, llegó después de publicar, o
  un HEIC que no se pudo convertir), el original de `Classmer-data/sesiones/<id>/adjuntos/`. Uno que
  no está en ningún lado no va.

## Modelos y facturación de Claude

- **Config:** `clasificar {motor: claude|gemini, modelo, esfuerzo, umbral_confianza}`, `apunte {modelo,
  esfuerzo, …}`, `claude {facturacion: api|max|api_luego_max|max_luego_api, reintentar_api_h}` y, por
  materia, `apunte: {modelo, esfuerzo}` (`cfg.modelo_apunte(materia)` → `(modelo, esfuerzo, origen)`).
  El panel las edita con `ConfigViva.editar_modelos` / `editar_materia` (sólo modelos del catálogo).
- **Clave:** `cfg.anthropic_api_key` sale sólo de `Classmer-data/.env` (no del entorno del proceso) y
  nunca se exporta. `AgenteClaude` la pone en el `env` de las opciones de la corrida sólo con
  facturación `api`; en las dos, `_conectar` saca del entorno heredado `VARIABLES_FUERA` (claves,
  `ANTHROPIC_BASE_URL` y variables de una sesión de Claude Code padre). El init del CLI informa
  `apiKeySource` (`ANTHROPIC_API_KEY` con la API, `none` con el plan Max); si no coincide, aviso en
  `agente.log`.
- **Errores** (`agente_apunte.clasificar_error`, `clasificador.error_api`): cambian de medio
  (`SinCreditos`) la API sin créditos (402, `billing_error`, "credit balance", tope de gasto del
  workspace), la clave inválida (401), el cupo del plan Max agotado ("usage limit", RateLimitEvent
  `rejected`) y la falta de login. Lo demás (429, 5xx, 529, sin red) es `CuotaAgotada` y no cambia.
- **Memoria** (tabla `facturacion`, una fila por medio que no anda): la API se vuelve a probar a las
  `reintentar_api_h` horas, si cambia la huella de la clave (8 hex de SHA-256; se guarda la de la clave
  con la que corrió el intento, y la config viva adelanta los jobs de Claude pospuestos al cambiarla) o
  con `Facturacion.reintentar("api")`; el plan Max, en `hasta` (resets_at + 60 s) o con
  `reintentar("max")`. `ejecutar` sin ningún medio disponible lanza `CuotaAgotada` con la espera hasta
  el primero que se libere (entre 1 min y 6 h).
- **Costo:** con la API, `costo_api(modelo, uso)` con los precios de la skill claude-api
  (`PRECIOS_USD_MTOK`); si el modelo no está, sólo tokens. Con el plan Max, el `total_cost_usd` del
  CLI ("equivalente API"). Va a `apuntes.costo`, `jobs.costo` y `jobs.facturacion` (los del intento
  que quedó; lo que gastó un intento descartado va en `pipeline.log` y en el resumen del job).
- **Catálogo:** `modelos.opciones_tarea(conn, tarea, elegido)` devuelve lo que muestra cada select (un
  elegido que no está queda primero, marcado). Filtros: Claude → `clasificar` si la API informa
  `structured_outputs`, `apunte` si informa `image_input` y `effort`; Google → `transcribir` si el id
  tiene `transcribe` y `generateContent`, `clasificar` si es `gemini-*` con `generateContent` y no es
  tts/imagen/embedding/live/robotics/omni/banana/customtools/deep-research. Un modelo que no aparece
  en el descubrimiento de su proveedor queda `presente = 0`; sin clave o con error, el proveedor no
  se toca. `esfuerzo_para` baja el esfuerzo del clasificador si el modelo no acepta ese nivel.
- **Clasificar por el plan Max:** `ClaudeAgentOptions(tools=[], allowed_tools=["StructuredOutput"],
  output_format={json_schema}, max_turns=3, setting_sources=[])` en `jobs/_clasificar/`; el resultado
  sale de `ResultMessage.structured_output`.
