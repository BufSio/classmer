<#
.SYNOPSIS
    Registra (o quita) la tarea "Classmer server" del Programador de tareas: arranca el server al
    iniciar sesión, sin ventana, y lo vuelve a levantar si se muere.

.DESCRIPTION
    Lo corre el usuario desde una consola de PowerShell normal (NO como administrador), parado en el
    checkout principal del repo (no en un worktree de .claude\worktrees):

        powershell -ExecutionPolicy Bypass -File .\server\scripts\registrar-arranque.ps1

    Una vez, y otra vez cada vez que cambia cómo se registra la tarea (p. ej. el 26/10: sin
    ventana y con el disparador de 5 minutos), seguido de -Reiniciar. -Reiniciar avisa si la tarea
    quedó registrada a la antigua.

    La tarea:
      - Arranca cuando el usuario inicia sesión en Windows (corre como su usuario, sin privilegios
        elevados y NO como servicio: el Claude Agent SDK necesita su login de Claude Code, §5).
      - Corre sin ninguna ventana: "conhost.exe --headless" con PowerShell y este mismo script con
        -Correr, que hace cd a la carpeta server/ y ejecuta "uv run classmer serve", con toda la
        salida en Classmer-data\logs\server-consola.log. Antes era "powershell -WindowStyle
        Hidden", pero con Windows Terminal como terminal por defecto esa consola terminaba en una
        ventana minimizada de Terminal, y cerrarla mataba el server (18/10 y 26/10, 0xC000013A).
      - El server (cmd -> uv -> python) corre con su propia consola sin ventana (CREATE_NO_WINDOW)
        y dentro de un job object. En una corrida a mano de -Correr comparte la consola (Ctrl+C
        o cerrarla lo cortan también).
      - Si el server se cae, -Correr lo vuelve a levantar a los 30 s (si se cae enseguida 5 veces
        seguidas, espera 5 minutos y sigue probando). Lo que haya quedado colgando del server
        muerto (p. ej. un claude.exe de un apunte a medias) se cierra antes, con el job object.
      - Si se muere -Correr (o todo junto), un segundo disparador cada 5 minutos, sin fin: mientras
        la tarea corre, el Programador lo ignora (IgnoreNew); si no corre, la vuelve a arrancar.
        Por eso, después de registrarla, arranca sola en 5 minutos aunque nadie la arranque.
      - -Correr nunca cierra un server que escucha en el puerto: si quedó uno vivo, espera a que
        se libere y recién ahí levanta otro. Mientras espera, mete sus procesos en job objects
        propios para cerrar lo que quede colgando cuando se muera.
      - Un solo -Correr por carpeta de datos: el candado Classmer-data\server-correr.lock es del
        proceso. Si ya lo tiene otro, el nuevo sale sin hacer nada.
      - Si alguien cierra el conhost de la tarea (Stop-ScheduledTask, "Finalizar" en el
        Programador), -Correr sale y deja el server andando con su consola; el disparador levanta
        otro -Correr que lo cuida.
      - Un error inesperado en -Correr se anota en el log y se vuelve a probar a los 30 s; -Correr
        nunca sigue de largo a los otros modos.
      - Sin límite de tiempo de ejecución, "iniciar lo antes posible" si se pasó la hora, y no
        se corta con batería.

    La primera vez que Python escucha en el puerto 8000, Windows pregunta si permite el acceso de
    red a python.exe: aceptá en "Redes privadas" (no hace falta en públicas). Conviene correr una
    vez "uv run classmer serve" a mano para que aparezca el cartel, porque con la tarea oculta
    puede quedar escondido. Si una VPN en la PC bloquea la red local, esa opción tiene que estar apagada.

    Para reiniciar el server (p. ej. después de tocar .env o config.yaml):
        powershell -ExecutionPolicy Bypass -File .\server\scripts\registrar-arranque.ps1 -Reiniciar
    No uses Stop/Start-ScheduledTask a secas: el server sigue vivo con la config vieja, y el
    -Correr nuevo no lo toca porque escucha en el puerto. -Reiniciar para la tarea, cierra el
    server y la vuelve a arrancar.
    Para tenerlo parado un rato (p. ej. "uv sync" después de un merge) sin que el disparador lo
    vuelva a levantar: -Parar, que además deshabilita la tarea; -Reiniciar la vuelve a habilitar.
    -Quitar da de baja la tarea y también cierra el server.

    -Reiniciar, -Parar y -Quitar actúan sobre el server de la tarea: su puerto y su carpeta de
    datos salen de cómo se registró (si se los pasás y no coinciden, no hacen nada). Cierran el
    árbol del -Correr que tiene el candado, lo que lanzó "cmd /c uv run classmer serve >> <su
    log>" aunque ese -Correr ya no esté, y el "classmer serve" que escucha en su puerto (sea de
    donde sea). No tocan servers de otro worktree o de otra carpeta de datos en otro puerto, ni
    comandos que sólo nombran "classmer serve".

    Para verla: Programador de tareas > Biblioteca > "Classmer server", o
        Get-ScheduledTaskInfo -TaskName 'Classmer server'

    Si cambiás servidor.port en config.yaml, registrá de nuevo con -Puerto <el nuevo>.

    Para probar sin tocar producción, otra tarea con otra carpeta de datos y otro puerto (el
    mismo que servidor.port de su config.yaml):
        ... registrar-arranque.ps1 -NombreTarea 'Classmer prueba' -Puerto 8777 -Datos C:\tmp\cm
    y después -Reiniciar, -Parar o -Quitar con -NombreTarea 'Classmer prueba'. Si la tarea no
    existe, -Parar y -Quitar no cierran nada. Volver a registrar una tarea parada con -Parar la
    deja deshabilitada.

.PARAMETER Quitar
    Da de baja la tarea y cierra el server (no toca datos ni logs).

.PARAMETER Reiniciar
    Para la tarea, cierra el server y la vuelve a arrancar (y a habilitar, si estaba con -Parar).

.PARAMETER Parar
    Para la tarea, la deshabilita (así el disparador no lo vuelve a levantar) y cierra el server.

.PARAMETER Correr
    Uso interno de la tarea: levanta el server en primer plano con la salida al log.

.PARAMETER Uv
    Uso interno de la tarea: ruta completa de uv.exe (se resuelve al registrar).

.PARAMETER Puerto
    Puerto del server (8000), el mismo que servidor.port. Al registrar queda en la tarea; -Correr
    lo usa para no pisar un server que ya escucha.

.PARAMETER NombreTarea
    Nombre de la tarea ("Classmer server"). Otro, sólo para pruebas.

.PARAMETER Datos
    Carpeta de datos (CLASSMER_HOME) del server. Al registrar queda en la tarea. Sin esto, la de
    siempre: CLASSMER_HOME o %USERPROFILE%\Documents\Classmer-data.
#>
[CmdletBinding()]
param(
    [switch] $Quitar,
    [switch] $Reiniciar,
    [switch] $Parar,
    [switch] $Correr,
    [string] $Uv,
    [int] $Puerto = 8000,
    [string] $NombreTarea = 'Classmer server',
    [string] $Datos
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

$TareaDeProduccion = 'Classmer server'
$CarpetaServer = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$PuertoExplicito = $PSBoundParameters.ContainsKey('Puerto')

# --- carpeta de datos -------------------------------------------------------------------------

# Ruta completa y sin "\" al final (en la tarea va entre comillas, y "C:\x\" rompe el cierre).
function Normalizar-Carpeta([string] $Ruta) {
    $ruta = $ExecutionContext.SessionState.Path.GetUnresolvedProviderPathFromPSPath($Ruta).TrimEnd('\')
    if ($ruta.EndsWith(':')) { $ruta += '\' }
    return $ruta
}

# Misma regla que classmer.config.classmer_home(): CLASSMER_HOME o %USERPROFILE%\Documents\Classmer-data.
function Carpeta-Por-Defecto([string] $ClassmerHome) {
    if ($ClassmerHome) { return [Environment]::ExpandEnvironmentVariables($ClassmerHome) }
    return Join-Path $env:USERPROFILE 'Documents\Classmer-data'
}

function Fijar-Carpeta([string] $Ruta) {
    $script:CarpetaDatos = $Ruta
    $script:CarpetaLogs = Join-Path $Ruta 'logs'
    $script:LogConsola = Join-Path $script:CarpetaLogs 'server-consola.log'
    $script:Candado = Join-Path $Ruta 'server-correr.lock'
}

# Una tarea registrada sin -Datos ve el CLASSMER_HOME del usuario (o de la máquina), no el de la
# consola que la registra o la reinicia.
function Carpeta-De-Tarea-Sin-Datos {
    $variable = [Environment]::GetEnvironmentVariable('CLASSMER_HOME', 'User')
    if (-not $variable) { $variable = [Environment]::GetEnvironmentVariable('CLASSMER_HOME', 'Machine') }
    return Carpeta-Por-Defecto $variable
}

if ($Datos) {
    Fijar-Carpeta (Normalizar-Carpeta $Datos)
} elseif ($Correr) {
    Fijar-Carpeta (Carpeta-Por-Defecto $env:CLASSMER_HOME)   # la misma que va a usar el server
} else {
    Fijar-Carpeta (Carpeta-De-Tarea-Sin-Datos)
}

# -Reiniciar, -Parar y -Quitar actúan sobre el server de la tarea: el puerto y la carpeta de datos
# salen de cómo se registró, no de quien lo llama, así una prueba que se olvida -Puerto o -Datos
# no cierra producción. Si se pasan y no coinciden, no se hace nada.
function Usar-Lo-De-La-Tarea($Tarea) {
    $texto = [string](@($Tarea.Actions)[0].Arguments)
    $puertoTarea = 8000
    if ($texto -match '\s-Puerto\s+(\d+)') { $puertoTarea = [int]$Matches[1] }
    if ($texto -match '\s-Datos\s+"([^"]+)"') {
        $datosTarea = Normalizar-Carpeta $Matches[1]
    } else {
        $datosTarea = Carpeta-De-Tarea-Sin-Datos
    }
    if ($PuertoExplicito -and $Puerto -ne $puertoTarea) {
        throw "La tarea '$NombreTarea' usa el puerto $puertoTarea, no $Puerto. No hago nada."
    }
    if ($Datos -and -not [string]::Equals((Normalizar-Carpeta $Datos), $datosTarea, [StringComparison]::OrdinalIgnoreCase)) {
        throw "La tarea '$NombreTarea' usa la carpeta de datos $datosTarea, no $Datos. No hago nada."
    }
    $script:Puerto = $puertoTarea
    Fijar-Carpeta $datosTarea
}

# --- el server de esta carpeta de datos -------------------------------------------------------

$PatronServe = 'classmer(\.exe)?"?\s+serve\b'
$PiezasServer = @('cmd.exe', 'uv.exe', 'classmer.exe', 'python.exe')

function Es-Bucle($p) {
    return ($p.Name -eq 'powershell.exe' -or $p.Name -eq 'pwsh.exe') -and $p.CommandLine -and
        $p.CommandLine -match "registrar-arranque\.ps1[`"']?\s.*-Correr\b"
}

# Windows recicla PIDs: un ParentProcessId viejo puede apuntar a un proceso nuevo que no tiene
# nada que ver. Un hijo cuenta sólo si nació después que su padre.
function Nacio-Despues($Hijo, $Padre) {
    if (-not $Hijo.PSObject.Properties['CreationDate'] -or -not $Padre.PSObject.Properties['CreationDate']) { return $true }
    if (-not $Hijo.CreationDate -or -not $Padre.CreationDate) { return $true }
    return $Hijo.CreationDate -ge $Padre.CreationDate
}

# $Raiz y sus descendientes, de arriba hacia abajo.
function Arbol-De([object[]] $Procesos, [int] $Raiz) {
    $orden = New-Object System.Collections.ArrayList
    $raizProc = @($Procesos | Where-Object { [int]$_.ProcessId -eq $Raiz })
    if ($raizProc.Count -eq 0) { return @() }
    [void]$orden.Add($raizProc[0])
    $vistos = @{ $Raiz = $true }
    for ($i = 0; $i -lt $orden.Count; $i++) {
        $padre = $orden[$i]
        foreach ($p in $Procesos) {
            $id = [int]$p.ProcessId
            if (-not $vistos.ContainsKey($id) -and [int]$p.ParentProcessId -eq [int]$padre.ProcessId -and (Nacio-Despues $p $padre)) {
                $vistos[$id] = $true
                [void]$orden.Add($p)
            }
        }
    }
    return $orden.ToArray()
}

# Desde una pieza del server, sube por los padres mientras sean del mismo server (cmd, uv,
# classmer.exe o python con "classmer serve"); si arriba está el -Correr que lo lanzó, también.
function Subir-Hasta-Raiz($p, [hashtable] $PorPid) {
    while ($true) {
        $padre = $PorPid[[int]$p.ParentProcessId]
        if (-not $padre -or [int]$padre.ProcessId -eq [int]$p.ProcessId -or -not (Nacio-Despues $p $padre)) { return $p }
        if (Es-Bucle $padre) { return $padre }
        if ($PiezasServer -notcontains $padre.Name -or -not $padre.CommandLine -or $padre.CommandLine -notmatch $PatronServe) { return $p }
        $p = $padre
    }
}

# De la lista de procesos (Win32_Process), los del server de esta carpeta de datos, en el orden en
# que hay que cerrarlos (el -Correr antes que su server, así no lo relanza). Son los árboles de:
#   - el -Correr que tiene el candado ($Bucle; 0 si no hay);
#   - cada "cmd /c uv run classmer serve >> <este log>" (lo que lanza -Correr), aunque su -Correr
#     ya no exista, desde el -Correr que lo lanzó si sigue vivo;
#   - el "classmer serve" que escucha en el puerto ($Oyentes), desde la primera pieza del server.
# Nada más: ni servers de otro worktree o de otra carpeta de datos en otro puerto, ni un comando
# cualquiera que nombre "classmer serve". Nunca $Propio ni sus descendientes.
function Buscar-Restos {
    param([object[]] $Procesos, [int] $Propio, [int] $Bucle = 0, [int[]] $Oyentes = @())
    $Procesos = @($Procesos | Where-Object { $_ })
    # Ojo: ProcessId viene como UInt32; las claves van como [int] para que coincidan.
    $porPid = @{}
    foreach ($p in $Procesos) { $porPid[[int]$p.ProcessId] = $p }
    $raices = New-Object System.Collections.ArrayList
    if ($Bucle -and $porPid.ContainsKey($Bucle) -and (Es-Bucle $porPid[$Bucle])) { [void]$raices.Add($porPid[$Bucle]) }
    foreach ($p in $Procesos) {
        if ($p.Name -eq 'cmd.exe' -and $p.CommandLine -and $p.CommandLine -match $PatronServe -and
            $p.CommandLine.IndexOf($LogConsola, [StringComparison]::OrdinalIgnoreCase) -ge 0) {
            [void]$raices.Add((Subir-Hasta-Raiz $p $porPid))
        }
    }
    foreach ($id in $Oyentes) {
        $p = $porPid[[int]$id]
        if ($p -and $p.CommandLine -and $p.CommandLine -match $PatronServe) { [void]$raices.Add((Subir-Hasta-Raiz $p $porPid)) }
    }
    $saltear = @{}
    foreach ($p in (Arbol-De $Procesos $Propio)) { $saltear[[int]$p.ProcessId] = $true }
    $restos = New-Object System.Collections.ArrayList
    foreach ($raiz in $raices) {
        foreach ($p in (Arbol-De $Procesos ([int]$raiz.ProcessId))) {
            if ($saltear.ContainsKey([int]$p.ProcessId)) { continue }
            $saltear[[int]$p.ProcessId] = $true
            [void]$restos.Add($p)
        }
    }
    return $restos.ToArray()
}

# PIDs que escuchan en el puerto del server.
function Oyentes-Del-Puerto {
    return @(Get-NetTCPConnection -LocalPort $Puerto -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { [int]$_.OwningProcess } | Sort-Object -Unique)
}

# El PID del -Correr vivo de esta carpeta de datos (lo escribió en el candado), o 0.
function Bucle-Vivo {
    if (-not (Test-Path -LiteralPath $Candado)) { return 0 }
    try {
        # Si lo puedo abrir para escribir, nadie lo tiene.
        [IO.File]::Open($Candado, 'Open', 'ReadWrite', 'ReadWrite').Dispose()
        return 0
    } catch { }
    try {
        $f = [IO.File]::Open($Candado, 'Open', 'Read', 'ReadWrite')
        try { $texto = (New-Object IO.StreamReader($f)).ReadToEnd() } finally { $f.Dispose() }
    } catch { return 0 }
    $id = 0
    if ([int]::TryParse($texto.Trim(), [ref]$id)) { return $id }
    return 0
}

# Cierra el server de esta carpeta de datos (ver Buscar-Restos), espera a que termine y devuelve
# lo que cerró. -Todo suma el -Correr del candado y lo que escucha en el puerto; sin -Todo (desde
# -Correr) sólo lo que quedó de un -Correr anterior y no escucha.
function Cerrar-Restos([switch] $Todo) {
    $bucle = 0
    $oyentes = @()
    if ($Todo) {
        $bucle = Bucle-Vivo
        $oyentes = @(Oyentes-Del-Puerto)
    }
    $procesos = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
    $tenedor = @($procesos | Where-Object { $_ -and $bucle -and [int]$_.ProcessId -eq $bucle })
    if ($tenedor.Count -gt 0 -and -not (Es-Bucle $tenedor[0])) {
        Write-Warning ("El candado {0} lo tiene el pid {1} ({2}), que no reconozco como -Correr: no lo cierro. Si es un -Correr a mano, cerralo vos o va a volver a levantar el server." -f $Candado, $bucle, $tenedor[0].Name)
    }
    $restos = @(Buscar-Restos -Procesos $procesos -Propio $PID -Bucle $bucle -Oyentes $oyentes)
    foreach ($p in $restos) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
    }
    if ($restos.Count -gt 0) {
        # Que suelten el puerto antes de levantar otro.
        try { Wait-Process -Id @($restos | ForEach-Object { [int]$_.ProcessId }) -Timeout 15 -ErrorAction SilentlyContinue } catch { }
    }
    return $restos
}

function Describir-Restos([object[]] $Restos) {
    return (@($Restos | ForEach-Object { '{0} (pid {1})' -f $_.Name, $_.ProcessId }) -join ', ')
}

# --- modo tarea: levantar el server -----------------------------------------------------------

if ($Correr) {
    # Los errores de uv/python van al log; que no corten este script.
    $ErrorActionPreference = 'Continue'
    New-Item -ItemType Directory -Force -Path $CarpetaLogs | Out-Null
    $env:PYTHONUTF8 = '1'
    $env:PYTHONUNBUFFERED = '1'
    if ($Datos) { $env:CLASSMER_HOME = $CarpetaDatos }   # para el server
    Set-Location -LiteralPath $CarpetaServer
    if (-not $Uv -or -not (Test-Path -LiteralPath $Uv)) {
        $encontrado = Get-Command uv -ErrorAction SilentlyContinue
        $Uv = if ($encontrado) { $encontrado.Source } else { 'uv' }
    }
    function Anotar-Log([string] $Texto) {
        $linea = '==== {0} {1} ====' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $Texto
        try {
            Add-Content -LiteralPath $LogConsola -Encoding UTF8 -Value $linea -ErrorAction Stop
        } catch {
            # Lo tiene abierto el "cmd >>" de un server que sigue vivo (el de otro -Correr o uno
            # que quedó de antes) y no deja escribir a nadie más.
            Add-Content -LiteralPath (Join-Path $CarpetaLogs 'server-correr.log') -Encoding UTF8 -Value $linea
        }
    }

    # Un solo -Correr por carpeta de datos: el candado queda tomado mientras viva este proceso
    # (Windows lo suelta solo si se muere). Si lo tiene otro, ese ya cuida el server.
    try {
        $candadoTomado = [IO.File]::Open($Candado, 'OpenOrCreate', 'ReadWrite', 'Read')
    } catch {
        Anotar-Log ('ya hay otro -Correr para {0} (pid {1}): no hago nada' -f $CarpetaDatos, (Bucle-Vivo))
        exit 0
    }
    try {
        $candadoTomado.SetLength(0)
        $bytes = [Text.Encoding]::ASCII.GetBytes([string]$PID)
        $candadoTomado.Write($bytes, 0, $bytes.Length)
        $candadoTomado.Flush()

        # cmd se lanza con ClassmerTrabajo:
        #   - bajo la tarea, sin ventana y con su propia consola (CREATE_NO_WINDOW): Windows
        #     Terminal no la adopta, y el server no depende del conhost de la tarea (si alguien lo
        #     cierra, el server y lo que lanza, como claude.exe, siguen pudiendo arrancar procesos).
        #     En una corrida a mano comparte la consola, así Ctrl+C o cerrarla lo cortan también;
        #   - suspendido hasta quedar en un job object, así todo lo que cuelga de él queda adentro.
        #     Cuando cmd termina se cierra lo que haya quedado (p. ej. un claude.exe de un apunte a
        #     medias) para que no siga en paralelo con el server nuevo. Sin KILL_ON_JOB_CLOSE: si se
        #     muere este -Correr, el server sigue.
        # Si no compila (o en esta consola quedó cargado uno viejo), se lanza como antes, sin job
        # object.
        if (-not ('ClassmerTrabajo' -as [type])) {
            try {
                Add-Type -ErrorAction Stop -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

public sealed class ClassmerProceso {
    public IntPtr Proceso;
    public IntPtr Trabajo;
    public int Pid;
}

public static class ClassmerTrabajo {
    const uint CREATE_SUSPENDED = 0x00000004;
    const uint CREATE_NO_WINDOW = 0x08000000;
    const uint PROCESS_TERMINATE = 0x0001;
    const uint PROCESS_SET_QUOTA = 0x0100;

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    struct STARTUPINFO {
        public int cb; public string lpReserved; public string lpDesktop; public string lpTitle;
        public int dwX; public int dwY; public int dwXSize; public int dwYSize;
        public int dwXCountChars; public int dwYCountChars; public int dwFillAttribute; public int dwFlags;
        public short wShowWindow; public short cbReserved2; public IntPtr lpReserved2;
        public IntPtr hStdInput; public IntPtr hStdOutput; public IntPtr hStdError;
    }
    [StructLayout(LayoutKind.Sequential)]
    struct PROCESS_INFORMATION { public IntPtr hProcess; public IntPtr hThread; public int dwProcessId; public int dwThreadId; }

    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern bool CreateProcess(string aplicacion, StringBuilder linea, IntPtr atributosProceso,
        IntPtr atributosHilo, bool heredarHandles, uint flags, IntPtr entorno, string carpeta,
        ref STARTUPINFO inicio, out PROCESS_INFORMATION info);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern IntPtr CreateJobObject(IntPtr atributos, string nombre);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool AssignProcessToJobObject(IntPtr trabajo, IntPtr proceso);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool TerminateJobObject(IntPtr trabajo, uint codigo);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint ResumeThread(IntPtr hilo);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern uint WaitForSingleObject(IntPtr handle, uint milisegundos);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool GetExitCodeProcess(IntPtr proceso, out uint codigo);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern bool CloseHandle(IntPtr handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    static extern IntPtr OpenProcess(uint acceso, bool heredar, int pid);

    public static ClassmerProceso Lanzar(string linea, string carpeta, bool sinVentana) {
        STARTUPINFO inicio = new STARTUPINFO();
        inicio.cb = Marshal.SizeOf(typeof(STARTUPINFO));
        PROCESS_INFORMATION info;
        uint flags = CREATE_SUSPENDED | (sinVentana ? CREATE_NO_WINDOW : 0);
        if (!CreateProcess(null, new StringBuilder(linea), IntPtr.Zero, IntPtr.Zero, false,
                flags, IntPtr.Zero, carpeta, ref inicio, out info)) {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }
        ClassmerProceso p = new ClassmerProceso();
        p.Proceso = info.hProcess;
        p.Pid = info.dwProcessId;
        IntPtr trabajo = CreateJobObject(IntPtr.Zero, null);
        if (trabajo != IntPtr.Zero) {
            if (AssignProcessToJobObject(trabajo, info.hProcess)) { p.Trabajo = trabajo; } else { CloseHandle(trabajo); }
        }
        ResumeThread(info.hThread);
        CloseHandle(info.hThread);
        return p;
    }

    public static bool Esperar(ClassmerProceso p, int milisegundos) {
        return WaitForSingleObject(p.Proceso, (uint)milisegundos) == 0;
    }

    public static int Codigo(ClassmerProceso p) {
        uint codigo;
        GetExitCodeProcess(p.Proceso, out codigo);
        return unchecked((int)codigo);
    }

    public static void Terminar(ClassmerProceso p) {
        TerminarTrabajo(p.Trabajo);
        p.Trabajo = IntPtr.Zero;
        if (p.Proceso != IntPtr.Zero) { CloseHandle(p.Proceso); p.Proceso = IntPtr.Zero; }
    }

    // Un proceso que ya existe (de un server que no lancé yo) a un job object propio: lo que lance
    // después hereda el job. Si ya está en otro job, el nuevo queda anidado (Windows 8 o más).
    public static IntPtr Adoptar(int pid) {
        IntPtr proceso = OpenProcess(PROCESS_TERMINATE | PROCESS_SET_QUOTA, false, pid);
        if (proceso == IntPtr.Zero) return IntPtr.Zero;
        IntPtr trabajo = CreateJobObject(IntPtr.Zero, null);
        if (trabajo != IntPtr.Zero && !AssignProcessToJobObject(trabajo, proceso)) { CloseHandle(trabajo); trabajo = IntPtr.Zero; }
        CloseHandle(proceso);
        return trabajo;
    }

    public static void TerminarTrabajo(IntPtr trabajo) {
        if (trabajo == IntPtr.Zero) return;
        TerminateJobObject(trabajo, 1);
        CloseHandle(trabajo);
    }
}
'@
            } catch {
                Anotar-Log ('no pude compilar el lanzador ({0}): lanzo el server como antes, sin job object' -f $_.Exception.Message)
            }
        }
        $tipo = 'ClassmerTrabajo' -as [type]
        $conLanzador = [bool]($tipo -and $tipo.GetMethod('Lanzar', [type[]]@([string], [string], [bool])) -and $tipo.GetMethod('Adoptar'))
        if ($tipo -and -not $conLanzador) {
            Anotar-Log 'en esta consola quedó cargado un lanzador viejo: lanzo el server como antes, sin job object'
        }

        # Si corro bajo el conhost de la tarea y alguien lo cierra (Stop-ScheduledTask,
        # "Finalizar" en el Programador), el Programador ya no ve la tarea corriendo y yo quedo sin
        # consola. Entonces salgo, dejo el server andando (tiene su propia consola) y el
        # disparador levanta otro -Correr que lo cuida.
        $consola = $null
        try {
            $yo = @(Get-CimInstance Win32_Process -Filter "ProcessId = $PID" -ErrorAction Stop | Where-Object { [int]$_.ProcessId -eq $PID })
            if ($yo.Count -gt 0) {
                $idPadre = [int]$yo[0].ParentProcessId
                $padre = @(Get-CimInstance Win32_Process -Filter "ProcessId = $idPadre" -ErrorAction Stop | Where-Object { [int]$_.ProcessId -eq $idPadre })
                if ($padre.Count -gt 0 -and $padre[0].Name -eq 'conhost.exe') {
                    $consola = Get-Process -Id $idPadre -ErrorAction Stop
                    $null = $consola.Handle
                }
            }
        } catch {
            $consola = $null
        }
        function Salir-Si-No-Hay-Consola {
            if ($consola -and $consola.HasExited) {
                Anotar-Log ('se cerró la consola de la tarea (conhost pid {0}): salgo y el disparador levanta otro -Correr' -f $consola.Id)
                exit 0
            }
        }

        # Mientras espero a un server que no lancé yo (p. ej. el que dejó el -Correr anterior), meto
        # cada proceso de su árbol en un job object propio, y lo que lancen después lo hereda: si
        # se muere, cierro lo que haya quedado colgando, como con los que lanzo yo.
        $adoptados = @{}
        function Adoptar-Arbol([int[]] $Oyentes) {
            $procesos = @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue)
            $porPid = @{}
            foreach ($p in $procesos) { $porPid[[int]$p.ProcessId] = $p }
            foreach ($id in $Oyentes) {
                $p = $porPid[[int]$id]
                if (-not $p -or -not $p.CommandLine -or $p.CommandLine -notmatch $PatronServe) { continue }
                foreach ($q in (Arbol-De $procesos ([int](Subir-Hasta-Raiz $p $porPid).ProcessId))) {
                    if (Es-Bucle $q) { continue }
                    $nacio = if ($q.PSObject.Properties['CreationDate']) { $q.CreationDate } else { '' }
                    $clave = '{0}|{1}' -f $q.ProcessId, $nacio
                    if (-not $adoptados.ContainsKey($clave)) { $adoptados[$clave] = [ClassmerTrabajo]::Adoptar([int]$q.ProcessId) }
                }
            }
        }

        $fallasRapidas = 0
        $esperando = $false
        while ($true) {
            # Un error inesperado en una vuelta no corta la vigilancia: se anota y se vuelve a probar.
            try {
                Salir-Si-No-Hay-Consola
                # Rotación simple: si el log pasa de 10 MB, el anterior queda como server-consola.1.log.
                if ((Test-Path -LiteralPath $LogConsola) -and (Get-Item -LiteralPath $LogConsola).Length -gt 10MB) {
                    Move-Item -LiteralPath $LogConsola -Destination (Join-Path $CarpetaLogs 'server-consola.1.log') -Force
                }
                # Si ya hay un server escuchando (p. ej. sobrevivió al -Correr anterior), no lo toco:
                # espero a que se libere el puerto.
                $oyentes = @(Oyentes-Del-Puerto)
                if ($oyentes.Count -gt 0) {
                    if (-not $esperando) {
                        Anotar-Log ('ya hay un server escuchando en el puerto {0} (pid {1}): no lo toco; vuelvo a mirar cada 30 s' -f $Puerto, ($oyentes -join ', '))
                        $esperando = $true
                    }
                    if ($conLanzador) { Adoptar-Arbol $oyentes }
                    Start-Sleep -Seconds 30
                    continue
                }
                $esperando = $false
                if ($adoptados.Count -gt 0) {
                    # El server adoptado ya no escucha: cierro lo que haya quedado de él.
                    foreach ($trabajo in @($adoptados.Values)) { [ClassmerTrabajo]::TerminarTrabajo($trabajo) }
                    $adoptados.Clear()
                }
                # Lo que quedó de un -Correr anterior y todavía no escucha (p. ej. uno que se quedó
                # colgado arrancando): lo cierro antes de levantar el nuevo.
                $restos = @(Cerrar-Restos)
                if ($restos.Count -gt 0) {
                    Anotar-Log ('cierro restos de un server anterior: {0}' -f (Describir-Restos $restos))
                }
                Anotar-Log ('arranca Classmer server ({0})' -f $CarpetaServer)
                $desde = Get-Date
                # cmd hace la redirección: los bytes de uv/python van tal cual (UTF-8) y se agregan al log.
                $comando = '/d /c ""{0}" run classmer serve >> "{1}" 2>&1"' -f $Uv, $LogConsola
                # Espero sólo a cmd, mirando cada 5 s si sigo teniendo consola (Start-Process -Wait de
                # PowerShell 5.1 espera también a todo lo que cuelga de él, y un huérfano del server
                # muerto no dejaba relanzarlo).
                if ($conLanzador) {
                    $server = [ClassmerTrabajo]::Lanzar(('"{0}" {1}' -f $env:ComSpec, $comando), $CarpetaServer, [bool]$consola)
                    while (-not [ClassmerTrabajo]::Esperar($server, 5000)) { Salir-Si-No-Hay-Consola }
                    $codigo = [ClassmerTrabajo]::Codigo($server)
                    [ClassmerTrabajo]::Terminar($server)
                } else {
                    $proceso = Start-Process -FilePath $env:ComSpec -ArgumentList $comando -NoNewWindow -PassThru
                    $null = $proceso.Handle   # sin el handle, PS 5.1 puede quedarse sin ExitCode
                    while (-not $proceso.WaitForExit(5000)) { Salir-Si-No-Hay-Consola }
                    $codigo = $proceso.ExitCode
                }
                Anotar-Log ('el server terminó con código {0}' -f $codigo)
                # Salida limpia (Ctrl+C en una corrida a mano): no lo relanzo. Con la tarea, el
                # disparador lo vuelve a levantar en 5 minutos; para que quede parado, -Parar.
                if ($codigo -eq 0) { exit 0 }
                # Se cayó: lo relanzo con espera. Si se cae enseguida muchas veces seguidas (p. ej.
                # el puerto lo tiene otro programa), espero más pero no salgo.
                if (((Get-Date) - $desde).TotalSeconds -lt 60) { $fallasRapidas++ } else { $fallasRapidas = 0 }
                if ($fallasRapidas -ge 5) {
                    Anotar-Log '5 caídas seguidas: espero 5 minutos y vuelvo a probar'
                    $fallasRapidas = 0
                    Start-Sleep -Seconds 300
                    continue
                }
                Start-Sleep -Seconds 30
            } catch {
                Anotar-Log ('error inesperado: {0}; vuelvo a probar en 30 s' -f $_.Exception.Message)
                Start-Sleep -Seconds 30
            }
        }
    } catch {
        Anotar-Log ('error inesperado: {0}; salgo y el disparador levanta otro -Correr' -f $_.Exception.Message)
        exit 1
    } finally {
        # También con exit y con Ctrl+C: si no, una consola abierta que corrió -Correr a mano se
        # queda con el candado y la tarea no puede volver a levantar el server.
        $candadoTomado.Dispose()
    }
    # -Correr nunca sigue de largo a los otros modos (el de alta registraría la tarea).
    exit 1
}

# --- modos baja y parar -----------------------------------------------------------------------

if ($Quitar -or $Parar) {
    $tarea = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
    if (-not $tarea) {
        Write-Host "No existe la tarea '$NombreTarea': no cierro nada."
        return
    }
    Usar-Lo-De-La-Tarea $tarea
    if ($Quitar) {
        Stop-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $NombreTarea -Confirm:$false
        Write-Host "Tarea '$NombreTarea' eliminada."
    } else {
        # Deshabilitada, el disparador de cada 5 minutos no lo vuelve a levantar.
        Disable-ScheduledTask -TaskName $NombreTarea | Out-Null
        Stop-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
    }
    # Parar la tarea corta su proceso, pero -Correr, uv y python pueden quedar vivos: los cierro.
    $restos = @(Cerrar-Restos -Todo)
    if ($restos.Count -gt 0) { Write-Host ('Cerré: {0}' -f (Describir-Restos $restos)) }
    if ($Parar) { Write-Host "Server parado. La tarea '$NombreTarea' queda deshabilitada hasta -Reiniciar." }
    return
}

# --- modo reinicio ----------------------------------------------------------------------------

if ($Reiniciar) {
    $tarea = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
    if (-not $tarea) {
        throw "No existe la tarea '$NombreTarea'. Registrala primero (este script sin parámetros)."
    }
    Usar-Lo-De-La-Tarea $tarea
    $conRepeticion = @($tarea.Triggers | Where-Object { $_.Repetition -and $_.Repetition.Interval })
    if (@($tarea.Actions)[0].Execute -notmatch 'conhost\.exe$' -or $conRepeticion.Count -eq 0) {
        Write-Warning "La tarea '$NombreTarea' está registrada a la antigua (con ventana o sin el disparador de 5 minutos): registrala de nuevo con este script sin parámetros y después -Reiniciar."
    }
    if (-not $tarea.Settings.Enabled) {
        Enable-ScheduledTask -TaskName $NombreTarea | Out-Null   # la había dejado así -Parar
    }
    $antes = (Get-ScheduledTaskInfo -TaskName $NombreTarea).LastRunTime
    $arranco = $false
    $ajenos = @()
    # Con MultipleInstances IgnoreNew, un Start mientras la tarea todavía figura "Running" se
    # ignora en silencio (pasó el 2026-10-14: -Reiniciar durante la primera corrida, que bajaba
    # dependencias, dejó el server caído). Por eso: parar, esperar a que deje de figurar
    # corriendo, arrancar y CONFIRMAR que hubo una corrida nueva; si no, reintentar.
    for ($intento = 1; $intento -le 3 -and -not $arranco; $intento++) {
        Stop-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
        # Stop-ScheduledTask corta el proceso de la tarea, pero no uv/python: los cierro yo.
        $restos = @(Cerrar-Restos -Todo)
        if ($restos.Count -gt 0) { Write-Host ('Cerré: {0}' -f (Describir-Restos $restos)) }
        # Lo que todavía escucha no es de Classmer (o no lo pude cerrar): el server nuevo lo va a
        # esperar, así que no cuenta como "volvió a escuchar".
        $ajenos = @(Oyentes-Del-Puerto)
        if ($ajenos.Count -gt 0) {
            Write-Warning ("El puerto {0} sigue tomado por el pid {1}, que no cerré: el server nuevo va a esperar a que se libere." -f $Puerto, ($ajenos -join ', '))
        }
        for ($i = 0; $i -lt 120; $i++) {
            $tarea = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue
            if (-not $tarea -or $tarea.State -ne 'Running') { break }
            Start-Sleep -Milliseconds 500
        }
        Start-ScheduledTask -TaskName $NombreTarea
        for ($i = 0; $i -lt 20; $i++) {
            if ((Get-ScheduledTaskInfo -TaskName $NombreTarea).LastRunTime -gt $antes) { $arranco = $true; break }
            Start-Sleep -Milliseconds 500
        }
        if (-not $arranco) { Write-Host "El Programador ignoró el arranque (intento $intento de 3); vuelvo a probar." }
    }
    if (-not $arranco) {
        throw "No pude volver a arrancar la tarea '$NombreTarea'. Probá Start-ScheduledTask -TaskName '$NombreTarea' y mirá $LogConsola"
    }
    # El primer arranque después de un merge puede crear el .venv y bajar dependencias: hasta 2 min.
    $escucha = $false
    for ($i = 0; $i -lt 120; $i++) {
        if (@(Oyentes-Del-Puerto | Where-Object { $ajenos -notcontains $_ }).Count -gt 0) { $escucha = $true; break }
        Start-Sleep -Seconds 1
    }
    if ($escucha) {
        Write-Host "Tarea '$NombreTarea' reiniciada: el server escucha en el puerto $Puerto. Log: $LogConsola"
    } else {
        Write-Warning "La tarea arrancó pero el server todavía no escucha en el puerto ${Puerto}: mirá $LogConsola"
    }
    return
}

# --- modo alta --------------------------------------------------------------------------------

if ($CarpetaServer -match '\\\.claude\\worktrees\\') {
    Write-Warning "Estás en un worktree ($CarpetaServer). La tarea va a apuntar acá; conviene correrlo desde el checkout principal."
}
if (-not (Test-Path -LiteralPath (Join-Path $CarpetaServer 'pyproject.toml'))) {
    throw "No encuentro server\pyproject.toml en $CarpetaServer"
}
$comandoUv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $comandoUv) {
    throw 'No encuentro uv en el PATH. Instalalo primero (https://docs.astral.sh/uv/).'
}
if (-not (Test-Path -LiteralPath (Join-Path $CarpetaDatos 'config.yaml'))) {
    Write-Warning "No existe $CarpetaDatos\config.yaml: corré 'uv run classmer init' antes de arrancar el server."
}
$existente = Get-ScheduledTask -TaskName $NombreTarea -ErrorAction SilentlyContinue

$usuario = [Security.Principal.WindowsIdentity]::GetCurrent().Name   # DOMINIO\usuario
# conhost --headless: la consola existe (uv/python la necesitan) pero sin ventana, y Windows
# Terminal no la adopta. Con "powershell -WindowStyle Hidden" a secas, sí.
$powershell = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$argumentos = '--headless "{0}" -NoProfile -NonInteractive -WindowStyle Hidden -ExecutionPolicy Bypass -File "{1}" -Correr -Uv "{2}" -Puerto {3}' -f `
    $powershell, $PSCommandPath, $comandoUv.Source, $Puerto
# Una raíz como "D:\" va como "D:\\": si no, la barra escapa la comilla de cierre.
if ($Datos) { $argumentos += ' -Datos "{0}"' -f ($CarpetaDatos -replace '\\$', '\\') }

$accion = New-ScheduledTaskAction -Execute (Join-Path $env:SystemRoot 'System32\conhost.exe') -Argument $argumentos -WorkingDirectory $CarpetaServer
$alIniciarSesion = New-ScheduledTaskTrigger -AtLogOn -User $usuario
# Y cada 5 minutos, sin fin, desde 5 minutos después de registrarla: si la tarea corre, el
# Programador lo ignora (IgnoreNew); si no, la arranca. Va aparte porque la repetición de un
# disparador de inicio de sesión recién empieza en el próximo inicio de sesión.
$cadaCincoMinutos = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(5) -RepetitionInterval (New-TimeSpan -Minutes 5)
$cadaCincoMinutos.Repetition.StopAtDurationEnd = $false
$principal = New-ScheduledTaskPrincipal -UserId $usuario -LogonType Interactive -RunLevel Limited
$ajustes = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -Priority 5
# Priority 5 = normal (por defecto las tareas corren con prioridad baja, 7).

Register-ScheduledTask -TaskName $NombreTarea `
    -Description 'Server de Classmer (API + worker). Lo registra server\scripts\registrar-arranque.ps1.' `
    -Action $accion -Trigger @($alIniciarSesion, $cadaCincoMinutos) -Principal $principal -Settings $ajustes -Force | Out-Null
# Registrar de nuevo la deja habilitada: si estaba parada con -Parar (p. ej. en medio de un
# uv sync), que siga así hasta -Reiniciar.
$sigueParada = $existente -and -not $existente.Settings.Enabled
if ($sigueParada) { Disable-ScheduledTask -TaskName $NombreTarea | Out-Null }

$comoReiniciar = "powershell -ExecutionPolicy Bypass -File `"$PSCommandPath`" -Reiniciar"
if ($NombreTarea -ne $TareaDeProduccion) { $comoReiniciar += " -NombreTarea '$NombreTarea'" }
Write-Host "Tarea '$NombreTarea' registrada para $usuario."
Write-Host "  Arranca al iniciar sesión, sin ventana: uv run classmer serve (en $CarpetaServer)"
Write-Host "  Puerto $Puerto, datos en $CarpetaDatos"
if (-not $sigueParada) {
    Write-Host "  Si no está corriendo, la arranca en 5 minutos como mucho (también ahora, si no la arrancás antes)."
}
Write-Host "  Log de la consola: $LogConsola"
if ($sigueParada) {
    Write-Host "Estaba parada con -Parar y sigue deshabilitada. Para levantarla: $comoReiniciar"
} elseif ($existente) {
    Write-Host "Ya existía: si estaba corriendo, sigue con lo de antes. Para pasar a lo nuevo: $comoReiniciar"
} else {
    Write-Host "Para arrancarla ya: Start-ScheduledTask -TaskName '$NombreTarea'"
}
Write-Host "Para reiniciar el server: $comoReiniciar"
Write-Host "Para pararlo sin que vuelva solo (p. ej. uv sync): ... -Parar (y después -Reiniciar)"
Write-Host 'Recordá: la primera vez Windows pide permiso de red para Python; aceptá en "Redes privadas".'
