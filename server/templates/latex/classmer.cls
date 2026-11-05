%% classmer.cls - plantilla de los apuntes de clase de Classmer (v1).
%%
%% Qué fija: SÓLO el look (página, letra, colores, encabezado y cajas).
%% Qué NO fija: el contenido. El apunte puede cargar cualquier paquete (TikZ,
%% pgfplots, circuitikz, siunitx, ...) y armar la estructura que mejor explique
%% la clase. La clase evita chocar con esos paquetes (ver la sección 9).
%%
%% Preámbulo obligatorio del apunte:
%%   \documentclass{classmer}
%%   \materia{Análisis Matemático II}
%%   \fechaclase{15 de octubre de 2026}
%%   \numeroclase{7}
%%   \tipoclase{Teórica}            % opcional
%%   \titulo{Derivada direccional y gradiente}
%%   \begin{document}               % el bloque de título se imprime solo
%%
%% Compila con pdflatex y con lualatex. El look sale de las fichas de estudio
%% que el usuario ya hacía a mano: misma paleta y mismas cajas.
%% Para retocar colores, ir a la sección 4; para las cajas, a la sección 7.
\NeedsTeXFormat{LaTeX2e}[2022/06/01]
\ProvidesClass{classmer}[2026/10/13 v1.0 Apuntes de clase de Classmer]

% ===========================================================================
% 1. Opciones de la clase
% ===========================================================================
%   [sinencabezado]       no imprime el bloque de título al empezar
%                         (si se quiere, \maketitle lo imprime donde se ponga)
%   [10pt] [11pt] [12pt]  tamaño de letra (por defecto 11pt)
%   [minutos]             muestra los \ts{hh:mm:ss} (por defecto son marcas internas
%                         invisibles: el usuario no los quiere en el PDF)
%   cualquier otra opción pasa a article (twocolumn, landscape, leqno, fleqn, ...)
\newif\ifclassmer@encabezado \classmer@encabezadotrue
\newif\ifclassmer@minutos
\def\classmer@tamano{11pt}
\DeclareOption{sinencabezado}{\classmer@encabezadofalse}
\DeclareOption{minutos}{\classmer@minutostrue}
\DeclareOption{10pt}{\def\classmer@tamano{10pt}}
\DeclareOption{11pt}{\def\classmer@tamano{11pt}}
\DeclareOption{12pt}{\def\classmer@tamano{12pt}}
\DeclareOption*{\PassOptionsToClass{\CurrentOption}{article}}
\ProcessOptions\relax
\PassOptionsToClass{a4paper,\classmer@tamano}{article}
\LoadClass{article}

% ===========================================================================
% 2. Paquetes base
% ===========================================================================
% Página A4 con márgenes cómodos para un apunte (la ficha usa ~1 cm).
\RequirePackage[a4paper, left=2cm, right=2cm, top=2.4cm, bottom=2.3cm,
  headheight=14pt, headsep=12pt, footskip=26pt]{geometry}
\RequirePackage{iftex}
% Matemática (antes de las fuentes: así fontspec no pisa nada de amsmath).
\RequirePackage{amsmath,amssymb,mathtools}
% Fuentes según el motor: pdflatex usa Latin Modern en T1; lualatex (o xelatex)
% usa fontspec, que por defecto también es Latin Modern (en OpenType); lmodern
% deja la matemática en Latin Modern también, igual que con pdflatex.
\iftutex
  \RequirePackage{fontspec}
  \RequirePackage{lmodern}
  \RequirePackage{newunicodechar}
  \def\classmer@uni#1#2{%
    \expandafter\newunicodechar\expandafter{\Uchar"#1}{\ensuremath{#2}}}
\else
  \RequirePackage[T1]{fontenc}
  \RequirePackage[utf8]{inputenc}
  \RequirePackage{lmodern}
  \def\classmer@uni#1#2{\DeclareUnicodeCharacter{#1}{\ensuremath{#2}}}
\fi
% Símbolos que a veces se escriben tal cual (→ ≤ ∈ α ℝ ...): con pdflatex
% darían "Unicode character not set up for use with LaTeX" y con lualatex
% desaparecerían (Latin Modern no los tiene). Se mapean a su versión
% matemática, que anda en texto y en fórmulas.
\classmer@uni{2192}{\to}             \classmer@uni{2190}{\leftarrow}
\classmer@uni{2194}{\leftrightarrow} \classmer@uni{21A6}{\mapsto}
\classmer@uni{21D2}{\Rightarrow}     \classmer@uni{21D0}{\Leftarrow}
\classmer@uni{21D4}{\Leftrightarrow} \classmer@uni{27F9}{\Longrightarrow}
\classmer@uni{27FA}{\Longleftrightarrow}
\classmer@uni{2264}{\leq}            \classmer@uni{2265}{\geq}
\classmer@uni{2260}{\neq}            \classmer@uni{2248}{\approx}
\classmer@uni{2261}{\equiv}          \classmer@uni{223C}{\sim}
\classmer@uni{2245}{\cong}           \classmer@uni{221D}{\propto}
\classmer@uni{2208}{\in}             \classmer@uni{2209}{\notin}
\classmer@uni{220B}{\ni}             \classmer@uni{2282}{\subset}
\classmer@uni{2283}{\supset}         \classmer@uni{2286}{\subseteq}
\classmer@uni{2287}{\supseteq}       \classmer@uni{222A}{\cup}
\classmer@uni{2229}{\cap}            \classmer@uni{2216}{\setminus}
\classmer@uni{2205}{\emptyset}       \classmer@uni{221E}{\infty}
\classmer@uni{2200}{\forall}         \classmer@uni{2203}{\exists}
\classmer@uni{2204}{\nexists}        \classmer@uni{2227}{\wedge}
\classmer@uni{2228}{\vee}            \classmer@uni{22A5}{\perp}
\classmer@uni{2218}{\circ}           \classmer@uni{22C5}{\cdot}
\classmer@uni{2202}{\partial}        \classmer@uni{2207}{\nabla}
\classmer@uni{221A}{\surd}           \classmer@uni{2211}{\sum}
\classmer@uni{220F}{\prod}           \classmer@uni{222B}{\int}
\classmer@uni{2016}{\|}              \classmer@uni{27E8}{\langle}
\classmer@uni{27E9}{\rangle}         \classmer@uni{2032}{'}
\classmer@uni{2212}{-}               \classmer@uni{2295}{\oplus}
\classmer@uni{2297}{\otimes}         \classmer@uni{2113}{\ell}
\classmer@uni{211D}{\mathbb{R}}      \classmer@uni{2115}{\mathbb{N}}
\classmer@uni{2124}{\mathbb{Z}}      \classmer@uni{211A}{\mathbb{Q}}
\classmer@uni{2102}{\mathbb{C}}
% Estrellas y tildes que el modelo copia de las marcas (⭐ marcado en clase):
\classmer@uni{2B50}{\bigstar}        \classmer@uni{2605}{\bigstar}
\classmer@uni{2606}{\star}           \classmer@uni{2713}{\checkmark}
\classmer@uni{2714}{\checkmark}      \classmer@uni{2717}{\times}
\classmer@uni{03B1}{\alpha}   \classmer@uni{03B2}{\beta}    \classmer@uni{03B3}{\gamma}
\classmer@uni{03B4}{\delta}   \classmer@uni{03B5}{\varepsilon} \classmer@uni{03B6}{\zeta}
\classmer@uni{03B7}{\eta}     \classmer@uni{03B8}{\theta}   \classmer@uni{03B9}{\iota}
\classmer@uni{03BA}{\kappa}   \classmer@uni{03BB}{\lambda}  \classmer@uni{03BC}{\mu}
\classmer@uni{03BD}{\nu}      \classmer@uni{03BE}{\xi}      \classmer@uni{03C0}{\pi}
\classmer@uni{03C1}{\rho}     \classmer@uni{03C2}{\varsigma} \classmer@uni{03C3}{\sigma}
\classmer@uni{03C4}{\tau}     \classmer@uni{03C5}{\upsilon} \classmer@uni{03C6}{\varphi}
\classmer@uni{03C7}{\chi}     \classmer@uni{03C8}{\psi}     \classmer@uni{03C9}{\omega}
\classmer@uni{0393}{\Gamma}   \classmer@uni{0394}{\Delta}   \classmer@uni{0398}{\Theta}
\classmer@uni{039B}{\Lambda}  \classmer@uni{039E}{\Xi}      \classmer@uni{03A0}{\Pi}
\classmer@uni{03A3}{\Sigma}   \classmer@uni{03A5}{\Upsilon} \classmer@uni{03A6}{\Phi}
\classmer@uni{03A8}{\Psi}     \classmer@uni{03A9}{\Omega}
% Español (el último idioma es el principal; inglés queda para
% \foreignlanguage{english}{...}). Opciones elegidas para que no rompa nada:
%   es-noshorthands  sin atajos activos (" ~ < > y el punto decimal): así TikZ,
%                    pgfplots y las flechas -> <-> andan sin \shorthandoff;
%   es-tabla         "Tabla" en vez de "Cuadro";
%   es-lcroman       números romanos en minúscula normal: i), ii), iii).
% Lo que sí queda de babel-spanish: guiones/silabeo, "Figura", "Índice",
% "Demostración", y los operadores con tilde: \lim -> lím, \max -> máx,
% \min -> mín, \inf -> ínf (más \sen, \tg, \arcsen, ...). \sin, \cos, \log,
% \exp, \sup, \det quedan igual. Para operadores sin tilde: \unaccentedoperators.
\RequirePackage[english, spanish, es-noshorthands, es-tabla, es-lcroman]{babel}
\RequirePackage{amsthm}
\RequirePackage{xcolor}
\RequirePackage{graphicx}
\RequirePackage{tikz}
\usetikzlibrary{arrows.meta, calc, positioning, babel}
\RequirePackage[most]{tcolorbox}
\RequirePackage[shortlabels, inline]{enumitem}
\RequirePackage{fancyhdr}
% hyperref va al final (va después de casi todo; cleveref, si el apunte lo usa,
% va después de la clase y por lo tanto después de hyperref, que es lo correcto).
\RequirePackage{hyperref}

% ===========================================================================
% 3. Fotos de la pizarra y material adjunto: dónde buscarlos
% ===========================================================================
% fotos/          carpeta publicada de la clase (al lado del .tex)
% ../input/fotos/ carpeta de trabajo del agente (se compila en output/)
% ../input/       por si el apunte escribe \pizarra{fotos/pizarra_07.jpg} (o
%                 \includegraphics{adjuntos/slides.pdf} en la carpeta de trabajo)
% adjuntos/ y ../input/adjuntos/: el material adjunto (PDF de slides o notas,
%                 imágenes), en la carpeta publicada y en la de trabajo; así
%                 también anda \includegraphics[page=2]{slides.pdf}
\graphicspath{{fotos/}{../input/fotos/}{../input/}{adjuntos/}{../input/adjuntos/}}

% ===========================================================================
% 4. Paleta (la misma de las fichas: tintas del manuscrito de la cátedra)
% ===========================================================================
\definecolor{azul}{RGB}{25,60,140}
\definecolor{rojo}{RGB}{190,30,35}
\definecolor{verde}{RGB}{18,120,60}
\definecolor{gris}{RGB}{105,105,105}
\definecolor{fondoazul}{RGB}{242,246,253}
\definecolor{fondoverde}{RGB}{242,250,244}
\definecolor{fondogris}{RGB}{248,248,246}
\definecolor{fondoamarillo}{RGB}{255,250,235}
\definecolor{marcoamarillo}{RGB}{200,160,60}
% Agregados para el apunte (no están en la ficha).
\definecolor{fondorojo}{RGB}{253,243,243}
\definecolor{violeta}{RGB}{112,56,160}
\definecolor{fondovioleta}{RGB}{248,244,253}
\definecolor{naranja}{RGB}{214,108,18}
\definecolor{fondonaranja}{RGB}{255,246,236}
\definecolor{azuloscuro}{RGB}{18,42,100}
\definecolor{turquesa}{RGB}{0,118,122}         % caja "importante"
\definecolor{fondoturquesa}{RGB}{235,247,247}
\definecolor{amarillo}{RGB}{255,206,40}        % la ⭐ de "marcado en clase"

% ===========================================================================
% 5. Página: párrafos, títulos de sección, listas, epígrafes, links
% ===========================================================================
\setlength{\parindent}{0pt}
\setlength{\parskip}{4pt plus 2pt minus 1pt}
\setlength{\emergencystretch}{1.5em}   % menos renglones que se salen del margen

% Secciones en azul (sin titlesec, para no chocar si el apunte lo carga).
\renewcommand\section{\@startsection{section}{1}{\z@}%
  {-3.2ex \@plus -1ex \@minus -.2ex}{1.4ex \@plus .2ex}%
  {\normalfont\Large\bfseries\color{azul}}}
\renewcommand\subsection{\@startsection{subsection}{2}{\z@}%
  {-2.6ex \@plus -1ex \@minus -.2ex}{0.9ex \@plus .2ex}%
  {\normalfont\large\bfseries\color{azul}}}
\renewcommand\subsubsection{\@startsection{subsubsection}{3}{\z@}%
  {-2.2ex \@plus -.8ex \@minus -.2ex}{0.6ex \@plus .2ex}%
  {\normalfont\normalsize\bfseries\color{azuloscuro}}}

% Listas un poco más compactas que las de article, con viñetas azules.
\setlist{topsep=3pt, partopsep=0pt, itemsep=1.5pt, parsep=0pt}
\setlist[itemize]{leftmargin=1.4em}
\setlist[itemize,1]{label=\textcolor{azul}{\textbullet}}
\setlist[itemize,2]{label=\textcolor{azul}{\normalfont\bfseries\textendash}}
\setlist[itemize,3]{label=\textcolor{azul}{\textperiodcentered}}
\setlist[enumerate]{leftmargin=1.9em}
\setlist[enumerate,1]{label=\textcolor{azul}{\arabic*.}, ref=\arabic*}
\setlist[enumerate,2]{label=\textcolor{azul}{\alph*)}, ref=\alph*}
\setlist[enumerate,3]{label=\textcolor{azul}{\roman*)}, ref=\roman*}

% Epígrafes: "Figura 3. texto", en letra chica.
\setlength{\abovecaptionskip}{5pt}
\setlength{\belowcaptionskip}{0pt}
\long\def\@makecaption#1#2{%
  \vskip\abovecaptionskip
  \sbox\@tempboxa{\small\textbf{#1.}\ #2}%
  \ifdim \wd\@tempboxa >\hsize
    {\small\textbf{#1.}\ #2\par}%
  \else
    \global\@minipagefalse
    \hb@xt@\hsize{\hfil\box\@tempboxa\hfil}%
  \fi
  \vskip\belowcaptionskip}

% Links sobrios: texto en azul, sin recuadros.
\hypersetup{colorlinks=true, linkcolor=azul, urlcolor=azuloscuro,
  citecolor=verde, filecolor=azul, bookmarksnumbered=true, bookmarksopen=true,
  pdfcreator={Classmer (classmer.cls)}}

% ===========================================================================
% 6. Datos de la clase, bloque de título, cabecera y pie
% ===========================================================================
\def\classmer@materia{}
\def\classmer@fecha{}
\def\classmer@numero{}
\def\classmer@tipo{}
\def\classmer@titulo{}
\newcommand\materia[1]{\gdef\classmer@materia{#1}}
\newcommand\numeroclase[1]{\gdef\classmer@numero{#1}}
\newcommand\titulo[1]{\gdef\classmer@titulo{#1}\gdef\@title{#1}}
% \tipoclase acepta "Teórica"/"Práctica" o las formas de la base (teorica/practica).
% \fechaclase acepta texto libre ("15 de octubre de 2026") o ISO (2026-10-15).
\ExplSyntaxOn
\NewDocumentCommand \tipoclase { m }
  {
    \str_case:nnF {#1}
      {
        { teorica }  { \gdef \classmer@tipo { Teórica } }
        { practica } { \gdef \classmer@tipo { Práctica } }
      }
      { \gdef \classmer@tipo {#1} }
  }
\seq_new:N \l__classmer_fecha_seq
\NewDocumentCommand \fechaclase { m }
  {
    \regex_match:nnTF { \A \s* \d{4} - \d{1,2} - \d{1,2} \s* \Z } {#1}
      {
        \seq_set_split:Nnn \l__classmer_fecha_seq { - } {#1}
        \xdef \classmer@fecha
          {
            \int_eval:n { \seq_item:Nn \l__classmer_fecha_seq { 3 } }
            \c_space_tl de \c_space_tl
            \int_case:nnF { \seq_item:Nn \l__classmer_fecha_seq { 2 } }
              {
                {1}{enero} {2}{febrero} {3}{marzo} {4}{abril} {5}{mayo}
                {6}{junio} {7}{julio} {8}{agosto} {9}{septiembre}
                {10}{octubre} {11}{noviembre} {12}{diciembre}
              }
              { \seq_item:Nn \l__classmer_fecha_seq { 2 } }
            \c_space_tl de \c_space_tl
            \int_eval:n { \seq_item:Nn \l__classmer_fecha_seq { 1 } }
          }
      }
      { \gdef \classmer@fecha {#1} }
  }
\ExplSyntaxOff
% \title también sirve (por costumbre); \titulo tiene prioridad.
\renewcommand\title[1]{\gdef\@title{#1}%
  \ifx\classmer@titulo\@empty\gdef\classmer@titulo{#1}\fi}

\def\classmer@punto{\ \textperiodcentered\ }
% "Clase 7 · Teórica · 15 de octubre de 2026" (salta lo que falte).
\newcommand\classmer@datos{%
  \begingroup
  \let\classmer@sep\@empty
  \ifx\classmer@numero\@empty\else
    Clase~\classmer@numero\let\classmer@sep\classmer@punto\fi
  \ifx\classmer@tipo\@empty\else
    \classmer@sep\classmer@tipo\let\classmer@sep\classmer@punto\fi
  \ifx\classmer@fecha\@empty\else
    \classmer@sep\classmer@fecha\fi
  \endgroup}
% "Materia · Clase 7" (para la cabecera).
\newcommand\classmer@cabecera{%
  \classmer@materia
  \ifx\classmer@numero\@empty\else
    \ifx\classmer@materia\@empty\else\classmer@punto\fi
    Clase~\classmer@numero
  \fi}

% Bloque de título: materia en gris, título grande, datos de la clase en gris.
% Si no hay ningún dato, no imprime nada.
\newif\ifclassmer@titulohecho
\newif\ifclassmer@hayfila   % hay número, tipo o fecha
\newif\ifclassmer@haydatos  % hay algo para imprimir
\newcommand\classmer@bloquetitulo{%
  \global\classmer@titulohechotrue
  \classmer@hayfilafalse
  \ifx\classmer@numero\@empty\else\classmer@hayfilatrue\fi
  \ifx\classmer@tipo\@empty\else\classmer@hayfilatrue\fi
  \ifx\classmer@fecha\@empty\else\classmer@hayfilatrue\fi
  \classmer@haydatosfalse
  \ifclassmer@hayfila\classmer@haydatostrue\fi
  \ifx\classmer@materia\@empty\else\classmer@haydatostrue\fi
  \ifx\classmer@titulo\@empty\else\classmer@haydatostrue\fi
  \ifclassmer@haydatos\classmer@imprimirtitulo\fi}
\newcommand\classmer@imprimirtitulo{%
  \thispagestyle{plain}%
  \begingroup
    \setlength{\parskip}{0pt}%
    \centering
    \ifx\classmer@materia\@empty\else
      {\normalsize\color{gris}\classmer@materia\par}%
      \vspace{3pt}%
    \fi
    \ifx\classmer@titulo\@empty\else
      {\LARGE\bfseries\color{azul}\classmer@titulo\par}%
      \vspace{5pt}%
    \fi
    \ifclassmer@hayfila
      {\small\color{gris}\classmer@datos\par}%
    \fi
    \vspace{6pt}%
    {\color{azul}\rule{\linewidth}{0.7pt}\par}%
  \endgroup
  \addvspace{10pt}}
\renewcommand\maketitle{\ifclassmer@titulohecho\else\classmer@bloquetitulo\fi}
\AddToHook{begindocument/end}{\ifclassmer@encabezado\classmer@bloquetitulo\fi}

% Metadatos del PDF (se fijan al cerrar el preámbulo, cuando ya están los datos).
\AddToHook{begindocument/before}{%
  \hypersetup{pdftitle={\classmer@titulo}, pdfsubject={\classmer@materia}}}

% Número de página "3 / 8". El total lo anota el propio LaTeX en el .aux
% (\@abspage@last) y se lee en la pasada siguiente: por eso se compila dos veces.
\newcommand\classmer@pagina{%
  \thepage
  \ifnum\@abspage@last<\maxdimen\,/\,\number\@abspage@last\fi}

% Cabecera: "Materia · Clase N" a la izquierda y la fecha a la derecha, con una
% raya gris; pie: número de página. La primera página (la del título) no lleva
% cabecera porque el bloque de título ya tiene esos datos.
\newcommand\classmer@letracabecera{\footnotesize\color{gris}}
\fancypagestyle{classmer}{%
  \fancyhf{}%
  \fancyhead[L]{\classmer@letracabecera\classmer@cabecera}%
  \fancyhead[R]{\classmer@letracabecera\classmer@fecha}%
  \fancyfoot[C]{\classmer@letracabecera\classmer@pagina}%
  \renewcommand\headrulewidth{0.4pt}%
  \renewcommand\footrulewidth{0pt}%
  \renewcommand\headrule{{\color{gris!45}%
    \hrule\@height\headrulewidth\@width\headwidth\vskip-\headrulewidth}}}
\fancypagestyle{plain}{%
  \fancyhf{}%
  \fancyfoot[C]{\classmer@letracabecera\classmer@pagina}%
  \renewcommand\headrulewidth{0pt}%
  \renewcommand\footrulewidth{0pt}}
\pagestyle{classmer}

% ===========================================================================
% 7. Cajas (tcolorbox), con el estilo "ficha" de las fichas de estudio
% ===========================================================================
% Uso: \begin{definicion} ... \end{definicion}
%      \begin{definicion}[Subespacio] ... \end{definicion}   -> "Definición · Subespacio"
% Todas se parten entre páginas (breakable) y aceptan adentro listas,
% ecuaciones, TikZ, minipages y otras cajas.
\tcbset{
  % La caja de la ficha, tal cual (sirve para copiar cajas de las fichas):
  %   \begin{tcolorbox}[ficha, colback=fondoazul, colframe=azul,
  %                     colbacktitle=azul, title={...}] ... \end{tcolorbox}
  ficha/.style={enhanced, sharp corners, boxrule=0.7pt, left=5pt, right=5pt,
    top=3pt, bottom=3pt, fonttitle=\bfseries\small, coltitle=white,
    before skip=5pt, after skip=5pt},
  % Base de los entornos del apunte: la ficha, partible entre páginas y con un
  % poco más de aire.
  classmer/base/.style={ficha, enhanced jigsaw, breakable, pad at break*=1mm,
    lines before break=4,
    left=6pt, right=6pt, top=4pt, bottom=4pt, toptitle=1.5pt, bottomtitle=1.5pt,
    before skip=9pt plus 2pt, after skip=9pt plus 2pt,
    before upper={\setlength{\parskip}{3pt plus 1pt}}},
  classmer/definicion/.style={classmer/base,
    colback=fondoazul, colframe=azul, colbacktitle=azul},
  classmer/teorema/.style={classmer/base,
    colback=fondoverde, colframe=verde, colbacktitle=verde},
  classmer/ejemplo/.style={classmer/base,
    colback=fondogris, colframe=gris!80!black, colbacktitle=gris!80!black},
  classmer/intuicion/.style={classmer/base,
    colback=fondoamarillo, colframe=marcoamarillo, colbacktitle=marcoamarillo},
  classmer/parcial/.style={classmer/base,
    colback=fondorojo, colframe=rojo, colbacktitle=rojo},
  % Lo importante (marca ⭐ del usuario o detectado por la IA): turquesa, con marco
  % más grueso y una franja a la izquierda, para que no se confunda con
  % "intuicion" (amarilla).
  classmer/importante/.style={classmer/base,
    colback=fondoturquesa, colframe=turquesa, colbacktitle=turquesa,
    boxrule=1.1pt, leftrule=4pt},
  classmer/duda/.style={classmer/base,
    colback=fondonaranja, colframe=naranja, colbacktitle=naranja},
  % Lo agregado por la IA se ve distinto a simple vista: sin barra de color
  % llena, borde violeta punteado y rótulo violeta en itálica.
  classmer/agregadoIA/.style={classmer/base,
    colback=fondovioleta, colbacktitle=violeta!12!white, coltitle=violeta,
    fonttitle=\bfseries\itshape\small, frame hidden,
    borderline={0.9pt}{0pt}{violeta, dash pattern=on 3.5pt off 2pt},
    titlerule=0.4pt, titlerule style={violeta!60, dash pattern=on 2pt off 1.5pt}},
}
% Rótulo de la caja: "Definición" o "Definición · <título opcional>".
\newcommand\classmer@rotulo[2]{%
  #1\if\relax\detokenize{#2}\relax\else\classmer@punto#2\fi}
% \nuevacaja{<entorno>}{<rótulo>}{<estilo>}: otra caja con el look de una de
% las de la plantilla. Ej.: \nuevacaja{lema}{Lema}{teorema}.
\newcommand\nuevacaja[3]{%
  \newtcolorbox{#1}[1][]{classmer/#3, title={\classmer@rotulo{#2}{##1}}}}
\nuevacaja{definicion}{Definición}{definicion}
\nuevacaja{teorema}{Teorema}{teorema}
\nuevacaja{ejemplo}{Ejemplo}{ejemplo}
\nuevacaja{intuicion}{Intuición}{intuicion}
\nuevacaja{agregadoIA}{Agregado de Classmer}{agregadoIA}
\nuevacaja{duda}{Duda / no entendí}{duda}

% Cajas con ORIGEN: importante y parcial muestran de dónde salen con un ícono
% chico a la derecha del rótulo, sin texto: una estrellita (marca ⭐ del usuario en
% clase) o un destellito de cuatro puntas, como el de Gemini (lo detectó Classmer).
%   \begin{importante}[clase] ... \end{importante}            origen, sin título
%   \begin{importante}[ia][Regla de la cadena] ...             origen y título
%   \begin{parcial}[clase][Entra hasta gradiente] ...
%   \begin{parcial}[Fecha del parcial] ...                     sin origen: el primer
%                                                              opcional es el título
\newcommand\classmer@destello{%
  \tikz[baseline=-0.55ex, x=0.75ex, y=0.75ex]
    \fill[white] (0,1) .. controls (0.12,0.12) .. (1,0) .. controls (0.12,-0.12) .. (0,-1)
                 .. controls (-0.12,-0.12) .. (-1,0) .. controls (-0.12,0.12) .. (0,1) -- cycle;}
\newcommand\classmer@tagclase{\textcolor{amarillo}{$\bigstar$}}
\newcommand\classmer@tagia{\classmer@destello}
\newcommand\classmer@tag[1]{\hfill{\normalfont\footnotesize\itshape #1}}
\ExplSyntaxOn
% #1 rótulo, #2 primer opcional (origen o título), #3 segundo opcional (título)
\cs_new_protected:Npn \classmer@rotuloorigen #1#2#3
  {
    \str_case_e:nnF { \tl_trim_spaces:n {#2} }
      {
        { clase } { \classmer@rotulo {#1} {#3} \classmer@tag { \classmer@tagclase } }
        { ia }    { \classmer@rotulo {#1} {#3} \classmer@tag { \classmer@tagia } }
        { IA }    { \classmer@rotulo {#1} {#3} \classmer@tag { \classmer@tagia } }
        { }       { \classmer@rotulo {#1} {#3} }
      }
      { \classmer@rotulo {#1} {#2} }
  }
\ExplSyntaxOff
\NewTColorBox{importante}{ O{} O{} }{classmer/importante,
  title={\classmer@rotuloorigen{Importante}{#1}{#2}}}
\NewTColorBox{parcial}{ O{} O{} }{classmer/parcial,
  title={\classmer@rotuloorigen{Para el parcial}{#1}{#2}}}

% ===========================================================================
% 8. Marcas del apunte: \ts, \nota, \pizarra
% ===========================================================================
% \ts{hh:mm:ss}: minuto del audio. Por defecto es una marca INTERNA que no se ve
% en el PDF: queda en el .tex para ubicar cada parte en el audio (el MCP, además,
% usa meta.json y el transcript). Se come el espacio de antes, así "texto \ts{..}."
% queda "texto.". Con la opción [minutos] se muestra en un recuadrito gris con un
% cronómetro, que se dibuja una sola vez (así \ts anda también dentro de TikZ).
\newsavebox\classmer@reloj
\AtBeginDocument{%
  \sbox\classmer@reloj{\begin{tikzpicture}[x=1pt, y=1pt, baseline=0pt,
      line width=0.5pt, line cap=round, draw=gris]
    \draw (0,2.5) circle (2.3);
    \draw (0,4.8) -- (0,5.7);
    \draw (-0.9,5.7) -- (0.9,5.7);
    \draw (0,2.5) -- (0,4.0);
    \draw (0,2.5) -- (1.1,1.8);
  \end{tikzpicture}}}
\DeclareRobustCommand\ts[1]{%
  \ifclassmer@minutos
    \if\relax\detokenize{#1}\relax\else\classmer@ts{#1}\fi
  \else
    \ifhmode\unskip\fi
  \fi}
\newcommand\classmer@ts[1]{%
  \leavevmode
  \begingroup
    \setlength\fboxsep{1pt}\setlength\fboxrule{0.4pt}%
    \fcolorbox{gris!55}{white}{\normalfont\sffamily\scriptsize\color{gris}%
      \usebox\classmer@reloj\kern1.5pt#1}%
  \endgroup}

% \nota{inaudible} / \nota{ilegible} / \nota{sic}: marca roja entre corchetes.
\DeclareRobustCommand\nota[1]{\textcolor{rojo}{\textnormal{[\textit{#1}]}}}

% En los marcadores del PDF, \nota queda como texto ([inaudible]); \ts, sólo con
% la opción [minutos] ([00:34:10]).
\ifclassmer@minutos
  \pdfstringdefDisableCommands{\def\ts#1{[#1]}\def\nota#1{[#1]}}
\else
  \pdfstringdefDisableCommands{\def\ts#1{}\def\nota#1{[#1]}}
\fi

% \pizarra{archivo}{hh:mm:ss}{epígrafe}: foto de la pizarra a lo ancho
% (alto máximo 0,45 de la página) con su epígrafe y el minuto. No flota: queda
% donde se escribe, así que también anda dentro de una caja. Si la foto no
% está, dibuja un recuadro "foto no encontrada" y avisa en el log (no corta).
% Para referenciarla: \pizarra{a.jpg}{00:10:00}{Texto\label{fig:a}}.
\newcommand\classmer@altofoto{0.45\textheight}
\newcommand\pizarra[3]{%
  \par\addvspace{\medskipamount}%
  \begingroup
    \centering
    \begin{minipage}{\linewidth}%
      \centering
      \classmer@siexiste{#1}%
        {\includegraphics[width=\linewidth, height=\classmer@altofoto,
          keepaspectratio]{#1}}%
        {\classmer@sinfoto{#1}}%
      \par
      % Epígrafe de figura fuera de un float (como \captionof). Si el apunte
      % cargó el paquete caption, se usa su forma, que respeta su formato.
      \ifdefined\captionsetup\captionsetup{type=figure}\else\def\@captype{figure}\fi
      \caption{\classmer@epigrafe{#2}{#3}}%
    \end{minipage}\par
  \endgroup
  \addvspace{\medskipamount}}
% El epígrafe; con [minutos], "[minuto] epígrafe", sin espacios de más si falta
% alguno de los dos.
\DeclareRobustCommand\classmer@epigrafe[2]{%
  \ifclassmer@minutos
    \ts{#1}%
    \if\relax\detokenize{#1}\relax\else\if\relax\detokenize{#2}\relax\else\enspace\fi\fi
  \fi
  #2}
% ¿Existe el archivo? Busca igual que \includegraphics: en la carpeta actual,
% en \graphicspath y, si no tiene extensión, probando las que conoce graphicx.
\newcommand\classmer@siexiste[3]{%
  \global\let\classmer@hallado\@empty
  \begingroup
    \ifdefined\Ginput@path\let\input@path\Ginput@path\fi
    \filename@parse{#1}%
    \ifx\filename@ext\relax
      \@for\classmer@ext:=\Gin@extensions\do{%
        \ifx\classmer@hallado\@empty
          \IfFileExists{#1\classmer@ext}{\gdef\classmer@hallado{1}}{}%
        \fi}%
    \else
      \IfFileExists{#1}{\gdef\classmer@hallado{1}}{}%
    \fi
  \endgroup
  \ifx\classmer@hallado\@empty
    \expandafter\@secondoftwo
  \else
    \expandafter\@firstoftwo
  \fi
  {#2}{#3}}
\newcommand\classmer@sinfoto[1]{%
  \ClassWarning{classmer}{Foto no encontrada: \detokenize{#1}}%
  \begin{tcolorbox}[enhanced, sharp corners, width=\linewidth,
      height=0.14\textheight, valign=center, halign=center, frame hidden,
      colback=fondorojo, borderline={0.6pt}{0pt}{rojo!70, dash pattern=on 3pt off 2pt},
      fontupper=\small\color{rojo}, before skip=0pt, after skip=0pt]
    foto no encontrada: \texttt{\detokenize{#1}}%
  \end{tcolorbox}}

% ===========================================================================
% 9. Atajos de la ficha y convivencia con los paquetes del apunte
% ===========================================================================
% \R \Q \CC \Z \tq \ok \cross: los mismos de las fichas. Se definen recién al
% cerrar el preámbulo y sólo si el apunte no los definió: así un
% \newcommand{\R}{...} en el apunte no da error (gana el del apunte).
\AddToHook{begindocument/before}{%
  \providecommand\R{\mathbb{R}}%
  \providecommand\Q{\mathbb{Q}}%
  \providecommand\CC{\mathbb{C}}%
  \providecommand\Z{\mathbb{Z}}%
  \providecommand\tq{\;/\;}%
  \providecommand\ok{{\color{verde}\checkmark}}%
  \providecommand\cross{{\color{rojo}\ensuremath{\boldsymbol{\times}}}}}

% cleveref y varioref no ven el idioma de babel si no va en \documentclass:
% se lo pasamos, así \cref escribe "sección 2" y no "section 2".
\PassOptionsToPackage{spanish}{cleveref}
\PassOptionsToPackage{spanish}{varioref}

% pgfplots: si el apunte lo carga, ya queda con compat (sin el aviso de
% "running in backwards compatibility mode") y con coma decimal en los ejes
% (3,5 en vez de 3.5). El apunte puede cambiar las dos cosas.
\AddToHook{package/pgfplots/after}{%
  \pgfplotsset{compat=1.18}%
  \pgfkeys{/pgf/number format/.cd, use comma, 1000 sep={\,}}}

% Volver a cargar con opciones un paquete que la clase ya cargó daría "Option
% clash". En vez de eso: geometry, hyperref y tcolorbox APLICAN las opciones
% nuevas (\geometry, \hypersetup, \tcbuselibrary); babel, enumitem, amsmath,
% graphicx, etc. las ignoran con un aviso en el log. (xcolor ya lo resuelve
% solo: \usepackage[dvipsnames]{xcolor} funciona.)
\def\classmer@reopciones#1#2{%
  % #1 paquete, #2 macro que recibe las opciones nuevas.
  \expandafter\let\csname classmer@opt@#1\expandafter\endcsname
    \csname opt@#1.sty\endcsname
  \@ifundefined{opt@handler@#1.sty}%
    {\@namedef{opt@handler@#1.sty}{\classmer@aplicar{#1}{#2}}}{}}
\def\classmer@aplicar#1#2{%
  % El kernel ya reemplazó \opt@<paquete>.sty por las opciones nuevas: se
  % guardan y se deja la lista vieja + las nuevas (para \@ifpackagewith).
  \expandafter\let\expandafter\classmer@nuevas\csname opt@#1.sty\endcsname
  \expandafter\let\expandafter\classmer@crudas\csname @raw@opt@#1.sty\endcsname
  \expandafter\let\expandafter\classmer@viejas\csname classmer@opt@#1\endcsname
  \expandafter\edef\csname opt@#1.sty\endcsname{\classmer@viejas,\classmer@nuevas}%
  \expandafter\let\csname classmer@opt@#1\expandafter\endcsname
    \csname opt@#1.sty\endcsname
  \ifx\classmer@nuevas\@empty\else
    \expandafter#2\expandafter{\classmer@crudas}%
  \fi}
\def\classmer@ignorar#1{%
  \let\classmer@extra\@empty
  \@for\classmer@o:=\classmer@nuevas\do{%
    \ifx\classmer@o\@empty\else
      \@expandtwoargs\in@{,\classmer@o,}{,\classmer@viejas,}%
      \ifin@\else\edef\classmer@extra{\classmer@extra\space\classmer@o}\fi
    \fi}%
  \ifx\classmer@extra\@empty\else
    \ClassWarningNoLine{classmer}{El paquete \@currname\space ya lo carga
      classmer.cls;\MessageBreak se ignoran las opciones:\classmer@extra}%
  \fi}
\classmer@reopciones{geometry}{\geometry}
\classmer@reopciones{hyperref}{\hypersetup}
\classmer@reopciones{tcolorbox}{\tcbuselibrary}
\@for\classmer@p:={babel,enumitem,amsmath,amssymb,mathtools,amsthm,graphicx,%
  fancyhdr,tikz,iftex,fontenc,inputenc,lmodern,fontspec}\do{%
  \@ifpackageloaded{\classmer@p}%
    {\expandafter\classmer@reopciones\expandafter{\classmer@p}{\classmer@ignorar}}{}}

\endinput
