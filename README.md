# helmcode-whisper — interfaz local

Una interfaz mínima para [helmcode-whisper](../Helmcode-whisper): darle a grabar,
leer las notas y la transcripción, y buscar en el histórico.

**Vive fuera del repo a propósito.** helmcode-whisper es una CLI, y la gracia de
una CLI es que cada uno se monte encima lo que quiera. Esta es una de esas cosas,
no *la* interfaz oficial. Si un día se publica una, será otra decisión.

## Cómo se levanta

Necesita el paquete instalado, así que se lanza con el intérprete del venv del
repo y **desde el directorio del repo**, que es donde está el `.env`:

```powershell
cd C:\dev\Helmcode-whisper
.venv\Scripts\python.exe ..\helmcode-whisper-ui\app.py
```

Y se abre <http://127.0.0.1:7864>.

Si lo lanzas desde otro sitio avisará de que no encuentra la `HELMCODE_API_KEY`:
grabar seguirá funcionando, procesar y la búsqueda por significado no.

## Qué hace

- **Grabar**: escribes el título, le das a grabar, y ves los medidores de las dos
  pistas en directo. Al parar avisa si alguna pista salió en silencio.
- **Procesar**: un botón por reunión sin procesar, con la salida de
  `hcw process` en vivo mientras corre.
- **Leer**: notas estructuradas (resumen, decisiones, action items, preguntas
  abiertas, citas) y la transcripción completa con hablantes y marcas de tiempo.
- **Buscar**: la misma búsqueda híbrida de la CLI sobre todas las reuniones.

## Cómo está hecho

Un fichero de Python con la biblioteca estándar y nada más, más un HTML. Sin
dependencias, sin build, sin npm.

- La grabación corre **dentro de este proceso**, con las mismas clases de captura
  que usa la CLI.
- El procesado se lanza como subproceso `hcw process`, para que una
  transcripción larga no se pueda llevar por delante la interfaz.
- Escucha solo en `127.0.0.1`. Un archivo de reuniones no debería convertirse en
  un servicio de la red local porque una interfaz venía bien.

## Lo que no hace

No hay autenticación, ni usuarios, ni cola de trabajos: es una herramienta para
una persona en su portátil. Una reunión grabándose a la vez, un procesado a la
vez. Si se cierra el servidor mientras graba, cierra los ficheros antes de salir.
