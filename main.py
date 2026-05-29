from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import zipfile
import io
import time
import math
import xml.etree.ElementTree as ET
import requests
from staticmap import StaticMap, CircleMarker, Line
from PIL import ImageDraw, ImageFont
from pyproj import Transformer

app = FastAPI(title="SGI Geo Server", version="1.2")

# Permite que el formulario / Apps Script consuman este API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
BIGDATA_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "SGI-GeoServer/1.2 (sgi.dministrativo@gmail.com)"

# MAGNA-SIRGAS Origen Nacional (CTM12) = EPSG:9377 (metros Este/Norte)
EPSG_MAGNA = "EPSG:9377"
# Mismos parámetros, explícitos (por si el servidor no trae el código EPSG en su base PROJ)
PROJ_MAGNA = ("+proj=tmerc +lat_0=4 +lon_0=-73 +k=0.9992 +x_0=5000000 "
              "+y_0=2000000 +ellps=GRS80 +towgs84=0,0,0 +units=m +no_defs +type=crs")


def _transformers_magna():
    """Devuelve (a_magna, a_wgs). Usa EPSG:9377 si está disponible; si no, los parámetros explícitos."""
    try:
        a = Transformer.from_crs("EPSG:4326", EPSG_MAGNA, always_xy=True)
        b = Transformer.from_crs(EPSG_MAGNA, "EPSG:4326", always_xy=True)
        a.transform(-73.0, 4.0)
        b.transform(5000000.0, 2000000.0)
        return a, b
    except Exception:
        a = Transformer.from_crs("EPSG:4326", PROJ_MAGNA, always_xy=True)
        b = Transformer.from_crs(PROJ_MAGNA, "EPSG:4326", always_xy=True)
        return a, b


@app.get("/")
def home():
    return {
        "mensaje": "Hola desde el servidor SGI",
        "empresa": "Soluciones en Geomática e Ingeniería SAS",
        "estado": "funcionando",
    }


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/diagnostico")
def diagnostico():
    """Verifica que el servidor pueda proyectar a MAGNA-SIRGAS (para depurar la grilla)."""
    info = {}
    try:
        import pyproj
        info["pyproj"] = pyproj.__version__
        info["proj"] = pyproj.proj_version_str
    except Exception as e:
        info["pyproj_error"] = str(e)
    try:
        Transformer.from_crs("EPSG:4326", EPSG_MAGNA, always_xy=True).transform(-73.0, 4.0)
        info["epsg_9377"] = "ok"
    except Exception as e:
        info["epsg_9377"] = f"error: {e}"
    try:
        Transformer.from_crs("EPSG:4326", PROJ_MAGNA, always_xy=True).transform(-73.0, 4.0)
        info["proj_string"] = "ok"
    except Exception as e:
        info["proj_string"] = f"error: {e}"
    return info


# ------------------------------------------------------------
# LECTURA DE KMZ / KML
# ------------------------------------------------------------
def _sin_namespace(tag: str) -> str:
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _leer_kml(contenido: bytes, nombre: str) -> str:
    nombre = (nombre or "").lower()
    if nombre.endswith(".kmz") or contenido[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            kmls = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not kmls:
                raise ValueError("El KMZ no contiene ningún archivo .kml")
            with z.open(kmls[0]) as f:
                return f.read().decode("utf-8", errors="ignore")
    return contenido.decode("utf-8", errors="ignore")


def _centroide(puntos):
    lons = [p[0] for p in puntos]
    lats = [p[1] for p in puntos]
    return sum(lons) / len(lons), sum(lats) / len(lats)


def _extraer_placemarks(kml_text: str):
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as e:
        raise ValueError(f"No se pudo leer el KML: {e}")

    resultados = []
    for elem in root.iter():
        if _sin_namespace(elem.tag) != "Placemark":
            continue
        nombre = None
        coords_texto = None
        tipo = None
        for hijo in elem.iter():
            etiqueta = _sin_namespace(hijo.tag)
            if etiqueta == "name" and nombre is None:
                nombre = (hijo.text or "").strip()
            if etiqueta in ("Point", "Polygon", "LineString") and tipo is None:
                tipo = etiqueta
            if etiqueta == "coordinates" and coords_texto is None:
                coords_texto = hijo.text or ""
        if not coords_texto:
            continue

        pares = []
        for c in coords_texto.split():
            partes = c.strip().split(",")
            if len(partes) >= 2:
                try:
                    pares.append((float(partes[0]), float(partes[1])))
                except ValueError:
                    continue
        if not pares:
            continue

        lon, lat = pares[0] if len(pares) == 1 else _centroide(pares)
        resultados.append({
            "nombre": nombre or "Sin nombre",
            "tipo": tipo or "Desconocido",
            "lon": round(lon, 6),
            "lat": round(lat, 6),
            "vertices": len(pares),
        })
    return resultados


def _extraer_geometrias(kml_text: str):
    try:
        root = ET.fromstring(kml_text)
    except ET.ParseError as e:
        raise ValueError(f"No se pudo leer el KML: {e}")

    geoms = []
    for elem in root.iter():
        if _sin_namespace(elem.tag) != "Placemark":
            continue
        for hijo in elem.iter():
            etiqueta = _sin_namespace(hijo.tag)
            if etiqueta not in ("Point", "Polygon", "LineString"):
                continue
            coords_texto = None
            for sub in hijo.iter():
                if _sin_namespace(sub.tag) == "coordinates":
                    coords_texto = sub.text or ""
                    break
            if not coords_texto:
                continue
            pares = []
            for c in coords_texto.split():
                partes = c.strip().split(",")
                if len(partes) >= 2:
                    try:
                        pares.append((float(partes[0]), float(partes[1])))
                    except ValueError:
                        continue
            if pares:
                geoms.append({"tipo": etiqueta, "coords": pares})
    return geoms


def _geocodificar_nominatim(lat: float, lon: float) -> dict:
    r = requests.get(
        NOMINATIM_URL,
        params={"lat": lat, "lon": lon, "format": "json",
                "accept-language": "es", "addressdetails": 1, "zoom": 14},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    dir_ = data.get("address", {})
    municipio = (dir_.get("city") or dir_.get("town") or dir_.get("village")
                 or dir_.get("municipality") or dir_.get("locality")
                 or dir_.get("hamlet") or dir_.get("county"))
    departamento = (dir_.get("state") or dir_.get("region")
                    or dir_.get("state_district") or dir_.get("province"))
    return {
        "municipio": municipio,
        "departamento": departamento,
        "pais": dir_.get("country"),
        "direccion_completa": data.get("display_name"),
        "fuente": "nominatim",
    }


def _geocodificar_bigdatacloud(lat: float, lon: float) -> dict:
    r = requests.get(
        BIGDATA_URL,
        params={"latitude": lat, "longitude": lon, "localityLanguage": "es"},
        headers={"User-Agent": USER_AGENT},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    municipio = (data.get("city") or data.get("locality") or None)
    departamento = (data.get("principalSubdivision") or None)
    partes = [data.get("locality"), data.get("city"),
              data.get("principalSubdivision"), data.get("countryName")]
    return {
        "municipio": municipio,
        "departamento": departamento,
        "pais": data.get("countryName"),
        "direccion_completa": ", ".join([p for p in partes if p]),
        "fuente": "bigdatacloud",
    }


def _geocodificar_inverso(lat: float, lon: float) -> dict:
    """Intenta Nominatim (con reintentos) y, si falla, usa BigDataCloud como respaldo."""
    errores = []
    for intento in range(2):
        try:
            res = _geocodificar_nominatim(lat, lon)
            if res.get("municipio") or res.get("departamento"):
                return res
        except Exception as e:
            errores.append(f"nominatim: {e}")
        time.sleep(1)
    try:
        res = _geocodificar_bigdatacloud(lat, lon)
        if res.get("municipio") or res.get("departamento"):
            return res
    except Exception as e:
        errores.append(f"bigdatacloud: {e}")
    return {
        "municipio": None,
        "departamento": None,
        "pais": None,
        "direccion_completa": None,
        "error": " | ".join(errores) if errores else "sin resultado de ninguna fuente",
    }


# ------------------------------------------------------------
# UTILIDADES DEL PLANO (grilla + escala)
# ------------------------------------------------------------
def _tilex_to_lon(x, zoom):
    return x / pow(2, zoom) * 360.0 - 180.0


def _tiley_to_lat(y, zoom):
    n = math.pi - 2.0 * math.pi * y / pow(2, zoom)
    return math.degrees(math.atan(math.sinh(n)))


def _lon_to_tilex(lon, zoom):
    return ((lon + 180.0) / 360.0) * pow(2, zoom)


def _lat_to_tiley(lat, zoom):
    lat_r = math.radians(lat)
    return (1 - math.log(math.tan(lat_r) + 1 / math.cos(lat_r)) / math.pi) / 2 * pow(2, zoom)


def _paso_bonito(rango, divisiones=5):
    """Devuelve un paso 'redondo' (1, 2, 5 x 10^n) cercano a rango/divisiones."""
    if rango <= 0:
        return 1
    bruto = rango / divisiones
    exp = math.floor(math.log10(bruto)) if bruto > 0 else 0
    base = bruto / (10 ** exp)
    if base < 1.5:
        nice = 1
    elif base < 3:
        nice = 2
    elif base < 7:
        nice = 5
    else:
        nice = 10
    return nice * (10 ** exp)


def _cargar_fuente(size):
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        pass
    for ruta in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                 "/usr/share/fonts/dejavu/DejaVuSans.ttf"):
        try:
            return ImageFont.truetype(ruta, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _miles(valor):
    """Formatea un entero con separador de miles tipo '4.812.300'."""
    return f"{int(round(valor)):,}".replace(",", ".")


def _texto_con_fondo(draw, xy, texto, fuente, color_txt=(20, 20, 20)):
    x, y = xy
    try:
        bbox = draw.textbbox((x, y), texto, font=fuente)
    except Exception:
        w = draw.textlength(texto, font=fuente)
        bbox = (x, y, x + w, y + 12)
    draw.rectangle([bbox[0] - 2, bbox[1] - 1, bbox[2] + 2, bbox[3] + 1], fill=(255, 255, 255))
    draw.text((x, y), texto, font=fuente, fill=color_txt)


def _dibujar_escala(draw, size, lonlat_to_pixel, to_magna, to_wgs, bounds, fuente):
    W, H = size
    lon_min, lat_min, lon_max, lat_max = bounds
    lon_c = (lon_min + lon_max) / 2
    lat_c = (lat_min + lat_max) / 2
    E_c, N_c = to_magna.transform(lon_c, lat_c)
    lon2, lat2 = to_wgs.transform(E_c + 1000, N_c)
    p1 = lonlat_to_pixel(lon_c, lat_c)
    p2 = lonlat_to_pixel(lon2, lat2)
    dpx = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
    if dpx <= 0:
        return
    m_por_px = 1000.0 / dpx
    bar_m = _paso_bonito(W * 0.25 * m_por_px, 1)
    if bar_m <= 0:
        bar_m = 100
    bar_px = bar_m / m_por_px
    bar_px = max(40, min(bar_px, W * 0.45))

    x2 = W - 18
    x1 = x2 - bar_px
    y1 = H - 24
    mid = (x1 + x2) / 2

    draw.rectangle([x1 - 8, y1 - 15, x2 + 8, y1 + 11], fill=(255, 255, 255))
    draw.rectangle([x1, y1, mid, y1 + 6], fill=(0, 0, 0), outline=(0, 0, 0))
    draw.rectangle([mid, y1, x2, y1 + 6], fill=(255, 255, 255), outline=(0, 0, 0))
    draw.rectangle([x1, y1, x2, y1 + 6], outline=(0, 0, 0))

    etiqueta = (f"{bar_m / 1000:g} km" if bar_m >= 1000 else f"{int(bar_m)} m")
    draw.text((x1 - 3, y1 - 14), "0", font=fuente, fill=(0, 0, 0))
    draw.text((x2 - 22, y1 - 14), etiqueta, font=fuente, fill=(0, 0, 0))


def _dibujar_grilla_escala(imagen, lonlat_to_pixel, bounds):
    """Dibuja la grilla MAGNA-SIRGAS (E/N) con etiquetas y la barra de escala."""
    draw = ImageDraw.Draw(imagen)
    W, H = imagen.size
    lon_min, lat_min, lon_max, lat_max = bounds

    to_magna, to_wgs = _transformers_magna()

    esquinas = [(lon_min, lat_min), (lon_max, lat_min), (lon_min, lat_max), (lon_max, lat_max)]
    EN = [to_magna.transform(lo, la) for lo, la in esquinas]
    Es = [e for e, n in EN]
    Ns = [n for e, n in EN]
    Emin, Emax = min(Es), max(Es)
    Nmin, Nmax = min(Ns), max(Ns)

    paso = _paso_bonito(max(Emax - Emin, Nmax - Nmin), 5)
    fuente = _cargar_fuente(12)
    color_linea = (70, 70, 70)
    grosor = 2

    # Líneas de Este (verticales)
    e = math.ceil(Emin / paso) * paso
    while e <= Emax:
        pts = []
        for k in range(21):
            n = Nmin + (Nmax - Nmin) * k / 20.0
            lon, lat = to_wgs.transform(e, n)
            pts.append(lonlat_to_pixel(lon, lat))
        draw.line(pts, fill=color_linea, width=grosor)
        lon, lat = to_wgs.transform(e, Nmin)
        px, _ = lonlat_to_pixel(lon, lat)
        _texto_con_fondo(draw, (min(max(px + 2, 2), W - 70), H - 16),
                         f"{_miles(e)} E", fuente)
        e += paso

    # Líneas de Norte (horizontales)
    n = math.ceil(Nmin / paso) * paso
    while n <= Nmax:
        pts = []
        for k in range(21):
            ee = Emin + (Emax - Emin) * k / 20.0
            lon, lat = to_wgs.transform(ee, n)
            pts.append(lonlat_to_pixel(lon, lat))
        draw.line(pts, fill=color_linea, width=grosor)
        lon, lat = to_wgs.transform(Emin, n)
        _, py = lonlat_to_pixel(lon, lat)
        _texto_con_fondo(draw, (2, min(max(py - 6, 2), H - 16)),
                         f"{_miles(n)} N", fuente)
        n += paso

    _dibujar_escala(draw, imagen.size, lonlat_to_pixel, to_magna, to_wgs, bounds, fuente)
    return imagen


# ------------------------------------------------------------
# ENDPOINTS
# ------------------------------------------------------------
@app.post("/procesar-kmz")
async def procesar_kmz(archivo: UploadFile = File(...)):
    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")
    try:
        kml_text = _leer_kml(contenido, archivo.filename)
        sitios = _extraer_placemarks(kml_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not sitios:
        raise HTTPException(status_code=422,
                            detail="No se encontraron puntos ni polígonos en el archivo.")

    resultados = []
    for i, p in enumerate(sitios):
        p["ubicacion"] = _geocodificar_inverso(p["lat"], p["lon"])
        resultados.append(p)
        if i < len(sitios) - 1:
            time.sleep(1)

    principal = resultados[0]["ubicacion"]
    return {
        "archivo": archivo.filename,
        "total_sitios": len(resultados),
        "localizacion_principal": {
            "municipio": principal.get("municipio"),
            "departamento": principal.get("departamento"),
        },
        "sitios": resultados,
    }


@app.post("/plano-kmz")
async def plano_kmz(archivo: UploadFile = File(...)):
    contenido = await archivo.read()
    if not contenido:
        raise HTTPException(status_code=400, detail="El archivo está vacío.")
    try:
        kml_text = _leer_kml(contenido, archivo.filename)
        geoms = _extraer_geometrias(kml_text)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    if not geoms:
        raise HTTPException(status_code=422, detail="No se encontraron geometrías en el archivo.")

    mapa = StaticMap(700, 470, url_template=TILE_URL,
                     headers={"User-Agent": USER_AGENT}, tile_request_timeout=20)

    todas = []
    for g in geoms:
        coords = g["coords"]
        todas.extend(coords)
        if g["tipo"] == "Point":
            lon, lat = coords[0]
            mapa.add_marker(CircleMarker((lon, lat), "#8E1B1B", 16))
            mapa.add_marker(CircleMarker((lon, lat), "#E74C3C", 10))
        elif g["tipo"] == "Polygon":
            anillo = list(coords)
            if anillo[0] != anillo[-1]:
                anillo.append(anillo[0])  # cerrar SOLO los polígonos
            mapa.add_line(Line(anillo, "#C0392B", 4))
        else:  # LineString u otros: dibujar tal cual, sin cerrar
            mapa.add_line(Line(list(coords), "#C0392B", 4))

    lons = [c[0] for c in todas]
    lats = [c[1] for c in todas]
    extension = max(max(lons) - min(lons), max(lats) - min(lats))

    try:
        if extension < 0.002:
            centro = (sum(lons) / len(lons), sum(lats) / len(lats))
            imagen = mapa.render(zoom=15, center=centro)
        else:
            imagen = mapa.render()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo generar el mapa base: {e}")

    # Overlay de grilla MAGNA-SIRGAS + barra de escala (si algo falla, devuelve el mapa base)
    try:
        imagen = imagen.convert("RGB")

        def lonlat_to_pixel(lon, lat):
            x = _lon_to_tilex(lon, mapa.zoom)
            y = _lat_to_tiley(lat, mapa.zoom)
            px = (x - mapa.x_center) * mapa.tile_size + mapa.width / 2
            py = (y - mapa.y_center) * mapa.tile_size + mapa.height / 2
            return (px, py)

        left = mapa.x_center - (mapa.width / 2) / mapa.tile_size
        right = mapa.x_center + (mapa.width / 2) / mapa.tile_size
        top = mapa.y_center - (mapa.height / 2) / mapa.tile_size
        bottom = mapa.y_center + (mapa.height / 2) / mapa.tile_size
        bounds = (
            _tilex_to_lon(left, mapa.zoom), _tiley_to_lat(bottom, mapa.zoom),
            _tilex_to_lon(right, mapa.zoom), _tiley_to_lat(top, mapa.zoom),
        )
        _dibujar_grilla_escala(imagen, lonlat_to_pixel, bounds)
    except Exception:
        pass  # devolver al menos el mapa base si la grilla falla

    buf = io.BytesIO()
    imagen.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
