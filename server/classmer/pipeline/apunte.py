"""Etapa 6 — apunte (§7, §8): el agente escribe apunte.tex + meta.json; el server compila e indexa.

1. Número de clase de la sesión (se guarda en sesiones.numero_clase).
2. Carpeta de trabajo jobs/<sesion>/apunte/ desde cero (docs/internos.md): input/ (timeline,
   transcript, fotos, adjuntos, perfil, estilo, clase anterior, contexto.json, adjuntos.json),
   plantilla/ y output/. Los adjuntos se leen de la base en ese momento: input/adjuntos.json dice
   cuáles toma esta corrida (y de qué job), así uno que llega con el agente andando queda "sin_usar".
3. Prompt en capas (prompts/apunte.md); su versión queda en la tabla prompts.
4. El agente (Servicios.agente_apunte o AgenteClaude) con el modelo y el esfuerzo de la materia
   (o los globales) y la facturación de ia.facturacion: si el medio se queda sin créditos o sin
   cupo, se rehace el paso entero (carpeta de trabajo limpia) con el otro. Si no dejó
   output/apunte.tex: error reintentable.
5. El server compila (ésa es la verdad; si el .tex lee archivos de afuera no lo compila y queda
   "no compiló"), valida meta.json (si está mal, lo repara: el índice nunca rompe la publicación) y
   guarda sesiones/<id>/apunte/vN/. En una transacción: la fila de apuntes (vN pasa a actual, con
   los adjuntos que tomó la corrida que quedó), secciones, items y el título de la sesión.
Sigue a publicar también si no compiló (publicar deja la sesión en error).
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import time
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from classmer import adjuntos, latex
from classmer.config import PROMPTS_DIR
from classmer.ia.agente_apunte import AgenteClaude, ResultadoAgente, TrabajoApunte, WebCorrida
from classmer.ia.facturacion import ETIQUETA_MEDIO, Facturacion, texto_costo
from classmer.ia.gemini import rellenar
from classmer.ia.meta import leer_meta, meta_desde_tex, reparar_meta, validar_datos, validar_meta
from classmer.materias import ES_CLASE
from classmer.pipeline import Contexto, CuotaAgotada, ErrorEtapa, Resultado, actualizar_sesion
from classmer.pipeline.linea_de_tiempo import CARPETA_FOTOS, encabezado, fecha_local
from classmer.sesiones import NoExiste, fila_sesion
from classmer.util import (ahora_iso, escribir_atomico, escribir_json, leer_json, limpiar_titulo, ms_a_hhmmss,
                           sha256_texto)

if TYPE_CHECKING:
    from classmer.cola import Job
    from classmer.config import Config, Materia
    from classmer.db import Base

PROMPT = "apunte"
SEPARADOR = "===== MENSAJE ====="
# El bloque de la búsqueda en internet del sistema: va sólo con apunte.web.activa (sin las líneas de marca).
_BLOQUE_WEB = re.compile(r"^===== WEB =====\n.*?^===== FIN WEB =====\n\n?", re.M | re.S)
_MARCAS_WEB = re.compile(r"^===== (?:FIN )?WEB =====\n", re.M)
_DE_LA_CONFIG: Any = object()  # armar_prompt sin `web`: la de la config
MAX_PERFIL = 15_000        # caracteres del perfil que van en el mensaje (el archivo va entero)
MAX_TIMELINE = 400_000     # más que esto, el agente lee el resto de input/timeline.md
MAX_LISTA_ADJUNTOS = 12    # adjuntos que se nombran en el mensaje (los demás están en input/timeline.md)
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre",
         "octubre", "noviembre", "diciembre")
TIPOS_LEGIBLES = {"teorica": "Teórica", "practica": "Práctica"}
_COMENTARIO = re.compile(r"<!--.*?-->\s*", re.DOTALL)
_CLASE_RE = re.compile(r"\\documentclass\s*(?:\[[^\]]*\])?\s*\{classmer\}")


# --- etapa -------------------------------------------------------------------------------------

def ejecutar(ctx: Contexto, job: Job) -> Resultado:
    sid = job.sesion_id
    if not sid:
        raise ErrorEtapa("apunte necesita una sesión", reintentable=False)
    cfg = ctx.cfg
    with ctx.base.conexion() as conn:
        try:
            sesion = fila_sesion(conn, sid)
        except NoExiste as e:
            raise ErrorEtapa(f"no existe la sesión {sid}", reintentable=False) from e
        if not sesion["materia_id"]:
            raise ErrorEtapa(f"falta la materia: asignala con `uv run classmer asignar {sid} --materia <id>`",
                             reintentable=False)
        materia = cfg.materia(sesion["materia_id"])
        if materia is None:
            raise ErrorEtapa(f"la materia {sesion['materia_id']} no está en config.yaml", reintentable=False)
        numero = calcular_numero_clase(conn, materia, sesion)
        actualizar_sesion(conn, sid, cfg.zona_horaria, numero_clase=numero)
        anterior = clase_anterior(conn, cfg, sesion)

    carpeta = ctx.sesion_dir(sid)
    if not (carpeta / "timeline.md").is_file():
        raise ErrorEtapa("falta timeline.md: reprocesá desde linea_de_tiempo", reintentable=False)
    duracion = _duracion(carpeta, sesion)
    contexto = {
        "materia_id": materia.id,
        "materia": materia.nombre,
        "fecha": fecha_local(cfg, sesion["inicio"]),
        "fecha_larga": fecha_larga(fecha_local(cfg, sesion["inicio"])),
        "numero_clase": numero,
        "tipo_clase": sesion["tipo_clase"],
        "duracion_ms": duracion,
        "inicio": sesion["inicio"],
    }

    workdir = ctx.job_dir(sid, "apunte")
    salida = workdir / "output"
    modelo_cfg, esfuerzo, origen = cfg.modelo_apunte(materia)
    fabrica = ctx.servicios.agente_apunte or AgenteClaude
    versiones: list[str] = []   # versión del prompt de cada intento (se registra una sola vez)
    agentes: list[Any] = []
    usados: list[list[str]] = []  # adjuntos que tomó cada intento (vale el del intento que quedó)
    descartados: list[str] = []  # lo que gastó un intento que se rehizo con el otro medio

    def correr(medio: str) -> ResultadoAgente:
        """Un intento entero con ese medio de facturación: carpeta de trabajo y output/ desde cero,
        prompt y agente. Si el medio no sirve (SinCreditos), Facturacion lo rehace con el otro."""
        fotos, material = preparar_carpeta(cfg, workdir, carpeta, materia, contexto, anterior,
                                           base=ctx.base, sesion_id=sid, job_id=job.id)
        usados.append([p.adjunto["id"] for p in material])
        web = WebCorrida.desde_config(cfg)  # la misma para el prompt y para la política de esta corrida
        sistema, mensaje, version, contenido = armar_prompt(cfg, materia, contexto, workdir, fotos, material=material,
                                                            web=web)
        if not versiones:
            with ctx.base.conexion() as conn:
                registrar_prompt(conn, version, contenido, cfg.zona_horaria)
        versiones.append(version)
        ctx.log_sesion(sid, f"apunte: clase {numero}, carpeta de trabajo lista ({len(fotos)} foto(s)"
                            + (f", {len(material)} adjunto(s)" if material else "") + f"), prompt {version}")
        for p in material:
            if not p.ok:
                ctx.log_sesion(sid, f"apunte: aviso: el adjunto {p.adjunto['nombre_original']!r} no está en "
                                    f"input/adjuntos/: {p.aviso}")
        ctx.log_sesion(sid, f"apunte: {describir_intento(modelo_cfg, esfuerzo, origen, medio)}")
        agente = fabrica(cfg)
        agentes.append(agente)
        trabajo = TrabajoApunte(
            workdir=workdir,
            carpeta_materia=materia.carpeta if materia.carpeta and materia.carpeta.is_dir() else None,
            prompt_sistema=sistema,
            prompt=mensaje,
            modelo=modelo_cfg,
            motor_latex=cfg.apunte.motor_latex,
            max_intentos_compilacion=cfg.apunte.max_intentos_compilacion,
            tope_correccion_s=cfg.apunte.tope_correccion_min * 60,
            timeout_s=cfg.apunte.timeout_min * 60,
            validar_meta=lambda: validar_salida(salida, duracion),
            duracion_ms=duracion,
            esfuerzo=esfuerzo,
            facturacion=medio,
            web=web,
        )
        try:
            return agente.ejecutar(trabajo)
        except CuotaAgotada as e:  # el intento se descarta, pero lo que gastó queda registrado
            gastado = getattr(e, "costo_usd", None)
            if gastado:
                ctx.log_sesion(sid, f"apunte: el intento con {ETIQUETA_MEDIO.get(medio, medio)} se descarta; "
                                    f"ya había gastado {texto_costo(gastado, medio)}")
                descartados.append(texto_costo(gastado, medio))
            raise

    try:
        resultado, medio = Facturacion(cfg, ctx.base).ejecutar(
            "apunte", correr, log_tarea=lambda m: ctx.log_sesion(sid, f"apunte: {m}"))
    except CuotaAgotada as e:  # pasajera, o ningún medio de facturación disponible: se pospone
        ctx.log_sesion(sid, f"apunte: {e}")
        raise
    agente, version, ids_adjuntos = agentes[-1], versiones[-1], usados[-1]
    ctx.log_sesion(sid, "apunte: " + resultado.log.split("\n--- detalle ---", 1)[0].replace("\n", "\n    "))

    tex = salida / "apunte.tex"
    if not tex.is_file():
        raise ErrorEtapa(f"el agente no escribió output/apunte.tex ({resultado.error or 'sin detalle'})")

    # La verdad sobre si compiló: el server compila con la plantilla original (si no lee archivos de afuera).
    latex.copiar_plantilla(salida)
    lecturas = latex.lecturas_externas_en(salida, "apunte.tex")
    if lecturas:
        comp = latex.ResultadoCompilacion(
            ok=False, pdf=None,
            log=("No se compiló: apunte.tex lee archivos de fuera de la carpeta del apunte ("
                 + ", ".join(lecturas[:10]) + "). Sacá eso y reprocesá desde apunte."))
        ctx.log_sesion(sid, f"apunte: no lo compilo: {comp.log}")
    else:
        comp = latex.compilar(salida, "apunte.tex", motor=cfg.apunte.motor_latex, pasadas=2)
        ctx.log_sesion(sid, f"apunte: compilación del server: {comp.log.splitlines()[0] if comp.log else comp.ok}")

    texto_tex = tex.read_text(encoding="utf-8", errors="replace")
    meta, aviso_meta, original = meta_final(salida / "meta.json", texto_tex, duracion)
    if aviso_meta:
        ctx.log_sesion(sid, f"apunte: aviso: {aviso_meta}")
    if not _CLASE_RE.search(re.sub(r"(?<!\\)%.*", "", texto_tex)):
        ctx.log_sesion(sid, "apunte: aviso: apunte.tex no usa \\documentclass{classmer}")

    modelo = resultado.modelo or modelo_cfg
    motor = getattr(agente, "motor", None) or cfg.apunte.motor
    n = _guardar_version(ctx, sid, materia, salida, comp, meta, original, resultado, motor=motor, modelo=modelo,
                         prompt_version=version, esfuerzo=esfuerzo, facturacion=medio, adjuntos_usados=ids_adjuntos)
    estado = "compiló" if comp.ok else "no compiló"
    uso = (f"{modelo or 'modelo por defecto'}, esfuerzo {esfuerzo or 'por defecto'}, facturación "
           f"{ETIQUETA_MEDIO.get(medio, medio)}, costo {texto_costo(resultado.costo_usd, medio)}")
    ctx.log_sesion(sid, f"apunte: guardado v{n} ({estado}): {meta['titulo']} · {uso}")
    resumen = (f"v{n} {estado}: {meta['titulo']} · {len(meta['secciones'])} secciones, {len(meta['items'])} items · "
               f"agente {uso} · {resultado.intentos_compilacion} compilación(es), {resultado.turnos} turnos")
    if ids_adjuntos:
        resumen += f" · {len(ids_adjuntos)} adjunto(s)"
    if aviso_meta:
        resumen += " · meta reparado"
    if descartados:  # el costo del job es el del intento que quedó; lo descartado se suma aparte
        resumen += " · además gastó un intento descartado: " + "; ".join(descartados)
    return Resultado(siguientes=[("publicar", {})], log=resumen + "\n\n" + resultado.log[:20000],
                     motor=f"{motor}/{modelo}" if modelo else motor, costo=resultado.costo_usd, facturacion=medio)


def describir_intento(modelo: str | None, esfuerzo: str | None, origen: str, medio: str) -> str:
    """'intento con claude-opus-5-5, esfuerzo high (global), facturación plan Max' (pipeline.log)."""
    de = "de la materia" if origen == "materia" else "global"
    return (f"intento con {modelo or 'el modelo por defecto'}, esfuerzo {esfuerzo or 'por defecto'} ({de}), "
            f"facturación {ETIQUETA_MEDIO.get(medio, medio)}")


# --- número de clase y clase anterior ------------------------------------------------------------

# Una sesión en error sin ningún apunte que compiló (prueba fallida, clase que no salió) no es una clase.
_ES_CLASE = ES_CLASE


def calcular_numero_clase(conn: sqlite3.Connection, materia: Materia, sesion: sqlite3.Row) -> int:
    """El número que la sesión fijó al crearse (lo manda la app o es el próximo de la materia).
    Si no tiene (Auto, sesiones viejas): numero_clase_inicial − 1 + sesiones de la materia que
    empezaron antes o a la vez (sin contar las que siguen grabando ni las que quedaron en error sin
    un apunte que compiló; a igual inicio desempata el id)."""
    if sesion["numero_clase"] is not None:
        return int(sesion["numero_clase"])
    cantidad = conn.execute(
        f"""
        SELECT COUNT(*) FROM sesiones s
        WHERE s.materia_id = ? AND (s.id = ? OR (s.estado != 'grabando' AND {_ES_CLASE}))
          AND (s.inicio < ? OR (s.inicio = ? AND s.id <= ?))
        """,
        (materia.id, sesion["id"], sesion["inicio"], sesion["inicio"], sesion["id"]),
    ).fetchone()[0]
    return materia.numero_clase_inicial - 1 + max(1, int(cantidad))


def clase_anterior(conn: sqlite3.Connection, cfg: Config, sesion: sqlite3.Row) -> str:
    """Título, resumen, secciones y pendientes del apunte actual de la clase anterior de la materia
    (sin las sesiones en error que nunca tuvieron un apunte que compiló)."""
    fila = conn.execute(
        f"""
        SELECT s.id, s.inicio, s.numero_clase, a.id AS apunte_id, a.titulo, a.resumen
        FROM sesiones s JOIN apuntes a ON a.sesion_id = s.id AND a.actual = 1
        WHERE s.materia_id = ? AND s.id != ? AND (s.inicio < ? OR (s.inicio = ? AND s.id < ?))
          AND {_ES_CLASE}
        ORDER BY s.inicio DESC, s.id DESC, a.version DESC LIMIT 1
        """,
        (sesion["materia_id"], sesion["id"], sesion["inicio"], sesion["inicio"], sesion["id"]),
    ).fetchone()
    if fila is None:
        return "Es la primera clase registrada de esta materia en Classmer."
    numero = f"Clase {fila['numero_clase']}" if fila["numero_clase"] is not None else "Clase anterior"
    lineas = [f"# {numero} — {fecha_local(cfg, fila['inicio'])} — {fila['titulo'] or 'sin título'}", ""]
    if fila["resumen"]:
        lineas += ["## Resumen", "", fila["resumen"].strip(), ""]
    secciones = conn.execute(
        "SELECT titulo, inicio_ms, fin_ms FROM secciones WHERE apunte_id = ? ORDER BY orden", (fila["apunte_id"],)
    ).fetchall()
    if secciones:
        lineas += ["## Secciones", ""]
        lineas += [f"- [{ms_a_hhmmss(s['inicio_ms'] or 0)}–{ms_a_hhmmss(s['fin_ms'] or 0)}] {s['titulo']}"
                   for s in secciones]
        lineas.append("")
    pendientes = conn.execute(
        "SELECT tipo, texto, fecha_limite FROM items WHERE sesion_id = ? AND estado = 'abierto' ORDER BY id",
        (fila["id"],),
    ).fetchall()
    if pendientes:
        lineas += ["## Pendientes que quedaron", ""]
        lineas += [f"- {p['tipo']}: {p['texto']}" + (f" (para el {p['fecha_limite']})" if p["fecha_limite"] else "")
                   for p in pendientes]
    return "\n".join(lineas).strip()


# --- carpeta de trabajo --------------------------------------------------------------------------

def preparar_carpeta(cfg: Config, workdir: Path, carpeta: Path, materia: Materia, contexto: dict[str, Any],
                     anterior: str, *, base: Base | None = None, sesion_id: str | None = None,
                     job_id: int | None = None) -> tuple[list[str], list[adjuntos.Preparado]]:
    """Arma jobs/<sesion>/apunte/ desde cero. Devuelve (los nombres de las fotos copiadas, los adjuntos).

    Los adjuntos de la sesión se leen de la base recién acá, con la carpeta ya limpia: van a
    input/adjuntos/ con el nombre para LaTeX y sus líneas ADJUNTO de input/timeline.md se rehacen (pudo
    llegar uno después de linea_de_tiempo). input/adjuntos.json lleva todos los que se consideraron,
    también los que no se pudieron preparar: regenerar no los arreglaría y quedarían "sin_usar" para
    siempre. Sin `base`, no hay adjuntos."""
    ruta = str(workdir.absolute())
    if not ruta.isascii() or " " in ruta:
        raise ErrorEtapa(
            f"la carpeta de trabajo {ruta} tiene tildes o espacios: LaTeX y el agente necesitan una ruta ASCII "
            "sin espacios. Mové la carpeta de datos (`datos` en config.yaml) a una ruta así.",
            reintentable=False,
        )
    if workdir.exists():
        _borrar_trabajo(workdir)
    entrada = workdir / "input"
    (entrada / "fotos").mkdir(parents=True)
    (workdir / "output").mkdir()

    lista: list[dict[str, Any]] = []
    if base is not None and sesion_id is not None:
        with base.conexion() as conn:
            lista = adjuntos.listar(conn, sesion_id)
    material = adjuntos.preparar_en(cfg, sesion_id, lista, entrada / adjuntos.CARPETA) if lista else []
    adjuntos.escribir_manifiesto(workdir, job_id, [a["id"] for a in lista])
    avisos = {p.adjunto["id"]: aviso_corto(p.aviso) for p in material if not p.ok}

    # El encabezado se escribió antes de clasificar: en una sesión Auto diría "materia sin asignar".
    lineas = (carpeta / "timeline.md").read_text(encoding="utf-8").splitlines()
    if lineas and lineas[0].startswith("# Clase:"):
        lineas[0] = encabezado(materia.nombre, contexto["fecha"], contexto["tipo_clase"])
    escribir_atomico(entrada / "timeline.md", adjuntos.refrescar_timeline("\n".join(lineas) + "\n", lista, avisos))
    if (carpeta / "transcript.json").is_file():
        shutil.copyfile(carpeta / "transcript.json", entrada / "transcript.json")
    else:
        escribir_json(entrada / "transcript.json", [])
    fotos = []
    for f in sorted((carpeta / CARPETA_FOTOS).glob("*.jpg")):
        shutil.copyfile(f, entrada / "fotos" / f.name)
        fotos.append(f.name)

    escribir_atomico(entrada / "perfil.md", perfil(materia))
    escribir_atomico(entrada / "estilo.md", estilo(cfg, materia))
    escribir_atomico(entrada / "clase_anterior.md", anterior + "\n")
    escribir_json(entrada / "contexto.json", contexto)
    latex.copiar_plantilla(workdir / "plantilla", ejemplo=True)
    latex.copiar_plantilla(workdir / "output")
    return fotos, material


def aviso_corto(aviso: str | None) -> str:
    """Lo que ve el agente de un adjunto que no se pudo preparar ('no se pudo abrir', 'falta el
    archivo en Classmer-data'); el detalle, con rutas de Classmer-data, queda en pipeline.log."""
    return (aviso or "no se pudo preparar").split(" (", 1)[0]


def perfil(materia: Materia) -> str:
    ruta = materia.ruta_perfil()
    if ruta is not None:
        try:
            return ruta.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    texto = f"# {materia.nombre}\n\nNo hay un perfil de la materia (PROMPT-AGENTE.md): trabajá con el nombre de la materia"
    if materia.carpeta and materia.carpeta.is_dir():
        texto += f" y, si te sirve, recorré su carpeta (sólo lectura): {materia.carpeta}"
    return texto + ".\n"


def estilo(cfg: Config, materia: Materia) -> str:
    return (f"# Estilo del apunte\n\n## Global\n\n{_o_nada(cfg.estilo_global)}\n\n"
            f"## {materia.nombre}\n\n{_o_nada(materia.estilo)}\n")


def _o_nada(texto: str | None) -> str:
    return (texto or "").strip() or "(sin indicaciones extra)"


# Al cambiar de medio de facturación la carpeta se borra apenas se cerró el CLI del intento anterior:
# en Windows algún archivo puede seguir tomado un momento (antivirus, indexador).
ESPERAS_BORRAR = (0.5, 1.0, 2.0)
_dormir = time.sleep


def _borrar_trabajo(carpeta: Path) -> None:
    """Borra la carpeta de trabajo; si Windows la tiene tomada, reintenta un par de veces."""
    for espera in (*ESPERAS_BORRAR, None):
        try:
            _borrar(carpeta)
            return
        except OSError as e:
            if not carpeta.exists():
                return
            if espera is None:
                raise ErrorEtapa(f"no pude limpiar la carpeta de trabajo {carpeta} (¿un archivo abierto?): {e}") from e
            _dormir(espera)


def _borrar(carpeta: Path) -> None:
    def solo_lectura(func: Any, ruta: str, _exc: BaseException) -> None:
        os.chmod(ruta, stat.S_IWRITE)
        func(ruta)

    shutil.rmtree(carpeta, onexc=solo_lectura)


def _duracion(carpeta: Path, sesion: sqlite3.Row) -> int | None:
    """La duración medida del audio (verificacion.json); si no está, la declarada o el último segmento."""
    verif = leer_json(carpeta / "verificacion.json") or {}
    if isinstance(verif, dict) and verif.get("duracion_ms"):
        return int(verif["duracion_ms"])
    if sesion["duracion_ms"]:
        return int(sesion["duracion_ms"])
    segmentos = leer_json(carpeta / "transcript.json") or []
    fines = [int(s.get("fin_ms") or 0) for s in segmentos if isinstance(s, dict)]
    return max(fines) if fines else None


# --- prompt --------------------------------------------------------------------------------------

def cargar_prompt(carpeta: Path | None = None) -> tuple[str, str]:
    """(sistema, mensaje) sin rellenar, de prompts/apunte.md (sin los comentarios <!-- -->)."""
    texto = _COMENTARIO.sub("", ((carpeta or PROMPTS_DIR) / f"{PROMPT}.md").read_text(encoding="utf-8"))
    if SEPARADOR not in texto:
        raise ErrorEtapa(f"prompts/{PROMPT}.md no tiene la línea {SEPARADOR!r}", reintentable=False)
    sistema, mensaje = texto.split(SEPARADOR, 1)
    return sistema.strip() + "\n", mensaje.strip() + "\n"


def armar_prompt(cfg: Config, materia: Materia, contexto: dict[str, Any], workdir: Path,
                 fotos: list[str], *, material: list[adjuntos.Preparado] | None = None,
                 carpeta_prompts: Path | None = None, web: WebCorrida | None = _DE_LA_CONFIG,
                 ) -> tuple[str, str, str, str]:
    """(sistema, mensaje, versión, contenido versionado) con las capas de §7 rellenas. `web`: la búsqueda en
    internet de la corrida (None: apagada; sin pasarla, la de la config); el bloque WEB va sólo si hay."""
    if web is _DE_LA_CONFIG:
        web = WebCorrida.desde_config(cfg)
    plantilla_sistema, plantilla_mensaje = cargar_prompt(carpeta_prompts)
    plantilla_sistema = con_bloque_web(plantilla_sistema, web is not None)
    sistema = rellenar(
        plantilla_sistema,
        max_busquedas=web.max_busquedas if web else None,
        max_paginas=web.max_paginas if web else None,
        motor_latex=cfg.apunte.motor_latex,
        max_intentos=cfg.apunte.max_intentos_compilacion,
        tope_min=f"{cfg.apunte.tope_correccion_min:g}",
        estilo_global=_o_nada(cfg.estilo_global),
        estilo_materia=_o_nada(materia.estilo),
    )
    contenido = sistema + "\n" + SEPARADOR + "\n" + plantilla_mensaje
    version = f"{PROMPT}@{sha256_texto(contenido)[:8]}"

    entrada = workdir / "input"
    texto_perfil = (entrada / "perfil.md").read_text(encoding="utf-8")
    if len(texto_perfil) > MAX_PERFIL:
        texto_perfil = texto_perfil[:MAX_PERFIL] + "\n\n[… sigue en input/perfil.md]"
    timeline = (entrada / "timeline.md").read_text(encoding="utf-8")
    if len(timeline) > MAX_TIMELINE:
        timeline = timeline[:MAX_TIMELINE] + "\n\n[… la línea de tiempo sigue en input/timeline.md: leela entera]"
    if fotos:
        lista = ", ".join(fotos) if len(fotos) <= 12 else f"{', '.join(fotos[:6])} … {fotos[-1]}"
        texto_fotos = f"{len(fotos)} en input/fotos/ ({lista})"
    else:
        texto_fotos = "no hay fotos de esta clase"
    carpeta_materia = materia.carpeta if materia.carpeta and materia.carpeta.is_dir() else None
    mensaje = rellenar(
        plantilla_mensaje,
        materia=materia.nombre,
        numero_clase=contexto["numero_clase"],
        fecha_larga=contexto["fecha_larga"],
        fecha=contexto["fecha"],
        tipo_clase=TIPOS_LEGIBLES.get(contexto["tipo_clase"] or "", contexto["tipo_clase"] or "sin indicar"),
        duracion=ms_a_hhmmss(contexto["duracion_ms"]) if contexto["duracion_ms"] else "desconocida",
        carpeta_materia=str(carpeta_materia) if carpeta_materia else "no hay",
        fotos=texto_fotos,
        adjuntos=texto_adjuntos(material or []),
        preambulo=preambulo(contexto),
        perfil=texto_perfil.strip(),
        clase_anterior=(entrada / "clase_anterior.md").read_text(encoding="utf-8").strip(),
        timeline=timeline.strip(),
    )
    return sistema, mensaje, version, contenido


def con_bloque_web(sistema: str, activa: bool) -> str:
    """El sistema con el bloque de la búsqueda en internet (sin sus líneas de marca) o sin él, entero: con la
    web apagada queda igual que antes del bloque."""
    return _MARCAS_WEB.sub("", sistema) if activa else _BLOQUE_WEB.sub("", sistema)


def texto_adjuntos(material: list[adjuntos.Preparado]) -> str:
    """{adjuntos} del mensaje: '2 en input/adjuntos/ (Slides_clase_5.pdf: PDF, 23 páginas; foto.jpg:
    imagen JPEG)', con los que no se pudieron preparar aparte, o 'no hay material adjunto'."""
    if not material:
        return "no hay material adjunto"
    listos = [p for p in material if p.ok]
    fallidos = [p for p in material if not p.ok]
    partes = []
    if listos:
        detalle = "; ".join(f"{p.nombre}: {adjuntos.describir_tipo(p.adjunto)}" for p in listos[:MAX_LISTA_ADJUNTOS])
        if len(listos) > MAX_LISTA_ADJUNTOS:
            detalle += f"; … y {len(listos) - MAX_LISTA_ADJUNTOS} más (ver input/timeline.md)"
        partes.append(f"{len(listos)} en input/adjuntos/ ({detalle})")
    else:
        partes.append("ninguno en input/adjuntos/")
    if fallidos:
        partes.append("no disponible (no lo uses): "
                      + "; ".join(f"{p.nombre}, {aviso_corto(p.aviso)}" for p in fallidos))
    return " · ".join(partes)


def preambulo(contexto: dict[str, Any]) -> str:
    lineas = [
        "\\documentclass{classmer}",
        f"\\materia{{{escapar_latex(contexto['materia'])}}}",
        f"\\fechaclase{{{contexto['fecha_larga']}}}",
        f"\\numeroclase{{{contexto['numero_clase']}}}",
    ]
    tipo = TIPOS_LEGIBLES.get(contexto.get("tipo_clase") or "")
    if tipo:
        lineas.append(f"\\tipoclase{{{tipo}}}")
    lineas.append("\\titulo{...}   % un título corto y descriptivo de la clase")
    return "\n".join(lineas)


def registrar_prompt(conn: sqlite3.Connection, version: str, contenido: str, zona: str) -> None:
    conn.execute(
        """
        INSERT INTO prompts (tarea, alcance, materia_id, version, contenido, activo, creado)
        VALUES (?, 'global', NULL, ?, ?, 1, ?)
        ON CONFLICT DO NOTHING
        """,
        (PROMPT, version, contenido, ahora_iso(zona)),
    )


_ESCAPES = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_",
            "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}


def escapar_latex(texto: str) -> str:
    return "".join(_ESCAPES.get(c, c) for c in texto)


def fecha_larga(fecha_iso: str) -> str:
    """'2026-10-15' → '15 de octubre de 2026'."""
    try:
        d = date.fromisoformat(fecha_iso[:10])
    except ValueError:
        return fecha_iso
    return f"{d.day} de {MESES[d.month - 1]} de {d.year}"


# --- salida del agente ---------------------------------------------------------------------------

def validar_salida(salida: Path, duracion_ms: int | None) -> list[str]:
    """Lo que se le marca al agente en los seguimientos: apunte.tex y meta.json."""
    errores: list[str] = []
    tex = salida / "apunte.tex"
    if not tex.is_file():
        errores.append("no existe output/apunte.tex")
    else:
        texto = re.sub(r"(?<!\\)%.*", "", tex.read_text(encoding="utf-8", errors="replace"))
        if not _CLASE_RE.search(texto):
            errores.append("apunte.tex tiene que usar \\documentclass{classmer}")
        if "\\end{document}" not in texto:
            errores.append("apunte.tex no tiene \\end{document}")
    errores += [f"meta.json: {e}" for e in validar_meta(salida / "meta.json", duracion_ms)]
    return errores


def meta_final(ruta: Path, tex: str, duracion_ms: int | None) -> tuple[dict[str, Any], str | None, Any]:
    """(meta válido, aviso o None, lo que escribió el agente si hubo que repararlo)."""
    datos, error = leer_meta(ruta)
    errores = [error] if error else validar_datos(datos, duracion_ms)
    if not errores:
        return _normalizar(datos), None, None
    meta = reparar_meta(datos, tex, duracion_ms)
    if validar_datos(meta, duracion_ms):
        meta = meta_desde_tex(tex, duracion_ms)
    if validar_datos(meta, duracion_ms):  # no debería pasar: último recurso
        meta = {"titulo": "Clase sin título", "resumen": "Apunte de la clase.", "secciones": [], "items": []}
    detalle = "; ".join(errores[:3]) + (f" (y {len(errores) - 3} más)" if len(errores) > 3 else "")
    que = "se armó uno mínimo desde el .tex" if datos is None else "se reparó con lo que se pudo salvar y el .tex"
    return _normalizar(meta), f"meta.json inválido ({detalle}): {que}", datos


def _normalizar(meta: dict[str, Any]) -> dict[str, Any]:
    """Tiempos como int (jsonschema acepta 12.0 como entero), textos sin espacios de más y el título
    en texto plano (sin LaTeX: va a nombres de carpeta, al índice y al panel; el .tex no se toca)."""
    meta = dict(meta)
    titulo = " ".join(str(meta["titulo"]).split())
    meta["titulo"] = limpiar_titulo(titulo) or titulo
    meta["resumen"] = str(meta["resumen"]).strip()
    meta["secciones"] = [{**s, "titulo": " ".join(s["titulo"].split()), "inicio_ms": int(s["inicio_ms"]),
                          "fin_ms": int(s["fin_ms"])} for s in meta["secciones"]]
    meta["items"] = [{**it, "texto": it["texto"].strip(),
                      "offset_ms": int(it["offset_ms"]) if it.get("offset_ms") is not None else None}
                     for it in meta["items"]]
    return meta


def _guardar_version(ctx: Contexto, sid: str, materia: Materia, salida: Path, comp: latex.ResultadoCompilacion,
                     meta: dict[str, Any], original: Any, resultado: ResultadoAgente, *, motor: str,
                     modelo: str | None, prompt_version: str, esfuerzo: str | None = None,
                     facturacion: str | None = None, adjuntos_usados: list[str] | None = None) -> int:
    """Copia a sesiones/<id>/apunte/vN/ y registra vN (actual) con secciones e items. Devuelve N.
    `adjuntos_usados`: los ids que tomó la corrida (apuntes.adjuntos; de ahí sale usado/sin_usar)."""
    cfg = ctx.cfg
    raiz = ctx.sesion_dir(sid) / "apunte"
    with ctx.base.conexion() as conn:
        n = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM apuntes WHERE sesion_id = ?", (sid,)).fetchone()[0]
    destino = raiz / f"v{n}"
    temporal = raiz / f".v{n}.tmp"
    for d in (temporal, destino):  # restos de una corrida que se cortó antes de registrar vN
        if d.exists():
            _borrar(d)
    temporal.mkdir(parents=True)
    shutil.copyfile(salida / "apunte.tex", temporal / "apunte.tex")
    if comp.ok and comp.pdf is not None:
        shutil.copyfile(comp.pdf, temporal / "apunte.pdf")
    escribir_json(temporal / "meta.json", meta)
    if original is not None:
        escribir_json(temporal / "meta.agente.json", original)
    if comp.ruta_log is not None and comp.ruta_log.is_file():
        shutil.copyfile(comp.ruta_log, temporal / "compilacion.log")
    else:
        escribir_atomico(temporal / "compilacion.log", comp.log + "\n")
    shutil.copyfile(salida / latex.CLS, temporal / latex.CLS)
    escribir_atomico(temporal / "agente.log", resultado.log + "\n")
    os.replace(temporal, destino)

    momento = ahora_iso(cfg.zona_horaria)
    with ctx.base.transaccion() as conn:
        conn.execute("UPDATE apuntes SET actual = 0 WHERE sesion_id = ?", (sid,))
        apunte_id = conn.execute(
            """
            INSERT INTO apuntes (sesion_id, version, actual, motor, modelo, esfuerzo, facturacion, prompt_version,
                                 ruta_tex, ruta_pdf, compilo, titulo, resumen, costo, creado, adjuntos)
            VALUES (?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (sid, n, motor, modelo, esfuerzo, facturacion, prompt_version, f"apunte/v{n}/apunte.tex",
             f"apunte/v{n}/apunte.pdf" if comp.ok else None, int(comp.ok), meta["titulo"], meta["resumen"],
             resultado.costo_usd, momento, json.dumps(list(adjuntos_usados or []))),
        ).lastrowid
        for orden, s in enumerate(meta["secciones"], 1):
            conn.execute(
                "INSERT INTO secciones (apunte_id, orden, titulo, inicio_ms, fin_ms, texto_plano) VALUES (?, ?, ?, ?, ?, ?)",
                (apunte_id, orden, s["titulo"], s["inicio_ms"], s["fin_ms"], s.get("texto_plano")),
            )
        _guardar_items(conn, sid, materia.id, apunte_id, meta["items"], momento)
        actualizar_sesion(conn, sid, cfg.zona_horaria, titulo=meta["titulo"])
    return n


def _guardar_items(conn: sqlite3.Connection, sid: str, materia_id: str, apunte_id: int,
                   items: list[dict[str, Any]], momento: str) -> None:
    """Reemplaza los items abiertos de la sesión. Los que el usuario ya marcó (hecho/descartado) se
    conservan: si el apunte nuevo trae el mismo item, se actualiza ése en vez de duplicarlo."""
    def clave(tipo: str, texto: str) -> tuple[str, str]:
        return tipo, " ".join(texto.casefold().split())

    marcados = {clave(f["tipo"], f["texto"]): f["id"] for f in conn.execute(
        "SELECT id, tipo, texto FROM items WHERE sesion_id = ? AND estado != 'abierto' ORDER BY id", (sid,))}
    conn.execute("DELETE FROM items WHERE sesion_id = ? AND estado = 'abierto'", (sid,))
    for it in items:
        previo = marcados.pop(clave(it["tipo"], it["texto"]), None)
        if previo is not None:
            conn.execute("UPDATE items SET apunte_id = ?, offset_ms = ?, fecha_limite = ? WHERE id = ?",
                         (apunte_id, it.get("offset_ms"), it.get("fecha_limite"), previo))
            continue
        conn.execute(
            """
            INSERT INTO items (materia_id, sesion_id, apunte_id, tipo, texto, fecha_limite, offset_ms, estado, creado)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'abierto', ?)
            """,
            (materia_id, sid, apunte_id, it["tipo"], it["texto"], it.get("fecha_limite"), it.get("offset_ms"), momento),
        )
