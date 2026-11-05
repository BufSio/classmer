"""Etapa 3: transcribir un bloque (job con args {"n": k}).

Prueba el transcriptor principal hasta `transcribir.intentos_validacion` veces y, si la salida no
valida, los respaldos (el LLM de Gemini). Si nada valida se queda con el mejor resultado y marca
el bloque como 'dudoso'. Pasa los tiempos a absolutos, escribe transcript/bloque_NN.json y
reemplaza las filas de `segmentos` del bloque. El último bloque listo encola linea_de_tiempo.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer.db import transaccion
from classmer.ia import gemini
from classmer.ia.transcriptor import (
    TEXTO_SIN_TRANSCRIPCION,
    ContextoTranscripcion,
    ResultadoTranscripcion,
    SegmentoRel,
    Transcriptor,
    crear_transcriptores,
    puntaje,
    texto_final,
    validar,
)
from classmer.pipeline import Contexto, CuotaAgotada, ErrorEtapa, Resultado
from classmer.sesiones import PISTA_CELU
from classmer.util import escribir_json, leer_json

if TYPE_CHECKING:
    from classmer.cola import Job

LIMITE_PERFIL = 8000


def ruta_transcript(carpeta: Path, n: int) -> Path:
    return carpeta / "transcript" / f"bloque_{n:02d}.json"


def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if sid is None or "n" not in job.args:
        raise ErrorEtapa("transcribir necesita sesion_id y args.n", reintentable=False)
    n = int(job.args["n"])
    carpeta = ctx.sesion_dir(sid)
    with ctx.base.conexion() as conn:
        bloque = conn.execute("SELECT * FROM bloques WHERE sesion_id = ? AND n = ?", (sid, n)).fetchone()
        sesion = conn.execute("SELECT materia_id FROM sesiones WHERE id = ?", (sid,)).fetchone()
    if bloque is None:
        raise ErrorEtapa(f"no existe el bloque {n} de la sesión", reintentable=False)
    inicio_bloque = int(bloque["inicio_ms"])
    duracion = max(0, int(bloque["fin_ms"]) - inicio_bloque)
    audio = carpeta / bloque["ruta"]
    # Un bloque del celu (§15): sus tiempos son del audio del celu y no va a la tabla `segmentos` (el
    # transcript de la clase sale del principal; linea_de_tiempo y la revisión usan el del celu).
    celu = bloque["pista"] == PISTA_CELU
    contexto = armar_contexto(ctx, sid, n, inicio_bloque, int(bloque["fin_ms"]),
                              sesion["materia_id"] if sesion else None, celu=celu)

    ultimo = job.intentos >= job.max_intentos
    try:
        elegido, estado, avisos = transcribir_bloque(ctx, sid, n, _cadena(ctx), audio, contexto,
                                                     ultimo=ultimo)
    except ErrorEtapa as e:
        # En el último intento, un error pasajero no deja la sesión sin apunte: el bloque queda
        # 'dudoso' con el tramo marcado como inaudible y el pipeline sigue.
        if isinstance(e, CuotaAgotada) or not e.reintentable or not ultimo:
            raise
        elegido = ResultadoTranscripcion(segmentos=[], motor="ninguno", modelo="-")
        estado, avisos = "dudoso", [f"sin transcripción tras {job.intentos} intentos: {e}"]

    segmentos = absolutos(elegido.segmentos, inicio_bloque, duracion)
    if estado == "dudoso" and not segmentos:
        segmentos = [{"inicio_ms": inicio_bloque, "fin_ms": inicio_bloque + duracion,
                      "hablante": "desconocido", "texto": TEXTO_SIN_TRANSCRIPCION}]
    motor = f"{elegido.motor}:{elegido.modelo}"
    escribir_json(ruta_transcript(carpeta, n), {
        "n": n,
        "inicio_ms": inicio_bloque,
        "fin_ms": int(bloque["fin_ms"]),
        "motor": elegido.motor,
        "modelo": elegido.modelo,
        "estado": estado,
        "avisos": avisos,
        "segmentos": segmentos,
    })

    with ctx.base.conexion() as conn, transaccion(conn):
        conn.execute("DELETE FROM segmentos WHERE sesion_id = ? AND bloque = ?", (sid, n))
        if not celu:
            conn.executemany(
                "INSERT INTO segmentos (sesion_id, bloque, inicio_ms, fin_ms, hablante, texto) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                [(sid, n, s["inicio_ms"], s["fin_ms"], s["hablante"], s["texto"]) for s in segmentos],
            )
        conn.execute("UPDATE bloques SET estado = ?, motor = ? WHERE sesion_id = ? AND n = ?",
                     (estado, motor, sid, n))
        todos = _todos_listos(conn, sid)

    resumen = f"bloque {n}{' (celu)' if celu else ''}: {estado} con {motor}, {len(segmentos)} segmento(s)"
    if elegido.uso:
        resumen += " · tokens " + ", ".join(f"{k}={v}" for k, v in elegido.uso.items())
    if avisos:
        resumen += " · avisos: " + "; ".join(avisos)
    ctx.log_sesion(sid, resumen)
    return Resultado(
        siguientes=[("linea_de_tiempo", {})] if todos else [],
        log=resumen,
        motor=motor,
        costo=elegido.costo,
    )


def _cadena(ctx: Contexto) -> list[Transcriptor]:
    """Principal y respaldos. Un servicio inyectado puede devolver uno solo o una lista."""
    fabrica = ctx.servicios.transcriptor
    if fabrica is None:
        return crear_transcriptores(ctx.cfg)
    t = fabrica(ctx.cfg)
    return list(t) if isinstance(t, (list, tuple)) else [t]


def _todos_listos(conn: sqlite3.Connection, sid: str) -> bool:
    fila = conn.execute(
        """
        SELECT COUNT(*) AS total,
               SUM(CASE WHEN estado IN ('transcripto', 'dudoso') THEN 1 ELSE 0 END) AS listos
        FROM bloques WHERE sesion_id = ?
        """,
        (sid,),
    ).fetchone()
    return bool(fila["total"]) and (fila["listos"] or 0) == fila["total"]


# --- intentos --------------------------------------------------------------------------------

def transcribir_bloque(
    ctx: Contexto, sid: str, n: int, cadena: list[Transcriptor], audio: Path,
    contexto: ContextoTranscripcion, *, ultimo: bool = True,
) -> tuple[ResultadoTranscripcion, str, list[str]]:
    """Corre la cadena de transcriptores. Devuelve (resultado, 'transcripto'|'dudoso', avisos).

    El principal se repite sólo si su salida no valida por algo que no sea un hueco (hasta
    intentos_validacion veces): un hueco suele ser pizarrón o práctica, y repetir no lo arregla.
    Si el principal no tiene cuota o está sobrecargado (CuotaAgotada: 429/503), se relanza en el
    acto: el job se pospone sin gastar intentos y vuelve a probar el principal. El respaldo no se
    usa por eso (el usuario quiere transcribir con el ASR, no pagar el LLM por un límite por minuto).
    Pero si la cuota se acaba al repetirlo (ya respondió y no validó), sigue con lo que respondió
    como si hubiera agotado sus intentos: posponer volvería a empezar por el intento 1 sin gastar
    intentos, y un bloque de ~15k tokens contra el límite por minuto lo repite para siempre (27/10).
    Otro error (5xx, timeout, 4xx) pasa directo al respaldo, así un job nunca encadena
    timeouts largos. Si nada valida, gana el resultado con menos problemas (a igualdad, el
    principal) y el bloque queda 'dudoso'; pero si el principal no respondió por algo pasajero y
    no es el `ultimo` intento del job, lanza un ErrorEtapa reintentable para volver a probarlo.
    Si ninguna llamada respondió, relanza el error más útil: CuotaAgotada si hubo alguna (no gasta
    intentos), si no un ErrorEtapa reintentable (la cola lo reintenta), si no el último error.
    """
    intentos_principal = max(1, ctx.cfg.transcribir.intentos_validacion)
    fallidos: list[tuple[ResultadoTranscripcion, list[str], int]] = []
    errores: list[ErrorEtapa] = []
    error_principal: ErrorEtapa | None = None
    historia: list[str] = []
    for k, t in enumerate(cadena):
        nombre = f"{t.nombre}:{getattr(t, 'modelo', '?')}"
        for intento in range(intentos_principal if k == 0 else 1):
            etiqueta = f"{nombre} (intento {intento + 1})"
            try:
                r = t.transcribir(audio, contexto)
            except Exception as e:  # noqa: BLE001 - ni un bug de parseo debe frenar al respaldo
                error = e if isinstance(e, ErrorEtapa) else ErrorEtapa(
                    f"{e.__class__.__name__}: {e}", reintentable=True)
                errores.append(error)
                if k == 0:
                    error_principal = error
                historia.append(f"{etiqueta}: {error}")
                ctx.log_sesion(sid, f"bloque {n}: {etiqueta} falló: {error}")
                if k == 0 and isinstance(error, CuotaAgotada):
                    if intento == 0:
                        ctx.log_sesion(sid, f"bloque {n}: sin cuota del principal; se pospone "
                                            f"{error.espera_s:.0f} s y se vuelve a probar el principal")
                        raise error
                    ctx.log_sesion(sid, f"bloque {n}: sin cuota para repetir el principal; "
                                        "sigue con lo que respondió")
                break
            problemas = validar(r, contexto)
            if not problemas:
                avisos = list(r.avisos)
                if historia or fallidos:
                    avisos.insert(0, f"se usó {nombre} después de: " + " | ".join(
                        historia + [f"{f.motor}:{f.modelo}: {'; '.join(p)}" for f, p, _ in fallidos]))
                return r, "transcripto", avisos
            fallidos.append((r, problemas, k))
            ctx.log_sesion(sid, f"bloque {n}: {etiqueta} no validó: {'; '.join(problemas)}")
            if all(p.startswith("hueco") for p in problemas):
                break

    principal_respondio = any(k == 0 for _, _, k in fallidos)
    no_vacios = [f for f in fallidos if not f[0].vacio]
    if no_vacios:
        if (not ultimo and not principal_respondio and error_principal is not None
                and error_principal.reintentable):
            # El principal (ASR) falló por algo pasajero y el respaldo no validó: mejor volver a
            # probar el principal en el próximo intento que quedarse con un 'dudoso' para siempre.
            _, problemas, _ = no_vacios[0]
            raise ErrorEtapa(
                f"bloque {n}: el principal falló ({error_principal}) y el respaldo no validó "
                f"({'; '.join(problemas)})"[:900], reintentable=True)

        def clave(f: tuple[ResultadoTranscripcion, list[str], int]) -> tuple[int, ...]:
            menos_problemas, cubierto, texto = puntaje(f[0], f[1])
            return (menos_problemas, -f[2], cubierto, texto)

        mejor, problemas, _ = max(no_vacios, key=clave)
        avisos = ["no pasó la validación: " + "; ".join(problemas), *mejor.avisos]
        if errores:
            avisos.append("errores: " + " | ".join(str(e) for e in errores))
        return mejor, "dudoso", avisos

    # Si el principal respondió (vacío) y después faltó cuota, posponer sin gastar intentos lo
    # repetiría para siempre: que el reintento cuente (el último deja el bloque 'dudoso').
    cuota = [e for e in errores if isinstance(e, CuotaAgotada)]
    if cuota and not principal_respondio:
        raise CuotaAgotada(str(cuota[0]), espera_s=min(e.espera_s for e in cuota))
    transitorios = [e for e in errores if e.reintentable]
    if transitorios:
        raise ErrorEtapa(" | ".join(str(e) for e in transitorios)[:900], reintentable=True)
    if fallidos:
        vacio = fallidos[-1][0]
        avisos = ["no se pudo transcribir: " + "; ".join(fallidos[-1][1]), *vacio.avisos]
        if errores:
            avisos.append("errores: " + " | ".join(str(e) for e in errores))
        return vacio, "dudoso", avisos
    if errores:
        raise ErrorEtapa(" | ".join(str(e) for e in errores)[:900], reintentable=False)
    raise ErrorEtapa("no hay transcriptores configurados", reintentable=False)


# --- contexto y tiempos ----------------------------------------------------------------------

def armar_contexto(ctx: Contexto, sid: str, n: int, inicio_ms: int, fin_ms: int,
                   materia_id: str | None, *, celu: bool = False) -> ContextoTranscripcion:
    cfg = ctx.cfg
    carpeta = ctx.sesion_dir(sid)
    materia = cfg.materia(materia_id)
    perfil_completo = gemini.leer_perfil(materia.ruta_perfil()) if materia else None
    duracion = max(0, fin_ms - inicio_ms)
    previo = None
    if n > 1:
        anterior = leer_json(ruta_transcript(carpeta, n - 1))
        if isinstance(anterior, dict):
            segs = [SegmentoRel(int(s.get("inicio_ms", 0)), int(s.get("fin_ms", 0)),
                                str(s.get("hablante") or ""), str(s.get("texto") or ""))
                    for s in anterior.get("segmentos") or []
                    if str(s.get("texto") or "") != TEXTO_SIN_TRANSCRIPCION]
            previo = texto_final(segs)
    return ContextoTranscripcion(
        materia=materia.nombre if materia else None,
        perfil=gemini.extracto_perfil(perfil_completo, LIMITE_PERFIL),
        glosario=gemini.extraer_glosario(perfil_completo),
        previo=previo,
        duracion_ms=duracion,
        habla=habla_del_bloque(leer_json(carpeta / ("silencios.celu.json" if celu else "silencios.json")),
                               inicio_ms, fin_ms),
    )


def habla_del_bloque(silencios: Any, inicio_ms: int, fin_ms: int) -> list[tuple[int, int]]:
    """Tramos de habla de silencios.json que caen en el bloque, relativos al bloque.

    Sin silencios.json (o sin la clave "habla") se asume que hubo habla en todo el bloque.
    """
    duracion = max(0, fin_ms - inicio_ms)
    if not isinstance(silencios, dict) or not isinstance(silencios.get("habla"), list):
        return [(0, duracion)] if duracion else []
    tramos = []
    for h in silencios["habla"]:
        try:
            i, f = int(h["inicio_ms"]), int(h["fin_ms"])
        except (KeyError, TypeError, ValueError):
            continue
        i, f = max(i, inicio_ms), min(f, fin_ms)
        if f > i:
            tramos.append((i - inicio_ms, f - inicio_ms))
    return sorted(tramos)


def absolutos(segmentos: list[SegmentoRel], inicio_ms: int, duracion_ms: int) -> list[dict[str, Any]]:
    """Relativos al bloque → absolutos, recortados al bloque y en orden."""
    salida = []
    for s in segmentos:
        texto = s.texto.strip()
        if not texto:
            continue
        i = max(0, int(s.inicio_ms))
        f = max(i, int(s.fin_ms))
        if duracion_ms:
            i = min(i, duracion_ms)
            f = min(f, duracion_ms)
        salida.append({"inicio_ms": inicio_ms + i, "fin_ms": inicio_ms + f,
                       "hablante": s.hablante or "desconocido", "texto": texto})
    salida.sort(key=lambda s: (s["inicio_ms"], s["fin_ms"]))
    return salida
