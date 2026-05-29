from fastapi import FastAPI

app = FastAPI()

@app.get("/")
def home():
    return {
        "mensaje": "Hola desde el servidor SGI",
        "empresa": "Soluciones en Geomática e Ingeniería SAS",
        "estado": "funcionando"
    }

@app.get("/health")
def health():
    return {"status": "ok"}
