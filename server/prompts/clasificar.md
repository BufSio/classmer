<!--
Prompt de la etapa clasificar (classmer.ia.clasificador): lo usan ClasificadorClaude (por defecto
Claude Sonnet 5.5, por la API o por el plan Max con el Agent SDK) y ClasificadorGemini (Gemini Flash).
Se usa sólo cuando la sesión quedó en "Auto (IA)" (sin materia). Respuesta en JSON con esquema: con
Claude, materia_id es una de las ids activas o null (enum del esquema).

Variables (classmer.ia.gemini.cargar_prompt reemplaza sólo estas; otras llaves quedan igual):
  {materias}    lista de materias: id, nombre y comienzo del perfil de cada una
  {ids}         ids válidos separados por coma
  {transcript}  extractos del transcript ([hh:mm:ss] texto), ≤ ~30000 caracteres
  {duracion}    duración total de la clase (hh:mm:ss)
Este comentario no se manda al modelo.
-->
Tenés que decidir a qué materia corresponde una clase universitaria grabada, a partir de
fragmentos de su transcripción. Las materias posibles son sólo las de la lista.

## Materias

{materias}

## Cómo decidir

- Fijate en los temas, la notación, los ejemplos, los nombres de profesores y la bibliografía que
  se mencionan, y comparalos con la descripción de cada materia.
- Si la clase no corresponde a ninguna o no hay información suficiente, respondé `materia_id: null`.
- `confianza` es un número entre 0 y 1: 0.9 o más sólo si la evidencia es clara (temas propios de
  esa materia); entre 0.5 y 0.8 si es probable pero hay dudas; menos de 0.5 si casi no hay pistas.
- `motivo`: una o dos oraciones en español con la evidencia concreta (temas o frases del transcript).

## Transcript (extractos, duración total {duracion})

{transcript}

## Respuesta

Sólo un objeto JSON: {"materia_id": uno de [{ids}] o null, "confianza": 0..1, "motivo": "..."}
