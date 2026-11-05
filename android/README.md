# Classmer — app Android de captura

Graba la clase entera (Ogg/Opus continuo), anota fotos, notas y marcas con su minuto, y sube todo
al server de la PC sin perder nada aunque se corte la red. Diseño: [ARCHITECTURE.md §4 y §5.1](../ARCHITECTURE.md).

## Compilar

Sólo hace falta el SDK de Android y un JDK 17 o más nuevo (no hace falta Android Studio):

- `local.properties` con la ruta del SDK (no se versiona):
  ```
  sdk.dir=C\:/Users/usuario/AppData/Local/Android/Sdk
  ```
- Plataforma `android-37.0` instalada: compileSdk es 37 porque las versiones actuales de Compose,
  AndroidX y OkHttp lo exigen. `targetSdk` sigue en 36 (Android 16) y `minSdk` en 29.
  ```bash
  "$LOCALAPPDATA/Android/Sdk/cmdline-tools/latest/bin/sdkmanager.bat" "platforms/android-37.0"
  ```

```bash
cd android
./gradlew assembleDebug          # APK en app/build/outputs/apk/debug/app-debug.apk
./gradlew testDebugUnitTest      # tests de la subida, el calendario, el reloj y el Ogg
```

## Instalar en el celu

1. En el celu (en un Galaxy): Ajustes → Información del teléfono → Información de software → tocar 7 veces
   "Número de compilación". Después, Opciones de desarrollador → Depuración USB.
2. Conectarlo por USB y aceptar la huella RSA de la PC.
3. Instalar (o actualizar, sin perder datos):
   ```bash
   "$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" install -r android/app/build/outputs/apk/debug/app-debug.apk
   ```

`install -r` conserva las clases guardadas. **No desinstales la app** si tiene clases sin subir: al
desinstalar, Android borra sus archivos. Si alguna vez `adb install -r` falla con
`INSTALL_FAILED_UPDATE_INCOMPATIBLE` (la PC firmó con otra clave de debug), rescatá primero las
clases (ver abajo).

## Configurar

En la app: ⚙ Ajustes.

- **URL del server:** `http://<IP de la PC>:<puerto>`, por ejemplo `http://192.168.1.10:8000`.
  Desde afuera de casa sirve la misma IP con el WireGuard del router prendido.
- **Token del dispositivo:** el `CLASSMER_TOKEN` del server; va en el header `Authorization: Bearer`.
- **Probar conexión:** llama a `/api/health` y `/api/materias`.
- **Batería:** conviene tocar "Permitir" para que Classmer funcione sin restricciones, y agregarla
  a "Apps que nunca se suspenden" de Samsung. Así la subida sigue después de la clase con la
  pantalla bloqueada.

**HTTP sin TLS:** Android sólo permite texto plano hacia las direcciones de
[`network_security_config.xml`](app/src/main/res/xml/network_security_config.xml). Vienen una IP de
ejemplo para la PC (`192.168.1.10`: cambiala por la de tu PC), `10.0.2.2` (el emulador) y `127.0.0.1`.
Si la PC cambia de IP, agregala ahí y reinstalá; mejor todavía, reservale la IP en el router.

## Cómo funciona

| Pieza | Archivo |
|---|---|
| Grabación (servicio en primer plano `microphone`, MediaRecorder Ogg/Opus mono 32 kbps 48 kHz) | `grabacion/ServicioGrabacion.kt` |
| `offset_ms` = audio grabado sin las pausas (reloj monotónico) | `grabacion/RelojGrabacion.kt` |
| Archivos de cada clase (`sesion.json`, `audio.ogg`, `eventos.jsonl`, `fotos/`, `subida.json`) | `datos/AlmacenSesiones.kt` |
| Protocolo de subida (append + 409, eventos idempotentes, `/finalizar` con SHA-256) | `subida/Subidor.kt` |
| Cuándo se sube (WorkManager con espera creciente + directo desde el servicio mientras graba) | `subida/SincronizacionSubida.kt`, `subida/PlanificadorSubida.kt` |
| Materia desde el calendario del celu (±15 min, reglas del server) | `calendario/ReglasCalendario.kt` |
| Adjuntos: copia al celu, subida y hoja de la clase | `datos/AlmacenAdjuntos.kt`, `subida/SubidorAdjuntos.kt`, `subida/SincronizacionAdjuntos.kt`, `ui/AdjuntosClase.kt` |
| Importar una clase: la pantalla, la copia al celu y su trabajo en segundo plano | `ui/ImportarScreen.kt`, `importacion/Importador.kt`, `importacion/CopiaImportacion.kt`, `importacion/TrabajadorImportacion.kt` |

- **Nada se borra:** la app nunca borra una clase (sólo las copias de una importación que falló y nunca
  llegó al server, con "Descartar": ver "Importar una clase"). Una hora de audio ocupa unos 19 MB
  (32 kbps de Opus más el encabezado de una página Ogg cada 20 ms), más las fotos.
- **Mientras graba**, el servicio sube lo nuevo cada ~30 s. En Android 16, WorkManager no corre
  con la pantalla bloqueada (Doze), pero un servicio en primer plano conserva la red.
- **Si se corta la red o el server**, todo queda en el celu y se retoma desde lo que confirmó
  el server (`bytes_recibidos`).
- **Si el server rechaza una clase** con un error que no se arregla reintentando (422, 413…), no
  se reintenta en loop: la clase muestra "El server la rechazó" con el motivo y un botón
  **Reintentar**. Si rechaza un solo evento (por ejemplo, una foto cuyo archivo ya no está), ese
  evento queda afuera y la clase se cierra igual.
- **Si la app se cae o la cierran desde el administrador de tareas** mientras graba, el audio queda
  válido hasta ese momento. Al volver a abrirla, la sesión se cierra como "interrumpida", con la
  duración real leída del Ogg, y se sube.
- **Reglas de calendario** (`calendario` de cada materia en `GET /api/materias`):
  - `calendario` se compara con el nombre del calendario en el celu, sin distinguir mayúsculas.
  - `titulo_regex` se busca en el título sin distinguir mayúsculas, también en letras con tilde
    (no hace falta que coincida el título entero).
  - Si una regla tiene las dos, tienen que coincidir las dos. Si un evento coincide con varias
    materias, gana la regla más específica (regex por encima de calendario).
  - Para excluir las clases particulares: `^(?!Clase particular).*An[aá]lisis Matem[aá]tico`.
  - El tipo (teórica/práctica) sale del título del evento.
  - Con dos clases seguidas, si la que está en curso termina en menos de 15 minutos, gana la que
    está por empezar.

### v0.2

- **Número de clase:** en Inicio, debajo de la materia, aparece "Clase N" con el `proximo_numero`
  que manda el server. El lápiz lo cambia (`PATCH /api/materias/{id}`, necesita conexión). La
  sesión se crea con ese `numero_clase`; en Auto no se manda (lo pone el server al asignar la
  materia). Sin conexión se usa el último número guardado, y al empezar a grabar se le suma uno en
  el celu hasta que el server responda. Si el server rechaza la sesión (400/422), se reintenta sin
  número y después como Auto, para que la clase no quede trabada.
- **Elegir la materia desde el celu:** una clase que el server no pudo clasificar
  (`esperando_usuario`) aparece como "Elegí la materia", con la materia sugerida si la hay. Se manda
  con `POST /api/sesiones/{id}/materia`; un 409 (el apunte ya empezó) o un 422 (la materia no
  existe) se muestran en el diálogo.
- **Revisión del transcript (§15):** si el server espera la revisión (`esperando_usuario` con
  `"espera": "revision"`), la clase aparece como "Revisá el transcript" con "Hay N dudas para revisar
  en el panel", y no ofrece elegir la materia. La revisión se hace en el panel. Sin `espera` (un server
  viejo) o con `"materia"`, es la espera de siempre.
- **Corte automático** (`grabacion/CorteAutomatico.kt`): "¿Sigue la clase?" con Seguir / Terminar, en
  una notificación con cuenta regresiva y en la pantalla de grabación.
  - Si la materia salió del calendario, se pregunta al pasar el fin del evento + 15 min. Si el
    evento ya había terminado cuando se empezó a grabar, se usa la regla sin calendario.
  - Sin calendario, a las 3 h 30 min de empezada la grabación (hora real, con las pausas).
  - Sin respuesta en 10 min termina sola, salvo que Android no haya dejado mostrar la notificación.
  - "Seguir" vuelve a preguntar 30 min después.
- **Materias inactivas:** el server no las manda. Si una materia de la cache desaparece, se saca, y
  si estaba elegida la selección vuelve a Auto o al calendario.

### Adjuntos

El PDF de las slides o de las notas del profe, o imágenes: el agente del apunte los usa como
material de referencia (la clase grabada sigue siendo la fuente principal).

- **Cómo:** grabando, "📎 Adjuntar PDF o imagen" (van con el minuto del momento en que se toca). Después
  (el material suele llegar por mail o por el campus), "📎 Adjuntos" en cada clase de Inicio abre una
  hoja con la lista, el estado de cada uno y "Adjuntar" (sin minuto); anda también con las clases que
  sólo están en el server. Se eligen con el selector del sistema: no hace falta ningún permiso.
- **Tipos:** PDF, JPEG, PNG, WebP y HEIC/HEIF, reconocidos por el contenido, hasta 100 MB. Lo demás se
  rechaza en el celu con el motivo. Tampoco se copia si el celu quedaría con menos de 300 MB libres
  (los que pide la grabación), ni el mismo archivo dos veces en la misma clase.
- **Dónde quedan:** se copian enseguida a `Android/data/ar.classmer/files/adjuntos/<clase>/`
  (`<id>.<ext>` y sus datos en `<id>.json`; `subida.json` dice qué confirmó el server), aparte de
  `sesiones/`. Cada copia se anota antes (`<id>.copiando`): si Android cierra la app copiando, se
  retoma al abrirla (o avisa que hay que adjuntarlo de nuevo). Sólo se borra un adjunto que todavía
  no se subió, con "Descartar".
- **Subida:** la misma cola que el resto (WorkManager con reintentos, y el servicio mientras graba),
  recién cuando la clase ya existe en el server; también para clases terminadas. Va con su propio
  candado (`subida/SincronizacionAdjuntos.kt`): la subida de una clase, y la final al terminar, nunca
  esperan detrás de un PDF; si se corta quien la pidió, el archivo se sigue mandando mientras la app
  viva.
- **Primero pregunta:** antes de mandar un archivo, un `POST /adjuntos` con sólo el `id` (unos bytes):
  422 "falta el archivo" → lo manda; 200 → el server ya lo tenía (se había perdido la respuesta);
  404 "sesión no encontrada" o un server viejo → no manda nada. Si mandarlo falla (sin red, 5xx), ese
  archivo espera cada vez más (30 s, 1 min, 2 min… hasta 30 min) antes de volver a mandarse.
- **Rechazos y clases perdidas:** si el server los rechaza (413, 415, 422 u otro 4xx), se muestra el motivo
  con "Reintentar" y "Descartar". Si el server no encuentra la clase 6 veces seguidas (por ejemplo, se
  borró desde el panel), se deja de insistir: se le pregunta cada hora o con "Reintentar". Si esa clase
  ya no tiene fila en Inicio, sus adjuntos aparecen al final de la lista ("Adjuntos de clases que el
  server no encuentra"). Si el server perdió la clase y la subida la vuelve a crear, los adjuntos se
  vuelven a mandar.
- **Estado:** Copiando…, Esperando que la clase llegue al server, Pendiente de subir, Subiendo…,
  Subido (con lo que informa el server: lo va a usar el próximo apunte, lo usó el apunte vigente o el
  apunte vigente no lo usa), Rechazado, La clase no está en el server y Server sin soporte. La hoja
  también muestra los que el server tiene y el celu no (por ejemplo, subidos desde el panel de la PC).
  En la pantalla de grabación, debajo del botón, se ve lo que se está copiando, cuántos tiene la clase
  y lo que no se pudo adjuntar.
- **Regenerar:** si hay adjuntos que el apunte no usa (llegaron tarde) y la clase está lista o en
  error, la fila lo avisa y la hoja ofrece "Regenerar el apunte con los adjuntos", con confirmación
  (cuesta lo mismo que un apunte nuevo). Necesita conexión: sin red no queda nada pendiente.
- **Server viejo:** si el server todavía no tiene la ruta (404 "Not Found" o 405), queda anotado en
  `adjuntos/soporte.json` (vale para todos) y todos los adjuntos sin subir muestran "Server sin
  soporte": quedan guardados y se le vuelve a preguntar cada 10 minutos (unos bytes, nunca el
  archivo), o ya con "Reintentar"; cambiar de server en Ajustes también pregunta enseguida. No se le
  piden reintentos a WorkManager.
- **Límite conocido:** WorkManager corta un trabajo a los 10 minutos. Un PDF de 100 MB con ~1 Mbps de
  subida tarda más: sigue subiéndose mientras la app esté abierta o grabando, pero si Android congela
  la app en el medio, empieza de nuevo (el contrato no tiene subida por partes).

## Importar una clase

Para una clase que no se grabó con Classmer: típicamente, el audio que mandó un amigo por WhatsApp.
Sigue el mismo camino que una grabada (se sube, se transcribe, sale el apunte) y aparece en "Clases
recientes" con la marca **Importada**: mientras se copia, si la copia falló o si espera la materia, se ve
aunque sea de hace semanas y haya 20 clases más nuevas.

- **Cómo:** en Inicio, **Importar clase** (debajo de Grabar; no se puede mientras se graba). Se elige:
  - **Audio** (obligatorio), con el selector de archivos: `.opus`/`.ogg` (las notas de voz de
    WhatsApp), `.m4a`, `.mp4`, `.mp3`, `.wav`, `.webm`, `.3gp`… Muestra el nombre, el tamaño, la
    duración y la fecha que trae el archivo (en un audio de WhatsApp suele ser la del envío, no la de
    la grabación).
  - **Fotos** (opcional), de la galería.
  - **Material** (opcional): el PDF de las slides o imágenes, como los adjuntos de siempre.
  - **¿Qué clase es?:** la lista de clases del calendario del celu de un día (los eventos que
    coinciden con una materia, con su horario y el tipo según el título). Arranca en el día del audio
    (o en hoy) y se cambia con ‹ › (o tocando la fecha). Tocar una clase completa la materia, el tipo y
    el inicio (el del evento). Si ese día no hay eventos (el calendario del celu puede tener sólo las
    semanas cercanas), **Elegir a mano**: materia (también Auto (IA)), tipo, fecha y hora. El inicio
    se puede cambiar siempre (por ejemplo, si la clase empezó tarde). Elegir el audio (o cambiarlo)
    después de elegir la clase no toca lo elegido: la fecha del audio sólo mueve la lista si todavía no
    se eligió nada.
  - **Número de clase:** el próximo de la materia, editable. En Auto no va (lo pone el server).
- **Importar y procesar** pide confirmación: genera un apunte nuevo, que cuesta lo mismo que una
  clase grabada (con la API, alrededor de US$ 1–2).

Qué pasa después:

- **Local-first, como al grabar:** la clase se guarda en `sesiones/<id>/` en el estado local
  `importando` y un trabajo de WorkManager (uno por clase) copia el audio a `audio.<ext>` y las fotos a
  `fotos/<id>.<ext>`. La fila muestra "Copiando… 40 %" y, cuando Android lo deja, una notificación
  con el progreso (primer plano `dataSync`). Sigue aunque se cierre la app.
  - El audio se copia a `audio.<ext>.parcial` y se renombra al final. Cada 8 MiB se hace `fsync` y se
    anota en `importacion.json` hasta dónde llegó: si se corta, sigue desde ahí sin repetir lo copiado.
  - Android 12+ sólo deja pasar a primer plano desde segundo plano (por ejemplo, un reintento con la
    app cerrada) si Classmer está **sin restricciones de batería**. Si no deja, el trabajo sigue como uno
    común y corta solo a los 9 minutos (WorkManager ya no lo frenaría: anota el trabajo como de primer
    plano aunque Android lo haya negado); el próximo intento sigue desde lo copiado.
  - Los documentos elegidos quedan con permiso persistente hasta que termina la copia (después se suelta).
    El permiso es uno por documento: no se suelta mientras lo use otra importación (por ejemplo, la misma
    clase importada de nuevo después de una falla), una copia de adjuntos o la pantalla Importar.
- **Al terminar la copia:** las fotos quedan como eventos `foto` **sin minuto** (el server las ubica por
  la hora de su EXIF), el celu mide la duración con `MediaMetadataRetriever` (si no puede, va `null` y la
  mide el server) y la clase pasa a `terminada`. Desde ahí sube como cualquier otra: "Subiendo 23 %",
  después procesando, lista, error o "Elegí la materia".
- **La subida de una importada va en primer plano** con "Subiendo <materia> · 45 %":
  - Si la copia iba en primer plano, la subida empieza ahí mismo, en la misma notificación (como la
    subida final de una grabación). Así no hace falta pedir el primer plano otra vez, desde segundo plano.
  - Si no (o si queda algo para después), la sube WorkManager, que pasa a primer plano cuando Android lo
    deja: con la app a la vista o con Classmer sin restricciones de batería. Si no, va como un trabajo
    común (de a 9 minutos y frenada con la pantalla bloqueada, en Doze) hasta que se abra la app.
- **Al server** le llegan `importada`, `audio_nombre` (el nombre original, para la extensión) y
  `audio_bytes` al crear la sesión. El audio va en tramos de 4 MiB (el de una grabada, de 1 MiB).
- **El material** entra a la cola de adjuntos de siempre: sube cuando la clase ya está en el server.
- **El número** de la materia se corre uno, como después de grabar.
- **Nada de la grabación la toca:** ni el cierre de grabaciones cortadas, ni el corte automático, ni el
  reloj, ni el servicio de grabación, que sube sólo las clases grabadas (mientras graba y al terminar).
- **Las importadas suben aparte:** en cada pasada van después de las grabadas y con su propio candado.
  Mientras sube una importada de cientos de MB, la clase que se está grabando sube igual, cada ~30 s y al
  terminar.

Fotos: se eligen con el **selector de fotos de Android** (Photo Picker), que devuelve el archivo
original con su EXIF; Android sólo le borra la ubicación (GPS), que no se usa. La fecha
(`DateTimeOriginal`) queda, y es lo que usa el server. Por eso no hace falta el permiso
`ACCESS_MEDIA_LOCATION` ni `setRequireOriginal` (que sólo devuelven la ubicación). Las fotos que pasaron
por WhatsApp llegan sin EXIF: quedan "sin hora".

Límites y errores:

- **Audio:** hasta 2 GB (más, se rechaza al elegirlo y, si el selector no informó el tamaño, al
  copiarlo; el server también responde 413, que se muestra como "El server la rechazó"). **Fotos:** 50 MB
  cada una (una más grande, o que no se puede leer, queda afuera y la clase sigue). **Material:** 100 MB
  cada uno, como los adjuntos.
- **Espacio:** antes de copiar se mira el espacio libre (`StatFs`): tienen que quedar 300 MB, los que
  pide la grabación.
- **Si el audio ya no se puede leer** (se borró, se movió o se perdió el permiso) o no hay lugar, la
  clase muestra "No se pudo importar" con el motivo, **Reintentar** y **Descartar**. Descartar borra del
  celu sólo las copias que hizo Classmer (y sólo si la clase nunca llegó al server); el archivo original
  no se toca.
- **Server viejo:** ignora los campos nuevos, guarda el audio como `audio.ogg` (ffmpeg lo lee igual por
  el contenido) y deja las fotos sin minuto como "sin hora". La app sólo usa `importada` de las
  respuestas para mostrar la marca.

## Probar sin el server real

[`tools/servidor_falso.py`](tools/servidor_falso.py) implementa el contrato v1 con la biblioteca
estándar de Python: guarda lo que llega, loguea cada request, simula el pipeline y genera un PDF de
juguete. También inyecta fallas.

```bash
python android/tools/servidor_falso.py --puerto 8765 --token classmer-dev
```

```bash
python android/tools/probar_servidor_falso.py
```

En la app: URL `http://<IP de la PC>:8765`, token `classmer-dev`. Con `--fallar-prob`,
`--cortar-prob` y `--latencia-ms` se simula una red mala.

Lo de v0.2:

- **Materias:** salen de `<datos>/materias.json` (o de `--materias`), que se relee solo al editarlo.
  Con `"activa": false` una materia deja de aparecer en `GET /api/materias`, y
  `numero_clase_inicial` fija la numeración. El lápiz de la app (`PATCH`) escribe ese archivo.
- **Sin cambiarle el token al celu:** con `--cualquier-token` el falso acepta cualquier token,
  así alcanza con cambiar la URL en Ajustes y después volver a la del server real.
- **Clases en Auto:** pasan a `esperando_usuario` y se resuelven desde la app con "Elegir
  materia". Con `--rechazar-materia 409` o `422`, ese pedido falla siempre, para ver los mensajes.

Adjuntos:

- El falso guarda los adjuntos en `<datos>/sesiones/<id>/adjuntos/` y los marca `sin_usar` si llegan
  con la clase lista; "Regenerar" la vuelve a procesar desde el apunte y el PDF de juguete lista los
  adjuntos que usó.
- Como el server real, un `POST /adjuntos` sin la parte `archivo` (la consulta que hace la app antes
  de mandarlo) da 200 si ese id ya está en la clase y 422 "falta el archivo" si no.
- `--sin-adjuntos` simula un server viejo (404 `{"detail": "Not Found"}`): la app tiene que mostrar
  "Server sin soporte" y conservarlos. Al sacar el flag y reiniciar, se suben a la próxima pregunta
  (a los 10 minutos, o enseguida con "Reintentar" en la hoja).
- `--max-adjunto-mb 1` baja el tope para ver el 413 con un PDF chico (el celu corta en 100 MB).
- Una clase en error: agregar `"error_simulado": "<motivo>"` a su `sesion.json` y reiniciar el falso
  (acepta adjuntos nuevos, que quedan sin usar; "Regenerar" la saca del error).

Clases importadas:

- El falso acepta `importada`, `audio_nombre` y `audio_bytes` al crear la sesión, las fotos sin
  `offset_ms` (quedan "sin hora" y van al final del PDF de juguete) y `/finalizar` con `duracion_ms`
  null. `GET /api/sesiones` trae `importada`.
- `--max-audio-importada-mb 5` baja el tope de 2 GiB para ver el 413 con un audio chico (al crear la
  sesión, o en el PUT que lo pasa si el selector no informó el tamaño).

Ambos y la revisión del transcript (§15):

- El falso guarda la pista del celu (`?pista=celu`) en `<datos>/sesiones/<id>/audio.celu.ogg` y el
  bloque `celu` de `/finalizar` en `sesion.json`. Sus 409 traen `"pista": "celu"`. El PDF de juguete
  muestra la pista y el desfase.
- `--sin-pista` simula un server de antes de Ambos: ignora `pista` (el PUT se pegaría al audio
  principal, y el log lo marca `pista=celu IGNORADA`) y el bloque `celu`. La app no tiene que mandarle
  la pista: en el log sólo aparece el `GET` con que pregunta.
- `--dudas 3` hace que cada clase que se finalice espere la revisión: `esperando_usuario` con
  `"espera": "revision"` y `"dudas": 3`. La app la muestra como "Revisá el transcript". Se resuelve con
  `--revision-seg 60` (sola, al minuto) o con un `POST /api/sesiones/<id>/revision`
  `{"accion": "listo"}`, que hace las veces del panel.

### Probar el corte automático sin esperar 3 h 30

Con la app instalada en modo debug se puede acortar el corte para UNA grabación. Se pone un entero
en las preferencias, con la app cerrada:

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" shell am force-stop ar.classmer
```

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" shell "run-as ar.classmer sed -i 's#</map>#<int name=\"prueba_corte_seg\" value=\"60\" /></map>#' shared_prefs/ajustes.xml"
```

- La pregunta aparece a los 60 s, se espera la respuesta 60 s y "Seguir" vuelve a preguntar a los
  60 s.
- El valor se borra al empezar esa grabación, así que nunca queda puesto para una clase de verdad.

## Apuntar al server real

Cambiar la URL y el token en Ajustes y tocar Guardar. Si el server real usa otro host que no está
en `network_security_config.xml`, agregarlo y reinstalar.

## Rescatar una clase a mano

Las clases están en el almacenamiento propio de la app. Si la app no puede subirlas, se copian por
USB y se cargan con `POST /api/importar` (plan B):

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" pull /sdcard/Android/data/ar.classmer/files/sesiones
```

Los adjuntos están al lado, en `files/adjuntos/<clase>/`.

## Grabar con el reloj (Galaxy Watch)

El reloj queda cerca del profe (en su muñeca o en el escritorio) y graba el audio. El celu sigue
en el banco para las fotos, las notas y las marcas. En una prueba, el reloj en la muñeca del profe
tuvo la voz 16 dB más arriba del ruido que el celu y un 13 % más de palabras
transcriptas.

En Inicio, **Graba: 📱 Teléfono / ⌚ Reloj / ⌚📱 Ambos** elige quién graba y recuerda la última
elección. Con el reloj (o los dos), debajo se ve si está conectado, si tiene Classmer y su batería
(con aviso por debajo del 30 %). Si el reloj no está o no responde, aparece **Grabar con el
teléfono** (con Ambos, **Grabar sólo con el teléfono**). Ambos está en "Grabar con los dos".

### Cómo se usa

1. En el celu, elegí ⌚ Reloj y tocá **Grabar clase**.
2. El reloj vibra y muestra **"Classmer: tocá para grabar"**: tocalo (en Wear OS el primer toque
   abre el aviso) y después **● Grabar**. Android sólo deja prender el micrófono con un toque en el
   reloj. El pedido vence a los 20 minutos.
   - **El reloj tiene que estar desbloqueado.** Fuera de la muñeca, el Galaxy Watch se bloquea con el
     patrón o el PIN y no deja ver Classmer. Lo más cómodo es tocar Grabar con el reloj puesto y
     recién después dárselo al profe. Una vez que empezó, sigue grabando aunque se bloquee.
   - Con No molestar o el Modo Sueño, el aviso no aparece encima: está en la lista de
     notificaciones (deslizar hacia la derecha desde la esfera). También se puede abrir Classmer
     en el reloj, que muestra "El celu pidió grabar".
3. Mientras tanto, en el celu, sacá fotos y poné notas y marcas como siempre. Sus minutos se
   cuentan desde el inicio del audio del reloj. La pantalla de grabación muestra si el audio va
   llegando ("audio al día", "faltan 1,2 MB por pasar", "sin contacto hace 3 min").
4. Para terminar, usá **Terminar clase** en el celu, el corte automático o **■ Terminar** en el
   reloj. Cualquiera de los tres le avisa al otro. Después, el celu espera hasta 15 minutos (en
   primer plano) a que llegue todo el audio, lo verifica y recién ahí lo cierra y lo sube.

También se puede empezar desde el reloj (Classmer → **● Grabar**). Cuando el celu se entera, arma
la clase con la materia del calendario y la sigue.

**Si se corta el Bluetooth**, el reloj sigue grabando y guarda el audio. El celu trae lo que falta
cuando vuelve la conexión. El reloj borra su copia recién cuando el celu la tiene verificada (misma
huella SHA-256) y el server ya la aceptó. Si el reloj no vuelve, la clase queda "Recibiendo del
reloj" y, a los 30 minutos sin contacto, la fila ofrece **Cerrar con lo recibido**. En ese caso la
copia entera queda en el reloj (ver "Rescatar el audio del reloj").

**Mientras el profe tiene el reloj**, las notificaciones de Classmer del celu no se copian al reloj.
Conviene poner el reloj en **No molestar**, así ni los avisos de otras apps ni sus vibraciones
(que se oyen en la grabación si el reloj está sobre el escritorio) molestan.

### Grabar con los dos (Ambos)

Con **⌚📱 Ambos**, el reloj graba como siempre y es el principal: las fotos, las notas y las marcas se
cuentan desde el inicio de su audio. El celu graba a la vez su propio audio, de respaldo y como
segunda versión para conciliar el transcript (ARCHITECTURE.md §15). Se usa igual que con el reloj:
**Grabar clase** en el celu y **● Grabar** en el reloj.

- **El celu empieza enseguida**, al tocar Grabar; el reloj, cuando se lo toca allá. Hasta que el reloj
  empieza, el cronómetro cuenta el audio del celu; después, el del reloj (vuelve a cero).
- **La pantalla de grabación** dice "📱 El celu también graba, de respaldo" debajo del estado del reloj.
  La notificación lleva ⌚📱.
- **Pausa:** con los dos grabando, se pausa el reloj y el celu lo sigue. Antes de que empiece el
  reloj, o si se cortó, se pausa sólo el celu.
- **Al terminar**, el celu cierra su audio y la clase espera el del reloj, como siempre. Mientras
  tanto, su audio ya se sube.

Si el reloj falla, la clase no se pierde:

| Qué pasa | Qué hace la app |
|---|---|
| El reloj nunca empieza (no está, no responde, nadie toca Grabar) | **📱 Seguir sólo con el teléfono** pasa la clase al celu sin cortar nada. Si se termina sin tocarlo, queda igual: una clase del teléfono con el audio del celu. |
| La grabación del reloj se corta (se cayó la app del reloj) | La clase sigue con el celu ("El reloj se cortó: la clase sigue con el audio del celu"). Lo que grabó el reloj hasta el corte llega igual. Si se cortó durante una pausa, el celu sigue en pausa y avisa que hay que tocar **Reanudar** para seguir con él. Si del reloj no llegó nada con sonido, la clase queda como del teléfono, y el reloj borra su copia cuando la clase está en el server (el celu la conserva). |
| El reloj se queda sin batería o se va lejos | El celu sigue grabando. Al terminar, la clase queda "Recibiendo del reloj" y a los 5 minutos sin contacto (30 con el reloj solo) ofrece **Cerrar con lo recibido**: sube lo que llegó del reloj y el audio entero del celu. Si del reloj no llegó nada que sirva, queda como del teléfono. |
| Se toca Terminar o se para desde el reloj | Termina la clase, como siempre. |
| Falla el micrófono del celu | Sigue sólo el reloj ("El celu dejó de grabar su audio"). |
| La app del celu se cae | Si el reloj graba, sigue. La pista del celu vale hasta el corte: al volver a seguir la clase no se graba otra, porque una clase lleva una sola (Inicio lo dice). Si el reloj todavía no había empezado, el celu era lo único que grababa: avisa que la grabación se cortó y la fila dice "Se cortó", con **Cerrar con lo recibido**, que la deja como del teléfono. Si el reloj aparece grabando, se la sigue sola. |

**Archivos.** La pista está en `sesiones/<id>/audio.celu.ogg` y `celu.json` (cuándo empezó, el
desfase, la duración y si se cortó). Como todo, no se borra nunca. En `sesion.json`, `pista_celu`
dice que la clase es de Ambos. Una que quedó del teléfono tiene `archivo_audio: "audio.celu.ogg"`.

**El desfase** (`desfase_ms`) es el momento del audio del reloj en que empieza el del celu: con el celu
empezando antes, es negativo.
- Cada 5 s con los dos grabando, el celu resta lo que grabó cada uno en ese instante, con el desfase
  entre los relojes que ya mide para las marcas (±23 ms en el Watch 8). Se queda con la medición de
  menos error.
- Después de la primera pausa queda fijo: las dos pistas se pausan juntas, pero no en el mismo
  milisegundo. `celu.json` anota cuánto se corrió (`corrimiento_max_ms`).
- Si nunca grabaron los dos a la vez con el celu siguiéndolos (la app se cayó antes de que empezara el
  reloj), sale de la hora de inicio de los dos, con alrededor de un segundo de error.

**Grabar con Ambos cuando el reloj ya está grabando** (una clase empezada en el reloj) la sigue en el
celu y le suma la pista del celu, con la app a la vista. Si el celu ya la seguía sin su pista, se la
suma.

**Los minutos de lo que se toma en el celu.** Antes de que empiece el reloj, o después de que se
corte, se cuentan con la pista del celu: el evento lo anota (`"pista": "celu"` en `eventos.jsonl`) y
la subida lo pasa al audio del reloj sumando el desfase. Si la clase quedó del teléfono, al revés. Al
server siempre le llegan en el audio principal, como hoy.
- **Adjuntos** (📎 grabando): igual. El adjunto guarda la pista de su minuto (`"pista": "celu"` en su
  `.json`) y la subida lo pasa al audio principal. Si todavía no se sabe el desfase, espera.

**Subida.** La pista va por `GET` y `PUT /api/sesiones/<id>/audio?pista=celu`, con el mismo esquema
reanudable, y `/finalizar` suma el bloque `celu` (`sha256`, `bytes_totales`, `duracion_ms`,
`desfase_ms`). Los 409 con `"pista": "celu"` siguen desde lo que tiene el server de esa pista.
- **Con un server viejo**, que ignora `pista`, un `PUT` de la pista se pegaría al audio principal y lo
  arruinaría. Por eso, antes de subirla, la app pregunta con el `GET`: un server nuevo contesta lo
  suyo y uno viejo, lo del principal. Recién cuando el principal ya tiene bytes en el server y los
  dos números difieren (o si el server repite `"pista": "celu"`), sube la pista.
- El principal se pregunta en el momento, no se usa lo anotado en `subida.json`: si se perdió la
  respuesta de un `hash_distinto`, el server pudo haberlo vuelto a 0. Lo confirmado vale sólo para
  ese server (`celu_soportada_en`): con otra URL en Ajustes, se pregunta de nuevo.
- Si al finalizar el server sigue sin separarlas, la clase se finaliza sin la pista, que queda en el
  celu (`celu_sin_subir` en `subida.json`) y la fila lo avisa. Se le puede agregar desde el panel
  cuando el server lo permita.
- Una falla de la pista nunca traba la clase. Si el server la rechaza con algo que no se arregla
  reintentando (un 4xx, o un 409 de la pista que no cierra después de 3 intentos), se anota en
  `celu_rechazada` y la clase se finaliza sin ella. Sin red, se reintenta como siempre.
- Si el server vuelve a crear la clase (otro server, o se borraron sus datos), pregunta de nuevo.

**Probarlo sin el reloj ni el server real:** el servidor falso acepta la pista
(`python android/tools/servidor_falso.py --puerto 8765`). Con `--sin-pista` hace de server viejo, y en
su log no tiene que aparecer ningún `PUT` con `pista=celu IGNORADA`.

### Compilar e instalar en el reloj

```bash
cd android
./gradlew :wear:assembleDebug      # APK en wear/build/outputs/apk/debug/wear-debug.apk
./gradlew testDebugUnitTest :watchlink:test
```

Las dos apps tienen que estar **firmadas con la misma clave**: el Data Layer sólo conecta apps con
el mismo paquete y la misma firma. Compilarlas en la misma PC (mismo `~/.android/debug.keystore`).

Para depurar por Wi-Fi, la PC y el reloj tienen que estar en el mismo Wi-Fi:

1. En el reloj: **Ajustes → Acerca del reloj → Software**, tocar 5 veces **Versión de software**.
2. En **Opciones de desarrollador**, activar:
   - **Depuración ADB**;
   - **Desactivar Wi-Fi automático**, para que el reloj no apague el Wi-Fi cuando está con el celu;
   - **Depuración inalámbrica**.
3. En **Depuración inalámbrica → Vincular dispositivo nuevo**, el reloj muestra un código y una
   IP:puerto de vinculación (el Galaxy Watch no anuncia ese puerto por la red: hay que leerlo en la
   pantalla, que tiene que seguir abierta). Desde la PC, escribiendo el código cuando lo pide:
   ```bash
   "$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" pair IP:PUERTO_DE_VINCULACION
   ```
4. Conectar con la IP:puerto que figura arriba en **Depuración inalámbrica**, que es otro puerto, e
   instalar:
   ```bash
   "$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" connect IP:PUERTO
   ```
   ```bash
   "$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" -s IP:PUERTO install -r android/wear/build/outputs/apk/debug/wear-debug.apk
   ```
5. Abrir Classmer en el reloj una vez y permitir el micrófono y las notificaciones. Sin el permiso
   de notificaciones no aparece el "tocá para grabar".

El celu necesita la versión con el reloj (`adb install -r` de `app-debug.apk`, como siempre).
Conviene que Classmer esté **sin restricciones de batería** y en **"Apps que nunca se suspenden"**
(Ajustes de Classmer): así el celu puede seguir una clase que empezó en el reloj aunque la app esté
cerrada. Si Android no lo deja, aparece un aviso "El reloj está grabando una clase" y con tocarlo
se engancha.

### Cómo funciona

| Pieza | Dónde |
|---|---|
| Protocolo del Data Layer, tramas con offset y CRC, desfase entre relojes (tests en la JVM) | `watchlink/` |
| App del reloj: grabación en primer plano (`microphone`) con Ongoing Activity, Ogg/Opus mono 48 kHz | `wear/` |
| Órdenes del celu y envío del audio por canal | `wear/.../ServicioDatos.kt` |
| Lo que hace el servicio de grabación del celu cuando graba el reloj | `app/.../watch/SesionWatch.kt` |
| Ambos: `celu.json`, el desfase de la pista, los minutos y la clase sin el reloj | `app/.../watch/PistaCelu.kt` |
| Traer el audio, verificar la huella, adoptar las clases empezadas en el reloj, pedir que borre | `app/.../watch/SincronizacionWatch.kt` |

- **Arranque:** el celu manda `/classmer/iniciar` (MessageClient). El reloj guarda el pedido y
  muestra el aviso, y el toque abre Classmer, que empieza a grabar con el id de la sesión del celu.
  Si el reloj no contesta, la pantalla de grabación ofrece pasar a grabar con el teléfono (la misma
  sesión, con lo que ya tenga).
- **Desfase:** al empezar (y cada vez que el error podría pasar de 0,5 s), el celu hace 5 idas y
  vueltas de ping y se queda con la más rápida: `reloj = celu + desfase`, con un error menor que la
  mitad de esa ida y vuelta. Con eso arma un reloj espejo del audio del Watch, sin las pausas. Las
  fotos, notas y marcas usan ese reloj, así que caen en el minuto del audio del reloj.
- **Audio:** el reloj graba un solo Ogg/Opus que sólo crece. El celu abre un canal
  `/classmer/audio/<id>/<desde>` cada vez que el reloj tiene más bytes (lo consulta cada 30 s y el
  reloj avisa cada minuto). Lo que llega se agrega al mismo `audio.ogg` que sube el `Subidor`, así
  que la subida al server no cambia.
  - Cada trama trae su offset y un CRC. Lo repetido se descarta (después de comprobar que coincide)
    y un canal cortado se retoma desde lo que tiene el celu.
  - Si el reloj deja de mandar 45 s, el celu cierra el canal y vuelve a pedir.
- **Cierre:** con el reloj, al terminar la sesión queda `recibiendo` (no `terminada`) hasta que el
  celu tiene el mismo largo y la misma SHA-256 que el reloj. Recién ahí pasa a `terminada` y el
  `/finalizar` va al server como siempre. Si la huella no coincide, se baja entera aparte y se
  reemplaza.
- **Server:** no cambia. `dispositivo` dice quién grabó (`galaxy-watch8-classic`).
- **`sesiones/<id>/watch.json`:** lo último que informó el reloj, el desfase medido, si ya está
  verificada y si el reloj ya la borró.

### Fuente, bitrate y lo que se midió en un Galaxy Watch 8

- **Fuente: MIC.** Se pasó tres veces el mismo fragmento de una clase por un parlante a ~30 cm del
  reloj y se midió con ffmpeg el nivel de voz (p95 de ventanas de 100 ms) y el piso de ruido (p10):

  | Fuente | Voz sobre ruido | Pico | Piso antes → después de la voz |
  |---|---|---|---|
  | MIC | 45,6 dB | −2,1 dBFS | −55,8 → −59,2 dB |
  | CAMCORDER | 44,2 dB | −1,5 dBFS | −56,8 → −57,5 dB |
  | VOICE_RECOGNITION | 32,1 dB | −9,2 dBFS | −53,9 → −53,0 dB |
  | VOICE_COMMUNICATION | 32,8 dB | −24,8 dBFS | −71,0 → −68,8 dB |

  UNPROCESSED no está soportada en el Watch 8. MIC le saca 13 dB a VOICE_RECOGNITION y no "bombea"
  en los silencios (el piso baja después de la voz: es supresión de ruido, no control de ganancia).
  Con el fragmento 6 dB más fuerte sube sólo 2 dB (limita) y toca 0 dBFS en 1,3 ms de 34 s. Es el
  mismo tipo de procesamiento que usó la Grabadora de Samsung en una prueba con el celu.
- **Bitrate: 32 kbps** (como el celu). Medido en el reloj: unos 40 kbps con el encabezado Ogg, **unos
  18 MB por hora**. La distorsión de Opus en la banda de 16 kHz que usa el ASR es de 20,8 dB a 32 kbps
  (23,4 dB a 48 kbps), y en estudios de ASR de campo lejano el error deja de bajar en 32 kbps.
- **Prueba punta a punta** (13,5 min con el reloj al lado de un parlante, materia `prueba`, server de
  prueba): con 2 min de Bluetooth, Wi-Fi y datos del celu cortados en el medio, el audio llegó entero
  (misma SHA-256 en el reloj, el celu y el server) y la clase quedó finalizada 2 s después de tocar
  Terminar; el reloj borró su copia recién después.
  - Desfase entre relojes medido con un error de ±23 ms (ida y vuelta de 45 ms).
  - Pitidos en el parlante y marcas ⭐ al mismo tiempo: el pitido cae entre 0,12 y 0,19 s después de
    su marca en el audio del reloj, también durante el corte (es la demora del parlante Bluetooth y
    de adb, no del desfase). Una foto sacada con un pitido quedó a 0,10 s.
- **Batería:** **≈ 18 % por hora** en esa prueba (de 98 a 94 % en 13 min, así que es una medida
  gruesa), con el Wi-Fi del reloj prendido por la depuración inalámbrica y adb despertándolo; en una
  clase normal debería ser menos. La Grabadora de Samsung gastó ≈ 12 % por hora. Cada clase anota la
  batería del reloj al empezar y al final en `watch.json` (`bateria_inicio`, `bateria_ultima`).

### Elegir fuente y bitrate de nuevo

La app del reloj en debug trae una pantalla de prueba que graba un clip corto con la fuente y el
bitrate que se le pasen, sin tocar las clases:

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" -s IP:PUERTO shell am start -n ar.classmer/ar.classmer.wear.PruebaAudioActivity --es fuente VOICE_RECOGNITION --ei bitrate 32000 --ei segundos 30 --es nombre vr32
```

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" -s IP:PUERTO exec-out run-as ar.classmer cat files/pruebas/vr32.ogg > vr32.ogg
```

La fuente y el bitrate de las clases (`AjustesWatch`) se pueden cambiar sin pantalla, igual que
`prueba_corte_seg` en el celu (con la app del reloj cerrada):

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" -s IP:PUERTO shell "run-as ar.classmer sed -i 's#</map>#<int name=\"fuente_audio\" value=\"1\" /></map>#' shared_prefs/ajustes.xml"
```

(1 = MIC, 6 = VOICE_RECOGNITION, 9 = UNPROCESSED; `bitrate` en bps.)

### Rescatar el audio del reloj

Las grabaciones están en el almacenamiento interno del reloj (`files/grabaciones/<id>/audio.ogg`),
hasta que el celu confirma que el server las tiene:

```bash
"$LOCALAPPDATA/Android/Sdk/platform-tools/adb.exe" -s IP:PUERTO exec-out run-as ar.classmer cat files/grabaciones/<id>/audio.ogg > reloj.ogg
```
