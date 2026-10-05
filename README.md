# JW Library Notes Sync

Servicio Docker que mantiene una carpeta de **notas Markdown** idéntica a las notas de la última copia `.jwlibrary`. No exporta imágenes, playlists, subrayados, índices ni otros archivos.

## Formato de las notas

Una nota con título `Cultivar el ánimo` y etiquetas `Vida cristiana` y `Ánimo` genera `Cultivar el ánimo.md`:

```markdown
# Cultivar el ánimo

#Vida_cristiana #Ánimo

El contenido de la nota de JW Library.
```

Las etiquetas aparecen después del H1 y antes del contenido. Los espacios de las etiquetas se convierten en guiones bajos. El H1 conserva el título completo; los caracteres incompatibles con un nombre de archivo se sustituyen y los nombres largos se recortan. Los títulos duplicados reciben sufijos `(2)`, `(3)`, etc. Las notas sin título usan `Sin título`.

## Comportamiento

- Revisa la carpeta de copias cada 30 segundos; solo considera archivos `.jwlibrary` situados directamente en esa carpeta.
- Espera que tamaño y fechas de modificación del archivo permanezcan estables al menos 10 segundos. Con el intervalo predeterminado, normalmente lo detecta en una o dos revisiones.
- Elige la copia con la fecha de creación más reciente **dentro del manifiesto**, no la que se haya subido más recientemente. No retrocede respecto a la última copia exportada, aunque se borre el respaldo más nuevo.
- Trabaja sobre una copia temporal y comprueba el ZIP y la integridad de SQLite antes de modificar el destino.
- Al arrancar, reemplaza todo el contenido del destino desde la copia elegida. También lo reemplaza al cambiar el respaldo o detectar cambios en el destino.
- **Los archivos, notas manuales, subcarpetas y archivos ocultos añadidos al destino se eliminan.** La carpeta es una salida gestionada exclusivamente por este servicio. Una copia válida sin notas deja el destino vacío.
- Mientras copia y destino coincidan, evita volver a escribir los archivos en cada revisión.
- Si la validación falla, conserva el destino. Si falla la publicación, restaura su contenido anterior. Una publicación interrumpida se recupera al siguiente arranque mediante el estado persistente.
- Los respaldos se montan en modo de solo lectura y no se modifican. El estado y los archivos temporales se guardan fuera del destino.

La sustitución usa una carpeta temporal y recuperación, pero no es una operación atómica de todo el directorio: durante la publicación un cliente puede observar un contenido parcial. No utiliza enlaces simbólicos para publicar las notas, de forma que el destino pueda integrarse con OpenCloud.

## Docker Compose

```yaml
services:
  jwlibrary-notes-sync:
    image: ghcr.io/ismola/jwlibrary-notes-sync:latest
    restart: unless-stopped
    stop_grace_period: 60s
    environment:
      POLL_INTERVAL_SECONDS: "30"
      SETTLE_SECONDS: "10"
    volumes:
      - "${JWLIBRARY_BACKUPS_PATH}:/backups:ro"
      - "${JWLIBRARY_NOTES_PATH}:/notes"
      - "${DOCKER_PATH}/jwlibrary-notes-sync/state:/state"
```

Define `JWLIBRARY_BACKUPS_PATH` y `JWLIBRARY_NOTES_PATH` con las rutas de entrada y salida del host. `DOCKER_PATH` contiene el estado del servicio. La integración en `Ismola/homelab` proporciona valores predeterminados para ambas rutas de archivos.

Entrada, salida y estado deben ser carpetas separadas, sin estar unas dentro de otras. Usa una única instancia del servicio y conserva `/state` entre reinicios. El proceso necesita permisos de lectura en la entrada y de escritura en salida y estado. El Compose de homelab lo ejecuta como root, igual que la configuración actual de OpenCloud.

## Variables del contenedor

| Variable | Predeterminado | Uso |
|---|---|---|
| `BACKUP_DIR` | `/backups` | Copias `.jwlibrary` |
| `NOTES_DIR` | `/notes` | Destino exclusivo de notas |
| `STATE_DIR` | `/state` | Estado, bloqueo, temporales y recuperación |
| `POLL_INTERVAL_SECONDS` | `30` | Intervalo de revisión, en segundos |
| `SETTLE_SECONDS` | `10` | Tiempo mínimo sin cambios en el archivo |

`JWLIBRARY_BACKUPS_PATH` y `JWLIBRARY_NOTES_PATH` son variables del Compose para los volúmenes del host, no rutas internas del contenedor.

Para una ejecución inmediata que reemplace todo el destino, con los volúmenes ya configurados:

```bash
docker compose run --rm jwlibrary-notes-sync --once
```

No ejecutes simultáneamente el servicio y `--once` usando el mismo estado. El bloqueo impide tener dos procesos escribiendo a la vez. `--once` omite la espera de estabilización; las comprobaciones del archivo y de SQLite se mantienen.

## Imagen y desarrollo

GitHub Actions ejecuta las pruebas antes de construir y publicar una imagen para `linux/amd64` y `linux/arm64`. Los pushes a `main` publican las etiquetas `latest` y el SHA completo del commit. La imagen incluye solamente el programa; no incluye copias ni datos personales y no necesita conexiones de red para extraer las notas.

La visibilidad del repositorio y la del paquete GHCR son independientes. Si GHCR crea inicialmente el paquete como privado, Portainer necesita las credenciales del registro. Para permitir descargas anónimas, el propietario puede cambiar la visibilidad desde la configuración del paquete en GitHub.

```bash
python3 -m unittest discover -s tests -v
docker build -t jwlibrary-notes-sync .
```

Para probarlo sin Docker (Python 3.11 o posterior, Linux/macOS):

```bash
BACKUP_DIR=/ruta/copias NOTES_DIR=/ruta/notas STATE_DIR=/ruta/estado python3 sync.py --once
```

Las pruebas crean respaldos sintéticos; cubren reemplazo completo, formato Markdown, etiquetas, duplicados, copias dañadas, subida parcial, copias vacías, cambios manuales, recuperación y concurrencia. La lectura se ha comprobado con un respaldo de esquema 16.

Licencia [MIT](LICENSE). Proyecto independiente de los desarrolladores de JW Library.
