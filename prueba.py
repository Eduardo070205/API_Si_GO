from fastapi import FastAPI, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from datetime import datetime, date, time as dtime
from typing import List
import requests
import json
from groq import Groq
from database import Base, engine, get_db, PlantaDB, HistorialRiegoDB
import os
from zoneinfo import ZoneInfo

# Crea las tablas si no existen
Base.metadata.create_all(bind=engine)

app = FastAPI()


TZ_MX = ZoneInfo("America/Mexico_City")

# ── API Keys ──────────────────────────────────────────────────────────────────
API_KEY = os.getenv("OPENWEATHER_API_KEY")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
groq_client  = Groq(api_key=GROQ_API_KEY)

# ── Estado en memoria ─────────────
db_sistema = {
    "configuracion": {
        "ciudad": "Jerez De Garcia Salinas",
        "planta": "Tomate"
    },
    "lecturas": {
        "humedad_suelo": 4095,
        "lluvia":        4095,
        "temp_aire":     0.0,
        "hum_aire":      0.0
    },
    "analisis": {
        "mensaje":        "Esperando datos del ESP32...",
        "clima_internet": "Desconocido"
    }
}


# ═══════════════════════════════════════════════════════════════════════════════
# SCHEMAS PYDANTIC
# ═══════════════════════════════════════════════════════════════════════════════

class ConfigApp(BaseModel):
    ciudad: str
    planta: str

class DatosESP32(BaseModel):
    humedad_suelo: int
    lluvia:        int
    temp_aire:     float
    hum_aire:      float

class PlantaSchema(BaseModel):
    nombre:               str
    umbral_riego:         int
    temp_min_recomendada: float
    temp_max_recomendada: float

    class Config:
        from_attributes = True

class HistorialSchema(BaseModel):
    id:          int
    fecha:       date
    hora:        dtime
    planta:      str
    litros:      float
    temperatura: float

    class Config:
        from_attributes = True

class NombrePlantaRequest(BaseModel):
    nombre: str


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES — CLIMA (OpenWeatherMap)
# ═══════════════════════════════════════════════════════════════════════════════

def obtener_coordenadas(ciudad, api_key):
    url = f"http://api.openweathermap.org/geo/1.0/direct?q={ciudad}&limit=1&appid={api_key}"
    try:
        response = requests.get(url)
        data     = response.json()
        if data:
            return data[0]["lat"], data[0]["lon"]
    except Exception as e:
        print(f"Error en geolocalización: {e}")
    return None, None


def obtener_clima_internet(lat, lon, api_key):
    url = f"https://api.openweathermap.org/data/2.5/weather?lat={lat}&lon={lon}&appid={api_key}&units=metric&lang=es"
    try:
        response = requests.get(url)
        data     = response.json()
        return data["weather"][0]["description"]
    except Exception as e:
        print(f"Error al obtener clima: {e}")
        return "No disponible"


def obtener_pronostico(ciudad):
    url        = "https://api.openweathermap.org/data/2.5/forecast"
    parametros = {"q": ciudad, "appid": API_KEY, "units": "metric", "lang": "es"}
    try:
        respuesta = requests.get(url, params=parametros)
        datos     = respuesta.json()

        if respuesta.status_code != 200:
            return {"error": "No se pudo obtener el pronóstico", "detalle": datos}

        proximas_horas = []
        for item in datos["list"][:5]:
            fecha = datetime.fromtimestamp(item["dt"], TZ_MX)
            proximas_horas.append({
                "fecha":       fecha.strftime("%H:%M"),
                "temperatura": item["main"]["temp"],
                "descripcion": item["weather"][0]["description"]
            })

        proximos_dias = {}
        for item in datos["list"]:
            fecha       = datetime.fromtimestamp(item["dt"], TZ_MX)
            dia         = fecha.strftime("%Y-%m-%d")
            temp_min    = item["main"]["temp_min"]
            temp_max    = item["main"]["temp_max"]
            descripcion = item["weather"][0]["description"]

            if dia not in proximos_dias:
                proximos_dias[dia] = {
                    "fecha":       fecha.strftime("%d/%m/%Y"),
                    "temp_min":    temp_min,
                    "temp_max":    temp_max,
                    "descripcion": descripcion
                }
            else:
                if temp_min < proximos_dias[dia]["temp_min"]:
                    proximos_dias[dia]["temp_min"] = temp_min
                if temp_max > proximos_dias[dia]["temp_max"]:
                    proximos_dias[dia]["temp_max"] = temp_max

        return {
            "ciudad":          ciudad,
            "proximas_horas":  proximas_horas,
            "proximos_3_dias": list(proximos_dias.values())[:3]
        }

    except Exception as e:
        return {"error": "Error al obtener pronóstico", "detalle": str(e)}


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES — LÓGICA DE RIEGO
# ═══════════════════════════════════════════════════════════════════════════════

def evaluar_sistema(temp_sensor, clima_web, humedad_suelo, planta, db: Session):
    planta_db      = db.query(PlantaDB).filter(PlantaDB.nombre.ilike(planta)).first()
    humedad_optima = planta_db.umbral_riego if planta_db else 2800

    if humedad_suelo > humedad_optima:
        if "lluvia" in clima_web.lower():
            mensaje = "Suelo seco, pero se detecta lluvia cercana. Esperando riego natural."
        else:
            mensaje = "Tierra seca. Activando riego automático."
    else:
        mensaje = "Humedad adecuada. Sistema en espera."

    if temp_sensor > 35:
        mensaje += " | ALERTA: Calor extremo detectado."
    elif temp_sensor < 5:
        mensaje += " | ALERTA: Riesgo de helada."

    return mensaje, "Activando riego" in mensaje


# ═══════════════════════════════════════════════════════════════════════════════
# FUNCIONES AUXILIARES — IA GROQ (LLaMA 3 gratis)
# ═══════════════════════════════════════════════════════════════════════════════

def obtener_datos_planta_ia(nombre_planta: str) -> dict:
    """
    Consulta a Groq los datos agronómicos de una planta.
    Si la IA falla o no conoce la planta, devuelve valores genéricos seguros.
    """
    prompt = f"""
Eres un experto agrónomo. Necesito datos técnicos de riego para la planta: "{nombre_planta}".

Responde ÚNICAMENTE con un objeto JSON válido, sin texto extra, sin explicaciones, sin markdown.
El JSON debe tener exactamente estas claves:

{{
  "nombre": "{nombre_planta}",
  "umbral_riego": <entero entre 1500 y 4000, valor ADC del sensor capacitivo de humedad de suelo. Valores altos = suelo seco>,
  "temp_min_recomendada": <float en grados Celsius, temperatura mínima tolerable para esta planta>,
  "temp_max_recomendada": <float en grados Celsius, temperatura máxima tolerable para esta planta>,
  "encontrado": <true si encontraste info específica de esta planta, false si usaste valores genéricos>
}}

Si no conoces la planta, usa: umbral_riego: 2800, temp_min: 10.0, temp_max: 30.0, encontrado: false
"""
    try:
        respuesta = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200
        )
        texto = respuesta.choices[0].message.content.strip()  # type: ignore
        
        print(f"🤖 Respuesta Groq RAW: {texto}")  # ← agrega esto

        texto = texto.replace("```json", "").replace("```", "").strip()
        datos = json.loads(texto)

        return {
            "nombre":               nombre_planta,
            "umbral_riego":         int(datos.get("umbral_riego", 2800)),
            "temp_min_recomendada": float(datos.get("temp_min_recomendada", 10.0)),
            "temp_max_recomendada": float(datos.get("temp_max_recomendada", 30.0)),
            "encontrado":           bool(datos.get("encontrado", False))
        }

    except Exception as e:
        print(f"Error con Groq: {e}")
        return {
            "nombre":               nombre_planta,
            "umbral_riego":         2800,
            "temp_min_recomendada": 10.0,
            "temp_max_recomendada": 30.0,
            "encontrado":           False
        }


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — CONFIGURACIÓN Y SENSORES
# ═══════════════════════════════════════════════════════════════════════════════

@app.post("/api/configurar")
async def configurar_sistema(config: ConfigApp):
    db_sistema["configuracion"]["ciudad"] = config.ciudad
    db_sistema["configuracion"]["planta"] = config.planta
    return {"status": "Configuración guardada", "actual": db_sistema["configuracion"]}


@app.post("/api/sensores")
async def recibir_sensores(datos: DatosESP32, db: Session = Depends(get_db)):
    db_sistema["lecturas"] = datos.dict()

    ciudad    = db_sistema["configuracion"]["ciudad"]
    lat, lon  = obtener_coordenadas(ciudad, API_KEY)
    clima_ext = obtener_clima_internet(lat, lon, API_KEY) if lat else "Desconocido"

    resultado, activar_riego = evaluar_sistema(
        datos.temp_aire,
        clima_ext,
        datos.humedad_suelo,
        db_sistema["configuracion"]["planta"],
        db
    )

    db_sistema["analisis"]["mensaje"]        = resultado
    db_sistema["analisis"]["clima_internet"] = clima_ext

    # Guardar en historial si se activó el riego
    if activar_riego:
        now = datetime.now(TZ_MX)
        registro = HistorialRiegoDB(
            fecha       = now.date(),
            hora        = now.time(),
            planta      = db_sistema["configuracion"]["planta"],
            litros      = 2.5,
            temperatura = datos.temp_aire
        )
        db.add(registro)
        db.commit()

    return {"status": "Lecturas procesadas correctamente"}


@app.get("/api/status-total")
async def obtener_todo():
    if db_sistema["analisis"]["clima_internet"] == "Desconocido":
        ciudad   = db_sistema["configuracion"]["ciudad"]
        lat, lon = obtener_coordenadas(ciudad, API_KEY)
        if lat:
            db_sistema["analisis"]["clima_internet"] = obtener_clima_internet(lat, lon, API_KEY)
    return db_sistema


@app.get("/api/pronostico")
async def pronostico():
    ciudad = db_sistema["configuracion"]["ciudad"]
    return obtener_pronostico(ciudad)


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — PLANTAS (MySQL + IA Groq)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/plantas")
async def listar_plantas(db: Session = Depends(get_db)):
    plantas = db.query(PlantaDB).order_by(PlantaDB.nombre).all()
    return {
        "plantas": [
            {
                "id":                   p.id,
                "nombre":               p.nombre,
                "umbral_riego":         p.umbral_riego,
                "temp_min_recomendada": p.temp_min_recomendada,
                "temp_max_recomendada": p.temp_max_recomendada
            }
            for p in plantas
        ]
    }


@app.get("/api/plantas/{nombre}")
async def obtener_planta(nombre: str, db: Session = Depends(get_db)):
    planta = db.query(PlantaDB).filter(PlantaDB.nombre.ilike(nombre)).first()
    if not planta:
        raise HTTPException(status_code=404, detail=f"Planta '{nombre}' no encontrada")
    return {
        "id":                   planta.id,
        "nombre":               planta.nombre,
        "umbral_riego":         planta.umbral_riego,
        "temp_min_recomendada": planta.temp_min_recomendada,
        "temp_max_recomendada": planta.temp_max_recomendada
    }


@app.post("/api/plantas/agregar")
async def agregar_planta(request: NombrePlantaRequest, db: Session = Depends(get_db)):
  
    nombre    = (request.nombre or "").strip().capitalize()

    if not nombre:
        raise HTTPException(status_code=400, detail="El nombre de la planta no puede estar vacío")

    existente = db.query(PlantaDB).filter(PlantaDB.nombre.ilike(nombre)).first()
    if existente:
        raise HTTPException(status_code=400, detail=f"La planta '{nombre}' ya existe")

    # Consultar IA para obtener datos agronómicos
    datos = obtener_datos_planta_ia(nombre)

    nueva = PlantaDB(
        nombre               = datos["nombre"],
        umbral_riego         = datos["umbral_riego"],
        temp_min_recomendada = datos["temp_min_recomendada"],
        temp_max_recomendada = datos["temp_max_recomendada"]
    )
    db.add(nueva)
    db.commit()
    db.refresh(nueva)

    return {
        "status":    "Planta agregada correctamente",
        "planta":    nueva.nombre,
        "datos_ia":  datos,
        "info_real": datos["encontrado"],
        "mensaje":   "Datos obtenidos de IA" if datos["encontrado"] else "Planta no encontrada, se usaron valores genéricos"
    }


@app.put("/api/plantas/{nombre}")
async def actualizar_planta(nombre: str, datos: PlantaSchema, db: Session = Depends(get_db)):
    planta = db.query(PlantaDB).filter(PlantaDB.nombre.ilike(nombre)).first()
    if not planta:
        raise HTTPException(status_code=404, detail=f"Planta '{nombre}' no encontrada")

 
    setattr(planta, "umbral_riego",         datos.umbral_riego)
    setattr(planta, "temp_min_recomendada", datos.temp_min_recomendada)
    setattr(planta, "temp_max_recomendada", datos.temp_max_recomendada)
    db.commit()
    db.refresh(planta)

    return {"status": "Planta actualizada", "planta": planta.nombre}


@app.delete("/api/plantas/{nombre}")
async def eliminar_planta(nombre: str, db: Session = Depends(get_db)):
    planta = db.query(PlantaDB).filter(PlantaDB.nombre.ilike(nombre)).first()
    if not planta:
        raise HTTPException(status_code=404, detail=f"Planta '{nombre}' no encontrada")
    db.delete(planta)
    db.commit()
    return {"status": f"Planta '{nombre}' eliminada correctamente"}


# ═══════════════════════════════════════════════════════════════════════════════
# ENDPOINTS — HISTORIAL DE RIEGO (MySQL)
# ═══════════════════════════════════════════════════════════════════════════════

@app.get("/api/historial")
async def obtener_historial(db: Session = Depends(get_db)):
    registros = db.query(HistorialRiegoDB).order_by(HistorialRiegoDB.id.desc()).all()
    return {
        "historial": [
            {
                "fecha":       r.fecha.strftime("%d/%m/%Y"),
                "hora":        r.hora.strftime("%H:%M"),
                "planta":      r.planta,
                "litros":      r.litros,
                "temperatura": r.temperatura
            }
            for r in registros
        ]
    }


@app.delete("/api/historial/limpiar")
async def limpiar_historial(db: Session = Depends(get_db)):
    db.query(HistorialRiegoDB).delete()
    db.commit()
    return {"status": "Historial limpiado"}