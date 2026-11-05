# Classmer — server (v0.1)

Server en Python (FastAPI + SQLite) que corre en la PC: recibe las clases que graba la app,
las transcribe con Gemini, arma la línea de tiempo con fotos, notas y marcas, y un agente de
Claude (Agent SDK, con la API key o con el login de Claude Code del plan Max) escribe el apunte en
LaTeX, lo compila con MiKTeX y lo publica en la carpeta de la materia.

- Diseño: [`../ARCHITECTURE.md`](../ARCHITECTURE.md) (fuente de verdad).
- Contratos internos entre módulos: [`docs/internos.md`](docs/internos.md).

## Puesta en marcha

Requisitos: Python ≥ 3.12 con `uv`, ffmpeg en el PATH, MiKTeX con instalación automática de
paquetes y Claude Code logueado (plan Max) o una API key de Anthropic.

```powershell
cd server
uv sync
uv run classmer init      # crea Classmer-data, config.yaml, .env y el token (no lo muestra)
uv run classmer token     # muestra el token para cargarlo en la app (Ajustes)
uv run classmer token --qr   # lo mismo, como QR para escanearlo con la cámara del celu
```

`--qr` deja `token-qr.png` en Classmer-data: borralo después de cargar el token en la app.

Después:

1. Pegá `GEMINI_API_KEY=...` en `%USERPROFILE%\Documents\Classmer-data\.env`. No la compartas en
   ningún chat ni la subas a git.
2. Opcional: `ANTHROPIC_API_KEY=...` en el mismo `.env`, para pagar lo que se le pide a Claude con
   la API (ver [Modelos y facturación](#modelos-y-facturación-de-claude)). Sólo se lee de ese archivo,
   nunca del entorno.
3. Revisá `Classmer-data\config.yaml`: materias, `titulo_regex` del calendario (están en `null`
   con un `TODO`) y modelos.

La carpeta de datos se puede mover con la variable `CLASSMER_HOME`.

## Levantar el server

```powershell
uv run classmer serve            # API en 0.0.0.0:8000 + worker de jobs
```

- **La primera vez**, Windows pregunta si Python puede usar la red: aceptá en **redes privadas**.
- Si una VPN en la PC tiene una opción que **bloquea la red local**, tiene que estar **apagada**,
  si no el celu no ve la PC.
- URL para la app: `http://<IP LAN de la PC>:8000` (por ejemplo `192.168.1.10`; conviene reservar
  la IP en el router).

Para que arranque solo al iniciar sesión, registrá la tarea programada. Corrélo desde el checkout
que va a usar el server (no desde un worktree temporal): la tarea apunta a la carpeta donde está el
script. Se registra una vez, y otra vez cada vez que cambia cómo se registra o el puerto
(`servidor.port`, con `-Puerto <el nuevo>`), seguido de `-Reiniciar`. Volver a registrar no toca la corrida en curso, y
`-Reiniciar` avisa si la tarea quedó registrada a la antigua.

```powershell
powershell -ExecutionPolicy Bypass -File scripts\registrar-arranque.ps1              # registrar
powershell -ExecutionPolicy Bypass -File scripts\registrar-arranque.ps1 -Reiniciar   # reiniciar el server
powershell -ExecutionPolicy Bypass -File scripts\registrar-arranque.ps1 -Parar       # pararlo sin que vuelva solo
powershell -ExecutionPolicy Bypass -File scripts\registrar-arranque.ps1 -Quitar      # dar de baja
```

- **Sin ventana:** la tarea corre `conhost.exe --headless` con PowerShell y el script en modo
  `-Correr`. No hay ninguna ventana que se pueda cerrar. Con `powershell -WindowStyle Hidden` y
  Windows Terminal como terminal por defecto, esa consola terminaba en una ventana minimizada de
  Terminal (titulada `...\powershell.exe`), y cerrarla mataba el server (`LastTaskResult`
  0xC000013A).
- **El server tiene su propia consola**, también sin ventana: bajo la tarea, `-Correr` lanza
  `cmd /c uv run classmer serve` con `CREATE_NO_WINDOW` y dentro de un job object, así no depende
  del conhost de la tarea. En una corrida a mano de `-Correr` comparte la consola, y Ctrl+C o
  cerrarla lo cortan también.
- **Se levanta solo:**
  - si se cae el server, `-Correr` lo relanza a los 30 s. Antes cierra lo que haya quedado
    colgando del server muerto (p. ej. un `claude.exe` de un apunte a medias), con el job object;
  - un error inesperado en `-Correr` se anota en el log y se vuelve a probar a los 30 s;
  - si se muere el `-Correr` (o todo junto), la tarea tiene, además del disparador de inicio de
    sesión, uno cada 5 minutos sin fin. Mientras la tarea corre, el Programador lo ignora
    (`IgnoreNew`); si no corre, la arranca de nuevo. Por eso, después de registrarla, arranca sola
    en 5 minutos aunque nadie la arranque;
  - si alguien cierra el conhost de la tarea (`Stop-ScheduledTask`, "Finalizar" en el
    Programador), `-Correr` sale enseguida y deja el server andando con su consola; el `-Correr`
    siguiente lo cuida.
- **Nunca dos servers:**
  - un solo `-Correr` por carpeta de datos: el candado `Classmer-data\server-correr.lock` es del
    proceso y Windows lo suelta solo si se muere. Si otro ya lo tiene, el nuevo sale sin hacer
    nada;
  - `-Correr` no cierra un server que escucha en el puerto: si quedó uno vivo, espera a que se
    libere y recién ahí levanta otro. Mientras espera, mete cada proceso de ese server en un job
    object propio, así cuando se muere también cierra lo que haya quedado colgando de él.

Para reiniciar el server (p. ej. después de tocar `.env` o `config.yaml`), usá `-Reiniciar`. No
uses `Stop-ScheduledTask`/`Start-ScheduledTask` a secas: el server sigue vivo con la config vieja
y el `-Correr` nuevo no lo toca porque escucha en el puerto. `-Reiniciar` avisa si el puerto lo
tiene otro programa y no cuenta eso como "reiniciado".

Para tenerlo parado un rato (p. ej. `uv sync` después de un merge), usá `-Parar`: además de
cerrarlo, deshabilita la tarea para que el disparador no lo levante en medio. Volver a registrarla
la deja deshabilitada, y `-Reiniciar` la vuelve a habilitar.

`-Reiniciar`, `-Parar` y `-Quitar` actúan sobre el server de la tarea. Su puerto y su carpeta de
datos salen de cómo se registró, no de la consola desde la que los corrés (si les pasás `-Puerto`
o `-Datos` y no coinciden, no hacen nada). Cierran:

- el `-Correr` que tiene el candado, con todo su árbol;
- lo que lanzó `cmd /c uv run classmer serve >> <su log>`, aunque su `-Correr` ya no esté;
- el `classmer serve` que escucha en el puerto de la tarea, venga de donde venga (también uno de
  otro worktree).

No tocan servers de otro worktree o de otra carpeta de datos que escuchan en otro puerto, ni
comandos que sólo nombran "classmer serve". Si el candado lo tiene algo que no reconocen como
`-Correr` (p. ej. una consola donde corriste `-Correr` a mano), avisan y no lo cierran.

Para probar cambios del script sin tocar el server en uso, registrá otra tarea con otra carpeta de datos
y otro puerto (el mismo que `servidor.port` de su `config.yaml`):
`registrar-arranque.ps1 -NombreTarea 'Classmer prueba' -Puerto 8777 -Datos C:\tmp\cm-prueba`.
Después `-Reiniciar`, `-Parar` y `-Quitar` con `-NombreTarea 'Classmer prueba'` (el puerto y la
carpeta salen de la tarea). Si la tarea no existe, `-Parar` y `-Quitar` no cierran nada.

La salida del server queda en `Classmer-data\logs\server-consola.log` (además de `classmer.log`).
Mientras un server vivo tiene ese log abierto, las notas de un `-Correr` que espera (o que sale
porque ya hay otro) van a `logs\server-correr.log`.

## Probar desde el celu (o desde otra PC de la LAN)

```powershell
# con el token de `uv run classmer token`
curl.exe -H "Authorization: Bearer <token>" http://192.168.1.10:8000/api/health
# → {"ok":true,"version":"0.1.0"}
```

`scripts/simular_celu.py` hace de celu de punta a punta contra el server: crea la sesión, sube
el audio por partes (respetando los 409), manda fotos, notas y marcas, finaliza y espera el PDF.

```powershell
uv run python scripts/simular_celu.py --url http://127.0.0.1:8000 --audio clase.ogg --materia analisis
```

## Importar una clase (audio de otro, plan B)

Para una clase que no grabaste con la app: el audio que te mandó un amigo por WhatsApp, la
grabadora de Samsung, un video. Va con fotos y material (PDF o imágenes) opcionales y sigue el
pipeline de siempre; la clase cuenta para la numeración como cualquier otra.

- **Desde la app:** "Importar clase". La app crea una sesión `importada` y la sube como una
  grabación: el audio por partes (`PUT /audio`), las fotos sin minuto y el material como adjuntos.
  `verificar` ubica esas fotos por su hora EXIF (las que no tienen EXIF quedan "sin hora").
- **Desde el panel:** **Clases → Importar clase** (<http://127.0.0.1:8000/panel/importar>).
  1. Elegí el audio y, si hay, las fotos. Se suben con una barra de progreso y el server los analiza
     sin crear nada: la duración, el inicio propuesto y dónde cae cada foto (con hora, sin hora o
     fuera de la grabación).
  2. Revisá el inicio. Cada candidato dice de dónde sale (las fotos, los metadatos del audio, la fecha
     del archivo); tocá uno o escribilo a mano, en hora de Buenos Aires.
  3. Elegí materia (o Auto), tipo y número: por defecto, el próximo de la materia; en Auto lo pone el
     server cuando se sabe la materia. Sumá el material (PDF o imágenes) si hay.
  4. **Importar y procesar**. Antes avisa que se genera un apunte nuevo y cuánto cuesta (con la
     facturación de ahora y el promedio de los últimos apuntes), y después te lleva a la clase.

  Si cambiás las fotos, se vuelve a analizar sin subir de nuevo el audio. Lo subido queda en
  `Classmer-data\importaciones\<token>\` hasta que importás o tocás **Cancelar** (o te vas de la
  página); lo abandonado se borra a las 24 h. Cancelar sirve hasta que el server recibe el pedido de
  importar: desde ahí ("Importando…") la clase se crea igual. Si la importación falla (por ejemplo,
  con el disco lleno), no queda ninguna clase a medias y el análisis sigue para volver a intentar.
- **Desde la PC:**

  ```powershell
  uv run classmer importar "D:\Audio de WhatsApp.m4a" --materia analisis --fotos "D:\fotos de hoy"
  # --inicio 2026-03-10T14:00:00-03:00   si la fecha del archivo no sirve
  # --numero 7                           número de clase (si no, el próximo de la materia)
  # --material "D:\Slides clase 7.pdf"   PDF o imagen para el apunte (se puede repetir)
  # --procesar                           para procesarlo acá mismo sin el server
  ```

  También por HTTP con `POST /api/importar` (multipart: `audio`, `fotos[]`, `material[]`,
  `materia_id`, `tipo_clase`, `inicio`, `numero_clase`).

**El inicio.** Sin `--inicio`, sale de la fecha del archivo menos su duración, del `creation_time`
del audio o de las fotos (si el audio no coincide con ellas, 1 min antes de la primera). Ojo con
los audios de WhatsApp: su fecha y su `creation_time` suelen ser la hora del envío, no la de la
grabación. `importacion.json`, en la carpeta de la sesión, dice de dónde salió el inicio y qué
fotos quedaron fuera de la grabación.

**Formatos.** ogg, opus, m4a, mp4, aac, mp3, wav, flac, webm, mka, amr, 3gp, wma y otros que lea
ffmpeg. Si el navegador no reproduce el audio (amr, wma…), `verificar` arma `audio.panel.ogg` para
el reproductor del panel; el pipeline usa siempre el original.

**Límites:** audio hasta **2 GB** (`413` si pasa); fotos hasta 50 MB; material hasta 100 MB cada
uno. Un material que no entra (tipo o tamaño) no frena la importación: queda avisado.

## Adjuntos de una clase

El PDF de slides o de notas del profe (o imágenes) se le adjunta a una clase para que el agente lo
use al hacer el apunte, como material de referencia: la clase grabada sigue siendo la fuente
principal.

- **Tipos:** PDF, JPEG, PNG, WebP o HEIC, detectados por el contenido (no por la extensión). Hasta
  **100 MB** cada uno.
- **Desde la app:** "Adjuntar" mientras se graba (queda con el minuto) o desde la clase en Inicio,
  también después. Se sube con la misma cola que el resto.
- **Desde el panel:** en la página de la clase, sección **Adjuntos** (uno o varios a la vez).
- **Por la API** (`id` es un UUID nuevo; con el mismo `id` es un reenvío y no se duplica):

  ```powershell
  curl.exe -H "Authorization: Bearer <token>" -F "id=$([guid]::NewGuid())" `
    -F "archivo=@C:\Users\usuario\Downloads\Slides clase 5.pdf" `
    http://127.0.0.1:8000/api/sesiones/<sesion>/adjuntos
  # opcional: -F offset_ms=2040000 (minuto del audio)
  ```

  `201` con el adjunto y la sesión (`200` si ese `id` ya estaba); `413` si pasa los 100 MB, `415` si
  no es de un tipo aceptado.
- **En cualquier estado de la clase**, también en error (por ejemplo, un apunte que no compiló): así
  se suma material antes de regenerar.
- **Si el apunte ya estaba hecho** (o no compiló), el adjunto queda "sin usar": el panel y la app ofrecen
  **Regenerar el apunte con los adjuntos** (`POST /api/sesiones/<sesion>/regenerar`), que rehace el
  apunte desde la etapa `apunte` y cuesta lo que un apunte nuevo.

## Revisión del transcript y audio del celu

Antes del apunte, la etapa `revisar_transcript` deja el transcript fiel y bien escrito (se puede
compartir, por ejemplo con el profe). Cada materia tiene su **tipo de revisión** (página de la materia, `revision` en
`config.yaml`), que vale para todas sus clases, grabadas con uno o con los dos equipos:

- **Automática** (por defecto): la revisión corre entera (concilia si hay celu, limpia, vuelve a
  escuchar con Gemini y resuelve) y cada duda que queda toma la propuesta del modelo, como con
  **Listo**, en el mismo job: el apunte sale solo, sin esperar al usuario.
- **Asistida:** lo mismo, pero si quedan dudas el apunte espera a que las revises en el panel (abajo).
  Para las materias en las que corregís seguido al modelo o en las que el transcript se comparte.
- **Apagada:** la clase sigue como antes: el crudo y el apunte directo (el celu sólo cubre donde el
  principal no tiene audio).

El switch viejo `revisar_transcript: false` de una materia vale como apagada (y `true`, como
automática); al guardar la materia desde el panel se reemplaza por `revision`. Cambiar el tipo no toca
las clases que ya esperan la revisión: siguen hasta **Listo**.

- **Con un solo audio, limpia:** saca muletillas y repeticiones, puntúa, corrige lo que el reconocedor
  entendió mal (sólo si suena parecido y el contexto lo deja claro), escribe los términos y la notación
  de la materia (σ, θ, μₜ), corrige quién habla y pone entre corchetes lo que agrega para que se
  entienda (`[vez de]`); lo que no se entiende queda como `[…]`. Conserva las líneas y los minutos.
- **Con dos audios (el reloj y el celu), primero concilia:** elige, en las líneas donde difieren, lo
  que más probablemente se dijo, y suma lo que sólo oyó el celu (una pregunta de un alumno). Después
  limpia.
- **Regla de fidelidad:** se corrige lo que el reconocedor entendió mal, nunca lo que dijo el profe
  (si dijo "sube" donde era "baja", queda "sube"). No se resume ni se reordena.
- **Dudas:** lo que cambia el sentido y no está claro queda como duda. Antes, Gemini vuelve a escuchar
  esos segundos en los dos audios (hasta `revisar_transcript.escuchar_max`, 30 dudas por clase) y una
  parte se resuelve sola. Los tramos van agrupados (hasta 12 o 5 min de audio por pedido, de a un
  pedido) porque la cuota de Gemini es la misma que transcribe las clases. Con el tope por minuto se
  espera lo que pide Gemini (hasta 3 veces por grupo); sin cuota diaria no se espera: las dudas pasan al
  usuario sin la nueva escucha. Los errores de Gemini dicen qué cuota se tocó (`[cuota: …]`).
- **Con la automática,** las dudas que quedan toman la propuesta del modelo y la clase sigue al apunte
  (`aplicada.como: "automatica"` en `revision/revision.json`, una línea en `pipeline.log` y en
  `cambios.md`). La página de la clase dice "aplicada automáticamente: N dudas con la propuesta del
  modelo" y las muestra, con las otras versiones, sin poder responderlas.
- **Con la asistida,** si quedan dudas, la clase espera (`esperando_usuario`, `espera: "revision"`, con
  `dudas: N` en la API) y el apunte arranca cuando las revisás en el panel:
  - cada duda muestra las versiones, la propuesta del modelo, lo que se volvió a escuchar y botones
    para escuchar ese momento en cada audio (con el desfase);
  - elegís una versión o la editás; **Listo** aplica y encola el apunte (lo que no tocaste queda con
    la propuesta), **Generar sin revisar** lo arranca con lo que eligió el modelo.
- **El revisado es el transcript de la clase** en todos lados: el publicado (`transcript.md`), el
  MCP (`transcript`, `buscar`) y la búsqueda. El profe va con su nombre (el campo **Profesor** de la
  materia; sin él, "Profe") y los alumnos como "Alumno". Los crudos quedan en la carpeta de la sesión
  (`transcript.crudo.*`, `timeline.crudo.md`, `transcript.celu.*`) y se ven desde la clase en el
  panel, junto con el registro de cada cambio (`revision/`).
- **Modelo:** es una tarea más en **Modelos** (`revisar_transcript`), con la facturación de siempre.
  Por defecto Sonnet 5.5 con esfuerzo `medium`; el costo queda en el job.
- **Volver a correrla** (reprocesar desde `revisar_transcript`) no vuelve a pagar lo que ya se pidió
  (`jobs/<sesion>/revisar/cache/`) y conserva lo que ya respondiste.

**El audio del celu** (la segunda pista):

- **Desde la app (Ambos):** el reloj graba el principal y el celu sube su audio como `pista=celu`
  (`audio.celu.ogg`), con el desfase que mide la app. Se segmentan y transcriben las dos pistas.
- **Desde el panel:** en una clase terminada, **Agregar audio del celu** acepta un audio de la misma
  clase grabado con otra app. El desfase se mide solo, por correlación de los dos audios; después se
  transcribe el celu, se vuelve a revisar el transcript y se rehace el apunte (pide confirmación con
  el costo).
- **Respaldo:** donde el principal no tiene audio (empezó tarde, se cortó o se quedó sin batería), el
  transcript sale del celu, también con la revisión apagada (ahí el celu se transcribe sólo en esos
  tramos).

## Otros comandos

```powershell
uv run classmer estado                    # clases recientes con estado y etapa
uv run classmer estado <sesion>           # detalle: jobs, errores, dónde se publicó
uv run classmer asignar <sesion> --materia <id>      # resuelve "esperando_usuario" (por la materia)
uv run classmer reprocesar <sesion> --desde apunte   # re-ejecuta desde una etapa
uv run classmer reindexar                 # rearma el índice de búsqueda con las clases ya publicadas
uv run classmer modelos                   # catálogo de modelos por tarea (y cuáles están elegidos)
uv run classmer modelos --redescubrir     # lo actualiza con las APIs de Anthropic y Google
```

## Modelos y facturación de Claude

Cada tarea usa un modelo que se elige de una lista en el panel (**Modelos**), nunca escrito a mano:

| Tarea | Motor | Por defecto |
|---|---|---|
| `transcribir` | Gemini ASR | `gemini-3.5-transcribe` |
| `clasificar` | Claude o Gemini (texto) | `claude-sonnet-5-5`, esfuerzo `medium` |
| `revisar_transcript` | Claude (salida estructurada) | `claude-sonnet-5-5`, esfuerzo `medium` |
| `apunte` | Claude (Agent SDK) | `claude-opus-5-5`, esfuerzo `high` |

- **Catálogo:** arranca con los modelos conocidos y se actualiza con **Redescubrir modelos** (o
  `classmer modelos --redescubrir`), que consulta las APIs de modelos de Anthropic y Google. Cada
  tarea muestra sólo los que le sirven:
  - `apunte`: los de Claude que, según la API, aceptan imágenes y esfuerzo;
  - `clasificar`: los de Claude con salida estructurada y los de Gemini de texto;
  - `revisar_transcript`: los de Claude con salida estructurada;
  - `transcribir`: los de Gemini que transcriben audio (`*transcribe*` con `generateContent`).

  Si falta la clave de un proveedor, se mantiene lo que había de él y el panel lo avisa. Un modelo
  elegido que deja de aparecer no se cambia solo: queda elegido y marcado.
- **Esfuerzo** (`low`, `medium`, `high`, `xhigh`, `max`): llega al CLI como `--effort` y queda en
  `agente.log`. Si el modelo del clasificador no acepta ese nivel, se baja al más alto que acepte.
- **Por materia:** en la página de cada materia, "Modelo del apunte" hereda el global o fija modelo
  y esfuerzo propios (`apunte:` dentro de la materia en `config.yaml`).
- **Facturación** (`claude.facturacion`, un switch para todo lo que se le pide a Claude):
  - `api`: sólo la API key;
  - `max`: sólo el plan Max (el login de Claude Code);
  - `api_luego_max` (por defecto): la API y, si se queda sin créditos o la clave es inválida, el
    plan Max;
  - `max_luego_api`: el plan Max y, si se agota su cupo, la API.

  Sólo se cambia de medio por falta de créditos o de cupo, o por una clave (o un login) ausente o
  inválido: los errores pasajeros (429, 5xx, sobrecarga) se posponen como siempre. El cambio rehace
  el paso entero y queda en `pipeline.log` ("API sin créditos (…): sigo con el plan Max").
- **Memoria:** después de un error de créditos, la API queda "sin créditos" y las tareas siguientes
  van directo al otro medio. Se vuelve a probar a las `claude.reintentar_api_h` horas (6 por
  defecto), si cambia la clave del `.env` o con **Reintentar API** en el panel. El cupo agotado del
  plan Max se recuerda hasta que se libera (o hasta **Reintentar plan Max**).
- **La clave:** `ANTHROPIC_API_KEY` sólo se lee de `Classmer-data\.env` y nunca se exporta: el agente
  la recibe en el entorno de su corrida únicamente cuando factura por API. Sin clave, los modos con
  API funcionan como `max`. Con el plan Max, el CLI arranca sin ninguna clave.
- **Registro:** cada apunte guarda modelo, esfuerzo, facturación y costo (real con la API,
  "equivalente API" con el plan Max); cada job, con qué se pagó y cuánto.

## Panel web

Con el server andando, abrí <http://127.0.0.1:8000/panel> **en la PC** (desde la LAN o el
WireGuard responde 403; no pide token).

- **Clases:** estado, etapa, progreso y errores. En cada clase: la versión vigente del apunte (PDF y
  .tex, con modelo, esfuerzo, facturación y costo), las versiones anteriores con su PDF, audio con
  saltos por minuto, transcript, fotos, línea de tiempo, jobs con lo que pagó cada uno y botones para
  reprocesar, asignar la materia (las que quedaron en `esperando_usuario`) y borrar (sólo las de la
  materia `prueba`).
  - **Adjuntos:** nombre original, tipo (y páginas), tamaño, minuto (salta el audio) y estado:
    *usado* (lo usó el apunte vigente), *pendiente* (lo usa el apunte en curso o el próximo) o *sin usar*. El nombre abre
    el archivo en el navegador y **Descargar** lo baja con su nombre original. Se suben uno o varios
    desde la PC con el formulario de la sección.
  - Si hay adjuntos que el apunte vigente no usa, junto a Reprocesar aparece el aviso con
    **Regenerar el apunte con los adjuntos** (reprocesa desde `apunte`; pide confirmación porque
    cuesta lo que un apunte nuevo). En la lista de clases, 📎 N marca las que tienen adjuntos.
  - **Importar clase** (botón arriba de la lista): un audio que no grabó la app, con fotos y material
    (ver [Importar una clase](#importar-una-clase-audio-de-otro-plan-b)). Las importadas llevan la
    marca *importada* en la lista y en su página, que además resume de dónde salió el inicio, cuántas
    fotos quedaron ubicadas, sin hora o fuera de la grabación, el material y los avisos. Mientras la
    app sube una importada figura *subiendo* (no *grabando*).
  - El reproductor de un audio que Chrome no reproduce (amr, wma…) usa la copia `audio.panel.ogg`
    que arma `verificar`.
  - **Revisión del transcript:** el resumen (modo, modelo, costo, dudas, cómo se aplicó); en la
    asistida, las dudas para revisar con **Listo** y **Generar sin revisar**; en la automática, las
    dudas que tomaron la propuesta del modelo, sin responder; y los enlaces al transcript de la clase,
    los crudos y los cambios. Si la clase tiene audio del celu, hay un segundo reproductor y los botones ▶ de cada duda
    reproducen ese momento en cada audio. **Agregar audio del celu**, en una clase terminada.
- **Materias:** nombre, carpeta, reglas de calendario, número de clase (inicial o próximo), tipos,
  estilo propio, modelo del apunte (heredado o propio), el tipo de revisión del transcript (automática
  por defecto, asistida o apagada),
  el nombre del profesor, activa o no, y alta de materias nuevas.
- **Estilo:** el estilo global del apunte y, en sólo lectura, el contrato fijo de
  `prompts/apunte.md`.
- **Modelos:** facturación de Claude (modo, estado de la API y del plan Max, Reintentar), el modelo
  y el esfuerzo de cada tarea, y el catálogo con **Redescubrir modelos**.
- **Pendientes:** por materia, los items de los apuntes (parcial, fecha, tarea, duda, aviso) con
  la clase y el minuto; se marcan como hechos o descartados.
- **Estado:** cola de jobs, errores recientes, facturación, carpetas que no se pudieron archivar y
  el final del log.

Lo que se cambia en el panel (y con `PATCH /api/materias/{id}`) se escribe en
`Classmer-data\config.yaml` sin tocar los comentarios, y se aplica sin reiniciar. Si editás
`config.yaml` a mano, el server lo recarga solo a los pocos segundos; si quedó con un error, sigue
con la versión anterior y lo muestra en Estado. `servidor` y `datos` sólo cambian al reiniciar
(y `servidor.port`, además, pide volver a registrar la tarea con `-Puerto`).

## MCP: consultar las clases desde Claude (y desde Codex o un bot propio)

El server expone un servidor MCP en **`http://127.0.0.1:8000/mcp`** (desde la LAN,
`http://192.168.1.10:8000/mcp`). Va por HTTP ("Streamable HTTP", respuestas JSON, sin sesiones) y
pide el **mismo token** que la app: `Authorization: Bearer <token>`.

| Herramienta | Qué devuelve |
|---|---|
| `listar_materias()` | Materias, si están activas, cuántas clases (listas, en curso, con error), pendientes y la última clase |
| `listar_clases(materia?, desde?, hasta?, limite?)` | Clases con número, fecha, título, resumen, temas, estado y rutas |
| `leer_apunte(sesion)` | `meta.json` (secciones con minuto, items), el LaTeX del apunte y las rutas (también de los adjuntos) |
| `transcript(sesion, desde?, hasta?)` | Transcript literal entre dos minutos (`34`, `34.5` o `"00:34:10"`) |
| `buscar(texto, materia?, tema?, limite?)` | Fragmentos del apunte y del transcript con clase y minuto (sin importar tildes) |
| `temas(materia)` | Temas con las clases y minutos que los tocaron (hoy, los de cada `meta.json`) |
| `pendientes(materia?, tipo?, estado?)` | Items: parcial, tarea, fecha, duda, aviso (por defecto, los abiertos) |
| `marcar_item(id, estado)` | Marca un item como `hecho`, `descartado` o lo vuelve a `abierto` |

- **Para citar:** todo lo que sale de una clase trae `sesion`, `materia`, `numero`, `fecha`,
  `titulo`, `inicio_ms`/`minuto` y una `cita` lista ("Análisis Matemático II, clase 7
  (2026-04-21), min 00:34:10").
- **Rutas absolutas:** `archivos` trae `pdf`, `tex` y `transcript` de la carpeta publicada (o los
  de `Classmer-data`, si la clase no se publicó afuera), para adjuntarlos desde la misma PC. En
  `adjuntos`, la lista de los adjuntos de la clase (slides, notas del profe, imágenes): la copia de
  `adjuntos/` en la carpeta publicada o, si no está, el original de `Classmer-data` (`[]` si no hay).
- **`materia`** acepta el id o el nombre, sin tildes ni mayúsculas; **`sesion`**, el id o un
  prefijo único.
- **Búsqueda:** índice FTS5 que se llena al publicar cada clase. Para las que ya estaban
  publicadas: `uv run classmer reindexar` (si el índice está vacío, la primera búsqueda lo arma
  sola).

Los ejemplos suponen el repo en `Documents\Claude\Projects\Classmer` y la carpeta de datos por
defecto.

### Claude Code

Una sola vez, con alcance de usuario (sirve en cualquier carpeta). En PowerShell, sin mostrar el
token:

```powershell
$token = (@(Get-Content "$env:USERPROFILE\Documents\Classmer-data\.env") -match '^CLASSMER_TOKEN=')[0].Split('=', 2)[1].Trim()
claude mcp add --scope user --transport http classmer http://127.0.0.1:8000/mcp --header "Authorization: Bearer $token"
Remove-Variable token
claude mcp list        # classmer: ... ✓ Connected
```

### Claude Desktop

La configuración de Claude Desktop lanza procesos (stdio), así que se usa un puente chico que
viene con Classmer: `server\classmer\mcp\puente.py`. Sólo usa la librería estándar de Python (no
necesita el entorno de uv) y lee el token de `Classmer-data\.env`, así que **el token no queda
escrito** en la configuración.

1. Hacé un backup de `%APPDATA%\Claude\claude_desktop_config.json`.
2. Agregá dentro de `"mcpServers"`:
   ```json
   "classmer": {
     "command": "C:\\Users\\usuario\\AppData\\Local\\Python\\pythoncore-3.14-64\\python.exe",
     "args": ["-I", "C:\\Users\\usuario\\Documents\\Claude\\Projects\\Classmer\\server\\classmer\\mcp\\puente.py"]
   }
   ```
   `--url http://…/mcp` al final de `args` cambia el server (por defecto, `127.0.0.1:8000`).
3. Cerrá Claude Desktop del todo (también desde el ícono de la bandeja) y abrilo de nuevo.

Si el server está caído, el puente responde a cada pedido con un error que lo explica ("Classmer
no responde en …"). Alternativa sin el puente: `npx -y mcp-remote http://127.0.0.1:8000/mcp
--header "Authorization:${CLASSMER_AUTH}"` con `CLASSMER_AUTH=Bearer <token>` en `env` (ahí el
token sí queda en el archivo).

### Un bot propio (Agent SDK)

Un bot que corre en la misma PC (por ejemplo, uno de WhatsApp o Telegram hecho con el Agent SDK)
habla HTTP directo. Lo que hace falta:

- **URL:** `http://127.0.0.1:8000/mcp`.
- **Token:** `uv run classmer token` (o `CLASSMER_TOKEN` en `Classmer-data\.env`).
- **Config del Agent SDK** (TypeScript):
  ```ts
  mcpServers: {
    classmer: {
      type: "http",
      url: "http://127.0.0.1:8000/mcp",
      headers: { Authorization: `Bearer ${process.env.CLASSMER_TOKEN}` },
    },
  },
  ```
  Las herramientas quedan como `mcp__classmer__buscar`, `mcp__classmer__listar_clases`, etc.
  (para `allowedTools`). `marcar_item` es la única que modifica algo.
- **PDF para mandar:** `listar_clases(materia)` → la última clase → `archivos.pdf` (ruta
  absoluta). `archivos.publicado` dice si es la copia de la carpeta de la materia.

### Codex

En `%USERPROFILE%\.codex\config.toml`, con el mismo puente (sin token en el archivo):

```toml
[mcp_servers.classmer]
command = 'C:\Users\usuario\AppData\Local\Python\pythoncore-3.14-64\python.exe'
args = ['-I', 'C:\Users\usuario\Documents\Claude\Projects\Classmer\server\classmer\mcp\puente.py']
```

Codex también acepta el servidor HTTP directo (`url = "http://127.0.0.1:8000/mcp"` y
`bearer_token_env_var = "CLASSMER_TOKEN"`, con esa variable en el entorno de Windows).

## Dónde queda cada cosa

```
Classmer-data/
  config.yaml, .env         configuración y secretos (fuera del repo)
  classmer.db               SQLite (WAL): índice y estado, cola de jobs
  sesiones/<id>/            audio, fotos, eventos, bloques, transcript, timeline, apunte/vN/
                            audio.celu.<ext>, bloques_celu/, transcript.celu.*  ← la segunda pista
                            transcript.crudo.*, timeline.crudo.md  ← lo de linea_de_tiempo, sin revisar
                            revision/  ← la revisión: revision.json (líneas y dudas), conciliar.json,
                                         limpiar.json, escuchar.json y cambios.md
                            adjuntos/  ← los adjuntos (nombre en ASCII armado por el server)
                            adjuntos.jsonl  ← una línea por adjunto recibido
                            importacion.json  ← clases importadas: inicio, fotos y avisos
                            audio.panel.ogg  ← copia para el panel de un audio importado que el
                                               navegador no reproduce
                            pipeline.log  ← qué pasó en cada etapa
  importaciones/            área temporal del panel ("Importar clase"): se borra al importar, al
                            cancelar y, lo abandonado, a las 24 h
  jobs/<id>/apunte/         carpeta de trabajo del agente (input/ con adjuntos/ y adjuntos.json,
                            plantilla/, output/)
  jobs/<id>/revisar/        caché de los pedidos de la revisión y los recortes que se volvieron a escuchar
  apuntes-anteriores/<materia>/<AAAA-MM-DD HH.MM.SS> <carpeta>/
                            versiones publicadas que se reemplazaron (las limpiás a mano)
  logs/classmer.log         log del server
```

El apunte publicado queda en `<carpeta de la materia>\<carpeta_clases>\AAAA-MM-DD Clase NN - Título\`
(PDF, .tex, `classmer.cls`, fotos, transcript y `adjuntos/` con los adjuntos: los PDF tal cual y las
imágenes como JPEG), más `INDICE-CLASES.md`. En esa carpeta queda sólo la versión vigente de cada
clase:

- Al reprocesar, la carpeta anterior se mueve entera (con lo que le hayas agregado) a
  `Classmer-data\apuntes-anteriores\` y la nueva se publica con el nombre normal, aunque cambie el
  título.
- `(v2)` sólo aparece si ya hay una carpeta con ese nombre que no es de esa clase (otra clase con el
  mismo título o una carpeta tuya): nada que no haya publicado esa misma clase se pisa ni se mueve.
- Si la versión nueva no compiló y la publicada sí, la publicada se queda donde está (la nueva queda
  en `sesiones\<id>\apunte\vN\` y la clase en error, para reprocesarla).
- Si Windows no deja mover la carpeta anterior (por ejemplo, con el PDF abierto), reintenta unos
  segundos; si sigue bloqueada, la nueva se publica al lado y el panel avisa que cierres el PDF: la
  vieja se archiva en la próxima publicación.
- Si algo falla a mitad, se borra la copia nueva y la anterior vuelve a su lugar.
- Los títulos se limpian de LaTeX (`Black--Scholes` → `Black-Scholes`) y se cortan en una palabra.

## Tests

```powershell
uv run pytest            # todo (los que usan ffmpeg o pdflatex se saltean si no están)
uv run pytest -m "not latex"
```

### Prueba de punta a punta con una clase sintética

```powershell
# 1. Audio de una clase (~8 min, voz de Windows, con pausas y un silencio largo) → clase-prueba.ogg
powershell -ExecutionPolicy Bypass -File scripts\generar-audio-prueba.ps1 -Salida C:\temp\e2e
# 2. Fotos de pizarrón con hora EXIF
uv run python scripts/generar_fotos_prueba.py --salida C:\temp\e2e\fotos --inicio 2026-03-10T19:00:00-03:00 --offsets 60,150,215,400
# 3. Importar y procesar (con una materia de prueba en un CLASSMER_HOME aparte)
uv run classmer importar C:\temp\e2e\clase-prueba.ogg --materia prueba --fotos C:\temp\e2e\fotos --inicio 2026-03-10T19:00:00-03:00 --procesar
```

Usá un `CLASSMER_HOME` de prueba con una materia cuya `carpeta` sea una carpeta temporal, así no
se publica nada en las carpetas reales.

## Problemas comunes

| Síntoma | Qué mirar |
|---|---|
| La app dice 401 | El token de la app no coincide con `uv run classmer token`. |
| La app no conecta | Firewall de Windows (permiso para Python en redes privadas), una VPN que bloquee la red local, IP de la PC. |
| La sesión queda en `procesando` con error "falta GEMINI_API_KEY" | Cargar la clave en `.env` y reiniciar el server (si corre como tarea: `powershell -ExecutionPolicy Bypass -File scripts\registrar-arranque.ps1 -Reiniciar`); el job reintenta solo. |
| `esperando_usuario` | La IA no supo la materia: `uv run classmer asignar <sesion> --materia <id>`. Si la clase espera la revisión del transcript (materia con la revisión asistida; en el panel dice "El apunte espera tu revisión"), revisá las dudas en la página de la clase y tocá **Listo** (o **Generar sin revisar**). |
| `classmer serve` sale con "Ya hay otro classmer serve … no abro la base" | Ya hay un server con esa carpeta de datos (la tarea programada, por ejemplo). Para probar, levantá otro con `CLASSMER_HOME` en otra carpeta: la primera línea del log dice qué carpeta de datos abre. |
| Una revisión dice "Gemini sin cuota: … pasan sin la nueva escucha" | Se acabó la cuota diaria de Gemini (es por proyecto de Google, la misma que transcribe las clases): las dudas quedan para revisar en el panel sin la nueva escucha. El error dice qué cuota se tocó (`[cuota: …]`). |
| "el apunte no compiló" | El `.tex` y el log quedan en `sesiones/<id>/apunte/vN/`; `uv run classmer reprocesar <sesion> --desde apunte`. |
| Algo raro en una etapa | `sesiones/<id>/pipeline.log` y `logs/classmer.log`. |
| Un job de Claude dice "ningún medio de facturación de Claude disponible" | La API quedó sin créditos y el plan Max sin cupo (o el modo usa uno solo). Panel → Modelos: cargá créditos o esperá el cupo y tocá **Reintentar API** / **Reintentar plan Max**. |
| "la API key no es válida" | Revisá `ANTHROPIC_API_KEY` en `Classmer-data\.env` (al cambiarla se vuelve a probar sola). |
| El panel avisa "no pude archivarla: está en uso" | Cerrá el PDF (o lo que tenga abierta la carpeta vieja): se archiva en la próxima publicación de esa clase. |
| `uv sync` / `uv add` dice "being used by another process" | Con el server corriendo, Windows bloquea `.venv\Scripts\classmer.exe`. Pará el server, sincronizá y volvé a levantarlo. `uv run classmer token` y `estado` andan igual con el server arriba mientras no cambien las dependencias. |
