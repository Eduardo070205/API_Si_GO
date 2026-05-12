

from sqlalchemy import create_engine, Column, Integer, Float, String, Date, Time, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime

# ── Configuración de conexión ─────────────────────────────────────────────────
MYSQL_USER     = "eduardo"
MYSQL_PASSWORD = "Eduardo10"
MYSQL_HOST     = "localhost"
MYSQL_PORT     = "3306"
MYSQL_DB       = "riego_db"

DATABASE_URL = f"mysql+pymysql://{MYSQL_USER}:{MYSQL_PASSWORD}@{MYSQL_HOST}:{MYSQL_PORT}/{MYSQL_DB}"

engine = create_engine(DATABASE_URL, pool_pre_ping=True)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


# ── Modelos (tablas) ──────────────────────────────────────────────────────────

class PlantaDB(Base):
    __tablename__ = "plantas"

    id                   = Column(Integer, primary_key=True, index=True)
    nombre               = Column(String(100), unique=True, nullable=False)
    umbral_riego         = Column(Integer, nullable=False)
    temp_min_recomendada = Column(Float, nullable=False)
    temp_max_recomendada = Column(Float, nullable=False)
    creado_en            = Column(DateTime, default=datetime.now)


class HistorialRiegoDB(Base):
    __tablename__ = "historial_riego"

    id            = Column(Integer, primary_key=True, index=True)
    fecha         = Column(Date, nullable=False)
    hora          = Column(Time, nullable=False)
    planta        = Column(String(100), nullable=False)
    litros        = Column(Float, nullable=False)
    temperatura   = Column(Float, nullable=False)
    registrado_en = Column(DateTime, default=datetime.now)


# ── Función helper para obtener sesión ───────────────────────────────────────
def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
