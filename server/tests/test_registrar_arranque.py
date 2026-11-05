"""scripts/registrar-arranque.ps1: qué cierra, reintentos de -Correr, -Reiniciar, -Parar y el alta.

El script se corre en Windows PowerShell 5.1 con TODOS los cmdlets con efectos reemplazados por
funciones falsas (las funciones le ganan a los cmdlets): ni procesos reales, ni tareas reales.
Los procesos son una lista inventada con PIDs que no existen. El candado sí es un archivo de
verdad, en la carpeta de datos temporal.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "registrar-arranque.ps1"
POWERSHELL = shutil.which("powershell.exe") if sys.platform == "win32" else None

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="falta Windows PowerShell")

MOCKS = [
    "Get-CimInstance", "Stop-Process", "Wait-Process", "Start-Process", "Start-Sleep", "Write-Warning",
    "Get-ScheduledTask", "Stop-ScheduledTask", "Start-ScheduledTask", "Enable-ScheduledTask",
    "Disable-ScheduledTask", "Unregister-ScheduledTask", "Register-ScheduledTask", "Get-ScheduledTaskInfo",
    "Get-NetTCPConnection",
]

# Mocks de todo lo que tiene efectos. Si alguno no queda activo, el arnés sale sin correr nada.
# ScheduledTasks y NetTCPIP son módulos CDXML: sus comandos son funciones, y si se autocargan a
# mitad de camino (p. ej. con New-ScheduledTaskAction al registrar) pisan los mocks. Pasó: un
# test registró una tarea de verdad. Por eso se importan antes de definir los mocks.
ARNES = r"""
$ErrorActionPreference = 'Stop'
Import-Module ScheduledTasks, NetTCPIP
$Script = '__SCRIPT__'
$SalidaJson = '__SALIDA__'
$global:Llamadas = New-Object System.Collections.ArrayList
$global:Arranques = 0
$global:CodigosArranque = @(__CODIGOS__)
$global:EsperasDelServer = __ESPERAS__      # cuántas veces WaitForExit(ms) dice "sigue andando"
$global:HayTarea = $__HAY_TAREA__
$global:Deshabilitada = $__DESHABILITADA__
$global:ArgumentosTarea = '__ARGUMENTOS_TAREA__'
$global:EjecutableTarea = '__EJECUTABLE_TAREA__'
$global:TareaParada = $false
$global:Corridas = 0                       # corridas nuevas que arrancó el Programador
$global:Ignorar = __IGNORAR__              # cuántos Start-ScheduledTask se ignoran (IgnoreNew)
$global:Escucha = $__ESCUCHA__             # si el server llega a escuchar después de arrancar
$global:OcupadoVeces = __OCUPADO__         # cuántas consultas del puerto lo ven ocupado de entrada
$global:Oyente = __OYENTE__                # el PID que escucha en el puerto
$global:Registro = $null
$global:HomeAlArrancar = $null

function global:Anotar([string] $Texto) { [void]$global:Llamadas.Add($Texto) }

# El lanzador (ClassmerTrabajo, que el script compila si no existe) falso: anota y devuelve los
# códigos de __CODIGOS__ (999 = falla al lanzar). Sin lanzador, el script usa Start-Process.
if ($__SIN_LANZADOR__) {
    function global:Add-Type {
        [CmdletBinding()] param([string] $TypeDefinition)
        Anotar 'Add-Type'
        throw 'no compila'
    }
} elseif ($__LANZADOR_VIEJO__) {
    # Uno de una versión anterior del script, cargado en la misma consola: no se puede reemplazar.
    Add-Type -TypeDefinition @'
using System;
public static class ClassmerTrabajo {
    public static IntPtr Crear(IntPtr proceso) { return IntPtr.Zero; }
    public static void Terminar(IntPtr trabajo) { }
}
'@
} else {
    Add-Type -TypeDefinition @'
using System;
public sealed class ClassmerProceso { public int Codigo; }
public static class ClassmerTrabajo {
    public static Func<string, string, bool, int> AlLanzar;
    public static Func<bool> AlEsperar;
    public static Action<string> Anotar;
    public static ClassmerProceso Lanzar(string linea, string carpeta, bool sinVentana) {
        ClassmerProceso p = new ClassmerProceso(); p.Codigo = AlLanzar(linea, carpeta, sinVentana); return p;
    }
    public static bool Esperar(ClassmerProceso p, int milisegundos) { return AlEsperar(); }
    public static int Codigo(ClassmerProceso p) { return p.Codigo; }
    public static void Terminar(ClassmerProceso p) { Anotar("Terminar"); }
    public static IntPtr Adoptar(int pid) { Anotar("Adoptar " + pid); return new IntPtr(pid); }
    public static void TerminarTrabajo(IntPtr trabajo) { Anotar("TerminarTrabajo " + trabajo.ToInt64()); }
}
'@
    [ClassmerTrabajo]::AlLanzar = [Func[string, string, bool, int]] {
        param($linea, $carpeta, $sinVentana)
        $codigo = 0
        if ($global:Arranques -lt $global:CodigosArranque.Count) { $codigo = $global:CodigosArranque[$global:Arranques] }
        $global:Arranques++
        $global:HomeAlArrancar = $env:CLASSMER_HOME
        Anotar "Lanzar $linea"
        Anotar "SinVentana $sinVentana"
        if ($codigo -eq 999) { throw 'falla al lanzar' }
        return $codigo
    }
    [ClassmerTrabajo]::AlEsperar = [Func[bool]] {
        if ($global:EsperasDelServer -gt 0) { $global:EsperasDelServer--; [Threading.Thread]::Sleep(200); return $false }
        return $true
    }
    [ClassmerTrabajo]::Anotar = [Action[string]] { param($texto) Anotar $texto }
}

# Con -ConsolaReal, el padre de este proceso es un "conhost" que en realidad es un ping de verdad
# (sin ventana) que termina solo en un par de segundos: como cerrar la consola de la tarea.
$global:PadrePropio = 4
$global:FalsoConhost = 70098
if ($__CONSOLA_REAL__) {
    $psi = New-Object Diagnostics.ProcessStartInfo 'ping.exe', '-n 3 127.0.0.1'
    $psi.UseShellExecute = $false; $psi.CreateNoWindow = $true; $psi.RedirectStandardOutput = $true
    $global:ConsolaReal = [Diagnostics.Process]::Start($psi)
    $global:PadrePropio = $global:ConsolaReal.Id
    $global:FalsoConhost = $global:ConsolaReal.Id
}

$global:ProcesosFalsos = @(
    [pscustomobject]@{ Name = 'System'; ProcessId = [uint32]4; ParentProcessId = [uint32]0; CommandLine = $null }
    [pscustomobject]@{ Name = 'conhost.exe'; ProcessId = [uint32]$global:FalsoConhost; ParentProcessId = [uint32]4
        CommandLine = 'conhost.exe --headless powershell.exe ...' }
    # este mismo proceso (el -Correr actual) y su arbol: nunca se cierran
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]$PID; ParentProcessId = [uint32]$global:PadrePropio
        CommandLine = 'powershell.exe -NoProfile -File "C:\Classmer\server\scripts\registrar-arranque.ps1" -Correr -Uv "C:\uv.exe"' }
    [pscustomobject]@{ Name = 'cmd.exe'; ProcessId = [uint32]70001; ParentProcessId = [uint32]$PID
        CommandLine = 'cmd.exe /d /c ""C:\uv.exe" run classmer serve >> "__LOG__" 2>&1"' }
    [pscustomobject]@{ Name = 'uv.exe'; ProcessId = [uint32]70010; ParentProcessId = [uint32]70001
        CommandLine = '"C:\uv.exe" run classmer serve' }
    # el -Correr que tiene el candado en las pruebas de -Reiniciar (entre dos arranques, sin server)
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]70002; ParentProcessId = [uint32]4
        CreationDate = (Get-Date '2026-10-26T10:00:00')
        CommandLine = 'powershell.exe -NoProfile -File "C:\Classmer\server\scripts\registrar-arranque.ps1" -Correr -Uv "C:\uv.exe"' }
    # su "hijo" por un PID reciclado: nacio antes, no es de el
    [pscustomobject]@{ Name = 'notepad.exe'; ProcessId = [uint32]70050; ParentProcessId = [uint32]70002
        CreationDate = (Get-Date '2026-10-26T09:00:00'); CommandLine = 'notepad.exe' }
    # server huerfano de esta carpeta de datos: su -Correr murio (cmd -> uv -> classmer -> python -> python -> claude)
    [pscustomobject]@{ Name = 'cmd.exe'; ProcessId = [uint32]70008; ParentProcessId = [uint32]70099
        CommandLine = 'cmd.exe /d /c ""C:\uv.exe" run classmer serve >> "__LOG__" 2>&1"' }
    [pscustomobject]@{ Name = 'uv.exe'; ProcessId = [uint32]70003; ParentProcessId = [uint32]70008
        CommandLine = '"C:\uv.exe" run classmer serve' }
    [pscustomobject]@{ Name = 'classmer.exe'; ProcessId = [uint32]70004; ParentProcessId = [uint32]70003
        CommandLine = '"C:\Classmer\server\.venv\Scripts\classmer.exe" serve' }
    [pscustomobject]@{ Name = 'python.exe'; ProcessId = [uint32]70005; ParentProcessId = [uint32]70004
        CommandLine = '"C:\Classmer\server\.venv\Scripts\python.exe" "C:\Classmer\server\.venv\Scripts\classmer.exe" serve' }
    [pscustomobject]@{ Name = 'python.exe'; ProcessId = [uint32]70013; ParentProcessId = [uint32]70005
        CommandLine = '"C:\Python\python.exe" "C:\Classmer\server\.venv\Scripts\classmer.exe" serve' }
    [pscustomobject]@{ Name = 'claude.exe'; ProcessId = [uint32]70014; ParentProcessId = [uint32]70013
        CommandLine = 'claude.exe -p --output-format stream-json' }
    # server de prueba de otro worktree, lanzado desde bash
    [pscustomobject]@{ Name = 'bash.exe'; ProcessId = [uint32]70019; ParentProcessId = [uint32]4
        CommandLine = 'bash.exe -c "uv run classmer serve --port 8777"' }
    [pscustomobject]@{ Name = 'uv.exe'; ProcessId = [uint32]70020; ParentProcessId = [uint32]70019
        CommandLine = '"C:\uv.exe" run classmer serve --port 8777' }
    [pscustomobject]@{ Name = 'classmer.exe'; ProcessId = [uint32]70021; ParentProcessId = [uint32]70020
        CommandLine = '"C:\wt\server\.venv\Scripts\classmer.exe" serve --port 8777' }
    [pscustomobject]@{ Name = 'python.exe'; ProcessId = [uint32]70022; ParentProcessId = [uint32]70021
        CommandLine = '"C:\Python\python.exe" "C:\wt\server\.venv\Scripts\classmer.exe" serve --port 8777' }
    # el -Correr de otra carpeta de datos
    [pscustomobject]@{ Name = 'cmd.exe'; ProcessId = [uint32]70030; ParentProcessId = [uint32]4
        CommandLine = 'cmd.exe /d /c ""C:\uv.exe" run classmer serve >> "C:\otra\logs\server-consola.log" 2>&1"' }
    [pscustomobject]@{ Name = 'uv.exe'; ProcessId = [uint32]70031; ParentProcessId = [uint32]70030
        CommandLine = '"C:\uv.exe" run classmer serve' }
    # comandos que solo nombran "classmer serve", y ajenos
    [pscustomobject]@{ Name = 'bash.exe'; ProcessId = [uint32]70040; ParentProcessId = [uint32]4
        CommandLine = 'bash.exe -c "grep -n ''classmer serve'' README.md"' }
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]70041; ParentProcessId = [uint32]4
        CommandLine = 'powershell.exe -Command "Get-CimInstance Win32_Process | ? CommandLine -match ''classmer serve''"' }
    [pscustomobject]@{ Name = 'notepad.exe'; ProcessId = [uint32]70006; ParentProcessId = [uint32]4
        CommandLine = 'notepad.exe C:\Classmer\server\notas.txt' }
    [pscustomobject]@{ Name = 'classmer.exe'; ProcessId = [uint32]70007; ParentProcessId = [uint32]4
        CommandLine = '"C:\Classmer\server\.venv\Scripts\classmer.exe" estado' }
__EXTRA__
)

function global:Get-CimInstance {
    [CmdletBinding()] param([Parameter(Position = 0)] [string] $ClassName, [string] $Filter)
    Anotar "Get-CimInstance $ClassName"
    $global:ProcesosFalsos
}
function global:Stop-Process {
    [CmdletBinding()] param([int[]] $Id, [switch] $Force)
    foreach ($i in $Id) { Anotar "Stop-Process $i" }
}
function global:Wait-Process {
    [CmdletBinding()] param([int[]] $Id, [int] $Timeout)
    Anotar ('Wait-Process ' + ($Id -join ','))
}
function global:Start-Process {
    [CmdletBinding()] param([string] $FilePath, [object] $ArgumentList, [switch] $NoNewWindow, [switch] $Wait, [switch] $PassThru)
    $codigo = 0
    if ($global:Arranques -lt $global:CodigosArranque.Count) { $codigo = $global:CodigosArranque[$global:Arranques] }
    $global:Arranques++
    $global:HomeAlArrancar = $env:CLASSMER_HOME
    $sufijo = ''
    if ($Wait) { $sufijo = ' -Wait' }
    Anotar "Start-Process $ArgumentList$sufijo"
    $p = [pscustomobject]@{ ExitCode = $codigo; Handle = [IntPtr]::Zero }
    $p | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value {
        param($ms)
        if ($global:EsperasDelServer -gt 0) { $global:EsperasDelServer--; [Threading.Thread]::Sleep(200); return $false }
        return $true
    }
    $p
}
function global:Start-Sleep {
    [CmdletBinding()] param([Parameter(Position = 0)] [int] $Seconds, [int] $Milliseconds)
    if ($PSBoundParameters.ContainsKey('Milliseconds')) { Anotar "Start-Sleep ms $Milliseconds" } else { Anotar "Start-Sleep $Seconds" }
    if ($__SLEEP_REVIENTA__) { [int]::Parse('x') }   # un error que se escapa hasta del catch de la vuelta
}
function global:Write-Warning {
    [CmdletBinding()] param([Parameter(Position = 0)] [string] $Message)
    Anotar "Write-Warning $Message"
}
function global:Get-ScheduledTask {
    [CmdletBinding()] param([string] $TaskName)
    Anotar "Get-ScheduledTask $TaskName"
    if (-not $global:HayTarea) { return }
    $estado = 'Running'
    if ($global:TareaParada) { $estado = 'Ready' }
    [pscustomobject]@{
        TaskName = $TaskName; State = $estado
        Actions = @([pscustomobject]@{ Execute = $global:EjecutableTarea; Arguments = $global:ArgumentosTarea })
        Triggers = @([pscustomobject]@{ Repetition = $null }, [pscustomobject]@{ Repetition = [pscustomobject]@{ Interval = 'PT5M' } })
        Settings = [pscustomobject]@{ Enabled = -not $global:Deshabilitada }
    }
}
function global:Stop-ScheduledTask {
    [CmdletBinding()] param([string] $TaskName)
    Anotar "Stop-ScheduledTask $TaskName"
    $global:TareaParada = $true
}
function global:Start-ScheduledTask {
    [CmdletBinding()] param([string] $TaskName)
    Anotar "Start-ScheduledTask $TaskName"
    if ($global:Ignorar -gt 0) { $global:Ignorar--; return }   # el Programador lo ignora en silencio
    $global:Corridas++
}
function global:Enable-ScheduledTask {
    [CmdletBinding()] param([string] $TaskName)
    Anotar "Enable-ScheduledTask $TaskName"
    $global:Deshabilitada = $false
}
function global:Disable-ScheduledTask {
    [CmdletBinding()] param([string] $TaskName)
    Anotar "Disable-ScheduledTask $TaskName"
    $global:Deshabilitada = $true
}
function global:Get-ScheduledTaskInfo {
    [CmdletBinding()] param([string] $TaskName)
    [pscustomobject]@{ LastRunTime = (Get-Date '2026-10-14T14:59:47').AddMinutes($global:Corridas) }
}
function global:Get-NetTCPConnection {
    [CmdletBinding()] param([int] $LocalPort, [string] $State)
    Anotar "Get-NetTCPConnection $LocalPort"
    $escucha = $false
    if ($global:OcupadoVeces -gt 0) { $global:OcupadoVeces--; $escucha = $true }
    elseif ($global:Escucha -and $global:Corridas -gt 0) { $escucha = $true }
    if ($escucha) { [pscustomobject]@{ LocalPort = $LocalPort; State = 'Listen'; OwningProcess = [uint32]$global:Oyente } }
}
function global:Unregister-ScheduledTask {
    [CmdletBinding(SupportsShouldProcess = $true)] param([string] $TaskName)
    Anotar "Unregister-ScheduledTask $TaskName"
}
function global:Register-ScheduledTask {
    [CmdletBinding()] param([string] $TaskName, $Description, $Action, $Trigger, $Principal, $Settings, [switch] $Force)
    Anotar "Register-ScheduledTask $TaskName"
    $global:Registro = @{
        ejecutable = $Action.Execute; argumentos = $Action.Arguments; carpeta = $Action.WorkingDirectory
        instancias = [string]$Settings.MultipleInstances
        disparadores = @($Trigger | ForEach-Object {
            $d = @{ tipo = $_.CimClass.CimClassName; inicio = $_.StartBoundary; intervalo = $null }
            if ($_.Repetition) {
                $d.intervalo = $_.Repetition.Interval; $d.duracion = $_.Repetition.Duration
                $d.corta_al_final = $_.Repetition.StopAtDurationEnd
            }
            $d })
    }
}

foreach ($n in @(__MOCKS__)) {
    $c = Get-Command $n
    if ($c.CommandType -ne 'Function' -or $c.ModuleName) { [Console]::Error.WriteLine("mock $n no activo"); exit 99 }
}

# Otro -Correr vivo: tiene el candado con su PID.
if ('__CANDADO__') {
    New-Item -ItemType Directory -Force -Path (Split-Path '__CANDADO_RUTA__') | Out-Null
    $global:CandadoAjeno = [IO.File]::Open('__CANDADO_RUTA__', 'OpenOrCreate', 'ReadWrite', 'Read')
    $b = [Text.Encoding]::ASCII.GetBytes('__CANDADO__')
    $global:CandadoAjeno.Write($b, 0, $b.Length)
    $global:CandadoAjeno.Flush()
}

$global:LASTEXITCODE = $null
$errorTexto = $null
# Sin try, como corre la tarea: un error que se escapa sigue de largo en vez de cortar.
__INVOCACION_SUELTA__
try {
    __INVOCACION__
} catch {
    $errorTexto = $_.Exception.Message
}
# ¿El script soltó el candado al terminar (aunque sea con exit)?
$candadoLibre = $null
if (Test-Path -LiteralPath '__CANDADO_PROPIO__') {
    try { [IO.File]::Open('__CANDADO_PROPIO__', 'Open', 'ReadWrite', 'None').Dispose(); $candadoLibre = $true } catch { $candadoLibre = $false }
}
$salida = @{ llamadas = @($global:Llamadas); codigo = $global:LASTEXITCODE; error = $errorTexto; pid = $PID
    registro = $global:Registro; home_al_arrancar = $global:HomeAlArrancar; candado_libre = $candadoLibre }
[IO.File]::WriteAllText($SalidaJson, (ConvertTo-Json -InputObject $salida -Depth 6))
"""

CONHOST = r"C:\WINDOWS\System32\conhost.exe"


def _argumentos(puerto: int, datos: Path | None) -> str:
    texto = (r'--headless "C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile '
             rf'-File "{SCRIPT}" -Correr -Uv "C:\uv.exe" -Puerto {puerto}')
    return texto + (f' -Datos "{datos}"' if datos else "")


def _correr(
    tmp_path: Path, invocacion: str, *, codigos: tuple[int, ...] = (), hay_tarea: bool = True,
    deshabilitada: bool = False, ignorados: int = 0, escucha: bool = True, ocupado: int = 0,
    oyente: int = 70013, candado: int | None = None, argumentos_tarea: str | None = None,
    ejecutable_tarea: str = CONHOST, extra: str = "", esperas: int = 0, consola_real: bool = False,
    candado_propio: Path | None = None, sin_lanzador: bool = False, sin_try: bool = False,
    lanzador_viejo: bool = False, sleep_revienta: bool = False,
) -> dict:
    datos = tmp_path / "Classmer-data"
    salida = tmp_path / "salida.json"
    if argumentos_tarea is None:
        # La tarea "de producción" de las pruebas apunta a la carpeta temporal, nunca a la real.
        argumentos_tarea = _argumentos(8000, datos)
    arnes = (
        ARNES.replace("__SCRIPT__", str(SCRIPT))
        .replace("__SALIDA__", str(salida))
        .replace("__EXTRA__", extra)
        .replace("__LOG__", str(datos / "logs" / "server-consola.log"))
        .replace("__MOCKS__", ", ".join(f"'{m}'" for m in MOCKS))
        .replace("__CODIGOS__", ", ".join(str(c) for c in codigos))
        .replace("__ESPERAS__", str(esperas))
        .replace("__HAY_TAREA__", "true" if hay_tarea else "false")
        .replace("__DESHABILITADA__", "true" if deshabilitada else "false")
        .replace("__ARGUMENTOS_TAREA__", argumentos_tarea.replace("'", "''"))
        .replace("__EJECUTABLE_TAREA__", ejecutable_tarea)
        .replace("__IGNORAR__", str(ignorados))
        .replace("__ESCUCHA__", "true" if escucha else "false")
        .replace("__OCUPADO__", str(ocupado))
        .replace("__OYENTE__", str(oyente))
        .replace("__CONSOLA_REAL__", "true" if consola_real else "false")
        .replace("__SIN_LANZADOR__", "true" if sin_lanzador else "false")
        .replace("__LANZADOR_VIEJO__", "true" if lanzador_viejo else "false")
        .replace("__SLEEP_REVIENTA__", "true" if sleep_revienta else "false")
        .replace("__CANDADO_PROPIO__", str(candado_propio or datos / "server-correr.lock"))
        .replace("__CANDADO_RUTA__", str(datos / "server-correr.lock"))
        .replace("__CANDADO__", "" if candado is None else str(candado))
        .replace("__INVOCACION_SUELTA__", invocacion if sin_try else "")
        .replace("__INVOCACION__", "" if sin_try else invocacion)
    )
    ruta = tmp_path / "arnes.ps1"
    ruta.write_text(arnes, encoding="utf-8-sig")
    env = {**os.environ, "CLASSMER_HOME": str(datos)}
    r = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(ruta)],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert r.returncode == 0, r.stderr
    resultado = json.loads(salida.read_text(encoding="utf-8-sig"))
    resultado["log"] = datos / "logs" / "server-consola.log"
    resultado["candado"] = datos / "server-correr.lock"
    return resultado


def _cerrados(llamadas: list[str]) -> list[int]:
    return [int(l.split()[1]) for l in llamadas if l.startswith("Stop-Process ")]


def _avisos(llamadas: list[str]) -> list[str]:
    return [l.removeprefix("Write-Warning ") for l in llamadas if l.startswith("Write-Warning ")]


# El server huérfano de esta carpeta de datos, de arriba hacia abajo (con el claude que lanzó).
HUERFANO = [70008, 70003, 70004, 70005, 70013, 70014]
BUCLE_CON_CANDADO = 70002
# Lo que nunca se cierra: el árbol propio, el "hijo" por PID reciclado, el server de prueba de
# otro worktree, el de otra carpeta de datos, comandos que nombran "classmer serve" y ajenos.
INTOCABLES = {70001, 70010, 70050, 70019, 70020, 70021, 70022, 70030, 70031, 70040, 70041, 70006, 70007}


def test_el_script_parsea_en_powershell_51_y_tiene_bom():
    assert SCRIPT.read_bytes()[:3] == b"\xef\xbb\xbf"  # sin BOM, PS 5.1 rompe los acentos
    r = subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
            "$e = $null; [void][System.Management.Automation.Language.Parser]::ParseFile("
            f"'{SCRIPT}', [ref]$null, [ref]$e); $e.Count",
        ],
        capture_output=True, text=True, timeout=60,
    )
    assert r.stdout.strip() == "0", r.stdout + r.stderr


def test_todo_lo_que_tiene_efectos_esta_simulado():
    # Si el script empieza a usar otro cmdlet con efectos (p. ej. Set-ScheduledTask) y nadie lo
    # agrega al arnés, los tests lo correrían de verdad contra la tarea "Classmer server".
    r = subprocess.run(
        [
            POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
            "$ast = [System.Management.Automation.Language.Parser]::ParseFile("
            f"'{SCRIPT}', [ref]$null, [ref]$null); "
            "$ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true) | "
            "ForEach-Object { $_.GetCommandName() } | Sort-Object -Unique",
        ],
        capture_output=True, text=True, timeout=60,
    )
    usados = {l.strip() for l in r.stdout.splitlines() if l.strip()}
    assert "Register-ScheduledTask" in usados, r.stderr
    peligrosos = {
        c for c in usados
        if re.match(r"(Start|Stop|Register|Unregister|Enable|Disable|Restart|Remove|Set)-", c, re.I)
        or (re.search(r"-ScheduledTask|-NetTCP|-CimInstance", c, re.I) and not c.startswith("New-"))
    } - {"Set-Location", "Set-StrictMode"}
    assert peligrosos <= set(MOCKS), peligrosos - set(MOCKS)


def test_el_lanzador_real_espera_solo_a_cmd_y_cierra_lo_que_queda(tmp_path: Path):
    # El C# del script, compilado de verdad: cmd deja un ping huérfano y termina con 7. Esperar
    # vuelve sin esperar al ping, Codigo da 7 y Terminar cierra el ping (el job object).
    texto = SCRIPT.read_text(encoding="utf-8-sig")
    codigo_cs = re.search(r"Add-Type -ErrorAction Stop -TypeDefinition @'\n(.*?)\n'@", texto, re.S).group(1)
    (tmp_path / "lanzador.cs").write_text(codigo_cs, encoding="utf-8")
    prueba = tmp_path / "prueba.ps1"
    prueba.write_text(r"""
$ErrorActionPreference = 'Stop'
Add-Type -TypeDefinition (Get-Content -Raw -LiteralPath (Join-Path $PSScriptRoot 'lanzador.cs'))
$marca = { param($w) @(Get-CimInstance Win32_Process -Filter "Name = 'PING.EXE'" | Where-Object { $_.CommandLine -match "-w $w" }) }
$p = [ClassmerTrabajo]::Lanzar(('"{0}" /d /c "start "" /b ping -n 37 -w 1007 127.0.0.1 >nul & exit /b 7"' -f $env:ComSpec), $env:TEMP, $true)
$desde = Get-Date
while (-not [ClassmerTrabajo]::Esperar($p, 5000)) { }
$segundos = ((Get-Date) - $desde).TotalSeconds
$codigo = [ClassmerTrabajo]::Codigo($p)
Start-Sleep -Milliseconds 300
$antes = @(& $marca 1007).Count
[ClassmerTrabajo]::Terminar($p)
Start-Sleep -Milliseconds 800
$despues = @(& $marca 1007).Count
# Adoptar: un proceso que ya está en otro job (el de Lanzar) va a uno propio, anidado.
$q = [ClassmerTrabajo]::Lanzar(('"{0}" /d /c "ping -n 37 -w 1009 127.0.0.1 >nul"' -f $env:ComSpec), $env:TEMP, $true)
Start-Sleep -Milliseconds 800
$ping = @(& $marca 1009)
$trabajo = [ClassmerTrabajo]::Adoptar([int]$ping[0].ProcessId)
[ClassmerTrabajo]::TerminarTrabajo($trabajo)
Start-Sleep -Milliseconds 800
$adoptadoVivo = @(& $marca 1009).Count
[ClassmerTrabajo]::Terminar($q)
'{0} {1} {2} {3} {4} {5}' -f $codigo, $antes, $despues, [int]$segundos, [int]($trabajo -ne [IntPtr]::Zero), $adoptadoVivo
""", encoding="utf-8-sig")
    r = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(prueba)],
        capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr
    codigo, antes, despues, segundos, adopto, adoptado_vivo = (int(x) for x in r.stdout.split())
    assert codigo == 7
    assert segundos < 10  # no esperó al ping (37 s)
    assert (antes, despues) == (1, 0)
    assert (adopto, adoptado_vivo) == (1, 0)


def test_correr_cierra_restos_antes_de_arrancar_y_no_se_rinde(tmp_path: Path):
    uv = tmp_path / "uv.exe"
    uv.write_bytes(b"")
    # 5 caidas rapidas seguidas y despues una salida limpia.
    r = _correr(tmp_path, f"& $Script -Correr -Uv '{uv}'", codigos=(1, 1, 1, 1, 1, 0))
    assert r["error"] is None
    llamadas = r["llamadas"]
    arranques = [i for i, l in enumerate(llamadas) if l.startswith("Lanzar ")]
    # No sale con exit 1 a la quinta: espera 5 minutos y vuelve a probar.
    assert len(arranques) == 6
    assert r["codigo"] == 0
    esperas = [l for l in llamadas if l.startswith("Start-Sleep")]
    assert esperas == ["Start-Sleep 30"] * 4 + ["Start-Sleep 300"]
    # Antes de cada arranque cierra el server huerfano de esta carpeta (no escucha); nunca su
    # propio arbol, ni el -Correr de otra carpeta, ni servers o comandos ajenos.
    previo = 0
    for i in arranques:
        assert _cerrados(llamadas[previo:i]) == HUERFANO
        previo = i
    assert not ({r["pid"], BUCLE_CON_CANDADO} | INTOCABLES) & set(_cerrados(llamadas))
    assert f'"{os.environ["ComSpec"]}" /d /c ""{uv}" run classmer serve >> "' in llamadas[arranques[0]]
    # Después de cada caída, el job object cierra lo que haya quedado colgando del server.
    assert llamadas.count("Terminar") == 6
    assert not any(l.startswith("Start-Process") for l in llamadas)
    log = r["log"].read_text(encoding="utf-8-sig")
    assert "cierro restos de un server anterior" in log
    assert "espero 5 minutos" in log
    assert r["candado"].read_text() == str(r["pid"])  # el candado dice quién es
    assert r["candado_libre"] is True  # y lo suelta al salir con exit


def test_correr_espera_al_server_sin_relanzarlo(tmp_path: Path):
    # Mientras cmd sigue vivo, -Correr sólo mira cada 5 s si sigue teniendo consola.
    r = _correr(tmp_path, "& $Script -Correr", codigos=(0,), esperas=3)
    assert r["error"] is None
    assert sum(l.startswith("Lanzar ") for l in r["llamadas"]) == 1
    # A mano (sin el conhost de la tarea) comparte la consola: Ctrl+C o cerrarla lo cortan también.
    assert "SinVentana False" in r["llamadas"]
    assert "el server terminó con código 0" in r["log"].read_text(encoding="utf-8-sig")


def test_correr_sale_si_se_cierra_la_consola_y_deja_el_server(tmp_path: Path):
    # 26/10, probado a mano: si se cierra el conhost de la tarea, el Programador deja de verla
    # corriendo y -Correr quedaba sin consola con el candado tomado. Ahora sale, lo suelta y deja
    # el server (que tiene su propia consola) para el -Correr siguiente.
    r = _correr(tmp_path, "& $Script -Correr", codigos=(1,), esperas=100, consola_real=True)
    assert r["error"] is None
    assert r["codigo"] == 0
    assert sum(l.startswith("Lanzar ") for l in r["llamadas"]) == 1
    assert "SinVentana True" in r["llamadas"]  # bajo la tarea, el server tiene su propia consola
    assert "Terminar" not in r["llamadas"]  # no lo cerró: sigue andando
    log = (r["log"].parent / "server-correr.log")
    texto = r["log"].read_text(encoding="utf-8-sig") + (log.read_text(encoding="utf-8-sig") if log.exists() else "")
    assert "se cerró la consola de la tarea" in texto
    assert "el server terminó" not in texto
    assert r["candado_libre"] is True


def test_correr_sigue_si_algo_falla_y_nunca_pasa_a_registrar(tmp_path: Path):
    # Sin try alrededor, como en la tarea: antes, un error dentro de -Correr seguía de largo hasta
    # el alta y registraba "Classmer server" con el puerto y la carpeta de la prueba.
    r = _correr(tmp_path, "& $Script -Correr", codigos=(999, 0), sin_try=True)
    llamadas = r["llamadas"]
    assert sum(l.startswith("Lanzar ") for l in llamadas) == 2
    assert not any(l.startswith(("Register-", "Get-ScheduledTask", "Disable-", "Unregister-")) for l in llamadas)
    assert "error inesperado: " in r["log"].read_text(encoding="utf-8-sig")
    assert r["codigo"] == 0
    assert r["candado_libre"] is True


def test_correr_sin_lanzador_lanza_como_antes(tmp_path: Path):
    # Si el lanzador no compila, el server arranca igual (en la consola de la tarea, sin job object).
    r = _correr(tmp_path, "& $Script -Correr", codigos=(1, 0), sin_lanzador=True, sin_try=True)
    llamadas = r["llamadas"]
    assert "Add-Type" in llamadas
    arranques = [l for l in llamadas if l.startswith("Start-Process ")]
    assert len(arranques) == 2 and not any(l.endswith(" -Wait") for l in arranques)
    assert not any(l.startswith("Register-") for l in llamadas)
    log = r["log"].read_text(encoding="utf-8-sig")
    assert "no pude compilar el lanzador" in log
    assert "el server terminó con código 1" in log


def test_correr_no_toca_un_server_que_escucha_y_lo_adopta(tmp_path: Path):
    # 26/10: se cerró la ventana y murió el -Correr. Si el server hubiera sobrevivido, el
    # disparador de 5 minutos levanta otro -Correr: no lo cierra, espera a que se libere el puerto.
    r = _correr(tmp_path, "& $Script -Correr -Puerto 8123", codigos=(0,), ocupado=3)
    assert r["error"] is None
    llamadas = r["llamadas"]
    consultas = [i for i, l in enumerate(llamadas) if l == "Get-NetTCPConnection 8123"]
    assert len(consultas) == 4
    for a, b in zip(consultas, consultas[1:]):
        assert "Start-Sleep 30" in llamadas[a:b]
    # Nada cerrado mientras escuchaba; recién con el puerto libre, el huérfano (que no escucha).
    assert _cerrados(llamadas[: consultas[3]]) == []
    assert _cerrados(llamadas) == HUERFANO
    lanzar = next(i for i, l in enumerate(llamadas) if l.startswith("Lanzar "))
    assert sum(l.startswith("Lanzar ") for l in llamadas) == 1
    # Mientras espera, mete cada proceso del server adoptado en un job propio (una sola vez cada
    # uno); cuando deja de escuchar, cierra esos jobs antes de lanzar el nuevo.
    assert [int(l.split()[1]) for l in llamadas if l.startswith("Adoptar ")] == HUERFANO
    terminados = [i for i, l in enumerate(llamadas) if l.startswith("TerminarTrabajo ")]
    assert len(terminados) == len(HUERFANO) and max(terminados) < lanzar
    assert min(terminados) > consultas[3]
    log = r["log"].read_text(encoding="utf-8-sig")
    assert log.count("ya hay un server escuchando en el puerto 8123 (pid 70013): no lo toco") == 1


def test_correr_con_un_lanzador_viejo_cargado_lanza_como_antes(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Correr", codigos=(0,), lanzador_viejo=True, sin_try=True)
    assert sum(l.startswith("Start-Process ") for l in r["llamadas"]) == 1
    assert "lanzador viejo" in r["log"].read_text(encoding="utf-8-sig")


def test_correr_un_error_que_se_escapa_sale_sin_registrar(tmp_path: Path):
    # Si hasta el catch de la vuelta falla, sale con 1 (suelta el candado) y no sigue de largo.
    r = _correr(tmp_path, "& $Script -Correr", codigos=(999,), sleep_revienta=True, sin_try=True)
    llamadas = r["llamadas"]
    assert r["codigo"] == 1
    assert not any(l.startswith(("Register-", "Get-ScheduledTask", "Disable-", "Unregister-")) for l in llamadas)
    assert "salgo y el disparador levanta otro -Correr" in r["log"].read_text(encoding="utf-8-sig")
    assert r["candado_libre"] is True


@pytest.mark.parametrize("log_tomado", [False, True])
def test_correr_sale_sin_hacer_nada_si_otro_tiene_el_candado(tmp_path: Path, log_tomado: bool):
    # Con el server del otro -Correr andando, su "cmd >>" tiene el log abierto y no deja escribir:
    # la línea va a server-correr.log.
    tomar = (
        "New-Item -ItemType Directory -Force -Path (Split-Path $LogFalso) | Out-Null; "
        "$global:LogTomado = [IO.File]::Open($LogFalso, 'Append', 'Write', 'Read'); "
    ) if log_tomado else ""
    log = tmp_path / "Classmer-data" / "logs" / "server-consola.log"
    r = _correr(tmp_path, f"$LogFalso = '{log}'; {tomar}& $Script -Correr", codigos=(1,), candado=BUCLE_CON_CANDADO)
    assert r["error"] is None
    assert r["codigo"] == 0
    assert not any(l.startswith(("Lanzar", "Stop-Process", "Get-NetTCPConnection")) for l in r["llamadas"])
    destino = log.with_name("server-correr.log") if log_tomado else log
    assert f"ya hay otro -Correr para {log.parents[1]} (pid {BUCLE_CON_CANDADO}): no hago nada" in (
        destino.read_text(encoding="utf-8-sig"))


def test_correr_con_otra_carpeta_de_datos(tmp_path: Path):
    otra = tmp_path / "otra-data"
    r = _correr(tmp_path, f"& $Script -Correr -Puerto 8777 -Datos '{otra}'", codigos=(0,),
                candado_propio=otra / "server-correr.lock")
    assert r["error"] is None
    assert r["home_al_arrancar"] == str(otra)  # el server usa esa carpeta, no la de siempre
    assert f'>> "{otra}\\logs\\server-consola.log"' in next(l for l in r["llamadas"] if l.startswith("Lanzar "))
    assert (otra / "logs" / "server-consola.log").exists()
    assert r["candado_libre"] is True
    # El huérfano es de la carpeta de siempre: con otra, no es suyo.
    assert _cerrados(r["llamadas"]) == []


def test_reiniciar_cierra_solo_el_server_de_esta_carpeta(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Reiniciar", candado=BUCLE_CON_CANDADO, ocupado=1)
    assert r["error"] is None
    llamadas = r["llamadas"]
    assert llamadas.count("Start-ScheduledTask Classmer server") == 1
    parada = llamadas.index("Stop-ScheduledTask Classmer server")
    arranque = llamadas.index("Start-ScheduledTask Classmer server")
    assert parada < arranque
    # Primero el -Correr del candado (así no relanza), después el server (también el que escucha).
    assert _cerrados(llamadas[parada:arranque]) == [BUCLE_CON_CANDADO] + HUERFANO
    assert _cerrados(llamadas) == [BUCLE_CON_CANDADO] + HUERFANO
    assert _avisos(llamadas) == []
    assert not any(l.startswith(("Register-", "Unregister-", "Enable-", "Disable-", "Lanzar")) for l in llamadas)


def test_reiniciar_cierra_lo_que_escucha_en_el_puerto_y_no_mas_arriba(tmp_path: Path):
    # Un "classmer serve" de otro lado ocupando el puerto de producción: se cierra desde uv, no bash.
    r = _correr(tmp_path, "& $Script -Reiniciar", ocupado=1, oyente=70022)
    assert r["error"] is None
    cerrados = _cerrados(r["llamadas"])
    assert cerrados == HUERFANO + [70020, 70021, 70022]
    assert 70019 not in cerrados


def test_reiniciar_avisa_si_el_puerto_lo_tiene_otro_programa(tmp_path: Path):
    # No es Classmer: no lo cierra, avisa, y que siga escuchando no cuenta como "reiniciado".
    r = _correr(tmp_path, "& $Script -Reiniciar", ocupado=2, oyente=70006)
    assert r["error"] is None
    assert _cerrados(r["llamadas"]) == HUERFANO
    avisos = _avisos(r["llamadas"])
    assert any("sigue tomado por el pid 70006" in a for a in avisos)
    assert any("todavía no escucha" in a for a in avisos)


def test_reiniciar_cierra_restos_viejos_y_respeta_pids_reciclados(tmp_path: Path):
    extra = r"""
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]70080; ParentProcessId = [uint32]4
        CreationDate = (Get-Date '2026-10-26T08:00:00')
        CommandLine = 'powershell.exe -NoProfile -WindowStyle Hidden -File "C:\Classmer\server\scripts\registrar-arranque.ps1" -Correr -Uv "C:\uv.exe"' }
    [pscustomobject]@{ Name = 'cmd.exe'; ProcessId = [uint32]70081; ParentProcessId = [uint32]70080
        CreationDate = (Get-Date '2026-10-26T08:01:00'); CommandLine = 'cmd.exe /d /c ""C:\uv.exe" run classmer serve >> "__LOG__" 2>&1"' }
    [pscustomobject]@{ Name = 'uv.exe'; ProcessId = [uint32]70082; ParentProcessId = [uint32]70081
        CommandLine = '"C:\uv.exe" run classmer serve' }
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]70070; ParentProcessId = [uint32]4
        CreationDate = (Get-Date '2026-10-26T10:00:00')
        CommandLine = 'powershell.exe -File "C:\otra\server\scripts\registrar-arranque.ps1" -Correr' }
    [pscustomobject]@{ Name = 'cmd.exe'; ProcessId = [uint32]70071; ParentProcessId = [uint32]70070
        CreationDate = (Get-Date '2026-10-26T09:00:00'); CommandLine = 'cmd.exe /d /c ""C:\uv.exe" run classmer serve >> "__LOG__" 2>&1"' }
"""
    r = _correr(tmp_path, "& $Script -Reiniciar", extra=extra)
    assert r["error"] is None
    # El -Correr de antes del candado (versión vieja del script) cae con su server, él primero.
    # El cmd huérfano cuyo PID padre ahora es otro -Correr (más nuevo que él) cae solo.
    assert _cerrados(r["llamadas"]) == HUERFANO + [70080, 70081, 70082, 70071]


def test_reiniciar_reintenta_si_el_programador_ignora_el_arranque(tmp_path: Path):
    # 2026-10-14: -Reiniciar durante la primera corrida; el Start se ignoró (IgnoreNew, la tarea
    # seguía figurando Running) y el server quedó caído. Ahora se confirma y se reintenta.
    r = _correr(tmp_path, "& $Script -Reiniciar", ignorados=1)
    assert r["error"] is None
    llamadas = r["llamadas"]
    assert llamadas.count("Start-ScheduledTask Classmer server") == 2
    assert llamadas.count("Stop-ScheduledTask Classmer server") == 2
    assert "Get-NetTCPConnection 8000" in llamadas  # espera a que el server vuelva a escuchar


def test_reiniciar_falla_claro_si_nunca_arranca(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Reiniciar", ignorados=10)
    assert "No pude volver a arrancar la tarea" in (r["error"] or "")
    assert r["llamadas"].count("Start-ScheduledTask Classmer server") == 3


def test_reiniciar_avisa_si_el_server_no_llega_a_escuchar(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Reiniciar", escucha=False,
                argumentos_tarea=_argumentos(8123, tmp_path / "Classmer-data"))
    assert r["error"] is None
    llamadas = r["llamadas"]
    arranque = llamadas.index("Start-ScheduledTask Classmer server")
    # el puerto sale de la tarea; 120 consultas esperando que vuelva, y el aviso
    assert llamadas[arranque:].count("Get-NetTCPConnection 8123") == 120
    assert any("todavía no escucha en el puerto 8123" in a for a in _avisos(llamadas))


def test_reiniciar_sin_tarea_no_cierra_nada(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Reiniciar", hay_tarea=False)
    assert "No existe la tarea" in (r["error"] or "")
    assert _cerrados(r["llamadas"]) == []
    assert not any("Start-ScheduledTask" in l for l in r["llamadas"])


def test_reiniciar_vuelve_a_habilitar_la_tarea_aunque_figure_corriendo(tmp_path: Path):
    # Deshabilitada con una corrida en curso figura "Running", no "Disabled".
    r = _correr(tmp_path, "& $Script -Reiniciar", deshabilitada=True)
    assert r["error"] is None
    llamadas = r["llamadas"]
    assert llamadas.index("Enable-ScheduledTask Classmer server") < llamadas.index("Start-ScheduledTask Classmer server")


def test_reiniciar_avisa_si_la_tarea_esta_registrada_a_la_antigua(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Reiniciar", ejecutable_tarea="powershell.exe")
    assert r["error"] is None
    assert any("registrada a la antigua" in a for a in _avisos(r["llamadas"]))


def test_parar_deshabilita_la_tarea_y_cierra_el_server(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Parar", candado=BUCLE_CON_CANDADO)
    assert r["error"] is None
    llamadas = r["llamadas"]
    assert llamadas.index("Disable-ScheduledTask Classmer server") < llamadas.index("Stop-ScheduledTask Classmer server")
    assert _cerrados(llamadas) == [BUCLE_CON_CANDADO] + HUERFANO
    assert not any(l.startswith(("Start-ScheduledTask", "Unregister-", "Lanzar")) for l in llamadas)


def test_parar_avisa_si_el_candado_lo_tiene_algo_que_no_es_un_correr(tmp_path: Path):
    # Un -Correr corrido a mano en una consola interactiva: el candado tiene el PID de la consola.
    extra = r"""
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]70060; ParentProcessId = [uint32]4
        CommandLine = '"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe"' }
    [pscustomobject]@{ Name = 'powershell.exe'; ProcessId = [uint32]70061; ParentProcessId = [uint32]4
        CommandLine = 'powershell.exe -NoProfile -Command & ''C:\x\server\scripts\registrar-arranque.ps1'' -Correr' }
"""
    r = _correr(tmp_path, "& $Script -Parar", candado=70060, extra=extra)
    assert r["error"] is None
    assert _cerrados(r["llamadas"]) == HUERFANO
    assert any("lo tiene el pid 70060" in a for a in _avisos(r["llamadas"]))
    # Con "& '...ps1' -Correr" sí lo reconoce y lo cierra primero.
    r = _correr(tmp_path, "& $Script -Parar", candado=70061, extra=extra)
    assert _cerrados(r["llamadas"]) == [70061] + HUERFANO
    assert _avisos(r["llamadas"]) == []


def test_quitar_cierra_el_server_pero_no_su_proceso(tmp_path: Path):
    r = _correr(tmp_path, "& $Script -Quitar", candado=BUCLE_CON_CANDADO)
    assert r["error"] is None
    assert "Unregister-ScheduledTask Classmer server" in r["llamadas"]
    assert _cerrados(r["llamadas"]) == [BUCLE_CON_CANDADO] + HUERFANO


def test_la_tarea_de_prueba_usa_su_puerto_y_su_carpeta(tmp_path: Path):
    otra = tmp_path / "otra-data"
    prueba = _argumentos(8777, otra)
    # Sin repetir -Puerto ni -Datos: salen de la tarea. Nada de producción (ni su candado, ni su
    # log, ni el puerto 8000) cae.
    r = _correr(tmp_path, "& $Script -Parar -NombreTarea 'Classmer prueba'", candado=BUCLE_CON_CANDADO,
                argumentos_tarea=prueba)
    assert r["error"] is None
    assert "Disable-ScheduledTask Classmer prueba" in r["llamadas"]
    assert "Get-NetTCPConnection 8777" in r["llamadas"]
    assert "Get-NetTCPConnection 8000" not in r["llamadas"]
    assert _cerrados(r["llamadas"]) == []
    # Lo que escucha en SU puerto sí cae (desde su primera pieza, cmd).
    r = _correr(tmp_path, f"& $Script -Parar -NombreTarea 'Classmer prueba' -Puerto 8777 -Datos '{otra}'",
                ocupado=1, oyente=70013, argumentos_tarea=prueba)
    assert _cerrados(r["llamadas"]) == HUERFANO


@pytest.mark.parametrize("invocacion", [
    "& $Script -Parar -NombreTarea 'Classmer prueba' -Puerto 8000",
    "& $Script -Quitar -NombreTarea 'Classmer prueba' -Datos 'C:\\Users\\otra'",
    "& $Script -Reiniciar -NombreTarea 'Classmer prueba' -Puerto 8000",
])
def test_si_los_parametros_no_coinciden_con_la_tarea_no_hace_nada(tmp_path: Path, invocacion: str):
    r = _correr(tmp_path, invocacion, ocupado=1, argumentos_tarea=_argumentos(8777, tmp_path / "otra-data"))
    assert "La tarea 'Classmer prueba' usa" in (r["error"] or "")
    assert not any(l.startswith(("Stop-", "Disable-", "Unregister-", "Start-ScheduledTask")) for l in r["llamadas"])


@pytest.mark.parametrize("modo", ["-Parar", "-Quitar"])
@pytest.mark.parametrize("nombre", ["Classmer prueba", "Classmer server"])
def test_una_tarea_que_no_existe_no_cierra_nada(tmp_path: Path, modo: str, nombre: str):
    r = _correr(tmp_path, f"& $Script {modo} -NombreTarea '{nombre}'", hay_tarea=False,
                candado=BUCLE_CON_CANDADO, ocupado=1)
    assert r["error"] is None
    assert _cerrados(r["llamadas"]) == []


@pytest.mark.skipif(shutil.which("uv") is None, reason="falta uv en el PATH")
def test_registrar_sin_ventana_y_con_disparador_cada_5_minutos(tmp_path: Path):
    otra = tmp_path / "otra-data"
    # Con "\" al final: no puede quedar -Datos "...\" (la barra escaparía la comilla).
    r = _correr(tmp_path, f"& $Script -NombreTarea 'Classmer prueba' -Puerto 8777 -Datos '{otra}\\'")
    assert r["error"] is None
    assert "Register-ScheduledTask Classmer prueba" in r["llamadas"]
    reg = r["registro"]
    # conhost --headless: sin ventana que Windows Terminal pueda adoptar.
    assert reg["ejecutable"].lower().endswith(r"\system32\conhost.exe")
    assert reg["argumentos"].startswith('--headless "')
    assert f'-File "{SCRIPT}" -Correr -Uv "' in reg["argumentos"]
    assert reg["argumentos"].endswith(f'-Puerto 8777 -Datos "{otra}"')
    assert reg["carpeta"] == str(SCRIPT.parents[1])
    assert reg["instancias"] == "IgnoreNew"
    # Al iniciar sesión, y aparte cada 5 minutos sin fin desde ya (la repetición de uno de
    # inicio de sesión recién empezaría en el próximo inicio de sesión).
    sesion, cada5 = reg["disparadores"]
    assert sesion["tipo"] == "MSFT_TaskLogonTrigger" and not sesion["intervalo"]
    assert cada5["tipo"] == "MSFT_TaskTimeTrigger"
    assert (cada5["intervalo"], cada5["duracion"], cada5["corta_al_final"]) == ("PT5M", None, False)
    assert cada5["inicio"]
    # Ya existía y estaba habilitada: sigue habilitada.
    assert not any(l.startswith("Disable-") for l in r["llamadas"])

    # Los valores de producción (sin -Datos), con otro nombre por las dudas.
    r = _correr(tmp_path, "& $Script -NombreTarea 'Classmer prueba'", hay_tarea=False)
    reg = r["registro"]
    assert reg["argumentos"].endswith("-Puerto 8000")
    assert not any(l.startswith("Disable-") for l in r["llamadas"])


@pytest.mark.skipif(shutil.which("uv") is None, reason="falta uv en el PATH")
def test_registrar_de_nuevo_una_tarea_parada_la_deja_parada(tmp_path: Path):
    # -Parar, merge, registrar de nuevo, uv sync: que el disparador no la levante en el medio.
    r = _correr(tmp_path, "& $Script -NombreTarea 'Classmer prueba'", deshabilitada=True)
    llamadas = r["llamadas"]
    assert llamadas.index("Register-ScheduledTask Classmer prueba") < llamadas.index("Disable-ScheduledTask Classmer prueba")
