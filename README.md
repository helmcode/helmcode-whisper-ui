# helmcode-whisper — interfaz local

Una interfaz mínima para [helmcode-whisper](https://github.com/helmcode/helmcode-whisper):
grabar, escuchar, leer las notas y buscar en el histórico.

**Vive fuera del repo a propósito.** helmcode-whisper es una CLI, y la gracia de
una CLI es que cada uno se monte encima lo que quiera. Esta es una de esas
cosas, no *la* interfaz oficial. Un fichero de Python con la biblioteca
estándar, un HTML, y nada más: sin dependencias, sin build, sin npm.

Escucha solo en `127.0.0.1`. Un archivo de reuniones no debería convertirse en
un servicio de tu red porque una interfaz venía bien.

## Cómo se levanta

Necesita el paquete instalado, así que se lanza con el intérprete del entorno
donde esté y **desde el directorio de tu checkout de helmcode-whisper**, que es
donde vive el `.env`:

```powershell
cd <tu checkout de helmcode-whisper>
.venv\Scripts\python.exe ..\helmcode-whisper-ui\app.py
```

Y se abre <http://127.0.0.1:7864>. Si lo lanzas desde otro sitio avisa de que no
encuentra la `HELMCODE_API_KEY`: grabar seguirá funcionando, procesar y la
búsqueda por significado no.

`--host` y `--port` existen. Cambiar el host te avisa de lo que implica.

## Qué hace

**Grabar.** El botón está arriba a la derecha. Antes de empezar te dice de qué
dispositivos va a grabar y si falta alguno — un loopback ausente significa que
de la reunión solo se transcribirá lo que digas tú, y eso es mejor saberlo
antes que después. Mientras graba, una franja ocupa el ancho de la pantalla con
el reloj y los medidores de las dos pistas.

**Al parar, se procesa.** Nadie graba una reunión para no leerla. La espera son
seis pasos con su estado, no un log: fragmentos transcritos sobre el total, y
para la diarización una estimación sacada del factor medido. El log crudo está
detrás de «ver detalle», que es donde va un log.

**Escuchar.** Las dos pistas se mezclan una vez en un fichero compacto y se
sirven con rangos de bytes, así que se puede saltar por dentro de una hora de
audio sin descargarla entera. Clicas un turno de la transcripción y suena desde
ahí, con el turno actual marcado y la transcripción siguiéndolo.

**Nombres de las voces.** `SPEAKER_00` es correcto e inútil. Clicas el nombre en
cualquier turno y escribes; se aplica a todos los suyos. Y hay detección
automática: lee el transcript buscando presentaciones y nombres al dirigirse a
alguien, y **propone** — con la frase que lo sugiere y su nivel de confianza —
para que lo revises antes de guardar.

**Corregir las notas.** El modelo se equivoca, y unas notas que no puedes
corregir son unas notas en las que no confías del todo. Al guardar se reescriben
`notes.json`, `notes.md` y `notes.html` con los mismos renderizadores que usa la
CLI. Hay un botón para copiarlas como markdown, que es lo que uno hace de verdad
con un resumen.

**Espacios.** Grupos plegables en la barra lateral para organizar el histórico,
y la búsqueda se puede limitar a uno. Un espacio es una **etiqueta en
`meta.json`**, nunca una carpeta: las rutas están referenciadas en el índice de
búsqueda, y mover carpetas convertiría un renombrado en una migración.

**Buscar.** La misma búsqueda híbrida de la CLI sobre todas las reuniones. Un
resultado te abre su reunión en el momento exacto y empieza a reproducirlo.

**Teclado.** `ctrl+k` busca, `/` filtra la lista, `j` y `k` la recorren, espacio
reproduce y pausa.

**Borrar** mueve a `.trash/` dentro del archivo. Una hora de reunión es cerca de
un gigabyte de audio irrecuperable, y la diferencia entre un error y un desastre
es un `mv`. Las filas del índice sí se borran de verdad: que la búsqueda ofrezca
una frase de algo que borraste es peor que una carpeta ocupando sitio.

## Calendario (opcional)

```powershell
$env:HCW_ICS = "https://calendar.google.com/calendar/ical/.../basic.ics"
# o una ruta a un .ics exportado
```

Con esto, al darle a nueva reunión te propone el título del evento que esté
ocurriendo y, más útil, trae **la lista de invitados** — que es lo que convierte
adivinar el nombre de una voz en elegir entre cinco personas.

**Por qué está aquí y no en el paquete.** Una URL de iCal es una petición al
servidor de otro; para la «dirección secreta en formato iCal» de Google, a
Google. El paquete promete que el contenido de la reunión solo toca un host y
tiene un test que lo verifica leyendo todos sus módulos. Esa promesa es lo
importante del proyecto; el calendario es comodidad, así que vive de este lado.

`ical.py` es un parser estrecho, no uno general. Cubre lo que los calendarios
reales llevan dentro —líneas plegadas, texto escapado, las tres formas de
`DTSTART`, y recurrencia diaria y semanal con `COUNT`, `UNTIL`, `BYDAY` y
`EXDATE`— y lo que no soporta (mensual, `RDATE`, excepciones por instancia)
devuelve la ocurrencia real en vez de inventarse un calendario.

## Cómo está hecho

La grabación corre **dentro de este proceso**, con las mismas clases de captura
que usa la CLI. Procesar lanza `hcw process --progress-json` como subproceso, así
que una transcripción larga no se puede llevar la interfaz por delante, y sus
eventos se leen de stdout mientras el log humano se lee de stderr.

```bash
pytest -q      # 39 tests: rutas, espacios, notas, papelera y el parser de iCal
ruff check .
```

Ninguno abre un socket, un dispositivo de audio ni la red.

## Licencia

Apache-2.0, igual que helmcode-whisper.
