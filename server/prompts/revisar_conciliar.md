<!--
Prompt del paso 1 de la etapa revisar_transcript (§15): conciliar los dos transcripts de una clase
(el del audio principal, normalmente el reloj en la muñeca del profe, y el del celu del usuario). Lo usa
classmer.ia.revisor por tramos de la clase, con salida JSON estructurada (Claude por la API o por el
plan Max). Sólo concilia: la limpieza es el paso siguiente.

Variables:
  {materia}    nombre de la materia
  {profesor}   nombre del profe ("el profe" si no se sabe)
  {perfil}     extracto del perfil de la materia (términos, notación)
  {lineas}     las líneas del tramo (ver "Formato")
  {pedidas}    los ids que hay que responder
Este comentario no se manda al modelo.
-->
Estás conciliando el transcript de una clase universitaria de {materia} (profe: {profesor}) que se
grabó con dos equipos a la vez. Un reconocedor de voz automático transcribió cada grabación por
separado. Tu trabajo es decidir, línea por línea, qué se dijo de verdad.

## Las dos grabaciones

- **reloj**: el audio principal, grabado cerca del profe (en su muñeca o su escritorio). Oye mucho
  mejor al profe. Sus líneas y sus minutos son la columna del transcript.
- **celu**: el celular del usuario, en su banco. Oye peor al profe (se come palabras sueltas, sobre todo
  nombres propios y términos técnicos) pero oye mejor a los alumnos que están cerca y a veces entiende
  bien una frase que el reloj entendió mal.

Las dos son la misma clase y ya están sincronizadas: para cada línea del reloj tenés el tramo del celu
que le corresponde. El corte del tramo es automático: puede traer una o dos palabras del final de la
línea anterior o del comienzo de la siguiente. No las pases a esta línea.

## Qué hacer

Para cada línea pedida, escribí lo que más probablemente se dijo, eligiendo entre las dos versiones
fragmento por fragmento:
- Si una dice algo que no tiene sentido en contexto y la otra sí, quedate con la que tiene sentido
  (p. ej., reloj "lo corregiremos aquí" / celu "lo corregiremos a tiempo" en una frase que contrasta
  con "lo corregiremos tarde").
- Si el celu omite palabras que el reloj sí tiene, no las saques: la ausencia en el celu no es
  evidencia (el celu se come palabras).
- Si las dos versiones dicen lo mismo con otras palabras sueltas, preferí el reloj para lo que dice el
  profe y el celu para lo que dicen los alumnos.
- Usá el perfil de la materia y el contexto para reconocer términos y nombres, pero **no corrijas al
  profe**: el transcript dice lo que el profe dijo, no lo que debería haber dicho. Si el profe se
  equivocó (dijo "sube" donde era "baja", un número mal, un nombre mal), eso queda. Sólo se corrige lo
  que el reconocedor entendió mal.
- No resumas, no reordenes, no completes ideas y no agregues nada que no esté en ninguna de las dos
  versiones. Conservá las muletillas y repeticiones tal como estén en la versión que elijas: la
  limpieza es otro paso.

**Duda**: si las dos versiones difieren en algo que cambia el sentido (una negación, "puede"/"no puede",
un número, un nombre, quién hizo qué, "sube"/"baja") y ni el contexto ni la materia permiten saber cuál
se dijo, no decidas: marcá `duda: true`, poné en `texto` la que te parezca más probable y explicá en
`motivo` qué está en juego. No marques dudas por diferencias que no cambian el sentido (una muletilla,
un artículo, el orden de dos palabras).

**Líneas que sólo están en el celu** (`C…`): son cosas que el reloj no registró, como una pregunta de
un alumno o una frase que se perdió. Incluilas (`incluir: true`) si son habla real de la clase que
aporta algo: una pregunta, una respuesta, un comentario del profe que el reloj perdió. No las incluyas
si son ruido, charla de fondo entre alumnos que no es parte de la clase, o si repiten algo que ya está
en una línea del reloj. Decí quién habla (`profe` o `alumno`).

`eleccion`: `reloj`, `celu` o `mezcla` (tomaste partes de las dos). `confianza`: `alta`, `media` o
`baja`. `motivo`: una oración corta y concreta.

## Formato de las líneas

```
L0123 [00:23:08] PROFE
  reloj: lo que transcribió el reloj
  celu:  lo que transcribió el celu en ese tramo   (o "(el celu no tiene esta línea)")
L0124 [00:23:12] PROFE: texto   ← igual en las dos versiones: es contexto, no se responde
C0005 [00:23:10] sólo en el celu, ALUMNO: texto
```

El hablante (PROFE, ALUMNO o DESCONOCIDO) es el que adivinó el reconocedor y puede estar mal.

## Perfil de la materia (para términos y nombres)

{perfil}

## Líneas

{lineas}

## Qué responder

Respondé en `lineas` cada una de estas líneas, con el mismo id: {pedidas}. En `solo_celu`, cada línea
`C…` de este tramo.
