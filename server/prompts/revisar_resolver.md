<!--
Prompt del último paso de la etapa revisar_transcript (§15): las dudas que dejaron la conciliación y
la limpieza, con lo que oyó Gemini al volver a escuchar esos segundos en los dos audios. Lo que no se
resuelve acá queda para que el usuario lo revise en el panel. Un pedido por clase, salida JSON estructurada.

Variables:
  {materia}    nombre de la materia
  {profesor}   nombre del profe
  {dudas}      las dudas (ver "Formato")
Este comentario no se manda al modelo.
-->
Estás revisando las dudas del transcript de una clase de {materia} (profe: {profesor}). Para cada duda
tenés las versiones posibles (ya limpias), cuál se propuso, unas líneas de contexto y lo que oyó un
reconocedor de voz al **volver a escuchar** ese momento en cada grabación (el reloj, cerca del profe, y
el celu del usuario, en su banco). Esa nueva escucha es la evidencia más directa de lo que se dijo; cada
grabación oye distinto, así que pueden no coincidir.

## Qué hacer

Para cada duda, decidí si ahora se sabe qué se dijo:
- **Resuelta** (`resuelta: true`) si la nueva escucha (o la nueva escucha más el contexto) deja claro
  qué se dijo. Poné en `texto` la línea final, limpia, con el mismo estilo que las versiones (sin
  muletillas, bien puntuada). Si coincide con una versión, usá esa versión.
- **Sigue en duda** (`resuelta: false`) si la nueva escucha no aclara o contradice. Poné en `texto` la
  versión que te parezca más probable: es la propuesta que va a ver el usuario. Sin evidencia nueva, la
  más probable es lo que oyó el reconocedor (la versión `audio`, o la que eligió la conciliación).

`texto` es sólo **esta** línea: las de antes y después son contexto y no van adentro.

Regla de fidelidad: se corrige lo que el reconocedor entendió mal, nunca lo que el profe dijo. Si el
profe se equivocó, la línea dice lo que dijo. Si una versión "corrige" el razonamiento del profe y la
escucha no lo confirma, no es la buena. No agregues nada que no esté en ninguna versión ni en la
escucha (salvo una palabra entre corchetes si hace falta para entender, o "[…]").

`eleccion`: la fuente de la versión elegida (`reloj`, `celu`, `audio`, `propuesta`) o `otra` si la
escribiste a partir de la escucha. `motivo`: una oración concreta. `confianza`: `alta`, `media` o
`baja`.

## Formato de cada duda

```
D07 [00:58:26] (de la limpieza) motivo de la duda
  antes:  línea anterior
  versiones:
    reloj: …
    celu:  …
  propuesta: reloj
  escuchado en el reloj: …
  escuchado en el celu:  …
  después: línea siguiente
```

## Dudas

{dudas}
