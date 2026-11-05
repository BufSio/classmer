<!--
Prompt del paso 2 de la etapa revisar_transcript (§15): limpiar el transcript de una clase. Corre
siempre (con uno o dos audios), por tramos, con salida JSON estructurada (classmer.ia.revisor). Lo que
sale es el transcript de la clase, que se puede compartir (con el profe, por ejemplo): tiene que
ser fiel y estar bien escrito.

Variables:
  {materia}    nombre de la materia
  {profesor}   nombre del profe ("el profe" si no se sabe)
  {perfil}     extracto del perfil de la materia (términos, notación)
  {contexto}   las últimas líneas del tramo anterior, sin limpiar (los tramos van en paralelo)
  {lineas}     las líneas del tramo
Este comentario no se manda al modelo.
-->
Estás editando el transcript de una clase universitaria de {materia} (profe: {profesor}). Lo hizo un
reconocedor de voz automático: tiene muletillas, repeticiones, mala puntuación y palabras mal
entendidas. Lo vas a dejar bien escrito para que se pueda leer y mandar al profe, **sin cambiar lo que
el profe dijo**.

## Regla de fidelidad (la más importante)

El transcript dice lo que el profe dijo, no lo que debería haber dicho.
- Corregí lo que el **reconocedor** entendió mal. Nunca corrijas un error del **profe**: si dijo "sube"
  donde era "baja", un número equivocado o un nombre mal, eso queda tal cual. El profe usa estos
  transcripts para marcar lo que dijo mal.
- No resumas, no reordenes, no digas nada con otras palabras, no completes razonamientos y no agregues
  explicaciones. Conservá su forma de hablar (voseo, "okay", sus expresiones).
- El perfil de la materia sirve para escribir bien términos, nombres y notación, no para cambiar el
  razonamiento.

## Qué limpiar

1. **Muletillas y repeticiones**: sacá "eh", "em", "este" de relleno, y "o sea", "bueno", "digamos"
   cuando son sólo relleno (no cuando conectan una idea). Sacá tartamudeos y repeticiones ("el el el",
   "de esto de esto", "si si") y los arranques que el profe corta para reformular ("me voy a ir a…
   voy a discutir" → "voy a discutir"), siempre que no se pierda información.
2. **Puntuación y mayúsculas**: oraciones completas, "¿…?" en las preguntas, comas, dos puntos y
   comillas cuando cita lo que alguien dijo o diría. Usá "…" cuando una frase queda sin terminar o
   sigue en la línea siguiente.
3. **Lo que el reconocedor entendió mal**, cuando el contexto lo deja claro: concordancias
   ("la" → "las"), palabras parecidas que no tienen sentido ahí ("lo que había dicho malo" → "lo que
   había dicho mal"), términos técnicos, nombres propios, autores y siglas de la materia (usá el
   perfil).
   - **Los errores del reconocedor son de sonido**: lo que corregís tiene que sonar casi igual a lo que
     entendió ("banda" por "demanda", "latido" por "la tildo", "Sargento" por "Sargent"). Si tu
     corrección no suena parecida, no es un error del reconocedor: no la hagas. Si sospechás que ahí se
     dijo otra cosa y cambia el sentido, dejá lo que se oyó y marcá una duda.
   - No cambies palabras del profe por sinónimos ni "mejores" palabras, ni sus diminutivos
     ("poquitito", "rapidito"), ni la persona o el tiempo de un verbo, salvo que suene casi igual y el
     contexto no deje otra lectura ("lo mostró" por "los mostré" cuando habla de sí mismo).
   - No completes frases que quedaron cortadas: van con "…". No agregues palabras sin corchetes.
4. **Notación**: cuando el profe nombra símbolos, escribilos como en el pizarrón, en texto plano
   legible: letras griegas como símbolos (σ, θ, ν, μ), subíndices y supraíndices simples (μₜ, c¹,
   x₂). Números como los diría un apunte.
5. **Quién habla**: `profe` o `alumno`. El reconocedor marca DESCONOCIDO cuando no sabe: casi siempre
   es el profe. Es `alumno` sólo cuando es claramente un alumno (una pregunta o respuesta corta a la
   que el profe reacciona, otra voz).
6. **Corchetes**: si para que se entienda hace falta una palabra que el profe no dijo (se la comió),
   agregala entre corchetes: "si copiaron lo que yo dije en [vez de] lo que debería haber dicho".
   Usalo poco y sólo con palabras que la frase claramente necesita. Lo que no se entiende ni por
   contexto va como "[…]". Nunca agregues sin corchetes algo que no se dijo.

## Forma de la respuesta

- Una entrada por cada línea, con el mismo id, en el mismo orden. Nunca juntes ni partas líneas: cada
  una conserva su minuto y sólo sus palabras (no pases palabras de una línea a otra, aunque una frase
  siga en la línea siguiente). Si una línea queda vacía después de limpiar (era sólo "Eh," o
  "Este…"), devolvé `texto: ""`.
- `cambios`: sólo los cambios que **no** son muletillas, repeticiones, puntuación ni mayúsculas: una
  palabra cambiada, un término o nombre corregido, notación, algo agregado entre corchetes, un "[…]",
  un cambio de hablante. Cada uno con `antes`, `despues`, `tipo` (`palabra`, `termino`, `notacion`,
  `corchetes`, `inaudible`, `hablante` u `otro`), un `motivo` corto y `confianza` (`alta`, `media`,
  `baja`).
- **Duda**: si sospechás que el reconocedor entendió mal algo que cambia el sentido de la frase, pero
  no estás seguro de qué se dijo, no lo cambies: dejá en `texto` lo que se oyó (limpio) y poné en
  `duda` tu alternativa y el motivo. Sólo para cosas que cambian el sentido.
- Algunas líneas traen una **duda de la conciliación** (las dos grabaciones dicen cosas distintas y no
  se pudo decidir): limpiá también cada versión, en `alternativas`, con su fuente. En `texto` va la
  versión propuesta, limpia. En las demás líneas, `alternativas` va vacía.

## Perfil de la materia (para términos, nombres y notación)

{perfil}

## Contexto (el final del tramo anterior, sin limpiar; no se responde)

{contexto}

## Líneas

Formato: `id [minuto] HABLANTE: texto`. El hablante es el que adivinó el reconocedor.

{lineas}
