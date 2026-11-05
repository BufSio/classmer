"""Etapa revisar_transcript (§15): concilia los dos transcripts (si hay celu) y siempre limpia.

Va después de `clasificar` (ya se sabe la materia) y antes de `apunte`. Con la revisión apagada en la
materia, la clase sigue como antes: el crudo (más el respaldo del celu donde el principal no tiene
audio, que ya armó linea_de_tiempo) y el apunte directo.

Automática o asistida (el tipo de revisión de la materia):
1. lee transcript.crudo.json (el principal, de linea_de_tiempo) y transcript.celu.json si hay;
2. concilia, limpia, vuelve a escuchar las dudas con Gemini y las resuelve (classmer.ia.revisor);
3. guarda todo en revision/ (classmer.revision) y aplica el revisado: transcript.json, transcript.md,
   timeline.md y la tabla `segmentos`. Desde acá, el revisado es el transcript de la clase;
4. si quedan dudas, depende del tipo de revisión de la materia:
   - automática (el default): cada duda toma la propuesta del modelo, como con "Listo", y sigue al
     apunte en el mismo job;
   - asistida: la clase espera al usuario (`esperando_usuario`, `espera: revision`) y el apunte arranca con
     "Listo" o "Generar sin revisar" en el panel.
   Sin dudas, sigue derecho al apunte.

Una revisión que se vuelve a correr (reprocesar) reusa lo que ya se pagó (caché por pedido) y lo que
el usuario ya respondió (mismas dudas).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer import audio as audio_mod
from classmer import revision
from classmer.ia import gemini, revisor
from classmer.ia.claude_json import ConsultaClaude
from classmer.ia.transcriptor import ContextoTranscripcion, crear_transcriptor
from classmer.pipeline import Contexto, CuotaAgotada, ErrorEtapa, EsperandoUsuario, Resultado
from classmer.sesiones import PISTA_CELU, NoExiste, datos_celu, fila_sesion, ruta_audio
from classmer.util import ahora_iso, escribir_json, leer_json, ms_a_hhmmss

if TYPE_CHECKING:
    from classmer.cola import Job
    from classmer.config import Materia

TAREA = "revisar_transcript"
ESCUCHA_REINTENTOS_MINUTO = 3   # esperas por el tope por minuto de Gemini, por grupo
ESCUCHA_ESPERA_MAX_S = 120      # más que esto (la cuota diaria pide 3600 s) no se espera


def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if not sid:
        raise ErrorEtapa("revisar_transcript necesita una sesión", reintentable=False)
    cfg = ctx.cfg
    with ctx.base.conexion() as conn:
        try:
            sesion = fila_sesion(conn, sid)
        except NoExiste as e:
            raise ErrorEtapa(f"no existe la sesión {sid}", reintentable=False) from e
        celu_datos = datos_celu(conn, sid)
    materia = cfg.materia(sesion["materia_id"])
    revision.asegurar_crudos(cfg, sid)
    if materia is None or not materia.revisar_transcript:
        restaurado = revision.restaurar_crudo(ctx, sid)
        que = "sin materia" if materia is None else "apagada en la materia"
        return Resultado(siguientes=[("apunte", {})],
                         log=f"revisión {que}: sigue el crudo" + (" (se deshizo la revisión anterior)" if restaurado else ""))

    sdir = ctx.sesion_dir(sid)
    crudos = [s for s in leer_json(revision.ruta_crudo(cfg, sid, "transcript.json"), []) or [] if isinstance(s, dict)]
    if not crudos:
        return Resultado(siguientes=[("apunte", {})], log="no hay transcript para revisar")
    celu = [s for s in leer_json(sdir / "transcript.celu.json", []) or [] if isinstance(s, dict)]
    anterior = revision.cargar(cfg, sid)
    r = Revision(ctx, sid, materia, crudos, celu, celu_datos)
    rev = r.correr()
    _heredar_respuestas(rev, anterior)
    automatica = materia.revision == "automatica" and bool(revision.pendientes(rev))
    con_propuesta = revision.usar_propuestas(rev) if automatica else 0
    revision.guardar(cfg, sid, rev)
    revision.escribir_cambios_md(cfg, sid, rev)
    revision.aplicar(ctx, sid, rev, materia)
    quedan = revision.pendientes(rev)
    resumen = r.resumen(rev)
    for aviso in rev["avisos"]:
        ctx.log_sesion(sid, f"revisar_transcript: aviso: {aviso}")
    ctx.log_sesion(sid, f"revisar_transcript: {resumen}")
    gasto = r.gasto
    costo = round(gasto.costo, 6) if gasto.con_costo else None
    if automatica:
        rev["estado"] = "aplicada"
        rev["aplicada"] = {"momento": ahora_iso(cfg.zona_horaria), "como": "automatica", "con_propuesta": con_propuesta}
        revision.guardar(cfg, sid, rev)
        revision.escribir_cambios_md(cfg, sid, rev)
        ctx.log_sesion(sid, f"revisar_transcript: revisión automática ({con_propuesta} duda(s) con la propuesta "
                            "del modelo)")
        return Resultado(siguientes=[("apunte", {})], log=resumen, motor=f"claude:{r.consulta.modelo}", costo=costo,
                         facturacion=gasto.facturacion)
    if quedan:
        rev["estado"] = "esperando"
        revision.guardar(cfg, sid, rev)
        raise EsperandoUsuario(
            f"Revisá {len(quedan)} duda{'s' if len(quedan) != 1 else ''} del transcript en el panel: el apunte "
            "arranca con \"Listo\" (o \"Generar sin revisar\").",
            espera="revision", motor=f"claude:{r.consulta.modelo}", costo=costo,
            facturacion=gasto.facturacion, log=resumen)
    rev["estado"] = "aplicada"
    rev["aplicada"] = {"momento": ahora_iso(cfg.zona_horaria), "como": "sin_dudas", "con_propuesta": 0}
    revision.guardar(cfg, sid, rev)
    return Resultado(siguientes=[("apunte", {})], log=resumen, motor=f"claude:{r.consulta.modelo}", costo=costo,
                     facturacion=gasto.facturacion)


def _heredar_respuestas(rev: dict[str, Any], anterior: dict[str, Any] | None) -> None:
    """Lo que el usuario ya respondió en una revisión anterior de la misma clase, si la duda es la misma."""
    if not anterior:
        return
    previas = {}
    for d in anterior.get("dudas") or []:
        if d.get("estado") == "respondida" and d.get("respuesta"):
            previas[(d.get("linea"), tuple(o["texto"] for o in d.get("opciones") or []))] = d["respuesta"]
    for d in rev.get("dudas") or []:
        clave = (d.get("linea"), tuple(o["texto"] for o in d["opciones"]))
        if d.get("estado") == "pendiente" and clave in previas:
            d["respuesta"], d["estado"] = previas[clave], "respondida"
            ln = next((x for x in rev["lineas"] if x.get("id") == d["linea"]), None)
            if ln is not None:
                revision.poner_texto(ln, d["respuesta"]["texto"])


class Revision:
    """Una corrida de la revisión de una clase."""

    def __init__(self, ctx: Contexto, sid: str, materia: Materia, crudos: list[dict[str, Any]],
                 celu: list[dict[str, Any]], celu_datos: dict[str, Any] | None):
        self.ctx = ctx
        self.cfg = ctx.cfg
        self.sid = sid
        self.materia = materia
        self.crudos = crudos
        self.celu = celu
        self.celu_datos = celu_datos or {}
        self.avisos: list[str] = []
        self.gasto = revisor.Gasto()
        t = self.cfg.revisar_transcript
        fabrica = ctx.servicios.revisor
        self.consulta = (fabrica(self.cfg) if fabrica is not None else
                         ConsultaClaude(self.cfg, tarea=TAREA, modelo=t.modelo, esfuerzo=t.esfuerzo,
                                        sistema=revisor.SISTEMA))
        self.trabajo = ctx.job_dir(sid, "revisar")
        self.pedidor = revisor.Pedidor(self.consulta, self.trabajo / "cache", self.gasto, self._avisar)
        self.textos = {"materia": materia.nombre, "profesor": materia.profesor or "el profe",
                       "perfil": revisor.perfil_para(gemini.leer_perfil(materia.ruta_perfil()))}
        self.t0 = time.time()
        self.escucha: dict[str, int] = {"pedidos": 0, "grupos": 0, "segundos": 0}

    def _avisar(self, aviso: str) -> None:
        if aviso not in self.avisos:
            self.avisos.append(aviso)

    # --- la corrida ---
    def correr(self) -> dict[str, Any]:
        lineas = [{"id": f"L{i + 1:04d}", "inicio_ms": int(s["inicio_ms"]), "fin_ms": int(s.get("fin_ms") or s["inicio_ms"]),
                   "bloque": s.get("bloque"), "hablante": s.get("hablante"), "texto": " ".join(str(s["texto"]).split()),
                   "crudo": " ".join(str(s["texto"]).split()), "origen": s.get("origen") or "principal"}
                  for i, s in enumerate(self.crudos)]
        conciliar: dict[str, Any] | None = None
        if self.celu:
            lineas, conciliar = self.conciliar(lineas)
        lineas, limpiar = self.limpiar(lineas)
        dudas = self.armar_dudas(lineas)
        if dudas:
            self.escuchar(dudas)
            self.resolver(dudas, lineas)
        sdir = revision.carpeta(self.cfg, self.sid)
        if conciliar is not None:
            escribir_json(sdir / "conciliar.json", conciliar)
        else:
            (sdir / "conciliar.json").unlink(missing_ok=True)
        escribir_json(sdir / "limpiar.json", limpiar)
        escribir_json(sdir / "escuchar.json", [{k: d.get(k) for k in ("id", "linea", "inicio_ms", "paso", "opciones",
                                                                       "escuchado", "resolucion", "estado")} for d in dudas])
        for ln in lineas:
            ln.pop("hablante", None)
            ln.pop("duda_conciliar", None)
            ln.pop("duda_limpiar", None)
        t = self.cfg.revisar_transcript
        return {
            "version": 1, "estado": "esperando", "creada": ahora_iso(self.cfg.zona_horaria),
            "materia_id": self.materia.id, "modo": "conciliacion" if self.celu else "limpieza",
            "modelo": self.consulta.modelo, "esfuerzo": getattr(self.consulta, "esfuerzo", t.esfuerzo),
            "facturacion": self.gasto.facturacion,
            "costo": round(self.gasto.costo, 6) if self.gasto.con_costo else None, "uso": self.gasto.uso,
            "pedidos": self.gasto.pedidos, "en_cache": self.gasto.en_cache,
            "segundos": round(time.time() - self.t0, 1), "escucha": self.escucha,
            "desfase_ms": self.celu_datos.get("desfase_ms") if self.celu else None,
            "lineas": lineas, "dudas": dudas, "avisos": self.avisos, "aplicada": None,
        }

    def resumen(self, rev: dict[str, Any]) -> str:
        dudas = rev["dudas"]
        para = "con la propuesta del modelo" if self.materia.revision == "automatica" else "para el usuario"
        partes = [f"{rev['modo']} con {rev['modelo']}", f"{len(rev['lineas'])} línea(s)",
                  f"{len(dudas)} duda(s): {len(revision.pendientes(rev))} {para}, "
                  f"{sum(1 for d in dudas if d['estado'] == 'resuelta')} resuelta(s) al volver a escuchar"]
        if self.gasto.pedidos or self.gasto.en_cache:
            partes.append(f"{self.gasto.pedidos} pedido(s)" + (f" + {self.gasto.en_cache} en caché" if self.gasto.en_cache else ""))
        if self.gasto.con_costo:
            partes.append(f"US$ {self.gasto.costo:.4f} ({self.gasto.facturacion})")
        partes.append(f"{time.time() - self.t0:.0f} s")
        return ", ".join(partes)

    # --- 1. conciliar ---
    def conciliar(self, lineas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        alineadas, solo = revisor.alinear(self.crudos, [s for s in self.celu if s.get("texto")])
        for ln, a in zip(lineas, alineadas, strict=True):
            ln["celu"] = a["celu"]
            ln["similitud"] = a["similitud"]
        for k, s in enumerate(solo):
            s["id"] = f"C{k + 1:04d}"
        pedidas = {ln["id"] for ln in lineas if ln.get("celu") is not None and revisor.difieren(ln["texto"], ln["celu"])}

        tramos = []
        for a, b in revisor.tramos(len(lineas), revisor.LINEAS_CONCILIAR):
            desde = lineas[a]["inicio_ms"] if a > 0 else -10**12
            hasta = lineas[b]["inicio_ms"] if b < len(lineas) else 10**12
            suyas = [s for s in solo if desde <= s["inicio_ms"] < hasta]
            ids = [ln["id"] for ln in lineas[a:b] if ln["id"] in pedidas]
            if ids or suyas:
                tramos.append((lineas[a:b], suyas, ids))

        def pedir(tramo: tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]) -> Any:
            ls, suyas, ids = tramo
            return revisor.pedir_tramo(
                self.pedidor, "conciliar",
                lambda items: revisor.prompt_conciliar(items, _suyas(items, ls, suyas),
                                                       [x["id"] for x in items if x["id"] in ids], **self.textos),
                ls, revisor.ESQUEMA_CONCILIAR)

        resultados_lineas: dict[str, dict[str, Any]] = {}
        resultados_solo: dict[str, dict[str, Any]] = {}
        for partes in revisor.en_paralelo(pedir, tramos):
            for _items, datos in partes:
                if datos is None:
                    continue
                rl, rs = revisor.interpretar_conciliar(datos)
                resultados_lineas.update(rl)
                resultados_solo.update(rs)

        registro: list[dict[str, Any]] = []
        for ln in lineas:
            if ln["id"] not in pedidas:
                continue
            r = resultados_lineas.get(ln["id"])
            if r is None or not r["texto"]:
                continue
            reloj, celu = ln["texto"], ln["celu"]
            ln["texto"] = r["texto"]
            registro.append({"linea": ln["id"], "inicio_ms": ln["inicio_ms"], "reloj": reloj, "celu": celu,
                             "similitud": ln.get("similitud"), "texto": r["texto"], "eleccion": r["eleccion"],
                             "motivo": r["motivo"], "confianza": r["confianza"], "duda": r["duda"]})
            if r["duda"]:
                opciones = [{"fuente": "reloj", "texto": reloj}, {"fuente": "celu", "texto": celu}]
                if r["texto"] not in (reloj, celu):
                    opciones.append({"fuente": "mezcla", "texto": r["texto"]})
                ln["duda_conciliar"] = {"opciones": opciones, "motivo": r["motivo"]}
        solo_registro = []
        nuevas = []
        for s in solo:
            r = resultados_solo.get(s["id"])
            incluir = bool(r and r["incluir"] and (r["texto"] or s["texto"]))
            solo_registro.append({"id": s["id"], "inicio_ms": s["inicio_ms"], "texto": s["texto"], "incluir": incluir,
                                  "hablante": r["hablante"] if r else None, "motivo": r["motivo"] if r else "sin respuesta"})
            if incluir:
                nuevas.append({"id": s["id"], "inicio_ms": int(s["inicio_ms"]), "fin_ms": int(s["fin_ms"]), "bloque": None,
                               "hablante": r["hablante"], "texto": r["texto"] or s["texto"], "crudo": None,
                               "celu": s["texto"], "origen": "celu"})
        if nuevas:
            lineas = sorted(lineas + nuevas, key=lambda x: (x["inicio_ms"], x["id"].startswith("C")))
        for ln in lineas:
            ln.pop("similitud", None)
        datos = {"desfase_ms": self.celu_datos.get("desfase_ms"), "lineas_con_par": sum(1 for ln in lineas if ln.get("celu")),
                 "lineas_distintas": len(pedidas), "cambios": registro, "solo_celu": solo_registro}
        return lineas, datos

    # --- 2. limpiar ---
    def limpiar(self, lineas: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        cortes = revisor.tramos(len(lineas), revisor.LINEAS_LIMPIAR)

        def pedir(corte: tuple[int, int]) -> Any:
            a, b = corte
            contexto = [revisor._linea_prompt(x) for x in lineas[max(0, a - revisor.CONTEXTO_LIMPIAR):a]]
            return revisor.pedir_tramo(
                self.pedidor, "limpiar",
                lambda items: revisor.prompt_limpiar(items, contexto, **self.textos),
                lineas[a:b], revisor.ESQUEMA_LIMPIAR)

        resultados: dict[str, dict[str, Any]] = {}
        for partes in revisor.en_paralelo(pedir, cortes):
            for _items, datos in partes:
                if datos is not None:
                    resultados.update(revisor.interpretar_limpiar(datos))

        registro = []
        faltan = 0
        for ln in lineas:
            r = resultados.get(ln["id"])
            antes, rol_antes = ln["texto"], revisor.rol_de(ln.get("hablante"))
            if r is None:
                faltan += 1
                ln["rol"] = "alumno" if rol_antes == "ALUMNO" else "profe"
                continue
            ln["rol"] = r["hablante"] or ("alumno" if rol_antes == "ALUMNO" else "profe")
            ln["texto"] = r["texto"]
            ln["eliminada"] = not r["texto"]
            duda_c = ln.get("duda_conciliar")
            if duda_c:
                limpias = {a["fuente"]: a["texto"] for a in r["alternativas"]}
                opciones = [{"fuente": o["fuente"], "texto": limpias.get(o["fuente"]) or o["texto"]}
                            for o in duda_c["opciones"]]
                if r["texto"] and r["texto"] not in [o["texto"] for o in opciones]:
                    opciones.append({"fuente": "propuesta", "texto": r["texto"]})
                ln["duda_conciliar"] = {"opciones": opciones, "motivo": duda_c["motivo"]}
            elif r["duda"]:
                ln["duda_limpiar"] = {"opciones": [{"fuente": "audio", "texto": r["texto"]},
                                                   {"fuente": "propuesta", "texto": r["duda"]["alternativa"]}],
                                      "motivo": r["duda"]["motivo"]}
            if r["cambios"] or antes != r["texto"] or ln["rol"] != rol_antes.lower():
                registro.append({"linea": ln["id"], "inicio_ms": ln["inicio_ms"], "antes": antes, "despues": r["texto"],
                                 "hablante_antes": rol_antes.lower(), "hablante": ln["rol"], "cambios": r["cambios"],
                                 "duda": r["duda"]})
        if faltan:
            self._avisar(f"limpiar: {faltan} línea(s) sin respuesta del modelo quedaron como estaban")
        return lineas, {"cambios": registro, "sin_respuesta": faltan}

    # --- 3. dudas ---
    def armar_dudas(self, lineas: list[dict[str, Any]]) -> list[dict[str, Any]]:
        dudas = []
        for k, ln in enumerate(lineas):
            for paso in ("conciliar", "limpiar"):
                d = ln.get(f"duda_{paso}")
                if not d:
                    continue
                opciones = [o for o in d["opciones"] if o["texto"]]
                if len({o["texto"] for o in opciones}) < 2:
                    continue
                propuesta = next((i for i, o in enumerate(opciones) if o["texto"] == ln["texto"]), 0)
                dudas.append({
                    "id": f"D{len(dudas) + 1:02d}", "linea": ln["id"], "inicio_ms": ln["inicio_ms"],
                    "fin_ms": ln.get("fin_ms") or ln["inicio_ms"], "paso": paso, "motivo": d["motivo"],
                    "opciones": opciones, "propuesta": propuesta, "escuchado": {}, "resolucion": None,
                    "estado": "pendiente", "respuesta": None,
                    "antes": _vecina(lineas, k, -1), "despues": _vecina(lineas, k, 1),
                })
                break
        return dudas

    def escuchar(self, dudas: list[dict[str, Any]]) -> None:
        """Gemini vuelve a escuchar esos segundos en los dos audios (hasta escuchar_max dudas).

        Los tramos van agrupados: varios por pedido (hasta ESCUCHA_POR_PEDIDO y ESCUCHA_MAX_PEDIDO_MS),
        separados por un silencio, y cada frase vuelve a su tramo por el minuto. Así una clase gasta
        pocos pedidos de la cuota de Gemini, que es la misma que transcribe las clases. Los grupos van de a
        uno: con el tope por minuto se espera lo que pide Gemini (hasta ESCUCHA_REINTENTOS_MINUTO veces);
        sin cuota diaria no se espera: las dudas que faltan pasan al usuario sin la nueva escucha."""
        tope = self.cfg.revisar_transcript.escuchar_max
        if tope <= 0:
            return
        with self.ctx.base.conexion() as conn:
            principal = ruta_audio(self.cfg, conn, self.sid)
            celu = ruta_audio(self.cfg, conn, self.sid, PISTA_CELU) if self.celu else None
        desfase = int(self.celu_datos.get("desfase_ms") or 0)
        fabrica = self.ctx.servicios.transcriptor
        if fabrica is None:
            transcriptor = crear_transcriptor(self.cfg)
        else:
            t = fabrica(self.cfg)
            transcriptor = t[0] if isinstance(t, (list, tuple)) else t
        if len(dudas) > tope:
            self._avisar(f"se volvieron a escuchar {tope} de {len(dudas)} dudas (revisar_transcript.escuchar_max)")
        fuentes = [("reloj", principal, 0)]
        if celu is not None and celu.is_file():
            fuentes.append(("celu", celu, desfase))
        lotes: list[tuple[str, Path, list[tuple[dict[str, Any], int, int]]]] = []
        for fuente, ruta, corrimiento in fuentes:
            tramos = []
            for d in dudas[:tope]:
                a, b = revisor.recortes_escucha(d["inicio_ms"], d["fin_ms"])
                a, b = max(0, a - corrimiento), b - corrimiento
                if b - a >= 1000:
                    tramos.append((d, a, b))
            for lote in revisor.lotes_escucha(tramos):
                lotes.append((fuente, ruta, lote))
        pedidos = fallas = 0
        sin_cuota: str | None = None
        for k, (fuente, ruta, lote) in enumerate(lotes, start=1):
            if sin_cuota is not None:
                break
            archivo = self.trabajo / "escuchar" / f"escuchar-{fuente}-{k}.mp3"
            try:
                inicios = audio_mod.unir_tramos([(ruta, a, b) for _, a, b in lote], archivo,
                                                 silencio_ms=revisor.SILENCIO_ESCUCHA_MS)
                total = inicios[-1] + lote[-1][2] - lote[-1][1]
                contexto = ContextoTranscripcion(materia=self.materia.nombre, duracion_ms=total)
                for intento in range(ESCUCHA_REINTENTOS_MINUTO + 1):
                    try:
                        r = transcriptor.transcribir(archivo, contexto)
                        break
                    except CuotaAgotada as e:
                        # El tope por minuto (Tier 1: 10.000 tokens de entrada, casi un grupo de 5 min)
                        # pega seguido: se espera lo que pide Gemini y se sigue, como la transcripción.
                        # La cuota diaria (o lo que pida más de ESCUCHA_ESPERA_MAX_S) no se espera.
                        if e.espera_s > ESCUCHA_ESPERA_MAX_S or intento == ESCUCHA_REINTENTOS_MINUTO:
                            raise
                        time.sleep(max(1.0, e.espera_s))
                pedidos += 1
            except CuotaAgotada as e:
                if e.espera_s > ESCUCHA_ESPERA_MAX_S:
                    sin_cuota = revisor.error_corta(e)
                else:
                    fallas += 1
                    self._avisar(f"no se pudo volver a escuchar un grupo de {len(lote)} tramo(s) ({fuente}): "
                                 f"Gemini sin cuota por minuto después de {ESCUCHA_REINTENTOS_MINUTO} esperas")
                continue
            except Exception as e:  # noqa: BLE001 - volver a escuchar ayuda, pero no traba la revisión
                fallas += 1
                self._avisar(f"no se pudo volver a escuchar un grupo de {len(lote)} tramo(s) ({fuente}): "
                             f"{revisor.error_corta(e)}")
                continue
            for (d, a, b), texto in zip(lote, revisor.repartir_escucha(r.segmentos, inicios, [b - a for _, a, b in lote]),
                                        strict=True):
                d["escuchado"][fuente] = texto
        if sin_cuota is not None:
            self._avisar(f"Gemini sin cuota: {len(lotes) - pedidos - fallas} grupo(s) de dudas pasan sin la nueva "
                         f"escucha ({sin_cuota})")
        self.escucha = {"pedidos": pedidos, "grupos": len(lotes),
                        "segundos": round(sum(b - a for _, _, lote in lotes for _, a, b in lote) / 1000)}

    def resolver(self, dudas: list[dict[str, Any]], lineas: list[dict[str, Any]]) -> None:
        por_id = {ln["id"]: ln for ln in lineas}
        resultados: dict[str, dict[str, Any]] = {}
        for _items, datos in revisor.pedir_tramo(
                self.pedidor, "resolver", lambda items: revisor.prompt_resolver(items, **_sin_perfil(self.textos)),
                dudas, revisor.ESQUEMA_RESOLVER, minimo=4):
            if datos is not None:
                resultados.update(revisor.interpretar_resolver(datos))
        for d in dudas:
            r = resultados.get(d["id"])
            ln = por_id.get(d["linea"])
            if r is None or not r["texto"]:
                continue
            if _se_come_vecinas(r["texto"], d):
                # La línea resuelta trae texto de la anterior o la siguiente (las vio como contexto): no
                # sirve. Queda la propuesta que había, sin resolver.
                self._avisar(f"{d['id']}: la resolución mezclaba líneas vecinas; queda la propuesta anterior")
                continue
            d["resolucion"] = r
            textos = [o["texto"] for o in d["opciones"]]
            if r["texto"] in textos:
                d["propuesta"] = textos.index(r["texto"])
            else:
                d["opciones"].append({"fuente": "escucha" if r["resuelta"] else "modelo", "texto": r["texto"]})
                d["propuesta"] = len(d["opciones"]) - 1
            if r["resuelta"]:
                d["estado"] = "resuelta"
            if ln is not None:
                ln["texto"] = r["texto"]
                ln["eliminada"] = False


def _suyas(items: list[dict[str, Any]], tramo: list[dict[str, Any]], solo: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Las líneas "sólo celu" de un pedido: todas las del tramo o, si el tramo se partió, las que caen
    entre el comienzo de esta parte y el de la siguiente."""
    if len(items) == len(tramo):
        return solo
    desde = items[0]["inicio_ms"] if items[0] is not tramo[0] else -10**12
    fin = tramo.index(items[-1]) + 1
    hasta = tramo[fin]["inicio_ms"] if fin < len(tramo) else 10**12
    return [s for s in solo if desde <= s["inicio_ms"] < hasta]


def _se_come_vecinas(texto: str, d: dict[str, Any]) -> bool:
    """¿El texto propuesto para la línea incluye palabras de una línea vecina que no están en ninguna de
    sus versiones? (Mira las primeras/últimas 4 palabras de las vecinas.)"""
    propio = " ".join(revisor.palabras(texto))
    versiones = [" ".join(revisor.palabras(o["texto"])) for o in d["opciones"]]
    for vecina, extremo in ((d.get("antes"), -4), (d.get("despues"), 4)):
        if not vecina:
            continue
        palabras = revisor.palabras(vecina.split("] ", 1)[-1])
        trozo = " ".join(palabras[extremo:] if extremo < 0 else palabras[:extremo])
        if len(palabras) >= 4 and trozo in propio and not any(trozo in v for v in versiones):
            return True
    return False


def _vecina(lineas: list[dict[str, Any]], k: int, paso: int) -> str | None:
    j = k + paso
    while 0 <= j < len(lineas):
        texto = lineas[j].get("texto")
        if texto:
            return f"[{ms_a_hhmmss(lineas[j]['inicio_ms'])}] {texto}"
        j += paso
    return None


def _sin_perfil(textos: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in textos.items() if k != "perfil"}
