from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
import zipfile
import io
import time
import xml.etree.ElementTree as ET
import requests
from staticmap import StaticMap, CircleMarker, Line

app = FastAPI(title="SGI Geo Server", version="1.1")

# Permite que el formulario / Apps Script consuman este API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

NOMINATIM_URL = "https://nominatim.openstreetmap.org/reverse"
TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
USER_AGENT = "SGI-GeoServer/1.1 (sgi.dministrativo@gmail.com)"


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


def _sin_namespace(tag: str) -> str:
    """'{http://...}Point' -> 'Point'."""
    return tag.split("}", 1)[-1] if "}" in tag else tag


def _leer_kml(contenido: bytes, nombre: str) -> str:
    """Obtiene el texto KML, venga como .kml o dentro de un .kmz (zip)."""
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
    """Devuelve lista de sitios para geocodificar: {nombre, tipo, lon, lat, vertices}."""
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
    """Devuelve geometrías completas para dibujar: [{tipo, coords:[(lon,lat),...]}]."""
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


def _geocodificar_inverso(lat: float, lon: float) -> dict:
    """Reverse geocoding con Nominatim (OpenStreetMap)."""
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={
                "lat": lat,
                "lon": lon,
                "format": "json",
                "accept-language": "es",
                "addressdetails": 1,
                "zoom": 14,
            },
            headers={"User-Agent": USER_AGENT},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        return {"error": f"No se pudo geocodificar: {e}"}

    dir_ = data.get("address", {})
    municipio = (
        dir_.get("city")
        or dir_.get("town")
        or dir_.get("village")
        or dir_.get("municipality")
        or dir_.get("county")
    )
    return {
        "municipio": municipio,
        "departamento": dir_.get("state"),
        "pais": dir_.get("country"),
        "direccion_completa": data.get("display_name"),
    }


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
        raise HTTPException(
            status_code=422,
            detail="No se encontraron puntos ni polígonos en el archivo.",
        )

    resultados = []
    for i, p in enumerate(sitios):
        p["ubicacion"] = _geocodificar_inverso(p["lat"], p["lon"])
        resultados.append(p)
        if i < len(sitios) - 1:
            time.sleep(1)  # Nominatim exige máx. 1 petición por segundo

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
    """Genera un PNG con el mapa de localización (puntos/polígonos del KMZ sobre OSM)."""
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

    # 600x400 = relación 3:2 (igual que los 450x300 que usa la oferta, sin distorsión)
    mapa = StaticMap(
        600, 400,
        url_template=TILE_URL,
        headers={"User-Agent": USER_AGENT},
        tile_request_timeout=15,
    )

    todas = []
    for g in geoms:
        coords = g["coords"]
        todas.extend(coords)
        if g["tipo"] == "Point":
            lon, lat = coords[0]
            mapa.add_marker(CircleMarker((lon, lat), "#8E1B1B", 16))
            mapa.add_marker(CircleMarker((lon, lat), "#E74C3C", 10))
        else:
            anillo = list(coords)
            if anillo[0] != anillo[-1]:
                anillo.append(anillo[0])  # cerrar el polígono
            mapa.add_line(Line(anillo, "#C0392B", 4))

    lons = [c[0] for c in todas]
    lats = [c[1] for c in todas]
    extension = max(max(lons) - min(lons), max(lats) - min(lats))

    try:
        if extension < 0.002:  # un solo punto o área diminuta: fijar un zoom cómodo
            centro = (sum(lons) / len(lons), sum(lats) / len(lats))
            imagen = mapa.render(zoom=15, center=centro)
        else:
            imagen = mapa.render()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"No se pudo generar el plano: {e}")

    buf = io.BytesIO()
    imagen.save(buf, format="PNG")
    buf.seek(0)
    return StreamingResponse(buf, media_type="image/png")
