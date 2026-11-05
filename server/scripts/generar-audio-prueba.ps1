<#
.SYNOPSIS
    Genera el audio de una clase de prueba (clase-prueba.wav y clase-prueba.ogg) con las voces de Windows.

.DESCRIPTION
    Lee guion-clase-prueba.txt, sintetiza cada tramo con System.Speech (un .wav por tramo),
    agrega los silencios de los marcadores [PAUSA n] y [SILENCIO n] y concatena todo con ffmpeg.
    Después lo convierte a Ogg/Opus mono 32 kbps, como graba el celu.

    Si hay una sola voz en español (hoy: Microsoft Sabina Desktop, es-MX), el alumno usa la misma
    voz con otra velocidad y un tono más grave (ffmpeg asetrate + atempo).

    Además deja clase-prueba.tramos.json con el inicio y el fin (ms) de cada tramo, para comparar
    con lo que transcribe y segmenta el pipeline.

.PARAMETER Salida
    Carpeta donde quedan clase-prueba.wav, clase-prueba.ogg y clase-prueba.tramos.json.

.EXAMPLE
    .\generar-audio-prueba.ps1 -Salida C:\temp\clase-prueba
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Salida,
    [string] $Guion,                       # por defecto, guion-clase-prueba.txt al lado del script
    [string] $VozProfe,
    [string] $VozAlumno,
    [ValidateRange(-10, 10)] [int] $RateProfe = 0,
    [ValidateRange(-10, 10)] [int] $RateAlumno = 2,
    [ValidateRange(0.6, 1.6)] [double] $TonoAlumno = 0.86,   # 1 = sin cambio; < 1 más grave
    [double] $PausaParrafo = 0.4,                            # silencio entre párrafos seguidos (s)
    [switch] $ConservarTramos
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# En Windows PowerShell 5.1, $PSScriptRoot puede llegar vacío a los valores por defecto de param().
if (-not $Guion) { $Guion = Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'guion-clase-prueba.txt' }

$MUESTREO = 22050
$Invariante = [Globalization.CultureInfo]::InvariantCulture
$Utf8SinBom = New-Object System.Text.UTF8Encoding($false)

function Invoke-Ffmpeg([string[]] $Argumentos) {
    # ffmpeg escribe todo en stderr: lo junto y sólo lo muestro si falla. Con 'Stop', Windows
    # PowerShell cortaría en la primera línea de stderr, por eso acá va 'Continue'.
    $ErrorActionPreference = 'Continue'
    $salidaFf = & ffmpeg -hide_banner -nostdin -v error -y @Argumentos 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "ffmpeg falló ($LASTEXITCODE): $($salidaFf -join "`n")"
    }
}

function Get-DuracionWavMs([string] $Ruta) {
    # Lee el encabezado RIFF: tamaño del chunk data / bytes por segundo.
    $fs = [IO.File]::OpenRead($Ruta)
    try {
        $br = New-Object IO.BinaryReader($fs)
        $null = $br.ReadBytes(12)
        $bytesPorSegundo = 0
        while ($fs.Position + 8 -le $fs.Length) {
            $id = [Text.Encoding]::ASCII.GetString($br.ReadBytes(4))
            $tam = $br.ReadUInt32()
            if ($id -eq 'fmt ') {
                $fmt = $br.ReadBytes([int]$tam)
                $bytesPorSegundo = [BitConverter]::ToUInt32($fmt, 8)
                if ($tam % 2) { $null = $br.ReadByte() }
            } elseif ($id -eq 'data') {
                if ($bytesPorSegundo -eq 0) { throw "WAV sin chunk fmt: $Ruta" }
                return [int64][Math]::Round($tam * 1000.0 / $bytesPorSegundo)
            } else {
                $null = $fs.Seek($tam + ($tam % 2), [IO.SeekOrigin]::Current)
            }
        }
        throw "WAV sin chunk data: $Ruta"
    } finally {
        $fs.Dispose()
    }
}

function Read-Guion([string] $Ruta) {
    # Devuelve una lista de tramos: @{Tipo='habla'; Hablante; Texto} o @{Tipo='silencio'; Segundos; Etiqueta}.
    $tramos = New-Object System.Collections.Generic.List[object]
    $hablante = 'PROFE'
    $parrafo = New-Object System.Collections.Generic.List[string]
    $patron = '(\[(?:PROFE|ALUMNO|PAUSA\s+[\d.,]+|SILENCIO\s+[\d.,]+)\])'

    $cerrar = {
        if ($parrafo.Count -gt 0) {
            $texto = (($parrafo -join ' ') -replace '\s+', ' ').Trim()
            if ($texto) { $tramos.Add(@{ Tipo = 'habla'; Hablante = $hablante; Texto = $texto }) }
            $parrafo.Clear()
        }
    }

    foreach ($linea in [IO.File]::ReadAllLines($Ruta, [Text.Encoding]::UTF8)) {
        if ($linea.TrimStart().StartsWith('#')) { continue }
        if (-not $linea.Trim()) { & $cerrar; continue }
        foreach ($pieza in [regex]::Split($linea, $patron)) {
            if (-not $pieza.Trim()) { continue }
            $m = [regex]::Match($pieza, '^\[(PROFE|ALUMNO|PAUSA|SILENCIO)\s*([\d.,]*)\]$')
            if ($m.Success) {
                & $cerrar
                $marca = $m.Groups[1].Value
                if ($marca -in 'PROFE', 'ALUMNO') {
                    $hablante = $marca
                } else {
                    $seg = [double]::Parse($m.Groups[2].Value.Replace(',', '.'), $Invariante)
                    $tramos.Add(@{ Tipo = 'silencio'; Segundos = $seg; Etiqueta = $marca })
                }
            } else {
                $parrafo.Add($pieza.Trim())
            }
        }
    }
    & $cerrar
    return , $tramos
}

# --- chequeos ------------------------------------------------------------------------------------

foreach ($programa in 'ffmpeg', 'ffprobe') {
    if (-not (Get-Command $programa -ErrorAction SilentlyContinue)) { throw "No encuentro $programa en el PATH." }
}
if (-not (Test-Path -LiteralPath $Guion)) { throw "No existe el guion: $Guion" }

New-Item -ItemType Directory -Force -Path $Salida | Out-Null
$Salida = (Resolve-Path -LiteralPath $Salida).Path
$carpetaTramos = Join-Path $Salida 'tramos'
if (Test-Path -LiteralPath $carpetaTramos) { Remove-Item -LiteralPath $carpetaTramos -Recurse -Force }
New-Item -ItemType Directory -Force -Path $carpetaTramos | Out-Null

Add-Type -AssemblyName System.Speech
$synth = New-Object System.Speech.Synthesis.SpeechSynthesizer
try {
    $voces = @($synth.GetInstalledVoices() | Where-Object { $_.Enabled } | ForEach-Object { $_.VoiceInfo })
    $espanol = @($voces | Where-Object { $_.Culture.Name -like 'es-*' })
    if (-not $VozProfe) {
        if ($espanol.Count -eq 0) { throw 'No hay ninguna voz en español instalada (Configuración > Hora e idioma > Voz).' }
        $VozProfe = $espanol[0].Name
    }
    if (-not $VozAlumno) {
        $otra = @($espanol | Where-Object { $_.Name -ne $VozProfe })
        $VozAlumno = if ($otra.Count -gt 0) { $otra[0].Name } else { $VozProfe }
    }
    $mismaVoz = $VozAlumno -eq $VozProfe
    Write-Host "Voz del profe:  $VozProfe (rate $RateProfe)"
    Write-Host ("Voz del alumno: {0} (rate {1}{2})" -f $VozAlumno, $RateAlumno,
        $(if ($mismaVoz -and $TonoAlumno -ne 1) { ", tono x$TonoAlumno" } else { '' }))

    $formato = New-Object System.Speech.AudioFormat.SpeechAudioFormatInfo(
        $MUESTREO, [System.Speech.AudioFormat.AudioBitsPerSample]::Sixteen, [System.Speech.AudioFormat.AudioChannel]::Mono)

    # --- síntesis por tramo ----------------------------------------------------------------------
    $tramos = Read-Guion $Guion
    $archivos = New-Object System.Collections.Generic.List[object]
    $silencios = @{}
    $n = 0
    $anteriorHabla = $false

    $agregarSilencio = {
        param([double] $segundos, [string] $etiqueta)
        $clave = $segundos.ToString('0.###', $Invariante)
        if (-not $silencios.ContainsKey($clave)) {
            $ruta = Join-Path $carpetaTramos "silencio_$($clave.Replace('.', '_')).wav"
            Invoke-Ffmpeg @('-f', 'lavfi', '-i', "anullsrc=r=$($MUESTREO):cl=mono", '-t', $clave,
                '-c:a', 'pcm_s16le', $ruta)
            $silencios[$clave] = $ruta
        }
        $archivos.Add(@{ Tipo = $etiqueta; Ruta = $silencios[$clave]; Texto = $null; Hablante = $null })
    }

    foreach ($t in $tramos) {
        if ($t.Tipo -eq 'silencio') {
            & $agregarSilencio $t.Segundos $t.Etiqueta
            $anteriorHabla = $false
            continue
        }
        if ($anteriorHabla -and $PausaParrafo -gt 0) { & $agregarSilencio $PausaParrafo 'PARRAFO' }
        $n++
        $ruta = Join-Path $carpetaTramos ('tramo_{0:000}.wav' -f $n)
        $esAlumno = $t.Hablante -eq 'ALUMNO'
        $synth.SelectVoice($(if ($esAlumno) { $VozAlumno } else { $VozProfe }))
        $synth.Rate = $(if ($esAlumno) { $RateAlumno } else { $RateProfe })
        $synth.SetOutputToWaveFile($ruta, $formato)
        $synth.Speak($t.Texto)
        $synth.SetOutputToNull()
        if ($esAlumno -and $mismaVoz -and $TonoAlumno -ne 1) {
            $grave = Join-Path $carpetaTramos ('tramo_{0:000}_alumno.wav' -f $n)
            $tono = $TonoAlumno.ToString('0.###', $Invariante)
            $tempo = (1.0 / $TonoAlumno).ToString('0.######', $Invariante)
            Invoke-Ffmpeg @('-i', $ruta, '-af', "asetrate=$($MUESTREO)*$tono,aresample=$MUESTREO,atempo=$tempo",
                '-ac', '1', '-ar', "$MUESTREO", '-c:a', 'pcm_s16le', $grave)
            $ruta = $grave
        }
        $archivos.Add(@{ Tipo = 'habla'; Ruta = $ruta; Texto = $t.Texto; Hablante = $t.Hablante })
        $anteriorHabla = $true
        Write-Host ('  tramo {0,3} {1,-6} {2}' -f $n, $t.Hablante, ($t.Texto.Substring(0, [Math]::Min(60, $t.Texto.Length))))
    }
} finally {
    $synth.Dispose()
}

# --- concatenar, convertir y resumir -----------------------------------------------------------

$lista = Join-Path $carpetaTramos 'lista.txt'
$lineas = foreach ($a in $archivos) { "file '" + ($a.Ruta -replace '\\', '/' -replace "'", "'\''") + "'" }
[IO.File]::WriteAllLines($lista, [string[]]$lineas, $Utf8SinBom)

$wav = Join-Path $Salida 'clase-prueba.wav'
$ogg = Join-Path $Salida 'clase-prueba.ogg'
Invoke-Ffmpeg @('-f', 'concat', '-safe', '0', '-i', $lista, '-ac', '1', '-ar', "$MUESTREO", '-c:a', 'pcm_s16le', $wav)
Invoke-Ffmpeg @('-i', $wav, '-ac', '1', '-ar', '48000', '-c:a', 'libopus', '-b:a', '32k',
    '-application', 'voip', $ogg)

$marcas = New-Object System.Collections.Generic.List[object]
$cursor = [int64]0
foreach ($a in $archivos) {
    $dur = Get-DuracionWavMs $a.Ruta
    $item = [ordered]@{ tipo = $a.Tipo.ToLower(); inicio_ms = $cursor; fin_ms = $cursor + $dur }
    if ($a.Hablante) { $item.hablante = $a.Hablante }
    if ($a.Texto) { $item.texto = $a.Texto }
    $marcas.Add($item)
    $cursor += $dur
}
$json = ConvertTo-Json -InputObject $marcas.ToArray() -Depth 4
[IO.File]::WriteAllText((Join-Path $Salida 'clase-prueba.tramos.json'), $json, $Utf8SinBom)

if (-not $ConservarTramos) { Remove-Item -LiteralPath $carpetaTramos -Recurse -Force }

function Get-DuracionS([string] $Ruta) {
    $d = & ffprobe -v error -show_entries format=duration -of 'default=noprint_wrappers=1:nokey=1' $Ruta
    return [double]::Parse(($d | Select-Object -First 1), $Invariante)
}
$durWav = Get-DuracionS $wav
$durOgg = Get-DuracionS $ogg
$hablado = ($marcas | Where-Object { $_.tipo -eq 'habla' } | ForEach-Object { $_.fin_ms - $_.inicio_ms } |
    Measure-Object -Sum).Sum / 1000.0
$largo = $marcas | Where-Object { $_.tipo -eq 'silencio' } | Select-Object -First 1

Write-Host ''
Write-Host ('Listo: {0}' -f $Salida)
Write-Host ('  clase-prueba.wav  {0:mm\:ss}  ({1:N1} MB)' -f [TimeSpan]::FromSeconds($durWav), ((Get-Item -LiteralPath $wav).Length / 1MB))
Write-Host ('  clase-prueba.ogg  {0:mm\:ss}  ({1:N2} MB, Opus mono 32 kbps)' -f [TimeSpan]::FromSeconds($durOgg), ((Get-Item -LiteralPath $ogg).Length / 1MB))
Write-Host ('  hablado: {0:mm\:ss} en {1} tramos' -f [TimeSpan]::FromSeconds($hablado), $n)
if ($largo) {
    Write-Host ('  silencio largo: de {0:mm\:ss} a {1:mm\:ss} (la segmentación lo tiene que descartar)' -f `
        [TimeSpan]::FromMilliseconds($largo.inicio_ms), [TimeSpan]::FromMilliseconds($largo.fin_ms))
}
