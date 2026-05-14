from fastapi import FastAPI, APIRouter, HTTPException, Depends, status, Header, Request
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.responses import StreamingResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from dotenv import load_dotenv
from starlette.middleware.cors import CORSMiddleware
from motor.motor_asyncio import AsyncIOMotorClient
import os
import logging
import gc  # Para liberación de memoria explícita
from pathlib import Path
from pydantic import BaseModel, Field
from typing import List, Optional, Literal
import uuid
from datetime import datetime, timedelta
import bcrypt
import jwt
from bson import ObjectId
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle, PageBreak, KeepTogether
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
import base64
from geopy.geocoders import Nominatim
from PIL import Image as PILImage
import requests
import pytz

# Timezone for Mexico
MEXICO_TZ = pytz.timezone('America/Mexico_City')

def to_mexico_time(dt: datetime) -> datetime:
    """Convert UTC datetime to Mexico timezone"""
    if dt.tzinfo is None:
        dt = pytz.utc.localize(dt)
    return dt.astimezone(MEXICO_TZ)

# Valid photo categories
VALID_CATEGORIES = ['folio', 'transporte', 'placas', 'temperatura', 'sello', 'licencia', 'carga', 'descarga']

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / '.env')

# MongoDB connection - PRODUCCIÓN (MongoDB Atlas)
# NO usar localhost - SIEMPRE usar la variable de entorno
import certifi

# Cargar variables de entorno (sin fallback)
mongo_url = os.environ['MONGO_URL']
db_name = os.environ['DB_NAME']

# Configuración para MongoDB - detectar si es Atlas (requiere SSL) o local (sin SSL)
is_atlas = 'mongodb+srv' in mongo_url or 'mongodb.net' in mongo_url

if is_atlas:
    client = AsyncIOMotorClient(
        mongo_url,
        tlsCAFile=certifi.where(),
        serverSelectionTimeoutMS=30000,
        connectTimeoutMS=30000,
        socketTimeoutMS=30000
    )
else:
    client = AsyncIOMotorClient(
        mongo_url,
        serverSelectionTimeoutMS=5000,
        connectTimeoutMS=5000
    )

db = client[db_name]

# Log de conexión
safe_mongo_url = mongo_url.split('@')[-1] if '@' in mongo_url else mongo_url
print("="*60)
print(f"[DB] Conectando a MongoDB {'Atlas' if is_atlas else 'Local'}...")
print(f"[DB] HOST: {safe_mongo_url}")
print(f"[DB] DB_NAME: {db_name}")
print("="*60)

# JWT Configuration
JWT_SECRET = os.environ.get('JWT_SECRET', 'transport-evidence-secret-key-2024')
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_HOURS = 24

# Logo URLs for PDF generation
LOGO_HEADER_URL = os.environ.get("LOGO_HEADER_URL", "https://res.cloudinary.com/dgp94fmou/image/upload/v1776955926/virgo_logo.png")
LOGO_WATERMARK_URL = os.environ.get("LOGO_WATERMARK_URL", "https://res.cloudinary.com/dgp94fmou/image/upload/v1776955926/virgo_logo.png")

# ============ BUILD EXPO WEB ON STARTUP ============
import subprocess
import shutil

def build_expo_web():
    pass  # Frontend served via Netlify
# Run build on module load
print("🚀 [STARTUP] Server module loading...")
# build_expo_web() - not needed

# Create the main app
app = FastAPI(title="Transport Evidence API")

# Create a router with the /api prefix
api_router = APIRouter(prefix="/api")

# Security
security = HTTPBearer()

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Geocoder for address lookup
geolocator = Nominatim(user_agent="transport_evidence_app")

# ============ MODELS ============

class UserBase(BaseModel):
    username: str
    nombre: str
    role: str

class UserCreate(UserBase):
    password: str

class User(UserBase):
    id: str
    created_at: datetime = Field(default_factory=datetime.utcnow)

class UserLogin(BaseModel):
    username: str
    password: str

class TokenResponse(BaseModel):
    token: str
    user: User

class FotoCreate(BaseModel):
    tipo: str  # 'carga' o 'descarga' - mantener para compatibilidad
    categoria: str = 'carga'  # Nueva categoría: folio, transporte, placas, temperatura, sello, licencia, carga, descarga
    imagen_base64: str
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    direccion: Optional[str] = None

class Foto(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    tipo: str
    categoria: str = 'carga'  # Nueva categoría
    imagen_base64: str
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    direccion: Optional[str] = None
    fecha: datetime = Field(default_factory=datetime.utcnow)
    usuario_id: str = ""
    aprobada: bool = False
    comentario: Optional[str] = None
    # Nuevos campos para edición de evidencia
    active: bool = True  # Si es False, no se incluye en el PDF
    added_by: str = "operador"  # "operador" | "admin"

class FotoUpdate(BaseModel):
    aprobada: Optional[bool] = None
    comentario: Optional[str] = None
    active: Optional[bool] = None  # Para marcar como eliminada del reporte
    imagen_base64: Optional[str] = None  # Para actualizar la imagen (ej: rotación)

class ServicioCreate(BaseModel):
    tipo_servicio: str = ""
    cliente: Optional[str] = None  # Nombre del cliente (opcional)
    # Datos del camión
    camion: Optional[str] = None  # ECO (ej: "ECO 01")
    placa_camion: Optional[str] = None  # Placa del camión
    # Datos de la caja
    tipo_caja: Optional[str] = None  # THERMO o CAJA SECA
    entidad_caja: Optional[str] = None  # Número de entidad
    placa_caja: Optional[str] = None  # Placa de la caja
    # Operador
    operador_nombre: str = ""
    operador_foto_url: Optional[str] = None  # Foto del operador al momento de crear
    operador_licencia: Optional[str] = None  # Licencia del operador
    # Ruta
    origenes: List[str]  # Multiple origins support
    destinos: List[str]  # Multiple destinations
    # Citas (nuevos campos separados)
    cita_carga: Optional[str] = None  # Fecha y hora de cita de carga
    cita_descarga: Optional[str] = None  # Fecha y hora de cita de descarga
    fecha_cita: Optional[Any] = None  # DEPRECATED: mantener para backward compat
    # Portada guardada
    portada_url: Optional[str] = None  # URL de la portada PDF guardada
    # Legacy field (backward compat)
    unidad: Optional[str] = None  # Deprecated: usar camion

class ServicioUpdate(BaseModel):
    tipo_servicio: Optional[str] = None
    cliente: Optional[str] = None  # Nombre del cliente
    camion: Optional[str] = None
    placa_camion: Optional[str] = None
    tipo_caja: Optional[str] = None  # THERMO o CAJA SECA
    entidad_caja: Optional[str] = None
    placa_caja: Optional[str] = None
    operador_nombre: Optional[str] = None
    operador_foto_url: Optional[str] = None
    operador_licencia: Optional[str] = None
    origen: Optional[str] = None  # Single origin (for compatibility)
    origenes: Optional[List[str]] = None  # Multiple origins
    destinos: Optional[List[str]] = None  # Multiple destinations
    estado: Optional[str] = None
    unidad: Optional[str] = None  # Legacy
    cita_carga: Optional[datetime] = None  # Fecha/hora cita de carga
    cita_descarga: Optional[datetime] = None  # Fecha/hora cita de descarga
    mostrar_trazabilidad: Optional[bool] = None  # Toggle para mostrar/ocultar trazabilidad en PDF

class SignatureUpdate(BaseModel):
    firma_base64: str
    firmante_nombre: Optional[str] = None

# Modelo para fotos organizadas por etapa
class FotosPorEtapa(BaseModel):
    espera: List[Foto] = []
    carga: List[Foto] = []
    entrega: List[Foto] = []

# Categorías de fotos disponibles
FOTO_CATEGORIAS = ["documentacion", "evidencia", "transporte", "placas", "temperatura", "sello", "licencia"]
MAX_FOTOS_POR_CATEGORIA = 10

# Helper para generar nombre de archivo PDF limpio
def generar_nombre_pdf(numero_factura: str = None, referencia_cliente: str = None) -> str:
    """Genera nombre limpio para PDF: VIRGO_{factura}_{referencia}.pdf
    
    Ejemplos:
    - Con factura y referencia: VIRGO_FAC001_PO1234.pdf
    - Solo factura: VIRGO_FAC001.pdf
    - Sin ninguna: VIRGO.pdf
    """
    import unicodedata
    import re
    
    def limpiar(texto: str) -> str:
        # Filtrar None, "None", vacíos
        if not texto or texto == "None" or texto.strip() == "":
            return None
        # Normalizar y quitar acentos
        texto = unicodedata.normalize('NFD', texto)
        texto = ''.join(c for c in texto if unicodedata.category(c) != 'Mn')
        # Espacios a guiones bajos, quitar caracteres especiales
        texto = re.sub(r'\s+', '_', texto)
        texto = re.sub(r'[^\w]', '', texto)
        return texto.upper() if texto else None
    
    factura_limpia = limpiar(numero_factura)
    ref_limpia = limpiar(referencia_cliente)
    
    # Construir nombre según disponibilidad de datos
    if factura_limpia and ref_limpia:
        return f"VIRGO_{factura_limpia}_{ref_limpia}.pdf"
    elif factura_limpia:
        return f"VIRGO_{factura_limpia}.pdf"
    else:
        return "VIRGO.pdf"

# Estructura de fotos por etapa y categoría
# fotos_etapas = {
#   espera: { documentacion: [], evidencia: [], transporte: [], ... },
#   carga: { documentacion: [], evidencia: [], transporte: [], ... },  # Legacy: single carga
#   carga_1: { documentacion: [], evidencia: [], ... },  # Multi-origin support
#   carga_2: { documentacion: [], evidencia: [], ... },
#   entrega: { documentacion: [], evidencia: [], transporte: [], ... },  # Legacy: single descarga
#   descarga_1: { documentacion: [], evidencia: [], ... },  # Multi-destination support
#   descarga_2: { documentacion: [], evidencia: [], ... },
# }

def crear_estructura_fotos_etapas(num_origenes: int = 1, num_destinos: int = 1):
    """Crea estructura vacía de fotos por etapa y categoría
    
    Args:
        num_origenes: Número de orígenes/cargas (default 1)
        num_destinos: Número de destinos/descargas (default 1)
    """
    estructura = {
        "espera": {cat: [] for cat in FOTO_CATEGORIAS},
    }
    
    # Agregar etapas de carga
    if num_origenes == 1:
        # Mantener compatibilidad con estructura antigua
        estructura["carga"] = {cat: [] for cat in FOTO_CATEGORIAS}
    else:
        # Múltiples cargas: carga_1, carga_2, etc.
        for i in range(1, num_origenes + 1):
            estructura[f"carga_{i}"] = {cat: [] for cat in FOTO_CATEGORIAS}
    
    # Agregar etapas de descarga
    if num_destinos == 1:
        # Mantener compatibilidad con estructura antigua
        estructura["entrega"] = {cat: [] for cat in FOTO_CATEGORIAS}
    else:
        # Múltiples descargas: descarga_1, descarga_2, etc.
        for i in range(1, num_destinos + 1):
            estructura[f"descarga_{i}"] = {cat: [] for cat in FOTO_CATEGORIAS}
    
    return estructura

class Servicio(BaseModel):
    id: str
    tipo_servicio: str
    cliente: Optional[str] = None  # Nombre del cliente
    # Datos del camión
    camion: Optional[str] = None  # ECO (ej: "ECO 01")
    placa_camion: Optional[str] = None  # Placa del camión
    unidad: Optional[str] = None  # Legacy (backward compat)
    # Datos de la caja
    tipo_caja: Optional[str] = None  # THERMO o CAJA SECA
    entidad_caja: Optional[str] = None  # Número de entidad
    placa_caja: Optional[str] = None  # Placa de la caja
    # Operador
    operador_nombre: str
    operador_foto_url: Optional[str] = None
    operador_licencia: Optional[str] = None
    # Ruta
    origenes: List[str] = []  # Multiple origins (backward compat: may be empty if old 'origen' exists)
    origen: Optional[str] = None  # DEPRECATED: kept for backward compatibility
    destinos: List[str] = []
    # Citas programadas (nuevos campos)
    cita_carga: Optional[str] = None  # Fecha y hora de cita de carga
    cita_descarga: Optional[str] = None  # Fecha y hora de cita de descarga
    fecha_cita: Optional[str] = None  # DEPRECATED: mantener para backward compat
    # Estado
    estado: str = "pendiente"
    estado_proceso: str = "ESPERA"  # ESPERA | CARGA | ENTREGA
    sub_estado: Optional[str] = None  # Sub-estado dentro de cada etapa
    # ============ TRAZABILIDAD COMPLETA (6 eventos BASE) ============
    # ETAPA ESPERA → CARGA
    hora_llegada_origen: Optional[datetime] = None   # 1. Llegó al origen
    hora_inicio_carga: Optional[datetime] = None     # 2. Inició la carga
    hora_fin_carga: Optional[datetime] = None        # 3. Terminó la carga
    # ETAPA CARGA → ENTREGA
    hora_llegada_destino: Optional[datetime] = None  # 4. Llegó al destino
    hora_inicio_descarga: Optional[datetime] = None  # 5. Inició la descarga
    hora_fin_descarga: Optional[datetime] = None     # 6. Terminó (servicio completado)
    # ============ TRAZABILIDAD DINÁMICA (Múltiples cargas/destinos) ============
    # Estructura: {"inicio_carga_1": datetime, "fin_carga_1": datetime, "inicio_carga_2": datetime, ...}
    trazabilidad: Optional[dict] = None  # Diccionario flexible para múltiples etapas
    # Legacy timestamps (backward compat)
    hora_llegada: Optional[datetime] = None  # DEPRECATED: usar hora_llegada_origen
    hora_carga: Optional[datetime] = None    # DEPRECATED: usar hora_fin_carga
    hora_entrega: Optional[datetime] = None  # DEPRECATED: usar hora_fin_descarga
    fotos: List[Foto] = []  # Mantener para compatibilidad
    fotos_etapas: Optional[dict] = None  # Estructura con categorías
    firma_base64: Optional[str] = None
    firmante_nombre: Optional[str] = None
    numero_factura: Optional[str] = None  # Número de factura (solo admin)
    referencia_cliente: Optional[str] = None  # Referencia del cliente (solo admin)
    mostrar_trazabilidad: Optional[bool] = True  # Toggle para mostrar/ocultar trazabilidad en PDF
    fecha_creacion: datetime = Field(default_factory=datetime.utcnow)
    fecha_actualizacion: datetime = Field(default_factory=datetime.utcnow)

# Modelo ligero para lista (sin fotos ni datos pesados) - Optimización tipo Uber
class ServicioListItem(BaseModel):
    id: str
    tipo_servicio: str
    cliente: Optional[str] = None  # Nombre del cliente
    camion: Optional[str] = None
    placa_camion: Optional[str] = None
    tipo_caja: Optional[str] = None  # THERMO o CAJA SECA
    placa_caja: Optional[str] = None
    unidad: Optional[str] = None  # Legacy
    operador_nombre: str
    origenes: List[str] = []  # Multiple origins
    origen: Optional[str] = None  # DEPRECATED: kept for backward compatibility
    destinos: List[str] = []
    estado: str = "pendiente"
    fotos_count: int = 0  # Solo el conteo, no las fotos
    fecha_creacion: datetime = Field(default_factory=datetime.utcnow)
    cita_carga: Optional[datetime] = None  # Fecha/hora cita de carga
    cita_descarga: Optional[datetime] = None  # Fecha/hora cita de descarga
    # Timestamps de trazabilidad para estado dinámico
    hora_llegada_origen: Optional[datetime] = None
    hora_inicio_carga: Optional[datetime] = None
    hora_fin_carga: Optional[datetime] = None
    hora_llegada_destino: Optional[datetime] = None
    hora_inicio_descarga: Optional[datetime] = None
    hora_fin_descarga: Optional[datetime] = None

# ============ CATALOG MODELS ============

class OperadorCreate(BaseModel):
    nombre: str
    telefono: str
    licencia: str
    vigencia_licencia: Optional[str] = None
    rfc: Optional[str] = None
    id_operador: str  # Unique ID for operator access (e.g., A102, B347)
    foto_url: Optional[str] = None  # URL de la foto del operador (legacy)
    foto_base64: Optional[str] = None  # Foto en base64 comprimido

class OperadorUpdate(BaseModel):
    nombre: Optional[str] = None
    telefono: Optional[str] = None
    licencia: Optional[str] = None
    vigencia_licencia: Optional[str] = None
    rfc: Optional[str] = None
    id_operador: Optional[str] = None
    foto_url: Optional[str] = None
    foto_base64: Optional[str] = None  # Foto en base64 comprimido
    status: Optional[str] = None  # activo/inactivo

class Operador(BaseModel):
    id: str
    nombre: str
    telefono: str
    licencia: str
    vigencia_licencia: Optional[str] = None
    rfc: Optional[str] = None
    id_operador: str  # Unique ID for operator access
    foto_url: Optional[str] = None  # URL de la foto del operador
    status: str = "activo"  # activo/inactivo
    fecha_creacion: Optional[datetime] = None
    fecha_actualizacion: Optional[datetime] = None

class CamionCreate(BaseModel):
    nombre: str
    numero: int
    placa: str
    tipo_caja: str

class Camion(BaseModel):
    id: str
    nombre: str
    numero: int
    placa: str
    tipo_caja: str

# ============ MODELO CAJA ============
class CajaCreate(BaseModel):
    tipo_caja: str  # "THERMO" o "CAJA SECA"
    numero_entidad: str  # Número de entidad (ej: "1141")
    placa: str  # Placa de la caja

class CajaUpdate(BaseModel):
    tipo_caja: Optional[str] = None
    numero_entidad: Optional[str] = None
    placa: Optional[str] = None
    status: Optional[str] = None  # "activo" o "inactivo"

class Caja(BaseModel):
    id: str
    tipo_caja: str
    numero_entidad: str
    placa: str
    status: str = "activo"
    fecha_creacion: Optional[datetime] = None
    fecha_actualizacion: Optional[datetime] = None

# ============ HELPER FUNCTIONS ============

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')

def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode('utf-8'), hashed.encode('utf-8'))

def create_token(user_id: str) -> str:
    payload = {
        "user_id": user_id,
        "exp": datetime.utcnow() + timedelta(hours=JWT_EXPIRATION_HOURS)
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)

async def get_current_user(credentials: HTTPAuthorizationCredentials = Depends(security)) -> dict:
    try:
        token = credentials.credentials
        payload = jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token inválido")
        
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user:
            raise HTTPException(status_code=401, detail="Usuario no encontrado")
        
        return {
            "id": str(user["_id"]),
            "username": user["username"],
            "nombre": user["nombre"],
            "role": user["role"]
        }
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")

def get_address_from_coords(lat: float, lon: float) -> str:
    try:
        location = geolocator.reverse(f"{lat}, {lon}", language="es")
        if location:
            return location.address
        return f"{lat}, {lon}"
    except Exception as e:
        logger.error(f"Error getting address: {e}")
        return f"{lat}, {lon}"

def servicio_to_response(s: dict) -> Servicio:
    fotos = []
    for f in s.get("fotos", []):
        fotos.append(Foto(
            id=f.get("id", str(uuid.uuid4())),
            tipo=f.get("tipo", ""),
            imagen_base64=f.get("imagen_base64", ""),
            latitud=f.get("latitud"),
            longitud=f.get("longitud"),
            direccion=f.get("direccion"),
            fecha=f.get("fecha", datetime.utcnow()),
            usuario_id=f.get("usuario_id", ""),
            aprobada=f.get("aprobada", False),
            comentario=f.get("comentario")
        ))
    
    # Handle backward compatibility - get tipo_servicio from various fields
    tipo_servicio = s.get("tipo_servicio") or s.get("cliente") or s.get("tipo") or ""
    
    # Handle backward compatibility for destinos - convert single destino to array
    destinos = s.get("destinos", [])
    if not destinos and s.get("destino"):
        destinos = [s.get("destino")]
    
    # Handle backward compatibility for origenes - convert single origen to array
    origenes = s.get("origenes", [])
    if not origenes and s.get("origen"):
        origenes = [s.get("origen")]
    
    # Keep origen for backward compat display
    origen_legacy = s.get("origen") or (origenes[0] if origenes else "")
    
    # Inicializar fotos_etapas con estructura de categorías si no existe o es formato antiguo
    fotos_etapas_raw = s.get("fotos_etapas")
    
    # Verificar si es formato antiguo (listas simples) o nuevo (categorías)
    if fotos_etapas_raw:
        # Detectar formato antiguo: { espera: [], carga: [], entrega: [] }
        first_etapa = fotos_etapas_raw.get("espera")
        if isinstance(first_etapa, list):
            # Formato antiguo - migrar a nuevo formato con categorías
            fotos_etapas = crear_estructura_fotos_etapas()
            for etapa in ["espera", "carga", "entrega"]:
                old_fotos = fotos_etapas_raw.get(etapa, [])
                # Migrar fotos antiguas a categoría "evidencia"
                fotos_etapas[etapa]["evidencia"] = old_fotos
        else:
            # Ya tiene formato nuevo
            fotos_etapas = fotos_etapas_raw
    else:
        # No tiene fotos_etapas - crear estructura vacía
        fotos_etapas = crear_estructura_fotos_etapas()
    
    return Servicio(
        id=str(s["_id"]),
        tipo_servicio=tipo_servicio,
        cliente=s.get("cliente"),  # Nombre del cliente
        # Datos del camión
        camion=s.get("camion"),  # ECO
        placa_camion=s.get("placa_camion"),  # Placa del camión
        unidad=s.get("unidad") or s.get("camion"),  # Backward compat
        # Datos de la caja
        tipo_caja=s.get("tipo_caja"),  # THERMO o CAJA SECA
        entidad_caja=s.get("entidad_caja"),  # Número de entidad
        placa_caja=s.get("placa_caja"),  # Placa de la caja
        # Operador
        operador_nombre=s.get("operador_nombre", ""),
        operador_foto_url=s.get("operador_foto_url"),
        operador_licencia=s.get("operador_licencia"),
        # Ruta
        origenes=origenes,  # Multiple origins
        origen=origen_legacy,  # Backward compat
        destinos=destinos,
        # Citas programadas (nuevos campos)
        cita_carga=s.get("cita_carga").isoformat() if isinstance(s.get("cita_carga"), datetime) else s.get("cita_carga"),  # Cita de carga
        cita_descarga=s.get("cita_descarga").isoformat() if isinstance(s.get("cita_descarga"), datetime) else s.get("cita_descarga"),  # Cita de descarga
        fecha_cita=s.get("fecha_cita").isoformat() if isinstance(s.get("fecha_cita"), datetime) else (s.get("fecha_cita") or s.get("cita_carga")),  # Legacy backward compat
        # Estado
        estado=s["estado"],
        estado_proceso=s.get("estado_proceso", "ESPERA"),
        sub_estado=s.get("sub_estado"),
        # ============ TRAZABILIDAD COMPLETA (6 eventos) ============
        hora_llegada_origen=s.get("hora_llegada_origen"),
        hora_inicio_carga=s.get("hora_inicio_carga"),
        hora_fin_carga=s.get("hora_fin_carga"),
        hora_llegada_destino=s.get("hora_llegada_destino"),
        hora_inicio_descarga=s.get("hora_inicio_descarga"),
        hora_fin_descarga=s.get("hora_fin_descarga"),
        # Trazabilidad dinámica (múltiples cargas/destinos)
        trazabilidad=s.get("trazabilidad"),
        # Legacy (backward compat)
        hora_llegada=s.get("hora_llegada") or s.get("hora_llegada_origen"),
        hora_carga=s.get("hora_carga") or s.get("hora_fin_carga"),
        hora_entrega=s.get("hora_entrega") or s.get("hora_fin_descarga"),
        fotos=fotos,
        fotos_etapas=fotos_etapas,
        firma_base64=s.get("firma_base64"),
        firmante_nombre=s.get("firmante_nombre"),
        fecha_creacion=s["fecha_creacion"],
        fecha_actualizacion=s["fecha_actualizacion"],
        numero_factura=s.get("numero_factura"),
        mostrar_trazabilidad=s.get("mostrar_trazabilidad", True)
    )

def servicio_to_list_item(s: dict) -> ServicioListItem:
    """Convierte documento a item ligero para lista (sin fotos ni datos pesados)"""
    # Handle backward compatibility
    tipo_servicio = s.get("tipo_servicio") or s.get("cliente") or s.get("tipo") or ""
    destinos = s.get("destinos", [])
    if not destinos and s.get("destino"):
        destinos = [s.get("destino")]
    
    # Handle backward compatibility for origenes
    origenes = s.get("origenes", [])
    if not origenes and s.get("origen"):
        origenes = [s.get("origen")]
    origen_legacy = s.get("origen") or (origenes[0] if origenes else "")
    
    # Calcular fotos_count soportando etapas dinámicas
    def calc_fotos_count(serv):
        total = 0
        fotos_etapas = serv.get("fotos_etapas", {})
        for etapa_key, etapa_data in fotos_etapas.items():
            if isinstance(etapa_data, list):
                total += len(etapa_data)
            elif isinstance(etapa_data, dict):
                for categoria, fotos in etapa_data.items():
                    if isinstance(fotos, list):
                        total += len(fotos)
        if total == 0:
            total = len(serv.get("fotos", []))
        return total
    
    return ServicioListItem(
        id=str(s["_id"]),
        tipo_servicio=tipo_servicio,
        cliente=s.get("cliente"),  # Nombre del cliente
        camion=s.get("camion"),
        placa_camion=s.get("placa_camion"),
        unidad=s.get("unidad") or s.get("camion"),  # Backward compat
        operador_nombre=s.get("operador_nombre", ""),
        origenes=origenes,
        origen=origen_legacy,  # Backward compat
        destinos=destinos,
        estado=s.get("estado", "pendiente"),
        fotos_count=calc_fotos_count(s),  # Ahora soporta etapas dinámicas
        fecha_creacion=s.get("fecha_creacion", datetime.utcnow())
    )

# ============ AUTH ROUTES (ADMIN ONLY) ============

@api_router.post("/auth/login", response_model=TokenResponse)
async def login(credentials: UserLogin):
    try:
        user = await db.users.find_one({"username": credentials.username})
        if not user:
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
        
        # Soportar ambos campos: password_hash (nuevo) y password (legacy)
        password_field = user.get("password_hash") or user.get("password")
        if not password_field or not verify_password(credentials.password, password_field):
            raise HTTPException(status_code=401, detail="Usuario o contraseña incorrectos")
        
        if user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Solo administradores pueden iniciar sesión")
        
        token = create_token(str(user["_id"]))
        
        return TokenResponse(
            token=token,
            user=User(
                id=str(user["_id"]),
                username=user["username"],
                nombre=user["nombre"],
                role=user["role"],
                created_at=user.get("created_at", datetime.utcnow())
            )
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"[LOGIN] Error de conexión a base de datos: {e}")
        raise HTTPException(status_code=503, detail="Error de conexión a la base de datos. Intente nuevamente.")

@api_router.get("/auth/me", response_model=User)
async def get_me(current_user: dict = Depends(get_current_user)):
    return User(
        id=current_user["id"],
        username=current_user["username"],
        nombre=current_user["nombre"],
        role=current_user["role"]
    )

# ============ PUBLIC ROUTES (OPERATOR - NO AUTH) ============

@api_router.get("/servicios/public", response_model=List[Servicio])
async def get_servicios_public():
    query = {"estado": {"$in": ["pendiente", "en_progreso"]}}
    servicios = await db.servicios.find(query).sort("fecha_creacion", -1).to_list(100)
    return [servicio_to_response(s) for s in servicios]

@api_router.get("/servicios/public/{servicio_id}", response_model=Servicio)
async def get_servicio_public(servicio_id: str):
    try:
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    return servicio_to_response(servicio)

def compress_image_base64(image_base64: str, max_width: int = 1280, quality: int = 75) -> str:
    """Compress image to ensure it stays within MongoDB limits.
    Optimizado para bajo uso de memoria en producción.
    """
    # gc ya importado globalmente
    img = None
    buffer = None
    try:
        # Remove data URL prefix if present
        if "," in image_base64:
            image_base64 = image_base64.split(",")[1]
        
        # Decode base64
        image_data = base64.b64decode(image_base64)
        
        # Open image
        img = PILImage.open(BytesIO(image_data))
        
        # Liberar image_data ya que no lo necesitamos más
        del image_data
        
        # Convert to RGB if necessary
        if img.mode in ('RGBA', 'P'):
            rgb_img = img.convert('RGB')
            img.close()
            img = rgb_img
        
        # Resize if too large
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            resized_img = img.resize((max_width, new_height), PILImage.LANCZOS)
            img.close()
            img = resized_img
        
        # Save to buffer with compression
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        buffer.seek(0)
        
        # Encode back to base64
        compressed_base64 = base64.b64encode(buffer.read()).decode('utf-8')
        result = f"data:image/jpeg;base64,{compressed_base64}"
        
        return result
    except Exception as e:
        logger.error(f"Error compressing image: {e}")
        # Return original if compression fails
        if not image_base64.startswith("data:"):
            return f"data:image/jpeg;base64,{image_base64}"
        return image_base64
    finally:
        # Liberar memoria explícitamente
        if img:
            try:
                img.close()
            except:
                pass
        if buffer:
            try:
                buffer.close()
            except:
                pass
        gc.collect()


def optimize_image_for_pdf(img_bytes: bytes, max_width: int = 800, quality: int = 60) -> bytes:
    """
    Optimiza una imagen para insertar en PDF.
    - Redimensiona a máximo 800px de ancho
    - Comprime a 60% JPEG
    - Retorna bytes optimizados
    Optimizado para bajo uso de memoria en producción.
    """
    # gc ya importado globalmente
    img = None
    buffer = None
    background = None
    try:
        img = PILImage.open(BytesIO(img_bytes))
        
        # Convert to RGB if necessary (PNG, RGBA, etc.)
        if img.mode in ('RGBA', 'P', 'LA', 'L'):
            # Create white background for transparent images
            background = PILImage.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'RGBA' or img.mode == 'LA':
                background.paste(img, mask=img.split()[-1] if len(img.split()) > 1 else None)
            else:
                background.paste(img)
            img.close()
            img = background
            background = None  # Ya no necesitamos la referencia
        elif img.mode != 'RGB':
            rgb_img = img.convert('RGB')
            img.close()
            img = rgb_img
        
        # Resize if width exceeds max
        if img.width > max_width:
            ratio = max_width / img.width
            new_height = int(img.height * ratio)
            resized_img = img.resize((max_width, new_height), PILImage.LANCZOS)
            img.close()
            img = resized_img
        
        # Compress to JPEG with specified quality
        buffer = BytesIO()
        img.save(buffer, format='JPEG', quality=quality, optimize=True)
        buffer.seek(0)
        
        result = buffer.read()
        return result
    except Exception as e:
        logger.error(f"Error optimizing image for PDF: {e}")
        # Return original if optimization fails
        return img_bytes
    finally:
        # Liberar memoria explícitamente
        if img:
            try:
                img.close()
            except:
                pass
        if background:
            try:
                background.close()
            except:
                pass
        if buffer:
            try:
                buffer.close()
            except:
                pass
        gc.collect()


@api_router.post("/servicios/public/{servicio_id}/fotos")
async def add_foto_public(servicio_id: str, foto_data: FotoCreate):
    try:
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Check current document size to avoid MongoDB limit
    current_fotos = servicio.get("fotos", [])
    if len(current_fotos) >= 50:
        raise HTTPException(status_code=400, detail="Límite de fotos alcanzado (máx. 50)")
    
    # Validate category
    categoria = foto_data.categoria.lower() if foto_data.categoria else foto_data.tipo
    if categoria not in VALID_CATEGORIES:
        categoria = foto_data.tipo  # fallback to tipo if invalid category
    
    # Compress the image on server side
    compressed_image = compress_image_base64(foto_data.imagen_base64)
    
    direccion = foto_data.direccion
    if foto_data.latitud and foto_data.longitud and not direccion:
        direccion = get_address_from_coords(foto_data.latitud, foto_data.longitud)
    
    # Use current time in Mexico timezone
    now_mexico = datetime.now(MEXICO_TZ)
    
    foto = {
        "id": str(uuid.uuid4()),
        "tipo": foto_data.tipo,
        "categoria": categoria,  # Nueva categoría
        "imagen_base64": compressed_image,
        "latitud": foto_data.latitud,
        "longitud": foto_data.longitud,
        "direccion": direccion,
        "fecha": now_mexico,  # Hora local de México
        "usuario_id": "operador",
        "aprobada": True,  # Auto-approve all photos
        "comentario": None
    }
    
    try:
        await db.servicios.update_one(
            {"_id": ObjectId(servicio_id)},
            {
                "$push": {"fotos": foto},
                "$set": {
                    "estado": "en_progreso",
                    "fecha_actualizacion": datetime.utcnow()
                }
            }
        )
    except Exception as e:
        logger.error(f"Error saving photo: {e}")
        raise HTTPException(status_code=500, detail="Error al guardar la foto. El servicio puede haber alcanzado su límite de almacenamiento.")
    
    return {"message": "Foto agregada exitosamente", "foto": foto}

@api_router.put("/servicios/public/{servicio_id}/completar")
async def completar_servicio_public(servicio_id: str):
    try:
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "estado": "completado",
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    return {"message": "Servicio completado exitosamente"}

# Delete photo from service (public - for operators)
@api_router.delete("/servicios/public/{servicio_id}/fotos/{foto_id}")
async def delete_foto_public(servicio_id: str, foto_id: str):
    try:
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Check if service is already completed
    if servicio.get("estado") == "completado":
        raise HTTPException(status_code=400, detail="No se pueden eliminar fotos de un servicio completado")
    
    # Find and remove the photo
    fotos = servicio.get("fotos", [])
    foto_found = False
    new_fotos = []
    
    for foto in fotos:
        if foto.get("id") == foto_id:
            foto_found = True
        else:
            new_fotos.append(foto)
    
    if not foto_found:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    
    # Update service with remaining photos
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "fotos": new_fotos,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    logger.info(f"Photo {foto_id} deleted from service {servicio_id}")
    return {"message": "Foto eliminada exitosamente"}

# ============ FLUJO DE 3 ETAPAS (ESPERA → CARGA → ENTREGA) ============

class AvanzarEtapaRequest(BaseModel):
    forzar: bool = False  # Permite avanzar sin fotos (solo para debugging)

class FotoEtapaRequest(BaseModel):
    imagen_base64: str
    categoria: str = "evidencia"  # documentacion, evidencia, transporte, placas, temperatura, sello, licencia
    tipo_foto: Optional[str] = None  # Tipo específico del checklist: llegada_unidad, camion, placa, etc.
    latitud: Optional[float] = None
    longitud: Optional[float] = None
    etapa_override: Optional[str] = None  # Permite especificar etapa para edición

@api_router.post("/servicios/public/{servicio_id}/etapa/foto")
async def agregar_foto_etapa(servicio_id: str, foto: FotoEtapaRequest):
    """Agregar foto a la etapa actual del servicio (máximo 10 por categoría)"""
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Determinar etapa
    if foto.etapa_override:
        etapa_key = foto.etapa_override.lower()
    else:
        estado_proceso = servicio.get("estado_proceso", "ESPERA").upper()
        etapa_key = estado_proceso.lower()
    
    # Mapear etapas del frontend a etapas de almacenamiento
    # llegada_patio en el frontend corresponde a espera en el almacenamiento
    if etapa_key == "llegada_patio":
        etapa_key = "espera"
    
    # Validar etapa válida (acepta etapas dinámicas: carga_1, carga_2, descarga_1, descarga_2, llegada_carga_2, etc.)
    etapas_base = ["espera", "carga", "entrega"]
    etapa_valida = etapa_key in etapas_base or \
                   etapa_key.startswith("carga_") or \
                   etapa_key.startswith("descarga_") or \
                   etapa_key.startswith("llegada_carga_")
    
    if not etapa_valida:
        raise HTTPException(status_code=400, detail=f"Etapa inválida: {etapa_key}")
    
    # Validar categoría válida
    categoria = foto.categoria.lower()
    if categoria not in FOTO_CATEGORIAS:
        raise HTTPException(status_code=400, detail=f"Categoría inválida. Válidas: {FOTO_CATEGORIAS}")
    
    # Obtener fotos_etapas existentes o inicializar con estructura nueva
    fotos_etapas_raw = servicio.get("fotos_etapas")
    
    # Detectar formato y migrar si es necesario
    if fotos_etapas_raw:
        first_etapa = fotos_etapas_raw.get("espera")
        if isinstance(first_etapa, list):
            # Formato antiguo - migrar
            fotos_etapas = crear_estructura_fotos_etapas()
            for et in ["espera", "carga", "entrega"]:
                old_fotos = fotos_etapas_raw.get(et, [])
                fotos_etapas[et]["evidencia"] = old_fotos
        else:
            fotos_etapas = fotos_etapas_raw
    else:
        fotos_etapas = crear_estructura_fotos_etapas()
    
    # Asegurar que la estructura existe
    if etapa_key not in fotos_etapas:
        fotos_etapas[etapa_key] = {cat: [] for cat in FOTO_CATEGORIAS}
    if categoria not in fotos_etapas[etapa_key]:
        fotos_etapas[etapa_key][categoria] = []
    
    # VALIDACIÓN: Máximo 10 fotos por categoría
    fotos_en_categoria = len(fotos_etapas[etapa_key][categoria])
    if fotos_en_categoria >= MAX_FOTOS_POR_CATEGORIA:
        raise HTTPException(
            status_code=400, 
            detail=f"Máximo {MAX_FOTOS_POR_CATEGORIA} fotos por categoría. Elimina alguna foto antes de agregar más."
        )
    
    # Obtener dirección si hay coordenadas
    direccion = None
    if foto.latitud and foto.longitud:
        direccion = get_address_from_coords(foto.latitud, foto.longitud)
    
    # Crear objeto foto
    nueva_foto = {
        "id": str(uuid.uuid4()),
        "tipo": categoria,
        "categoria": categoria,
        "tipo_foto": foto.tipo_foto,  # Tipo específico del checklist (llegada_unidad, placa, etc.)
        "imagen_base64": foto.imagen_base64,
        "latitud": foto.latitud,
        "longitud": foto.longitud,
        "direccion": direccion,
        "fecha": datetime.utcnow(),
        "etapa": etapa_key.upper()
    }
    
    # Agregar foto a la categoría correspondiente
    fotos_etapas[etapa_key][categoria].append(nueva_foto)
    
    # Actualizar servicio
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "fotos_etapas": fotos_etapas,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    total_fotos_etapa = sum(len(fotos_etapas[etapa_key].get(c, [])) for c in FOTO_CATEGORIAS)
    logger.info(f"Foto agregada a {etapa_key.upper()}/{categoria} del servicio {servicio_id} (total etapa: {total_fotos_etapa})")
    
    # Retornar servicio actualizado
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    return servicio_to_response(servicio)

# Endpoint para reabrir servicio completado para edición (en cualquier etapa)
class ReabrirRequest(BaseModel):
    etapa: str = "entrega"  # espera, carga, entrega

@api_router.put("/servicios/public/{servicio_id}/reabrir")
async def reabrir_servicio(servicio_id: str, request: ReabrirRequest = ReabrirRequest()):
    """Reabrir servicio completado para edición en cualquier etapa"""
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if servicio.get("estado") != "completado":
        raise HTTPException(status_code=400, detail="Solo se pueden reabrir servicios completados")
    
    etapa = request.etapa.upper()
    if etapa not in ["ESPERA", "CARGA", "ENTREGA"]:
        raise HTTPException(status_code=400, detail="Etapa inválida")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "estado": "en_progreso",
                "estado_proceso": etapa,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    logger.info(f"Servicio {servicio_id} reabierto para edición en etapa {etapa}")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    return {
        "message": f"Servicio reabierto para edición en etapa {etapa}",
        "servicio": servicio_to_response(servicio)
    }

# Helper para contar fotos en etapa con estructura nueva
def contar_fotos_etapa(fotos_etapas: dict, etapa_key: str) -> int:
    """Cuenta total de fotos en una etapa (todas las categorías)"""
    etapa_data = fotos_etapas.get(etapa_key, {})
    if isinstance(etapa_data, list):
        # Formato antiguo
        return len(etapa_data)
    elif isinstance(etapa_data, dict):
        # Formato nuevo con categorías
        total = 0
        for cat_fotos in etapa_data.values():
            if isinstance(cat_fotos, list):
                total += len(cat_fotos)
        return total
    return 0

# ============ NUEVO: REGISTRO DE EVENTOS DE TRAZABILIDAD ============

class RegistrarEventoRequest(BaseModel):
    evento: str  # llegada_origen, inicio_carga, fin_carga, llegada_destino, inicio_descarga, fin_descarga, cambio_carga, cambio_descarga
    siguiente_etapa: Optional[str] = None  # Para etapas dinámicas: CARGA_2, DESCARGA_1, etc.

# Mapeo de eventos a campos de tiempo y transiciones de estado
# IMPORTANTE: Solo los eventos "inicio_carga" y "fin_carga"/"fin_descarga" cambian de etapa
# Los eventos de "llegada" solo registran timestamps, NO cambian etapa
EVENTOS_CONFIG = {
    "llegada_origen": {
        "campo": "hora_llegada_origen",
        "etapa_requerida": "ESPERA",
        "siguiente_estado": None,  # CORREGIDO: NO cambiar etapa al llegar
        "sub_estado": "en_origen",
        "finaliza": False
    },
    "inicio_carga": {
        "campo": "hora_inicio_carga",
        "etapa_requerida": "ESPERA",  # CORREGIDO: Permitir desde ESPERA para iniciar carga
        "siguiente_estado": "CARGA",  # Este evento SÍ cambia a CARGA
        "sub_estado": "cargando",
        "finaliza": False
    },
    "fin_carga": {
        "campo": "hora_fin_carga",
        "etapa_requerida": None,  # Permitir desde cualquier etapa de carga
        "siguiente_estado": "ENTREGA",  # Este evento SÍ cambia a ENTREGA
        "sub_estado": None,
        "finaliza": False
    },
    "cambio_carga": {
        "campo": None,  # No registra timestamp específico
        "etapa_requerida": None,  # Permitir desde cualquier etapa de carga
        "siguiente_estado": None,  # Se determina dinámicamente
        "sub_estado": "cargando",
        "finaliza": False,
        "dinamico": True  # Indica que siguiente_estado viene del request
    },
    "llegada_carga": {
        "campo": None,  # Se guarda en trazabilidad dinámicamente
        "etapa_requerida": None,  # Permitir desde LLEGADA_CARGA_X
        "siguiente_estado": None,  # NO cambia etapa, solo registra llegada
        "sub_estado": "en_origen",
        "finaliza": False,
        "tipo_etapa_requerida": "LLEGADA_CARGA"  # Solo válido desde etapas LLEGADA_CARGA_X
    },
    "iniciar_carga_adicional": {
        "campo": None,  # Se guarda en trazabilidad dinámicamente
        "etapa_requerida": None,  # Permitir desde LLEGADA_CARGA_X
        "siguiente_estado": None,  # Se determina dinámicamente (CARGA_2, CARGA_3, etc.)
        "sub_estado": "cargando",
        "finaliza": False,
        "dinamico": True
    },
    "llegada_destino": {
        "campo": "hora_llegada_destino",
        "etapa_requerida": None,  # Permitir desde ENTREGA o DESCARGA_X
        "siguiente_estado": None,  # NO cambia etapa
        "sub_estado": "en_destino",
        "finaliza": False,
        "tipo_etapa_requerida": "ENTREGA"  # Validar que sea una etapa de tipo ENTREGA/DESCARGA
    },
    "inicio_descarga": {
        "campo": "hora_inicio_descarga",
        "etapa_requerida": None,  # Permitir desde ENTREGA o DESCARGA_X
        "siguiente_estado": None,  # NO cambia etapa
        "sub_estado": "descargando",
        "finaliza": False,
        "tipo_etapa_requerida": "ENTREGA"  # Validar que sea una etapa de tipo ENTREGA/DESCARGA
    },
    "cambio_descarga": {
        "campo": None,  # No registra timestamp específico
        "etapa_requerida": None,  # Permitir desde cualquier etapa de descarga
        "siguiente_estado": None,  # Se determina dinámicamente
        "sub_estado": "descargando",
        "finaliza": False,
        "dinamico": True  # Indica que siguiente_estado viene del request
    },
    "fin_descarga": {
        "campo": "hora_fin_descarga",
        "etapa_requerida": None,  # Permitir desde cualquier etapa de descarga
        "siguiente_estado": None,
        "sub_estado": "completado",
        "finaliza": True  # Finaliza el servicio
    }
}

@api_router.put("/servicios/public/{servicio_id}/evento")
async def registrar_evento(servicio_id: str, request: RegistrarEventoRequest):
    """Registrar un evento de trazabilidad (6 eventos posibles)"""
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    evento = request.evento.lower()
    
    if evento not in EVENTOS_CONFIG:
        raise HTTPException(
            status_code=400, 
            detail=f"Evento no válido. Eventos permitidos: {', '.join(EVENTOS_CONFIG.keys())}"
        )
    
    config = EVENTOS_CONFIG[evento]
    
    # IMPORTANTE: Verificar si llegadas ya existen ANTES de validar etapa
    # Esto permite que la primera foto registre llegada sin cambiar de etapa
    campo = config["campo"]
    if campo == "hora_llegada_origen" and servicio.get("hora_llegada_origen"):
        logger.info(f"Servicio {servicio_id}: hora_llegada_origen ya existe, ignorando")
        return {
            "message": f"Evento '{evento}' ya fue registrado anteriormente",
            "timestamp": servicio.get("hora_llegada_origen").isoformat() if servicio.get("hora_llegada_origen") else None,
            "estado_proceso": servicio.get("estado_proceso"),
            "sub_estado": servicio.get("sub_estado"),
            "estado": servicio.get("estado"),
            "ya_registrado": True
        }
    
    if campo == "hora_llegada_destino" and servicio.get("hora_llegada_destino"):
        logger.info(f"Servicio {servicio_id}: hora_llegada_destino ya existe, ignorando")
        return {
            "message": f"Evento '{evento}' ya fue registrado anteriormente",
            "timestamp": servicio.get("hora_llegada_destino").isoformat() if servicio.get("hora_llegada_destino") else None,
            "estado_proceso": servicio.get("estado_proceso"),
            "sub_estado": servicio.get("sub_estado"),
            "estado": servicio.get("estado"),
            "ya_registrado": True
        }
    
    estado_actual = servicio.get("estado_proceso", "ESPERA").upper()
    
    # Validar que estamos en la etapa correcta
    if config["etapa_requerida"] and estado_actual != config["etapa_requerida"]:
        raise HTTPException(
            status_code=400, 
            detail=f"El evento '{evento}' solo puede registrarse en etapa {config['etapa_requerida']}. Estás en {estado_actual}."
        )
    
    # Validar tipo de etapa si se especifica (para eventos que funcionan con múltiples etapas)
    if config.get("tipo_etapa_requerida"):
        tipo_requerido = config["tipo_etapa_requerida"]
        # Determinar el tipo de la etapa actual
        if estado_actual.startswith("LLEGADA_CARGA_"):
            tipo_actual = "LLEGADA_CARGA"
        elif estado_actual.startswith("CARGA"):
            tipo_actual = "CARGA"
        elif estado_actual.startswith("DESCARGA") or estado_actual == "ENTREGA":
            tipo_actual = "ENTREGA"
        elif estado_actual == "ESPERA":
            tipo_actual = "ESPERA"
        else:
            tipo_actual = estado_actual
        
        if tipo_actual != tipo_requerido:
            raise HTTPException(
                status_code=400,
                detail=f"El evento '{evento}' solo puede registrarse en etapas de tipo {tipo_requerido}. Estás en {estado_actual}."
            )
    
    ahora = datetime.utcnow()
    
    # Preparar campos a actualizar
    update_fields = {
        "fecha_actualizacion": ahora
    }
    
    # Solo agregar campo de tiempo si existe en la config
    if config["campo"]:
        update_fields[config["campo"]] = ahora
    
    # Para llegada_destino en DESCARGA_X, también guardar en trazabilidad
    if evento == "llegada_destino" and estado_actual.startswith("DESCARGA_"):
        num_descarga = estado_actual.replace("DESCARGA_", "")
        trazabilidad = servicio.get("trazabilidad", {}) or {}
        trazabilidad[f"llegada_destino_{num_descarga}"] = ahora.isoformat()
        update_fields["trazabilidad"] = trazabilidad
        logger.info(f"Servicio {servicio_id}: registrado llegada_destino_{num_descarga} en trazabilidad")
    
    # Para llegada_carga en LLEGADA_CARGA_X, guardar en trazabilidad
    if evento == "llegada_carga" and estado_actual.startswith("LLEGADA_CARGA_"):
        num_carga = estado_actual.replace("LLEGADA_CARGA_", "")
        trazabilidad = servicio.get("trazabilidad", {}) or {}
        trazabilidad[f"llegada_origen_{num_carga}"] = ahora.isoformat()
        update_fields["trazabilidad"] = trazabilidad
        logger.info(f"Servicio {servicio_id}: registrado llegada_origen_{num_carga} en trazabilidad")
    
    # Para iniciar_carga_adicional desde LLEGADA_CARGA_X, avanzar a CARGA_X
    if evento == "iniciar_carga_adicional" and estado_actual.startswith("LLEGADA_CARGA_"):
        num_carga = estado_actual.replace("LLEGADA_CARGA_", "")
        trazabilidad = servicio.get("trazabilidad", {}) or {}
        trazabilidad[f"inicio_carga_{num_carga}"] = ahora.isoformat()
        update_fields["trazabilidad"] = trazabilidad
        update_fields["estado_proceso"] = f"CARGA_{num_carga}"
        update_fields["sub_estado"] = "cargando"
        logger.info(f"Servicio {servicio_id}: iniciando carga {num_carga}, avanzando a CARGA_{num_carga}")
    
    # Para inicio_descarga en DESCARGA_X, también guardar en trazabilidad
    if evento == "inicio_descarga" and estado_actual.startswith("DESCARGA_"):
        num_descarga = estado_actual.replace("DESCARGA_", "")
        trazabilidad = servicio.get("trazabilidad", {}) or {}
        trazabilidad[f"inicio_descarga_{num_descarga}"] = ahora.isoformat()
        update_fields["trazabilidad"] = trazabilidad
        logger.info(f"Servicio {servicio_id}: registrado inicio_descarga_{num_descarga} en trazabilidad")
    
    # Actualizar sub_estado si corresponde
    if config["sub_estado"]:
        update_fields["sub_estado"] = config["sub_estado"]
    
    # Determinar siguiente etapa dinámicamente según el servicio
    origenes = servicio.get("origenes", [])
    destinos = servicio.get("destinos", [])
    num_origenes = len(origenes) if origenes else 1
    num_destinos = len(destinos) if destinos else 1
    
    # Manejar eventos dinámicos (cambio_carga, cambio_descarga)
    # IMPORTANTE: Registrar timestamps dinámicos para cargas/descargas adicionales
    if config.get("dinamico") and request.siguiente_etapa:
        update_fields["estado_proceso"] = request.siguiente_etapa.upper()
        update_fields["estado"] = "en_progreso"
        
        # Extraer número de la etapa actual para registrar fin de la etapa anterior
        estado_actual = servicio.get("estado_proceso", "").upper()
        trazabilidad = servicio.get("trazabilidad", {}) or {}
        
        if evento == "cambio_carga":
            # Registrar fin de la carga actual antes de pasar a la siguiente
            if estado_actual.startswith("CARGA_"):
                num_carga_actual = int(estado_actual.replace("CARGA_", ""))
                trazabilidad[f"fin_carga_{num_carga_actual}"] = ahora.isoformat()
                # Registrar inicio de la siguiente carga
                siguiente_num = request.siguiente_etapa.upper().replace("CARGA_", "")
                trazabilidad[f"inicio_carga_{siguiente_num}"] = ahora.isoformat()
            elif estado_actual == "CARGA":
                # Legacy: asumimos que es carga 1
                trazabilidad["fin_carga_1"] = ahora.isoformat()
                siguiente_num = request.siguiente_etapa.upper().replace("CARGA_", "")
                trazabilidad[f"inicio_carga_{siguiente_num}"] = ahora.isoformat()
            update_fields["trazabilidad"] = trazabilidad
            logger.info(f"Servicio {servicio_id}: registrado fin_carga y inicio_carga en trazabilidad")
        elif evento == "cambio_descarga":
            # Registrar SOLO fin de la descarga actual antes de pasar a la siguiente
            # El inicio_descarga_X se registra cuando el usuario presiona "INICIAR DESCARGA X"
            if estado_actual.startswith("DESCARGA_"):
                num_descarga_actual = int(estado_actual.replace("DESCARGA_", ""))
                trazabilidad[f"fin_descarga_{num_descarga_actual}"] = ahora.isoformat()
            elif estado_actual == "ENTREGA":
                # Legacy
                trazabilidad["fin_descarga_1"] = ahora.isoformat()
            update_fields["trazabilidad"] = trazabilidad
            logger.info(f"Servicio {servicio_id}: registrado fin_descarga_{num_descarga_actual if estado_actual.startswith('DESCARGA_') else 1} en trazabilidad")
        
        logger.info(f"Servicio {servicio_id}: cambio dinámico a etapa {request.siguiente_etapa}")
    elif config["siguiente_estado"]:
        # Determinar el siguiente estado según el tipo de evento y número de orígenes/destinos
        siguiente = config["siguiente_estado"]
        trazabilidad = servicio.get("trazabilidad", {}) or {}
        
        # Si es inicio_carga y hay múltiples orígenes, ir a CARGA_1 y registrar inicio_carga_1
        if evento == "inicio_carga" and num_origenes > 1:
            siguiente = "CARGA_1"
            trazabilidad["inicio_carga_1"] = ahora.isoformat()
            update_fields["trazabilidad"] = trazabilidad
            logger.info(f"Servicio {servicio_id}: múltiples orígenes ({num_origenes}), cambiando a {siguiente}, registrado inicio_carga_1")
        elif evento == "inicio_carga" and num_origenes == 1:
            # Solo 1 origen, registrar como carga 1 también para consistencia
            trazabilidad["inicio_carga_1"] = ahora.isoformat()
            update_fields["trazabilidad"] = trazabilidad
        
        # Si es fin_carga, verificar si hay más cargas o ir a descarga
        if evento == "fin_carga":
            estado_actual = servicio.get("estado_proceso", "").upper()
            # Determinar la carga actual
            if estado_actual.startswith("CARGA_"):
                num_carga_actual = int(estado_actual.replace("CARGA_", ""))
                # Registrar fin de la carga actual
                trazabilidad[f"fin_carga_{num_carga_actual}"] = ahora.isoformat()
                
                if num_carga_actual < num_origenes:
                    # Aún hay más cargas - ir a LLEGADA de la siguiente carga
                    siguiente = f"LLEGADA_CARGA_{num_carga_actual + 1}"
                    logger.info(f"Servicio {servicio_id}: avanzando a llegada de siguiente carga {siguiente}")
                else:
                    # Terminamos todas las cargas, ir a descarga
                    if num_destinos > 1:
                        siguiente = "DESCARGA_1"
                    else:
                        siguiente = "ENTREGA"
                    logger.info(f"Servicio {servicio_id}: fin de cargas, avanzando a {siguiente}")
                update_fields["trazabilidad"] = trazabilidad
            elif estado_actual == "CARGA":
                # Solo 1 origen - registrar fin_carga_1
                trazabilidad["fin_carga_1"] = ahora.isoformat()
                update_fields["trazabilidad"] = trazabilidad
                if num_origenes > 1:
                    # Estado legacy, asumimos que es la primera carga - ir a llegada carga 2
                    siguiente = "LLEGADA_CARGA_2"
                    logger.info(f"Servicio {servicio_id}: avanzando de CARGA a {siguiente}")
                else:
                    # Solo 1 origen, ir a descarga
                    if num_destinos > 1:
                        siguiente = "DESCARGA_1"
                    logger.info(f"Servicio {servicio_id}: fin de carga única, avanzando a {siguiente}")
        
        update_fields["estado_proceso"] = siguiente
        update_fields["estado"] = "en_progreso"
        update_fields["sub_estado"] = None  # Reset sub_estado al cambiar etapa
    
    # Para fin_descarga en DESCARGA_X, también guardar en trazabilidad
    if evento == "fin_descarga" and estado_actual.startswith("DESCARGA_"):
        num_descarga = estado_actual.replace("DESCARGA_", "")
        trazabilidad = update_fields.get("trazabilidad") or servicio.get("trazabilidad", {}) or {}
        trazabilidad[f"fin_descarga_{num_descarga}"] = ahora.isoformat()
        update_fields["trazabilidad"] = trazabilidad
        logger.info(f"Servicio {servicio_id}: registrado fin_descarga_{num_descarga} en trazabilidad")
    
    # Marcar como completado si finaliza
    if config["finaliza"]:
        update_fields["estado"] = "completado"
    
    # Guardar también en campos legacy para backward compat
    legacy_map = {
        "hora_llegada_origen": "hora_llegada",
        "hora_fin_carga": "hora_carga",
        "hora_fin_descarga": "hora_entrega"
    }
    if config["campo"] and config["campo"] in legacy_map:
        update_fields[legacy_map[config["campo"]]] = ahora
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {"$set": update_fields}
    )
    
    logger.info(f"Servicio {servicio_id}: evento '{evento}' registrado = {ahora}")
    
    servicio_actualizado = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    return {
        "message": f"Evento '{evento}' registrado exitosamente",
        "timestamp": ahora.isoformat(),
        "estado_proceso": servicio_actualizado.get("estado_proceso"),
        "sub_estado": servicio_actualizado.get("sub_estado"),
        "estado": servicio_actualizado.get("estado")
    }

# ============ FIN TRAZABILIDAD ============

@api_router.put("/servicios/public/{servicio_id}/etapa/avanzar")
async def avanzar_etapa(servicio_id: str, request: AvanzarEtapaRequest = AvanzarEtapaRequest()):
    """Avanzar a la siguiente etapa del viaje (requiere al menos 1 foto en la etapa actual)"""
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    estado_actual = servicio.get("estado_proceso", "ESPERA").upper()
    fotos_etapas = servicio.get("fotos_etapas", crear_estructura_fotos_etapas())
    
    etapa_key = estado_actual.lower()
    fotos_en_etapa = contar_fotos_etapa(fotos_etapas, etapa_key)
    
    # Validar que hay al menos 1 foto (a menos que sea forzado)
    if not request.forzar and fotos_en_etapa == 0:
        raise HTTPException(
            status_code=400, 
            detail=f"Debes tomar al menos una foto en la etapa {estado_actual} antes de avanzar"
        )
    
    ahora = datetime.utcnow()
    
    # Determinar siguiente etapa y qué tiempo guardar
    if estado_actual == "ESPERA":
        nuevo_estado = "CARGA"
        # Guardar hora_llegada (cuando termina ESPERA) si no existe
        update_fields = {
            "estado_proceso": nuevo_estado,
            "estado": "en_progreso",
            "fecha_actualizacion": ahora
        }
        if not servicio.get("hora_llegada"):
            update_fields["hora_llegada"] = ahora
            logger.info(f"Servicio {servicio_id}: hora_llegada guardada = {ahora}")
        
    elif estado_actual == "CARGA":
        nuevo_estado = "ENTREGA"
        # Guardar hora_carga (cuando termina CARGA) si no existe
        update_fields = {
            "estado_proceso": nuevo_estado,
            "estado": "en_progreso",
            "fecha_actualizacion": ahora
        }
        if not servicio.get("hora_carga"):
            update_fields["hora_carga"] = ahora
            logger.info(f"Servicio {servicio_id}: hora_carga guardada = {ahora}")
        
    elif estado_actual == "ENTREGA":
        # En ENTREGA, avanzar significa finalizar el viaje
        update_fields = {
            "estado": "completado",
            "fecha_actualizacion": ahora
        }
        # Guardar hora_entrega (cuando finaliza) si no existe
        if not servicio.get("hora_entrega"):
            update_fields["hora_entrega"] = ahora
            logger.info(f"Servicio {servicio_id}: hora_entrega guardada = {ahora}")
        
        await db.servicios.update_one(
            {"_id": ObjectId(servicio_id)},
            {"$set": update_fields}
        )
        logger.info(f"Servicio {servicio_id} finalizado desde etapa ENTREGA")
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
        return {
            "message": "Viaje finalizado exitosamente",
            "servicio": servicio_to_response(servicio)
        }
    else:
        raise HTTPException(status_code=400, detail=f"Estado de proceso inválido: {estado_actual}")
    
    # Actualizar estado de proceso
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {"$set": update_fields}
    )
    
    logger.info(f"Servicio {servicio_id} avanzó de {estado_actual} a {nuevo_estado}")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    return {
        "message": f"Avanzado a etapa {nuevo_estado}",
        "servicio": servicio_to_response(servicio)
    }

@api_router.delete("/servicios/public/{servicio_id}/etapa/foto/{foto_id}")
async def eliminar_foto_etapa(servicio_id: str, foto_id: str):
    """Eliminar foto de una etapa específica"""
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    fotos_etapas = servicio.get("fotos_etapas", crear_estructura_fotos_etapas())
    foto_eliminada = False
    etapa_encontrada = ""
    categoria_encontrada = ""
    
    # Buscar y eliminar foto en TODAS las etapas (incluyendo dinámicas como carga_1, carga_2, descarga_1, etc.)
    # Obtener todas las claves de etapas del servicio
    todas_etapas = list(fotos_etapas.keys())
    
    for etapa in todas_etapas:
        etapa_data = fotos_etapas.get(etapa, {})
        
        # Verificar si es formato antiguo (lista) o nuevo (dict con categorías)
        if isinstance(etapa_data, list):
            # Formato antiguo
            fotos_filtradas = [f for f in etapa_data if f.get("id") != foto_id]
            if len(fotos_filtradas) < len(etapa_data):
                fotos_etapas[etapa] = fotos_filtradas
                foto_eliminada = True
                etapa_encontrada = etapa
                break
        else:
            # Formato nuevo con categorías
            for categoria, fotos in etapa_data.items():
                if isinstance(fotos, list):
                    fotos_filtradas = [f for f in fotos if f.get("id") != foto_id]
                    if len(fotos_filtradas) < len(fotos):
                        fotos_etapas[etapa][categoria] = fotos_filtradas
                        foto_eliminada = True
                        etapa_encontrada = etapa
                        categoria_encontrada = categoria
                        break
            if foto_eliminada:
                break
    
    if not foto_eliminada:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "fotos_etapas": fotos_etapas,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    log_msg = f"Foto {foto_id} eliminada de etapa {etapa_encontrada}"
    if categoria_encontrada:
        log_msg += f"/{categoria_encontrada}"
    logger.info(log_msg)
    
    return {"message": "Foto eliminada exitosamente"}

# ============ OPERATOR ACCESS BY ID ============

class OperadorLogin(BaseModel):
    id_operador: str

@api_router.post("/operador/login")
async def operador_login(data: OperadorLogin):
    """Validate operator ID and return operator info"""
    operador = await db.operadores.find_one({"id_operador": data.id_operador.upper()})
    
    if not operador:
        raise HTTPException(status_code=401, detail="ID de operador incorrecto")
    
    return {
        "success": True,
        "operador": {
            "id": str(operador["_id"]),
            "nombre": operador["nombre"],
            "id_operador": operador["id_operador"],
            "telefono": operador.get("telefono", "")
        }
    }

@api_router.get("/operador/{id_operador}/servicios")
async def get_operador_servicios(
    id_operador: str,
    solo_hoy: bool = True,  # Por defecto solo servicios de hoy
    historial: bool = False,  # Ver historial completo
    page: int = 1,  # Página actual (1-indexed)
    limit: int = 10  # Límite de resultados
):
    """Get services assigned to a specific operator - OPTIMIZADO
    - solo_hoy=True (default): Solo servicios del día actual
    - historial=True: Ver todos los servicios históricos (últimos 30 días)
    - page: Página actual para paginación
    - limit: Máximo 10 servicios por request
    """
    # Validar límite máximo
    limit = min(limit, 20)
    skip = (page - 1) * limit
    
    # Validate operator exists
    operador = await db.operadores.find_one({"id_operador": id_operador.upper()})
    
    if not operador:
        raise HTTPException(status_code=404, detail="Operador no encontrado")
    
    operador_nombre = operador["nombre"]
    
    # Calcular filtro de fecha
    # IMPORTANTE: Usar zona horaria de México para filtros
    import pytz
    mexico_tz = pytz.timezone('America/Mexico_City')
    ahora_mexico = datetime.now(mexico_tz)
    
    logger.info(f"Filtro de servicios - Hora actual México: {ahora_mexico}, UTC: {datetime.utcnow()}")
    
    if historial:
        # Historial: últimos 30 días
        fecha_desde = datetime.utcnow() - timedelta(days=30)
    elif solo_hoy:
        # Solo hoy: desde inicio del día actual EN ZONA HORARIA DE MÉXICO
        inicio_dia_mexico = ahora_mexico.replace(hour=0, minute=0, second=0, microsecond=0)
        # Convertir a UTC para la query
        fecha_desde = inicio_dia_mexico.astimezone(pytz.UTC).replace(tzinfo=None)
        logger.info(f"Filtro 'hoy' - Inicio día México: {inicio_dia_mexico}, En UTC: {fecha_desde}")
    else:
        # Fallback: últimos 2 días
        fecha_desde = datetime.utcnow() - timedelta(days=2)
    
    # Query con filtro de fecha
    query = {
        "operador_nombre": operador_nombre,
        "fecha_creacion": {"$gte": fecha_desde}
    }
    
    # Contar total para paginación
    total_count = await db.servicios.count_documents(query)
    
    # Si es solo hoy, priorizar servicios pendientes/en progreso
    if solo_hoy and not historial:
        # Primero servicios activos, luego completados
        servicios = await db.servicios.find(query).sort([
            ("estado", 1),  # pendiente < en_progreso < completado
            ("fecha_creacion", -1)
        ]).skip(skip).limit(limit).to_list(limit)
    else:
        servicios = await db.servicios.find(query).sort("fecha_creacion", -1).skip(skip).limit(limit).to_list(limit)
    
    # Respuesta ULTRA LIGERA - SIN fotos_etapas completo
    result = []
    for s in servicios:
        # Calcular total de fotos desde fotos_etapas (solo conteo, NO el contenido)
        # SOPORTA etapas dinámicas: carga_1, carga_2, descarga_1, descarga_2, etc.
        fotos_etapas = s.get("fotos_etapas", {})
        total_fotos = 0
        
        # Iterar sobre TODAS las etapas, no solo las fijas
        for etapa_key, etapa_data in fotos_etapas.items():
            if isinstance(etapa_data, list):
                total_fotos += len(etapa_data)
            elif isinstance(etapa_data, dict):
                for categoria, fotos in etapa_data.items():
                    if isinstance(fotos, list):
                        total_fotos += len(fotos)
        
        if total_fotos == 0:
            total_fotos = len(s.get("fotos", []))
        
        # Handle backward compatibility for origenes
        origenes = s.get("origenes", [])
        if not origenes and s.get("origen"):
            origenes = [s.get("origen")]
        
        result.append({
            "id": str(s["_id"]),
            "tipo_servicio": s.get("tipo_servicio") or s.get("cliente") or "N/A",
            "unidad": s.get("unidad", "N/A"),
            "operador_nombre": s.get("operador_nombre", "N/A"),
            "origen": s.get("origen", "N/A"),  # Deprecated - keep for backward compat
            "origenes": origenes,  # NEW: Array de orígenes
            "destinos": s.get("destinos", []),
            "estado": s.get("estado", "pendiente"),
            "estado_proceso": s.get("estado_proceso", "espera"),
            "fecha_creacion": s.get("fecha_creacion"),
            "fecha_cita": s.get("fecha_cita"),  # Deprecated
            "cita_carga": s.get("cita_carga"),  # NEW
            "cita_descarga": s.get("cita_descarga"),  # NEW
            "fotos_count": total_fotos,
            # Timestamps de trazabilidad para estado dinámico
            "hora_llegada_origen": s.get("hora_llegada_origen"),
            "hora_inicio_carga": s.get("hora_inicio_carga"),
            "hora_fin_carga": s.get("hora_fin_carga"),
            "hora_llegada_destino": s.get("hora_llegada_destino"),
            "hora_inicio_descarga": s.get("hora_inicio_descarga"),
            "hora_fin_descarga": s.get("hora_fin_descarga")
            # NO incluir fotos_etapas - eso se carga al abrir el detalle
        })
    
    # Respuesta con metadata de paginación
    return {
        "items": result,
        "total": total_count,
        "page": page,
        "limit": limit,
        "has_more": (skip + len(result)) < total_count
    }

# ============ ADMIN ROUTES (AUTH REQUIRED) ============

@api_router.get("/users/operadores", response_model=List[User])
async def get_operadores(current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Acceso denegado")
    
    operadores = await db.users.find({"role": "operador"}).to_list(100)
    return [
        User(
            id=str(op["_id"]),
            username=op["username"],
            nombre=op["nombre"],
            role=op["role"],
            created_at=op.get("created_at", datetime.utcnow())
        )
        for op in operadores
    ]

@api_router.post("/servicios", response_model=Servicio)
async def create_servicio(servicio_data: ServicioCreate, current_user: dict = Depends(get_current_user)):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede crear servicios")
    
    # Buscar operador para obtener foto y licencia
    operador_foto_url = servicio_data.operador_foto_url
    operador_licencia = servicio_data.operador_licencia
    
    # Si no se proporcionaron, buscar en el catálogo
    if not operador_foto_url or not operador_licencia:
        operador = await db.operadores.find_one({"nombre": servicio_data.operador_nombre})
        if operador:
            if not operador_foto_url:
                operador_foto_url = operador.get("foto_url")
            if not operador_licencia:
                operador_licencia = operador.get("licencia")
    
    servicio_doc = {
        "tipo_servicio": servicio_data.tipo_servicio,
        "cliente": servicio_data.cliente,  # Nombre del cliente (opcional)
        # Datos del camión
        "camion": servicio_data.camion,  # ECO (ej: "ECO 01")
        "placa_camion": servicio_data.placa_camion,  # Placa del camión
        "unidad": servicio_data.unidad or servicio_data.camion,  # Backward compat
        # Datos de la caja
        "tipo_caja": servicio_data.tipo_caja,  # THERMO o CAJA SECA
        "entidad_caja": servicio_data.entidad_caja,  # Número de entidad
        "placa_caja": servicio_data.placa_caja,  # Placa de la caja
        # Operador
        "operador_nombre": servicio_data.operador_nombre,
        "operador_foto_url": operador_foto_url,  # Foto del operador guardada
        "operador_licencia": operador_licencia,  # Licencia del operador
        # Ruta
        "origenes": servicio_data.origenes,  # Multiple origins
        "destinos": servicio_data.destinos,  # Multiple destinations
        # Citas programadas (nuevos campos separados)
        "cita_carga": servicio_data.cita_carga,  # Cita de carga
        "cita_descarga": servicio_data.cita_descarga,  # Cita de descarga
        "fecha_cita": servicio_data.fecha_cita or servicio_data.cita_carga,  # Legacy backward compat
        # Estado
        "estado": "pendiente",
        "estado_proceso": "ESPERA",  # NUEVO: Inicia en etapa ESPERA
        "fotos": [],
        # Crear estructura dinámica basada en número de orígenes y destinos
        "fotos_etapas": crear_estructura_fotos_etapas(
            num_origenes=len(servicio_data.origenes) if servicio_data.origenes else 1,
            num_destinos=len(servicio_data.destinos) if servicio_data.destinos else 1
        ),
        "firma_base64": None,
        "firmante_nombre": None,
        "fecha_creacion": datetime.utcnow(),
        "fecha_actualizacion": datetime.utcnow()
    }
    
    result = await db.servicios.insert_one(servicio_doc)
    servicio_doc["_id"] = result.inserted_id
    
    # Generar y guardar portada automáticamente en background
    try:
        portada_base64 = await generate_portada_base64_internal(servicio_doc)
        if portada_base64:
            await db.servicios.update_one(
                {"_id": result.inserted_id},
                {"$set": {"portada_url": portada_base64}}
            )
            servicio_doc["portada_url"] = portada_base64
            logger.info(f"Portada guardada automáticamente para servicio {result.inserted_id}")
    except Exception as e:
        logger.error(f"Error generando portada automática: {e}")
    
    # DEBUG: Log completo del servicio creado
    logger.info(f"Service created - fecha_cita: {servicio_data.fecha_cita}")
    logger.info(f"Service created with operator photo: {operador_foto_url}")
    
    return servicio_to_response(servicio_doc)

# Modelo para respuesta paginada
class PaginatedServiciosList(BaseModel):
    items: List[ServicioListItem]
    total: int
    page: int
    page_size: int
    has_more: bool

# Endpoint LIGERO para lista con PAGINACIÓN - Scroll infinito
@api_router.get("/servicios/list")
async def get_servicios_list(
    page: int = 1,
    page_size: int = 15,
    current_user: dict = Depends(get_current_user)
):
    """Lista paginada de servicios para scroll infinito (sin fotos ni datos pesados)"""
    
    # Validar parámetros
    page = max(1, page)
    page_size = min(max(1, page_size), 50)  # Máximo 50 por página
    skip = (page - 1) * page_size
    
    # Contar total de documentos
    total = await db.servicios.count_documents({})
    
    # Pipeline con paginación
    pipeline = [
        {"$sort": {"fecha_creacion": -1}},
        {"$skip": skip},
        {"$limit": page_size},
        {"$project": {
            "_id": 1,
            "tipo_servicio": {"$ifNull": ["$tipo_servicio", {"$ifNull": ["$cliente", {"$ifNull": ["$tipo", ""]}]}]},
            "cliente": 1,
            "tipo_caja": 1,
            "placa_caja": 1,
            "unidad": {"$ifNull": ["$unidad", ""]},
            "operador_nombre": {"$ifNull": ["$operador_nombre", ""]},
            "origen": {"$ifNull": ["$origen", ""]},
            "origenes": {"$ifNull": ["$origenes", []]},
            "destinos": {"$ifNull": ["$destinos", []]},
            "destino": 1,
            "estado": {"$ifNull": ["$estado", "pendiente"]},
            "fotos": 1,  # Para compatibilidad con servicios antiguos
            "fotos_etapas": 1,  # Nueva estructura
            "fecha_creacion": {"$ifNull": ["$fecha_creacion", datetime.utcnow()]},
            "cita_carga": 1,
            "cita_descarga": 1,
            # Timestamps de trazabilidad
            "hora_llegada_origen": 1,
            "hora_inicio_carga": 1,
            "hora_fin_carga": 1,
            "hora_llegada_destino": 1,
            "hora_inicio_descarga": 1,
            "hora_fin_descarga": 1
        }}
    ]
    
    servicios = await db.servicios.aggregate(pipeline).to_list(page_size)
    
    def calcular_fotos_count(s):
        """Calcula total de fotos desde fotos_etapas o fotos antiguo
        Ahora soporta etapas dinámicas: carga_1, carga_2, descarga_1, descarga_2, etc."""
        total = 0
        fotos_etapas = s.get("fotos_etapas", {})
        
        # Iterar sobre TODAS las etapas, no solo las fijas
        for etapa_key, etapa_data in fotos_etapas.items():
            if isinstance(etapa_data, list):
                # Formato antiguo: array directo
                total += len(etapa_data)
            elif isinstance(etapa_data, dict):
                # Formato nuevo: objeto con categorías
                for categoria, fotos in etapa_data.items():
                    if isinstance(fotos, list):
                        total += len(fotos)
        
        # Fallback a fotos antiguo si no hay fotos_etapas
        if total == 0:
            total = len(s.get("fotos", []))
        
        return total
    
    items = []
    for s in servicios:
        destinos = s.get("destinos", [])
        if not destinos and s.get("destino"):
            destinos = [s.get("destino")]
        
        # Handle origenes
        origenes = s.get("origenes", [])
        if not origenes and s.get("origen"):
            origenes = [s.get("origen")]
        
        items.append(ServicioListItem(
            id=str(s["_id"]),
            tipo_servicio=s.get("tipo_servicio", ""),
            cliente=s.get("cliente"),
            tipo_caja=s.get("tipo_caja"),
            placa_caja=s.get("placa_caja"),
            unidad=s.get("unidad", ""),
            operador_nombre=s.get("operador_nombre", ""),
            origen=s.get("origen", ""),
            origenes=origenes,
            destinos=destinos,
            estado=s.get("estado", "pendiente"),
            fotos_count=calcular_fotos_count(s),
            fecha_creacion=s.get("fecha_creacion", datetime.utcnow()),
            cita_carga=s.get("cita_carga"),
            cita_descarga=s.get("cita_descarga"),
            # Timestamps de trazabilidad
            hora_llegada_origen=s.get("hora_llegada_origen"),
            hora_inicio_carga=s.get("hora_inicio_carga"),
            hora_fin_carga=s.get("hora_fin_carga"),
            hora_llegada_destino=s.get("hora_llegada_destino"),
            hora_inicio_descarga=s.get("hora_inicio_descarga"),
            hora_fin_descarga=s.get("hora_fin_descarga")
        ))
    
    has_more = (skip + len(items)) < total
    
    return PaginatedServiciosList(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        has_more=has_more
    )

@api_router.get("/servicios", response_model=List[Servicio])
async def get_servicios(current_user: dict = Depends(get_current_user)):
    servicios = await db.servicios.find().sort("fecha_creacion", -1).to_list(100)
    return [servicio_to_response(s) for s in servicios]

@api_router.get("/servicios/{servicio_id}", response_model=Servicio)
async def get_servicio(servicio_id: str, current_user: dict = Depends(get_current_user)):
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    return servicio_to_response(servicio)

@api_router.put("/servicios/{servicio_id}")
async def update_servicio(servicio_id: str, update_data: ServicioUpdate, current_user: dict = Depends(get_current_user)):
    """Update a service (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede modificar servicios")
    
    try:
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Build update dict with only provided fields
    update_fields = {}
    if update_data.tipo_servicio is not None:
        update_fields["tipo_servicio"] = update_data.tipo_servicio
    if update_data.cliente is not None:
        update_fields["cliente"] = update_data.cliente
    if update_data.camion is not None:
        update_fields["camion"] = update_data.camion
    if update_data.placa_camion is not None:
        update_fields["placa_camion"] = update_data.placa_camion
    if update_data.tipo_caja is not None:
        update_fields["tipo_caja"] = update_data.tipo_caja
    if update_data.entidad_caja is not None:
        update_fields["entidad_caja"] = update_data.entidad_caja
    if update_data.placa_caja is not None:
        update_fields["placa_caja"] = update_data.placa_caja
    if update_data.operador_nombre is not None:
        update_fields["operador_nombre"] = update_data.operador_nombre
    if update_data.operador_foto_url is not None:
        update_fields["operador_foto_url"] = update_data.operador_foto_url
    if update_data.operador_licencia is not None:
        update_fields["operador_licencia"] = update_data.operador_licencia
    if update_data.origenes is not None:
        update_fields["origenes"] = update_data.origenes
        # Also update legacy origen field with first origin for backward compatibility
        update_fields["origen"] = update_data.origenes[0] if update_data.origenes else None
    if update_data.origen is not None:
        update_fields["origen"] = update_data.origen
        # Also add to origenes array if not already set
        if update_data.origenes is None:
            update_fields["origenes"] = [update_data.origen] if update_data.origen else []
    if update_data.destinos is not None:
        update_fields["destinos"] = update_data.destinos
    if update_data.estado is not None:
        update_fields["estado"] = update_data.estado
    if update_data.unidad is not None:
        update_fields["unidad"] = update_data.unidad
    if update_data.cita_carga is not None:
        update_fields["cita_carga"] = update_data.cita_carga
    if update_data.cita_descarga is not None:
        update_fields["cita_descarga"] = update_data.cita_descarga
    if update_data.mostrar_trazabilidad is not None:
        update_fields["mostrar_trazabilidad"] = update_data.mostrar_trazabilidad
    
    if update_fields:
        update_fields["fecha_actualizacion"] = datetime.utcnow()
        await db.servicios.update_one(
            {"_id": ObjectId(servicio_id)},
            {"$set": update_fields}
        )
    
    return {"message": "Servicio actualizado exitosamente"}

@api_router.delete("/servicios/{servicio_id}")
async def delete_servicio(servicio_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a service completely (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede eliminar servicios")
    
    try:
        result = await db.servicios.delete_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    return {"message": "Servicio eliminado exitosamente"}

# ============ INVOICE NUMBER (ADMIN ONLY) ============

class FacturaUpdate(BaseModel):
    numero_factura: Optional[str] = None
    referencia_cliente: Optional[str] = None

@api_router.put("/servicios/{servicio_id}/factura")
async def update_factura(servicio_id: str, factura_data: FacturaUpdate, current_user: dict = Depends(get_current_user)):
    """Update invoice number and client reference for a service (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede modificar estos datos")
    
    try:
        servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    except:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    update_data = {"fecha_actualizacion": datetime.utcnow()}
    
    # Procesar numero_factura - string vacío o None = borrar el campo
    factura_value = factura_data.numero_factura
    if factura_value is not None:
        # Si viene vacío o solo espacios, usar $unset para borrar
        if not factura_value or not factura_value.strip():
            update_data["numero_factura"] = None  # Esto lo pone como null en la DB
        else:
            update_data["numero_factura"] = factura_value.strip()
    
    # Procesar referencia_cliente - string vacío o None = borrar el campo
    referencia_value = factura_data.referencia_cliente
    if referencia_value is not None:
        if not referencia_value or not referencia_value.strip():
            update_data["referencia_cliente"] = None
        else:
            update_data["referencia_cliente"] = referencia_value.strip()
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {"$set": update_data}
    )
    
    return {
        "message": "Datos actualizados exitosamente", 
        "numero_factura": update_data.get("numero_factura"),
        "referencia_cliente": update_data.get("referencia_cliente")
    }


# ============ EDICIÓN DE TRAZABILIDAD (ADMIN) ============
class TrazabilidadUpdate(BaseModel):
    """Modelo para actualizar campos de trazabilidad"""
    campo: str  # Nombre del campo (ej: "hora_llegada_origen", "inicio_carga_1", etc.)
    valor: Optional[str] = None  # ISO string de datetime o None para borrar

@api_router.put("/servicios/{servicio_id}/trazabilidad")
async def update_trazabilidad(
    servicio_id: str, 
    data: TrazabilidadUpdate, 
    current_user: dict = Depends(get_current_user)
):
    """Actualizar un campo de trazabilidad (solo admin)
    
    Permite editar manualmente las horas de trazabilidad cuando el admin
    sube fotos en nombre del chofer (por falta de señal).
    
    Campos soportados:
    - Básicos: hora_llegada_origen, hora_inicio_carga, hora_fin_carga, 
               hora_llegada_destino, hora_inicio_descarga, hora_fin_descarga
    - Dinámicos: inicio_carga_1, fin_carga_1, inicio_carga_2, fin_carga_2,
                 llegada_destino_1, inicio_descarga_1, fin_descarga_1, etc.
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede editar trazabilidad")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    campo = data.campo
    valor = data.valor
    
    # Parsear el valor a datetime con timezone de México
    datetime_valor = None
    if valor:
        try:
            # Intentar parsear ISO format primero
            dt = datetime.fromisoformat(valor.replace('Z', '+00:00'))
            # Si no tiene timezone, asumir que es hora de México
            if dt.tzinfo is None:
                datetime_valor = MEXICO_TZ.localize(dt)
            else:
                # Convertir a México para consistencia
                datetime_valor = dt.astimezone(MEXICO_TZ)
            # Guardar como UTC naive para MongoDB (consistente con el resto del sistema)
            datetime_valor = datetime_valor.astimezone(pytz.UTC).replace(tzinfo=None)
        except ValueError as e:
            logger.error(f"Error parseando fecha '{valor}': {e}")
            raise HTTPException(status_code=400, detail=f"Formato de fecha inválido: {valor}")
    
    # Campos básicos de trazabilidad (6 eventos originales)
    campos_basicos = [
        "hora_llegada_origen", "hora_inicio_carga", "hora_fin_carga",
        "hora_llegada_destino", "hora_inicio_descarga", "hora_fin_descarga"
    ]
    
    update_data = {"fecha_actualizacion": datetime.utcnow()}
    
    if campo in campos_basicos:
        # Actualizar campo básico directamente
        update_data[campo] = datetime_valor
    else:
        # Campos dinámicos van en el diccionario 'trazabilidad'
        trazabilidad = servicio.get("trazabilidad") or {}
        if datetime_valor:
            trazabilidad[campo] = datetime_valor
        else:
            # Si el valor es None, eliminar el campo
            trazabilidad.pop(campo, None)
        update_data["trazabilidad"] = trazabilidad
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {"$set": update_data}
    )
    
    logger.info(f"Trazabilidad actualizada - Servicio {servicio_id}: {campo} = {datetime_valor}")
    
    return {
        "message": "Trazabilidad actualizada",
        "campo": campo,
        "valor": datetime_valor.isoformat() if datetime_valor else None
    }



# ============ AGREGAR DESTINO A SERVICIO EN CURSO ============
class AgregarDestinoRequest(BaseModel):
    destino: str  # Nombre del nuevo destino

@api_router.post("/servicios/{servicio_id}/destino")
async def agregar_destino(servicio_id: str, request: AgregarDestinoRequest, current_user: dict = Depends(get_current_user)):
    """Agregar un nuevo destino de descarga a un servicio en curso (solo admin)
    
    IMPORTANTE: Preserva todas las fotos existentes de DESCARGA 1.
    Si el servicio tiene la estructura legacy ('entrega'), la renombra a 'descarga_1'.
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede agregar destinos")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Verificar que el servicio no esté completado
    if servicio.get("estado") == "completado":
        raise HTTPException(status_code=400, detail="No se puede agregar destino a servicio completado")
    
    # Obtener destinos actuales
    destinos_actuales = servicio.get("destinos", [])
    nuevo_destino = request.destino.strip()
    
    if not nuevo_destino:
        raise HTTPException(status_code=400, detail="El nombre del destino no puede estar vacío")
    
    # Obtener fotos_etapas actual (PRESERVAR TODO)
    fotos_etapas = servicio.get("fotos_etapas", {})
    
    # CASO ESPECIAL: Si este es el primer destino adicional y existe 'entrega' (legacy)
    # Renombrar 'entrega' a 'descarga_1' para mantener consistencia
    if len(destinos_actuales) == 1 and "entrega" in fotos_etapas and "descarga_1" not in fotos_etapas:
        logger.info(f"Servicio {servicio_id}: Renombrando 'entrega' a 'descarga_1' para mantener consistencia")
        fotos_etapas["descarga_1"] = fotos_etapas.pop("entrega")
    
    # Agregar el nuevo destino
    destinos_actuales.append(nuevo_destino)
    num_destinos = len(destinos_actuales)
    
    # Crear la estructura de fotos para la nueva etapa de descarga
    nueva_etapa_key = f"descarga_{num_destinos}"
    fotos_etapas[nueva_etapa_key] = {cat: [] for cat in FOTO_CATEGORIAS}
    
    # Actualizar el servicio
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {"$set": {
            "destinos": destinos_actuales,
            "fotos_etapas": fotos_etapas,
            "fecha_actualizacion": datetime.utcnow()
        }}
    )
    
    logger.info(f"Destino '{nuevo_destino}' agregado a servicio {servicio_id} como {nueva_etapa_key}")
    
    return {
        "message": f"Destino agregado exitosamente",
        "destino": nuevo_destino,
        "etapa_key": nueva_etapa_key,
        "total_destinos": num_destinos
    }


# ============ FINALIZAR VIAJE (ADMIN) ============
@api_router.post("/servicios/{servicio_id}/finalizar")
async def finalizar_servicio(servicio_id: str, current_user: dict = Depends(get_current_user)):
    """Finalizar un servicio manualmente (solo admin)
    
    Cambia el estado del servicio a COMPLETADO.
    Solo disponible si el servicio no está ya completado.
    """
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede finalizar servicios")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Verificar que no esté ya completado
    if servicio.get("estado") == "completado":
        raise HTTPException(status_code=400, detail="El servicio ya está completado")
    
    # Actualizar estado a completado
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {"$set": {
            "estado": "completado",
            "estado_proceso": "COMPLETADO",
            "hora_fin_descarga": datetime.utcnow(),
            "fecha_actualizacion": datetime.utcnow(),
            "finalizado_por": "admin"
        }}
    )
    
    logger.info(f"Servicio {servicio_id} finalizado manualmente por admin")
    
    return {
        "message": "Viaje finalizado exitosamente",
        "estado": "completado"
    }


# ============ PHOTO MANAGEMENT (ADMIN ONLY) ============

@api_router.put("/servicios/{servicio_id}/fotos/{foto_id}")
async def update_foto(servicio_id: str, foto_id: str, foto_update: FotoUpdate, current_user: dict = Depends(get_current_user)):
    """Update photo approval status, comment, active state, and optionally the image itself"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede modificar fotos")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    foto_found = False
    
    # Search in fotos array
    fotos = servicio.get("fotos", [])
    for foto in fotos:
        if foto.get("id") == foto_id:
            if foto_update.aprobada is not None:
                foto["aprobada"] = foto_update.aprobada
            if foto_update.comentario is not None:
                foto["comentario"] = foto_update.comentario
            if foto_update.active is not None:
                foto["active"] = foto_update.active
            if foto_update.imagen_base64 is not None:
                foto["imagen_base64"] = foto_update.imagen_base64
                foto["fecha_rotacion"] = datetime.utcnow().isoformat()
            foto_found = True
            break
    
    # Also search in fotos_etapas
    fotos_etapas = servicio.get("fotos_etapas", {})
    for etapa, categorias in fotos_etapas.items():
        if isinstance(categorias, dict):
            for categoria, fotos_cat in categorias.items():
                if isinstance(fotos_cat, list):
                    for foto in fotos_cat:
                        if foto.get("id") == foto_id:
                            if foto_update.aprobada is not None:
                                foto["aprobada"] = foto_update.aprobada
                            if foto_update.comentario is not None:
                                foto["comentario"] = foto_update.comentario
                            if foto_update.active is not None:
                                foto["active"] = foto_update.active
                            if foto_update.imagen_base64 is not None:
                                foto["imagen_base64"] = foto_update.imagen_base64
                                foto["fecha_rotacion"] = datetime.utcnow().isoformat()
                            foto_found = True
                            break
    
    if not foto_found:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "fotos": fotos,
                "fotos_etapas": fotos_etapas,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    return {"message": "Foto actualizada exitosamente"}

@api_router.delete("/servicios/{servicio_id}/fotos/{foto_id}")
async def delete_foto(servicio_id: str, foto_id: str, current_user: dict = Depends(get_current_user)):
    """Delete a photo (admin only)"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede eliminar fotos")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Remove the photo from the list
    fotos = servicio.get("fotos", [])
    original_count = len(fotos)
    fotos = [f for f in fotos if f.get("id") != foto_id]
    
    if len(fotos) == original_count:
        raise HTTPException(status_code=404, detail="Foto no encontrada")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "fotos": fotos,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    return {"message": "Foto eliminada exitosamente"}

# ============ ADMIN PHOTO MANAGEMENT FOR FINALIZED SERVICES ============

class AdminFotoCreate(BaseModel):
    etapa: str  # espera, carga, entrega
    categoria: str  # documentacion, carga, descarga, etc.
    imagen_base64: str

@api_router.post("/servicios/{servicio_id}/admin/fotos")
async def admin_add_foto(servicio_id: str, foto_data: AdminFotoCreate, current_user: dict = Depends(get_current_user)):
    """Add a photo as admin to a finalized service.
    Optimizado para bajo uso de memoria en producción.
    """
    # gc ya importado globalmente
    
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede agregar fotos")
    
    # Guardar datos necesarios antes de liberar memoria
    etapa_param = foto_data.etapa
    categoria_param = foto_data.categoria
    imagen_base64_param = foto_data.imagen_base64
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # VERIFICAR tamaño del documento ANTES de agregar
    import bson
    doc_size_mb = len(bson.encode(servicio)) / 1024 / 1024
    if doc_size_mb > 14.5:  # Dejar margen de 1.5MB
        raise HTTPException(
            status_code=413, 
            detail=f"El servicio tiene demasiadas fotos ({doc_size_mb:.1f}MB). Elimina algunas fotos antes de agregar más."
        )
    
    # El frontend ya comprime las imágenes a 1200px/65%
    # Solo hacemos logging del tamaño recibido
    img_size_kb = len(imagen_base64_param) / 1024
    logger.info(f"Admin foto recibida: {img_size_kb:.1f}KB")
    
    # Si la imagen es muy grande (>200KB), el frontend no comprimió bien
    # En ese caso hacemos compresión de respaldo
    imagen_final = None
    if img_size_kb > 200:
        logger.warning(f"Imagen muy grande ({img_size_kb:.1f}KB), aplicando compresión de respaldo")
        try:
            imagen_final = compress_image_base64(imagen_base64_param, max_width=1000, quality=65)
            logger.info(f"Comprimida a: {len(imagen_final)/1024:.1f}KB")
        except Exception as e:
            logger.error(f"Error en compresión de respaldo: {e}")
            imagen_final = imagen_base64_param
    else:
        imagen_final = imagen_base64_param
    
    # Liberar la imagen original de memoria
    del imagen_base64_param
    del foto_data
    gc.collect()
    
    # Create new photo object
    nueva_foto = {
        "id": str(uuid.uuid4()),
        "tipo": categoria_param,
        "categoria": categoria_param,
        "imagen_base64": imagen_final,
        "fecha": datetime.utcnow(),
        "active": True,
        "added_by": "admin",
        "aprobada": True
    }
    
    # Liberar imagen_final después de crear el dict
    del imagen_final
    gc.collect()
    
    # Get fotos_etapas or initialize
    fotos_etapas_raw = servicio.get("fotos_etapas")
    
    # Detectar formato y migrar si es necesario
    if fotos_etapas_raw:
        first_etapa = fotos_etapas_raw.get("espera")
        if isinstance(first_etapa, list):
            # Formato antiguo - migrar
            fotos_etapas = crear_estructura_fotos_etapas()
            for et in ["espera", "carga", "entrega"]:
                old_fotos = fotos_etapas_raw.get(et, [])
                fotos_etapas[et]["evidencia"] = old_fotos
        else:
            fotos_etapas = fotos_etapas_raw
    else:
        fotos_etapas = crear_estructura_fotos_etapas()
    
    etapa = etapa_param.lower()
    categoria = categoria_param.lower()
    
    # Ensure etapa exists
    if etapa not in fotos_etapas:
        fotos_etapas[etapa] = {cat: [] for cat in FOTO_CATEGORIAS}
    
    # Ensure categoria exists in etapa
    if categoria not in fotos_etapas[etapa]:
        fotos_etapas[etapa][categoria] = []
    
    # Add the new photo
    fotos_etapas[etapa][categoria].append(nueva_foto)
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "fotos_etapas": fotos_etapas,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    # Liberar memoria final
    gc.collect()
    
    return {"message": "Foto agregada exitosamente", "foto_id": nueva_foto["id"]}

# ============ SIGNATURE MANAGEMENT (ADMIN ONLY) ============

@api_router.put("/servicios/{servicio_id}/firma")
async def save_signature(servicio_id: str, signature_data: SignatureUpdate, current_user: dict = Depends(get_current_user)):
    """Save signature to service"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede guardar firma")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "firma_base64": signature_data.firma_base64,
                "firmante_nombre": signature_data.firmante_nombre,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    return {"message": "Firma guardada exitosamente"}

@api_router.delete("/servicios/{servicio_id}/firma")
async def delete_signature(servicio_id: str, current_user: dict = Depends(get_current_user)):
    """Delete signature from service"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede eliminar firma")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    await db.servicios.update_one(
        {"_id": ObjectId(servicio_id)},
        {
            "$set": {
                "firma_base64": None,
                "firmante_nombre": None,
                "fecha_actualizacion": datetime.utcnow()
            }
        }
    )
    
    return {"message": "Firma eliminada exitosamente"}

# ============ PDF GENERATION - CORPORATE PROFESSIONAL DESIGN ============

def download_logo(url):
    """Download logo from URL and return as BytesIO"""
    try:
        response = requests.get(url, timeout=10)
        if response.status_code == 200:
            return BytesIO(response.content)
    except Exception as e:
        logger.error(f"Error downloading logo from {url}: {e}")
    return None

# Corporate color palette
CORPORATE_DARK_BLUE = colors.HexColor('#1a365d')
CORPORATE_BLUE = colors.HexColor('#2c5282')
CORPORATE_LIGHT_BLUE = colors.HexColor('#3182ce')
CORPORATE_BG = colors.HexColor('#f8fafc')
CORPORATE_BORDER = colors.HexColor('#e2e8f0')
CORPORATE_TEXT = colors.HexColor('#2d3748')
CORPORATE_MUTED = colors.HexColor('#718096')
STATUS_GREEN = colors.HexColor('#38a169')
STATUS_YELLOW = colors.HexColor('#d69e2e')
STATUS_GRAY = colors.HexColor('#a0aec0')
FOOTER_LEGAL_GRAY = colors.HexColor('#9ca3af')  # Gray for legal footer text

# ========== HELPER FUNCTION: Download operator photo from URL ==========
def download_operator_photo(url: str, max_size: int = 150, make_circular: bool = False) -> Optional[bytes]:
    """
    Download and process operator photo from URL or base64.
    Returns bytes if successful, None if failed.
    If make_circular=True, creates a circular image with the photo centered (contain mode).
    """
    if not url:
        logger.info("No URL provided for operator photo")
        return None
    
    try:
        # Check if it's a base64 data URI
        if url.startswith('data:image'):
            logger.info("Processing base64 operator photo")
            # Extract base64 data from data URI
            # Format: data:image/jpeg;base64,/9j/4AAQSk...
            if ';base64,' in url:
                base64_data = url.split(';base64,')[1]
            else:
                base64_data = url.split(',')[1] if ',' in url else url
            
            import base64
            image_data = base64.b64decode(base64_data)
            logger.info(f"Base64 decoded, size: {len(image_data)} bytes")
            img = PILImage.open(BytesIO(image_data))
        else:
            # Download from URL
            logger.info(f"Downloading operator photo from: {url}")
            
            # Set headers to avoid blocking
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
            }
            response = requests.get(url, timeout=10, headers=headers)
            
            if response.status_code != 200:
                logger.error(f"Failed to download photo, status: {response.status_code}")
                return None
            
            logger.info(f"Photo downloaded successfully, size: {len(response.content)} bytes")
            img = PILImage.open(BytesIO(response.content))
        
        logger.info(f"Image opened, mode: {img.mode}, size: {img.size}")
        
        # Convert to RGBA for processing
        if img.mode != 'RGBA':
            img = img.convert('RGBA')
        
        # CONTAIN MODE: Resize maintaining aspect ratio to fit within max_size
        # This ensures the full image is visible without cropping
        img.thumbnail((max_size, max_size), PILImage.Resampling.LANCZOS)
        
        # Create a square canvas and center the image (white background for non-circular)
        width, height = img.size
        canvas = PILImage.new('RGBA', (max_size, max_size), (255, 255, 255, 255))
        
        # Calculate position to center the image
        x_offset = (max_size - width) // 2
        y_offset = (max_size - height) // 2
        canvas.paste(img, (x_offset, y_offset), img)
        
        logger.info(f"Image centered on canvas: {max_size}x{max_size}, image size: {width}x{height}")
        
        if make_circular:
            # Create circular mask
            from PIL import ImageDraw
            mask = PILImage.new('L', (max_size, max_size), 0)
            draw = ImageDraw.Draw(mask)
            draw.ellipse((0, 0, max_size, max_size), fill=255)
            
            # Apply mask to create circular image
            output_img = PILImage.new('RGBA', (max_size, max_size), (255, 255, 255, 0))
            output_img.paste(canvas, mask=mask)
            
            # Save as PNG to preserve transparency
            output = BytesIO()
            output_img.save(output, format='PNG', optimize=True)
            output.seek(0)
            result_bytes = output.getvalue()
            logger.info(f"Final circular image size: {len(result_bytes)} bytes (PNG)")
        else:
            # Convert to RGB for JPEG output
            if canvas.mode == 'RGBA':
                background = PILImage.new('RGB', canvas.size, (255, 255, 255))
                background.paste(canvas, mask=canvas.split()[3])
                canvas = background
            elif canvas.mode != 'RGB':
                canvas = canvas.convert('RGB')
            
            # Save compressed to BytesIO
            output = BytesIO()
            canvas.save(output, format='JPEG', quality=85, optimize=True)
            output.seek(0)
            result_bytes = output.getvalue()
            logger.info(f"Final compressed image size: {len(result_bytes)} bytes")
        
        return result_bytes
    except Exception as e:
        logger.error(f"Error processing operator photo: {e}")
        import traceback
        traceback.print_exc()
    
    return None


async def get_operador_foto_url(servicio: dict, db_instance) -> Optional[str]:
    """
    Obtiene la URL o base64 de la foto del operador. Primero intenta del servicio,
    si no existe, busca en el catálogo de operadores.
    Retorna URL, data URI (base64), o None.
    """
    # 1. Primero intentar desde el servicio guardado
    foto_url = servicio.get("operador_foto_url")
    if foto_url:
        logger.info(f"Using operator photo from service document: {foto_url[:50]}...")
        return foto_url
    
    # 2. Si no hay foto en el servicio, buscar en el catálogo de operadores
    operador_nombre = servicio.get("operador_nombre", "")
    if operador_nombre:
        operador_doc = await db_instance.operadores.find_one({"nombre": operador_nombre})
        if operador_doc:
            # Priorizar foto_base64 sobre foto_url
            if operador_doc.get("foto_base64"):
                foto_base64 = operador_doc.get("foto_base64")
                logger.info(f"Using operator photo base64 from catalog (length: {len(foto_base64)})")
                return foto_base64
            elif operador_doc.get("foto_url"):
                foto_url = operador_doc.get("foto_url")
                logger.info(f"Using operator photo URL from catalog: {foto_url}")
                return foto_url
    
    # 3. No se encontró foto
    logger.warning(f"No operator photo found for service {servicio.get('_id')}")
    return None

class CorporatePDF(SimpleDocTemplate):
    """Professional corporate PDF with header and watermark"""
    
    def __init__(self, *args, **kwargs):
        self.header_logo = kwargs.pop('header_logo', None)
        self.watermark_logo = kwargs.pop('watermark_logo', None)
        self.servicio_info = kwargs.pop('servicio_info', {})
        self._page_count = 0
        super().__init__(*args, **kwargs)
    
    def beforePage(self):
        """Called before each page - draw watermark FIRST (background)"""
        c = self.canv
        width, height = letter
        
        # Draw watermark FIRST (behind all content)
        if self.watermark_logo:
            try:
                self.watermark_logo.seek(0)
                c.saveState()
                
                # Slightly more visible opacity (12%) - mejor presencia visual
                c.setFillAlpha(0.12)
                
                # Large watermark: 80% of page width
                watermark_width = width * 0.80
                watermark_height = watermark_width  # Square aspect
                
                # Center position
                x = (width - watermark_width) / 2
                y = (height - watermark_height) / 2
                
                c.drawImage(
                    ImageReader(self.watermark_logo),
                    x, y,
                    width=watermark_width,
                    height=watermark_height,
                    preserveAspectRatio=True,
                    mask='auto'
                )
                
                c.restoreState()
            except Exception as e:
                logger.error(f"Error drawing watermark: {e}")
    
    def afterPage(self):
        """Called after each page - draw header and footer"""
        c = self.canv
        width, height = letter
        self._page_count = c.getPageNumber()
        
        # ========== HEADER MEJORADO (100px altura) ==========
        header_height = 100  # 100px de altura
        c.setFillColor(CORPORATE_DARK_BLUE)
        c.rect(0, height - header_height, width, header_height, fill=1, stroke=0)
        
        # Padding interno del logo
        logo_padding = 12  # 12px de padding interno
        logo_x = 0.5 * inch
        logo_max_width = 1.3 * inch  # Ligeramente más grande
        
        if self.header_logo:
            try:
                self.header_logo.seek(0)
                pil_img = PILImage.open(self.header_logo)
                img_w, img_h = pil_img.size
                aspect = img_h / img_w
                logo_height = logo_max_width * aspect
                
                # Limitar altura del logo si es muy alto
                max_logo_height = header_height - (logo_padding * 2)
                if logo_height > max_logo_height:
                    logo_height = max_logo_height
                    logo_width = logo_height / aspect
                else:
                    logo_width = logo_max_width
                
                # Centrar logo verticalmente en header con padding
                logo_y = height - header_height + (header_height - logo_height) / 2
                
                # Fondo blanco para el logo con padding
                bg_padding = 8
                c.setFillColor(colors.white)
                c.roundRect(
                    logo_x - bg_padding,
                    logo_y - bg_padding,
                    logo_width + (bg_padding * 2),
                    logo_height + (bg_padding * 2),
                    radius=4,
                    fill=1,
                    stroke=0
                )
                
                self.header_logo.seek(0)
                c.drawImage(
                    ImageReader(self.header_logo),
                    logo_x, logo_y,
                    width=logo_width,
                    height=logo_height,
                    preserveAspectRatio=True,
                    mask='auto'
                )
            except Exception as e:
                logger.error(f"Error drawing header logo: {e}")
        
        # ========== TÍTULO CENTRADO VERTICALMENTE ==========
        # Calcular posición del título alineado con el centro del header
        title_x = logo_x + logo_max_width + 0.6 * inch
        
        # Centro vertical del header
        header_center_y = height - (header_height / 2)
        
        # Título principal (línea superior)
        c.setFillColor(colors.white)
        c.setFont('Helvetica-Bold', 16)
        title_main_y = header_center_y + 8  # 8px arriba del centro
        c.drawString(title_x, title_main_y, "REPORTE DE SERVICIO")
        
        # Subtítulo (línea inferior)
        c.setFont('Helvetica', 12)
        title_sub_y = header_center_y - 10  # 10px abajo del centro
        c.drawString(title_x, title_sub_y, "DE TRANSPORTE")
        
        # ========== PROFESSIONAL FOOTER WITH LEGAL DISCLAIMER ==========
        footer_y = 0.6 * inch
        
        # Footer separator line
        c.setStrokeColor(CORPORATE_BORDER)
        c.setLineWidth(0.5)
        c.line(0.5 * inch, footer_y + 0.25 * inch, width - 0.5 * inch, footer_y + 0.25 * inch)
        
        # Legal disclaimer text - centered, multiple lines
        c.setFillColor(FOOTER_LEGAL_GRAY)  # Gray muted color
        c.setFont('Helvetica', 7)
        
        # Legal text lines
        legal_line1 = "Este documento contiene información proporcionada por el cliente."
        legal_line2 = "Transportes Virgo la gestiona de forma confidencial y exclusivamente para fines operativos, sin asumir responsabilidad sobre su contenido."
        legal_line3 = "Uso exclusivo del destinatario autorizado."
        
        # Calculate center position
        center_x = width / 2
        
        # Draw legal text centered
        c.drawCentredString(center_x, footer_y + 0.08 * inch, legal_line1)
        c.drawCentredString(center_x, footer_y - 0.04 * inch, legal_line2)
        c.drawCentredString(center_x, footer_y - 0.16 * inch, legal_line3)
        
        # Page number - right side, below legal text
        page_num = c.getPageNumber()
        c.setFont('Helvetica', 8)
        c.drawRightString(width - 0.5 * inch, footer_y - 0.30 * inch, f"Página {page_num}")

@api_router.get("/servicios/{servicio_id}/pdf")
async def generate_pdf(
    servicio_id: str, 
    token: Optional[str] = None, 
    authorization: Optional[str] = Header(None, alias="Authorization")
):
    # Support both header auth and query parameter auth for mobile compatibility
    auth_token = token  # Query parameter takes priority for mobile
    
    # Fall back to header if no query token
    if not auth_token and authorization:
        auth_token = authorization
        if auth_token.startswith("Bearer "):
            auth_token = auth_token[7:]
    
    if not auth_token:
        raise HTTPException(status_code=401, detail="Token requerido")
    
    try:
        payload = jwt.decode(auth_token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
        user_id = payload.get("user_id")
        if not user_id:
            raise HTTPException(status_code=401, detail="Token inválido")
        user = await db.users.find_one({"_id": ObjectId(user_id)})
        if not user or user["role"] != "admin":
            raise HTTPException(status_code=403, detail="Solo admin puede generar PDFs")
    except jwt.ExpiredSignatureError:
        raise HTTPException(status_code=401, detail="Token expirado")
    except jwt.InvalidTokenError:
        raise HTTPException(status_code=401, detail="Token inválido")
    
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # ========== BUSCAR DATOS ADICIONALES ==========
    # Buscar placas del camión en el catálogo
    unidad_nombre = servicio.get("unidad", "") or servicio.get("camion", "")
    placas = servicio.get("placa_camion", "N/A")
    
    # tipo_caja: SIEMPRE del servicio (THERMO o CAJA SECA seleccionado en el formulario)
    # NO usar fallback al catálogo de camiones
    tipo_caja = servicio.get("tipo_caja") or "N/A"
    entidad_caja = servicio.get("entidad_caja") or "N/A"
    placa_caja = servicio.get("placa_caja") or "N/A"
    
    logger.info(f"=== PDF DATOS CAJA ===")
    logger.info(f"  tipo_caja (from servicio): '{tipo_caja}'")
    logger.info(f"  entidad_caja: '{entidad_caja}'")
    logger.info(f"  placa_caja: '{placa_caja}'")
    logger.info(f"  unidad/camion: '{unidad_nombre}'")
    logger.info(f"  placa_camion: '{placas}'")
    
    # OBTENER FOTO DEL OPERADOR (primero servicio, luego catálogo)
    foto_url = await get_operador_foto_url(servicio, db)
    
    # Buscar teléfono del operador en el catálogo por nombre
    operador_nombre = servicio.get("operador_nombre", "")
    telefono_operador = ""
    if operador_nombre:
        operador_doc = await db.operadores.find_one({"nombre": operador_nombre})
        if operador_doc:
            telefono_operador = operador_doc.get("telefono", "")
    
    operador_data = {
        "nombre": operador_nombre,
        "foto_url": foto_url,  # Foto desde servicio o catálogo
        "licencia": servicio.get("operador_licencia", ""),  # Licencia guardada con el servicio
        "telefono": telefono_operador  # Teléfono desde catálogo de operadores
    }
    
    logger.info(f"PDF generation - Operator photo URL: {operador_data.get('foto_url')}")
    logger.info(f"PDF generation - Operator phone: {telefono_operador}")
    
    # Download logos
    header_logo = download_logo(LOGO_HEADER_URL)
    watermark_logo = download_logo(LOGO_WATERMARK_URL)
    
    buffer = BytesIO()
    doc = CorporatePDF(
        buffer, 
        pagesize=letter, 
        topMargin=1.5*inch,  # Space for header (100px + padding)
        bottomMargin=1.0*inch,  # Space for legal footer
        leftMargin=0.6*inch,
        rightMargin=0.6*inch,
        header_logo=header_logo,
        watermark_logo=watermark_logo,
        servicio_info=servicio
    )
    
    # ========== STYLES ==========
    styles = getSampleStyleSheet()
    
    section_title_style = ParagraphStyle(
        'SectionTitle', 
        parent=styles['Heading2'], 
        fontSize=16, 
        spaceAfter=15,
        spaceBefore=20,
        textColor=CORPORATE_DARK_BLUE,
        fontName='Helvetica-Bold'
    )
    
    normal_style = ParagraphStyle(
        'Normal', 
        parent=styles['Normal'], 
        fontSize=10, 
        textColor=CORPORATE_TEXT
    )
    
    photo_info_style = ParagraphStyle(
        'PhotoInfo',
        parent=styles['Normal'],
        fontSize=8,
        textColor=CORPORATE_MUTED,
        alignment=1  # Center
    )
    
    # ========== BUILD STORY ==========
    story = []
    
    # ==========================================
    # PÁGINA 1: PORTADA CORPORATIVA DEL SERVICIO
    # ==========================================
    # Función render_portada - genera la portada con todos los datos
    
    def render_portada(story, servicio, placas, tipo_caja, operador_data, styles):
        """
        Renderiza la portada corporativa del servicio - VERSIÓN PROFESIONAL CENTRADA.
        Foto circular 120x120, todo centrado verticalmente, diseño compacto.
        """
        # Extraer datos del servicio
        tipo_servicio = servicio.get("tipo_servicio") or servicio.get("cliente") or "N/A"
        cliente = servicio.get("cliente")  # Nombre del cliente (opcional)
        unidad = servicio.get("unidad", "N/A")
        operador = servicio.get("operador_nombre", "N/A")
        estado = servicio.get("estado", "pendiente")
        fecha = servicio.get("fecha_creacion", datetime.utcnow())
        if isinstance(fecha, str):
            fecha = datetime.fromisoformat(fecha.replace('Z', '+00:00'))
        
        # Convert to Mexico timezone
        fecha_mexico = to_mexico_time(fecha)
        fecha_str = fecha_mexico.strftime("%d/%m/%Y")
        hora_str = fecha_mexico.strftime("%H:%M")
        
        # Handle multiple origins (backward compatible)
        origenes = servicio.get("origenes", [])
        if not origenes and servicio.get("origen"):
            origenes = [servicio.get("origen")]
        origen_primario = origenes[0] if origenes else "N/A"
        
        destinos = servicio.get("destinos", [])
        if not destinos and servicio.get("destino"):
            destinos = [servicio.get("destino")]
        destino_final = destinos[-1] if destinos else "N/A"
        
        # Licencia del operador
        licencia = operador_data.get("licencia", "") if operador_data else ""
        telefono_operador = operador_data.get("telefono", "") if operador_data else ""
        
        # Datos de la caja (nuevos campos)
        entidad_caja = servicio.get("entidad_caja") or "N/A"
        placa_caja = servicio.get("placa_caja") or "N/A"
        
        # Status color and text
        if estado == "completado":
            status_color = STATUS_GREEN
            status_text = "COMPLETADO"
        elif estado == "en_progreso":
            status_color = STATUS_YELLOW
            status_text = "EN PROGRESO"
        else:
            status_color = STATUS_GRAY
            status_text = "PENDIENTE"
        
        # Unidad sin tipo de caja (el tipo de caja va en su propio campo)
        unidad_display = unidad
        
        # ========== ENCABEZADO: SERVICIO Y CLIENTE ==========
        story.append(Spacer(1, 0.05*inch))
        
        # Nombre de empresa (pequeño, arriba)
        story.append(Paragraph(
            '<font size="9" color="#a0aec0">VIRGO TRANSPORTES REFRIGERADOS</font>',
            ParagraphStyle('CompanyName', alignment=1, spaceAfter=8)
        ))
        
        # SERVICIO: {valor} - en una línea, centrado
        # Label 16px normal, valor 22px bold
        story.append(Paragraph(
            f'<font size="16" color="#4a5568">SERVICIO: </font><font size="22" color="#1a365d"><b>{tipo_servicio.upper()}</b></font>',
            ParagraphStyle('ServiceLine', alignment=1, spaceAfter=8)
        ))
        
        # CLIENTE: {valor} - en una línea, centrado (si existe cliente diferente)
        cliente_display = cliente if cliente and cliente.upper() != tipo_servicio.upper() else None
        if cliente_display:
            story.append(Paragraph(
                f'<font size="16" color="#4a5568">CLIENTE: </font><font size="22" color="#1a365d"><b>{cliente_display.upper()}</b></font>',
                ParagraphStyle('ClientLine', alignment=1, spaceAfter=6)
            ))
        else:
            story.append(Spacer(1, 0.02*inch))
        
        # Línea decorativa centrada
        line_table = Table([['']], colWidths=[3*inch])
        line_table.setStyle(TableStyle([
            ('LINEBELOW', (0, 0), (-1, -1), 1.5, CORPORATE_BLUE),
        ]))
        centered_line = Table([[line_table]], colWidths=[7*inch])
        centered_line.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        story.append(centered_line)
        story.append(Spacer(1, 0.12*inch))
        
        # ========== FOTO DEL OPERADOR CIRCULAR (COMPACTA) ==========
        PHOTO_SIZE = 1.5 * inch  # ~108px - reducido 25% para layout compacto
        operator_photo = None
        
        if operador_data and operador_data.get("foto_url"):
            # Descargar con recorte circular
            foto_bytes = download_operator_photo(operador_data.get("foto_url"), max_size=200, make_circular=True)
            if foto_bytes:
                try:
                    foto_buffer = BytesIO(foto_bytes)
                    operator_photo = RLImage(foto_buffer, width=PHOTO_SIZE, height=PHOTO_SIZE)
                    logger.info("Operator circular photo created successfully for PDF")
                except Exception as e:
                    logger.error(f"Error creating operator photo image: {e}")
                    operator_photo = None
        
        # Foto o ningún fallback (NO mostrar inicial)
        if operator_photo:
            # Contenedor con borde azul simulando círculo
            photo_container = Table([[operator_photo]], colWidths=[PHOTO_SIZE + 0.1*inch], rowHeights=[PHOTO_SIZE + 0.1*inch])
            photo_container.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                ('BOX', (0, 0), (-1, -1), 3, CORPORATE_BLUE),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ('LEFTPADDING', (0, 0), (-1, -1), 0),
                ('RIGHTPADDING', (0, 0), (-1, -1), 0),
                ('TOPPADDING', (0, 0), (-1, -1), 0),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
            ]))
            
            # Centrar foto
            centered_photo = Table([[photo_container]], colWidths=[7*inch])
            centered_photo.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_photo)
            story.append(Spacer(1, 0.1*inch))
        else:
            # Sin foto - espacio mínimo
            story.append(Spacer(1, 0.1*inch))
        
        # ========== NOMBRE DEL OPERADOR (MÁS DESTACADO) ==========
        story.append(Paragraph(
            f'<font size="18" color="#1a365d"><b>{operador.upper()}</b></font>',
            ParagraphStyle('Name', alignment=1, spaceAfter=6)
        ))
        story.append(Spacer(1, 0.05*inch))  # 3.6px spacing
        story.append(Paragraph(
            '<font size="9" color="#718096">OPERADOR ASIGNADO</font>',
            ParagraphStyle('Label', alignment=1, spaceAfter=2)
        ))
        # Teléfono del operador (si existe)
        if telefono_operador:
            story.append(Paragraph(
                f'<font size="10" color="#4a5568">Tel: {telefono_operador}</font>',
                ParagraphStyle('Phone', alignment=1, spaceAfter=10)
            ))
        else:
            story.append(Spacer(1, 0.05*inch))
        
        # ========== DATOS DEL VEHÍCULO (TARJETAS COMPACTAS EN FILA) ==========
        card_style = TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f7fafc')),
            ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BORDER),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('TOPPADDING', (0, 0), (-1, -1), 5),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
        ])
        
        # Tarjeta Tracto/Camion (antes UNIDAD)
        unidad_card = Table([[Paragraph(f'<font size="7" color="#718096">TRACTO/CAMION</font><br/><font size="10" color="#1a365d"><b>{unidad_display}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
        unidad_card.setStyle(card_style)
        
        # Tarjeta Placas
        placas_card = Table([[Paragraph(f'<font size="7" color="#718096">PLACAS</font><br/><font size="10" color="#1a365d"><b>{placas}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
        placas_card.setStyle(card_style)
        
        # Tarjeta Licencia
        licencia_display = licencia if licencia else "N/A"
        licencia_card = Table([[Paragraph(f'<font size="7" color="#718096">LICENCIA</font><br/><font size="10" color="#1a365d"><b>{licencia_display}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
        licencia_card.setStyle(card_style)
        
        # Fila de tarjetas compacta
        cards_row = Table([[unidad_card, placas_card, licencia_card]], colWidths=[2.2*inch, 2.2*inch, 2.2*inch], spaceBefore=0, spaceAfter=0)
        cards_row.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ]))
        
        centered_cards = Table([[cards_row]], colWidths=[7*inch])
        centered_cards.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        story.append(centered_cards)
        story.append(Spacer(1, 0.06*inch))
        
        # ========== FILA DE DATOS DE CAJA ==========
        tipo_caja_display = tipo_caja if tipo_caja else "N/A"
        
        # Tarjeta Tipo Caja
        tipo_caja_card = Table([[Paragraph(f'<font size="7" color="#718096">TIPO CAJA</font><br/><font size="10" color="#1a365d"><b>{tipo_caja_display}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
        tipo_caja_card.setStyle(card_style)
        
        # Tarjeta Entidad
        entidad_card = Table([[Paragraph(f'<font size="7" color="#718096">ENTIDAD</font><br/><font size="10" color="#1a365d"><b>{entidad_caja}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
        entidad_card.setStyle(card_style)
        
        # Tarjeta Placa Caja
        placa_caja_card = Table([[Paragraph(f'<font size="7" color="#718096">PLACA CAJA</font><br/><font size="10" color="#1a365d"><b>{placa_caja}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
        placa_caja_card.setStyle(card_style)
        
        # Fila de tarjetas de caja
        caja_cards_row = Table([[tipo_caja_card, entidad_card, placa_caja_card]], colWidths=[2.2*inch, 2.2*inch, 2.2*inch], spaceBefore=0, spaceAfter=0)
        caja_cards_row.setStyle(TableStyle([
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 2),
            ('RIGHTPADDING', (0, 0), (-1, -1), 2),
        ]))
        
        centered_caja_cards = Table([[caja_cards_row]], colWidths=[7*inch])
        centered_caja_cards.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        story.append(centered_caja_cards)
        story.append(Spacer(1, 0.08*inch))
        
        # ==================================================================================
        # LAYOUT HORIZONTAL: 3 COLUMNAS (RUTA | CITAS | TRAZABILIDAD) o 2 COLUMNAS si mostrar_trazabilidad=False
        # ==================================================================================
        
        # Verificar si se debe mostrar la trazabilidad (default True)
        mostrar_trazabilidad = servicio.get("mostrar_trazabilidad", True)
        if mostrar_trazabilidad is None:
            mostrar_trazabilidad = True
        
        # Preparar datos de CITAS
        cita_carga_raw = servicio.get("cita_carga") or servicio.get("fecha_cita")
        cita_descarga_raw = servicio.get("cita_descarga")
        
        def formatear_cita_compacta(fecha_raw: str) -> str:
            """Formatea una fecha de forma compacta"""
            if not fecha_raw:
                return None
            try:
                fecha_str_clean = str(fecha_raw).replace('Z', '').replace('+00:00', '').split('+')[0]
                fecha_dt = datetime.fromisoformat(fecha_str_clean) if 'T' in fecha_str_clean else datetime.strptime(fecha_str_clean, "%Y-%m-%d %H:%M")
                return f"{fecha_dt.strftime('%d/%m/%Y')}<br/>{fecha_dt.strftime('%I:%M %p').lstrip('0')}"
            except:
                return None
        
        cita_carga_fmt = formatear_cita_compacta(cita_carga_raw)
        cita_descarga_fmt = formatear_cita_compacta(cita_descarga_raw)
        
        # Preparar datos de TRAZABILIDAD
        hora_llegada_origen = servicio.get("hora_llegada_origen") or servicio.get("hora_llegada")
        hora_inicio_carga = servicio.get("hora_inicio_carga")
        hora_fin_carga = servicio.get("hora_fin_carga") or servicio.get("hora_carga")
        hora_llegada_destino = servicio.get("hora_llegada_destino")
        hora_inicio_descarga = servicio.get("hora_inicio_descarga")
        hora_fin_descarga = servicio.get("hora_fin_descarga") or servicio.get("hora_entrega")
        
        def format_hora_compacta(dt):
            if not dt:
                return "--"
            # Manejar strings ISO
            if isinstance(dt, str):
                try:
                    dt = datetime.fromisoformat(dt.replace('Z', '+00:00'))
                except Exception as e:
                    logger.warning(f"Error parseando fecha string '{dt}': {e}")
                    return "--"
            # Manejar datetime objects
            if isinstance(dt, datetime):
                try:
                    return to_mexico_time(dt).strftime("%I:%M %p").lstrip("0")
                except Exception as e:
                    logger.warning(f"Error convirtiendo datetime: {e}")
                    return "--"
            return "--"
        
        # ========== COLUMNA 1: RUTA (Origen → Destino) ==========
        if len(origenes) > 1:
            origenes_txt = "<br/>".join([f'<font size="8">• {o[:25]}</font>' for o in origenes[:3]])
            ruta_col1 = f'<font size="7" color="#718096"><b>ORIGEN(ES)</b></font><br/>{origenes_txt}'
        else:
            ruta_col1 = f'<font size="7" color="#718096"><b>ORIGEN</b></font><br/><font size="9" color="#1a365d"><b>{origen_primario[:30]}</b></font>'
        
        ruta_col1 += f'<br/><font size="12" color="#3182ce">↓</font><br/>'
        
        if len(destinos) > 1:
            destinos_txt = "<br/>".join([f'<font size="8">• {d[:25]}</font>' for d in destinos[:3]])
            ruta_col1 += f'<font size="7" color="#718096"><b>DESTINO(S)</b></font><br/>{destinos_txt}'
        else:
            ruta_col1 += f'<font size="7" color="#718096"><b>DESTINO</b></font><br/><font size="9" color="#1a365d"><b>{destino_final[:30]}</b></font>'
        
        col1_table = Table([[Paragraph(ruta_col1, ParagraphStyle('Col1', alignment=1, leading=10))]], colWidths=[2.2*inch])
        col1_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f0f5ff')),
            ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BLUE),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        
        # ========== COLUMNA 2: CITAS ==========
        citas_content = '<font size="7" color="#d69e2e"><b>CITAS PROGRAMADAS</b></font><br/>'
        if cita_carga_fmt:
            citas_content += f'<font size="7" color="#3182ce"><b>Carga:</b></font><br/><font size="8" color="#1a365d">{cita_carga_fmt}</font><br/>'
        if cita_descarga_fmt:
            citas_content += f'<font size="7" color="#38a169"><b>Descarga:</b></font><br/><font size="8" color="#1a365d">{cita_descarga_fmt}</font>'
        if not cita_carga_fmt and not cita_descarga_fmt:
            citas_content += '<font size="8" color="#a0aec0">Sin citas</font>'
        
        col2_table = Table([[Paragraph(citas_content, ParagraphStyle('Col2', alignment=1, leading=10))]], colWidths=[2.2*inch])
        col2_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#fffbeb')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#d69e2e')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
            ('LEFTPADDING', (0, 0), (-1, -1), 4),
            ('RIGHTPADDING', (0, 0), (-1, -1), 4),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
        ]))
        
        # ========== CONDICIONAL: TRAZABILIDAD EN PDF ==========
        if mostrar_trazabilidad:
            # ========== COLUMNA 3: TRAZABILIDAD DINÁMICA ==========
            # Obtener trazabilidad dinámica si existe
            trazabilidad_dinamica = servicio.get("trazabilidad") or {}
            num_origenes = len(origenes) if origenes else 1
            num_destinos = len(destinos) if destinos else 1
            
            def get_traz_value(campo_basico, campo_dinamico=None):
                """Obtiene valor de trazabilidad priorizando dinámico sobre básico"""
                if campo_dinamico and campo_dinamico in trazabilidad_dinamica:
                    return trazabilidad_dinamica[campo_dinamico]
                return servicio.get(campo_basico)
            
            traz_content = '<font size="7" color="#d69e2e"><b>TRAZABILIDAD</b></font><br/>'
            
            # Llegada siempre
            traz_content += f'<font size="6" color="#4a5568">Llegada:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(get_traz_value("hora_llegada_origen"))}</b></font><br/>'
            
            # Cargas (dinámico según número de orígenes)
            if num_origenes == 1:
                traz_content += f'<font size="6" color="#4a5568">Ini.Carga:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(get_traz_value("hora_inicio_carga", "inicio_carga_1"))}</b></font><br/>'
                traz_content += f'<font size="6" color="#4a5568">Fin Carga:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(get_traz_value("hora_fin_carga", "fin_carga_1"))}</b></font><br/>'
            else:
                for i in range(1, num_origenes + 1):
                    ini_val = trazabilidad_dinamica.get(f"inicio_carga_{i}") or (servicio.get("hora_inicio_carga") if i == 1 else None)
                    fin_val = trazabilidad_dinamica.get(f"fin_carga_{i}") or (servicio.get("hora_fin_carga") if i == 1 else None)
                    traz_content += f'<font size="6" color="#4a5568">Ini.Carga {i}:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(ini_val)}</b></font><br/>'
                    traz_content += f'<font size="6" color="#4a5568">Fin Carga {i}:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(fin_val)}</b></font><br/>'
            
            # Descargas (dinámico según número de destinos)
            if num_destinos == 1:
                traz_content += f'<font size="6" color="#4a5568">Lleg.Dest:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(get_traz_value("hora_llegada_destino", "llegada_destino_1"))}</b></font><br/>'
                traz_content += f'<font size="6" color="#4a5568">Ini.Desc:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(get_traz_value("hora_inicio_descarga", "inicio_descarga_1"))}</b></font><br/>'
                traz_content += f'<font size="6" color="#4a5568">Fin Desc:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(get_traz_value("hora_fin_descarga", "fin_descarga_1"))}</b></font>'
            else:
                for i in range(1, num_destinos + 1):
                    lleg_val = trazabilidad_dinamica.get(f"llegada_destino_{i}") or (servicio.get("hora_llegada_destino") if i == 1 else None)
                    ini_val = trazabilidad_dinamica.get(f"inicio_descarga_{i}") or (servicio.get("hora_inicio_descarga") if i == 1 else None)
                    fin_val = trazabilidad_dinamica.get(f"fin_descarga_{i}") or (servicio.get("hora_fin_descarga") if i == 1 else None)
                    traz_content += f'<font size="6" color="#4a5568">Lleg.Dest {i}:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(lleg_val)}</b></font><br/>'
                    traz_content += f'<font size="6" color="#4a5568">Ini.Desc {i}:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(ini_val)}</b></font><br/>'
                    # El último sin <br/>
                    if i < num_destinos:
                        traz_content += f'<font size="6" color="#4a5568">Fin Desc {i}:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(fin_val)}</b></font><br/>'
                    else:
                        traz_content += f'<font size="6" color="#4a5568">Fin Desc {i}:</font> <font size="7" color="#1a365d"><b>{format_hora_compacta(fin_val)}</b></font>'
            
            col3_table = Table([[Paragraph(traz_content, ParagraphStyle('Col3', alignment=1, leading=9))]], colWidths=[2.2*inch])
            col3_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#fffff0')),
                ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#d69e2e')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
            ]))
            
            # ========== FILA HORIZONTAL DE 3 COLUMNAS ==========
            three_col_row = Table(
                [[col1_table, col2_table, col3_table]], 
                colWidths=[2.3*inch, 2.3*inch, 2.3*inch],
                spaceBefore=0, 
                spaceAfter=0
            )
            three_col_row.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ]))
            
            centered_three_col = Table([[three_col_row]], colWidths=[7.2*inch])
            centered_three_col.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_three_col)
        else:
            # ========== FILA HORIZONTAL DE 2 COLUMNAS (SIN TRAZABILIDAD) ==========
            two_col_row = Table(
                [[col1_table, col2_table]], 
                colWidths=[3.4*inch, 3.4*inch],
                spaceBefore=0, 
                spaceAfter=0
            )
            two_col_row.setStyle(TableStyle([
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 3),
                ('RIGHTPADDING', (0, 0), (-1, -1), 3),
            ]))
            
            centered_two_col = Table([[two_col_row]], colWidths=[7.2*inch])
            centered_two_col.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_two_col)
        
        story.append(Spacer(1, 0.08*inch))
        
        # ========== NÚMERO DE FACTURA Y REFERENCIA (SI EXISTEN) ==========
        numero_factura = servicio.get("numero_factura")
        referencia_cliente = servicio.get("referencia_cliente")
        
        # Solo mostrar si hay al menos un valor (no vacío, no None)
        has_factura = numero_factura and str(numero_factura).strip()
        has_referencia = referencia_cliente and str(referencia_cliente).strip()
        
        if has_factura or has_referencia:
            # Construir contenido en LÍNEAS SEPARADAS dentro del mismo recuadro
            factura_parts = []
            if has_factura:
                factura_parts.append(f'<font size="10" color="#000000">No. Factura:</font> <font size="13" color="#000000"><b>{numero_factura}</b></font>')
            if has_referencia:
                if has_factura:
                    factura_parts.append('<br/>')  # Salto de línea entre factura y referencia
                factura_parts.append(f'<font size="10" color="#000000">Ref. Cliente:</font> <font size="13" color="#000000"><b>{referencia_cliente}</b></font>')
            
            factura_table = Table(
                [[Paragraph(''.join(factura_parts), ParagraphStyle('Factura', alignment=1, leading=18))]],
                colWidths=[5.5*inch]
            )
            factura_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f5f0ff')),
                ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#805ad5')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ]))
            centered_factura = Table([[factura_table]], colWidths=[7.2*inch])
            centered_factura.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_factura)
            story.append(Spacer(1, 0.05*inch))
        
        # ========== FECHA/HORA y ESTADO (COMPACTO) ==========
        datetime_status = f'''<font size="9" color="#4a5568"><b>{fecha_str}</b></font>  <font size="8" color="#718096">|</font>  <font size="9" color="#4a5568"><b>{hora_str}</b></font>   <font size="11" color="{status_color.hexval()}"><b>● {status_text}</b></font>'''
        
        story.append(Paragraph(datetime_status, ParagraphStyle('DateTime', alignment=1, leading=12)))
        story.append(Spacer(1, 0.05*inch))
        
        # ========== PIE DISCRETO ==========
        story.append(Paragraph(
            f'<font size="7" color="#a0aec0">Documento generado el {datetime.now(MEXICO_TZ).strftime("%d/%m/%Y a las %H:%M")}</font>',
            ParagraphStyle('CoverFooter', alignment=1)
        ))
        
        return story
    
    # Ejecutar render_portada
    render_portada(story, servicio, placas, tipo_caja, operador_data, styles)
    
    # ==========================================
    # PÁGINAS DE FOTOS - FOLIOS Y EVIDENCIAS
    # ==========================================
    
    fotos = servicio.get("fotos", [])
    
    # FILTRAR solo fotos activas (active=True o sin campo para backward compat)
    fotos = [f for f in fotos if isinstance(f, dict) and f.get("active", True)]
    
    if fotos:
        # Category labels for display
        CATEGORY_LABELS = {
            'folio': 'FOLIO',
            'transporte': 'TRANSPORTE',
            'placas': 'PLACAS',
            'temperatura': 'TEMPERATURA',
            'sello': 'SELLO',
            'licencia': 'LICENCIA',
            'carga': 'CARGA',
            'descarga': 'DESCARGA',
        }
        
        # Category colors for badges
        CATEGORY_COLORS = {
            'folio': '#dd6b20',
            'transporte': '#3182ce',
            'placas': '#805ad5',
            'temperatura': '#38a169',
            'sello': '#e53e3e',
            'licencia': '#718096',
            'carga': '#3182ce',
            'descarga': '#805ad5',
        }
        
        # Separate folios from other photos
        folio_photos = []
        regular_photos = []
        
        for foto in fotos:
            cat = foto.get('categoria', foto.get('tipo', 'carga')).lower()
            if cat == 'folio':
                folio_photos.append(foto)
            else:
                regular_photos.append(foto)
        
        # ==========================================
        # FOLIOS - BLOQUE INDIVISIBLE CON ESCALADO AUTOMÁTICO
        # ==========================================
        # Cálculo de espacio disponible:
        # - Página Letter: 11" altura
        # - Margen superior: 1.1"
        # - Margen inferior: 0.8"
        # - Header del PDF: ~0.5"
        # - Título sección: ~0.4"
        # - Metadata: ~0.5"
        # - Disponible para imagen: ~7.7" (usamos 65% = ~6.5")
        
        MAX_FOLIO_HEIGHT = 6.0 * inch  # 65% de espacio útil - garantiza que quepa
        MAX_FOLIO_WIDTH = 6.2 * inch
        
        for idx, foto in enumerate(folio_photos):
            try:
                img_data = foto.get("imagen_base64", "")
                
                # VALIDACIÓN TEMPRANA: skip si no hay datos de imagen válidos
                if not img_data or not isinstance(img_data, str) or len(img_data) < 100:
                    logger.warning(f"Folio photo {idx} sin imagen válida, saltando...")
                    continue
                
                if img_data.startswith("data:"):
                    img_data = img_data.split(",")[1]
                
                img_bytes = base64.b64decode(img_data)
                
                # OPTIMIZACIÓN: Comprimir imagen para PDF (800px max, 60% calidad)
                img_bytes = optimize_image_for_pdf(img_bytes, max_width=800, quality=60)
                
                # Obtener dimensiones reales de la imagen optimizada
                pil_img = PILImage.open(BytesIO(img_bytes))
                img_width, img_height = pil_img.size
                
                # Calcular escala para que quepa (contain)
                width_ratio = MAX_FOLIO_WIDTH / img_width
                height_ratio = MAX_FOLIO_HEIGHT / img_height
                scale = min(width_ratio, height_ratio)
                
                final_width = img_width * scale
                final_height = img_height * scale
                
                # Crear imagen con tamaño calculado
                img_buffer = BytesIO(img_bytes)
                img = RLImage(img_buffer, width=final_width, height=final_height)
                
                # Metadata compacta
                fecha_foto = foto.get("fecha", datetime.utcnow())
                if isinstance(fecha_foto, str):
                    fecha_foto = datetime.fromisoformat(fecha_foto.replace('Z', '+00:00'))
                fecha_foto_mexico = to_mexico_time(fecha_foto)
                
                fecha_str = fecha_foto_mexico.strftime("%d/%m/%Y")
                hora_str = fecha_foto_mexico.strftime("%H:%M")
                ubicacion = foto.get("direccion", "")
                if len(ubicacion) > 60:
                    ubicacion = ubicacion[:60] + "..."
                
                # Metadata en una sola línea compacta
                meta_parts = [f'<b>{fecha_str}</b>', f'<b>{hora_str}</b>']
                if ubicacion:
                    meta_parts.append(f'<font color="#718096">{ubicacion}</font>')
                meta_text = ' | '.join(meta_parts)
                
                # Tabla única con imagen + metadata (SIN separación posible)
                folio_content = [
                    [img],
                    [Paragraph(f'<font size="9">{meta_text}</font>', ParagraphStyle(
                        'FolioMeta',
                        parent=styles['Normal'],
                        fontSize=9,
                        alignment=1,  # Center
                        spaceAfter=0,
                        spaceBefore=3,
                    ))]
                ]
                
                folio_table = Table(folio_content, colWidths=[6.5*inch])
                folio_table.setStyle(TableStyle([
                    ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                    ('BOX', (0, 0), (-1, -1), 1, CORPORATE_BORDER),
                    ('TOPPADDING', (0, 0), (0, 0), 6),
                    ('BOTTOMPADDING', (0, 0), (0, 0), 3),
                    ('TOPPADDING', (0, 1), (0, 1), 4),
                    ('BOTTOMPADDING', (0, 1), (0, 1), 6),
                    ('LEFTPADDING', (0, 0), (-1, -1), 8),
                    ('RIGHTPADDING', (0, 0), (-1, -1), 8),
                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                    ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                ]))
                
                # Título del folio
                folio_title = Paragraph("FOLIO / DOCUMENTACIÓN", ParagraphStyle(
                    'FolioTitle',
                    parent=styles['Heading2'],
                    fontSize=14,
                    spaceAfter=8,
                    spaceBefore=0,
                    textColor=CORPORATE_DARK_BLUE,
                    fontName='Helvetica-Bold'
                ))
                
                # BLOQUE INDIVISIBLE: PageBreak + Título + Tabla
                # KeepTogether garantiza que NO se separe
                story.append(PageBreak())
                story.append(KeepTogether([
                    Spacer(1, 0.1*inch),
                    folio_title,
                    folio_table,
                ]))
                
            except Exception as e:
                logger.error(f"Error processing folio photo: {e}")
                # NO mostrar error en el PDF - simplemente omitir documento con problemas
        
        # ==========================================
        # TODAS LAS FOTOS - Agrupadas por categoría
        # Grid 2x2, categorías pueden compartir página
        # ==========================================
        if regular_photos:
            # Agrupar fotos por categoría
            photos_by_category = {}
            for foto in regular_photos:
                cat = foto.get('categoria', foto.get('tipo', 'carga')).lower()
                if cat not in photos_by_category:
                    photos_by_category[cat] = []
                photos_by_category[cat].append(foto)
            
            # Orden de categorías preferido
            CATEGORY_ORDER = ['transporte', 'placas', 'temperatura', 'sello', 'licencia', 'carga', 'descarga']
            
            # Section titles por categoría
            SECTION_TITLES = {
                'transporte': 'EVIDENCIA DE TRANSPORTE',
                'placas': 'EVIDENCIA DE PLACAS',
                'temperatura': 'EVIDENCIA DE TEMPERATURA',
                'sello': 'EVIDENCIA DE SELLO',
                'licencia': 'EVIDENCIA DE LICENCIA',
                'carga': 'EVIDENCIA DE CARGA',
                'descarga': 'EVIDENCIA DE DESCARGA',
            }
            
            # Iniciar sección de evidencias
            story.append(PageBreak())
            story.append(Spacer(1, 0.15*inch))
            
            # Procesar cada categoría
            for cat in CATEGORY_ORDER:
                if cat not in photos_by_category:
                    continue
                    
                cat_photos = photos_by_category[cat]
                section_title = SECTION_TITLES.get(cat, f'EVIDENCIA DE {cat.upper()}')
                cat_color = CATEGORY_COLORS.get(cat, '#718096')
                
                # Título de categoría
                title_text = f'<font color="{cat_color}">●</font> {section_title}'
                story.append(Paragraph(title_text, section_title_style))
                story.append(Spacer(1, 0.08*inch))
                
                # Build grid for ALL photos of this category at once
                grid_rows = []
                for row_idx in range(0, len(cat_photos), 2):
                    row_photos = cat_photos[row_idx:row_idx+2]
                    row_cells = []
                    
                    for foto in row_photos:
                        try:
                            img_data = foto.get("imagen_base64", "")
                            
                            # VALIDACIÓN TEMPRANA: skip si no hay datos de imagen válidos
                            if not img_data or not isinstance(img_data, str) or len(img_data) < 100:
                                continue
                            
                            if img_data.startswith("data:"):
                                img_data = img_data.split(",")[1]
                            
                            img_bytes = base64.b64decode(img_data)
                            # OPTIMIZACIÓN: Comprimir imagen para PDF
                            img_bytes = optimize_image_for_pdf(img_bytes, max_width=800, quality=60)
                            img_buffer = BytesIO(img_bytes)
                            
                            # Size for 2x2 grid
                            img = RLImage(img_buffer, width=3.2*inch, height=2.4*inch)
                            
                            # Photo info
                            fecha_foto = foto.get("fecha", datetime.utcnow())
                            if isinstance(fecha_foto, str):
                                fecha_foto = datetime.fromisoformat(fecha_foto.replace('Z', '+00:00'))
                            fecha_foto_mexico = to_mexico_time(fecha_foto)
                            
                            fecha_str = fecha_foto_mexico.strftime("%d/%m/%Y %H:%M")
                            ubicacion = foto.get("direccion", "")
                            if len(ubicacion) > 35:
                                ubicacion = ubicacion[:35] + "..."
                            
                            # Date + location
                            info_text = f'<font size="8"><b>{fecha_str}</b></font>'
                            if ubicacion:
                                info_text += f'<br/><font size="7" color="#718096">{ubicacion}</font>'
                            
                            # Cell with photo + info
                            cell_data = [
                                [img],
                                [Paragraph(info_text, photo_info_style)]
                            ]
                            cell_table = Table(cell_data, colWidths=[3.3*inch])
                            cell_table.setStyle(TableStyle([
                                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                                ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BORDER),
                                ('TOPPADDING', (0, 0), (0, 0), 5),
                                ('BOTTOMPADDING', (0, 0), (0, 0), 3),
                                ('TOPPADDING', (0, 1), (0, 1), 5),
                                ('BOTTOMPADDING', (0, 1), (0, 1), 5),
                                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                            ]))
                            row_cells.append(cell_table)
                            
                        except Exception as e:
                            logger.error(f"Error processing photo in PDF: {e}")
                            # NO mostrar error - simplemente omitir la foto con problemas
                            continue
                    
                    # Fill empty cells if odd number
                    while len(row_cells) < 2:
                        row_cells.append(Paragraph("", normal_style))
                    
                    grid_rows.append(row_cells)
                
                # Create grid table - ReportLab will auto-split across pages if needed
                if grid_rows:
                    grid_table = Table(grid_rows, colWidths=[3.4*inch, 3.4*inch])
                    grid_table.setStyle(TableStyle([
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 3),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                        ('TOPPADDING', (0, 0), (-1, -1), 5),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
                    ]))
                    story.append(grid_table)
                
                # Small space between categories
                story.append(Spacer(1, 0.12*inch))
    
    # ==========================================
    # FOTOS POR ETAPA (DINÁMICO: soporta múltiples cargas/descargas)
    # Dentro de cada etapa:
    #   1. Documentación: 1 imagen por página
    #   2. Resto de categorías: combinadas en grid 2x2
    # ==========================================
    fotos_etapas = servicio.get("fotos_etapas", {"espera": {}, "carga": {}, "entrega": {}})
    
    # Generar lista de etapas dinámicamente basado en orígenes y destinos
    origenes = servicio.get("origenes", [])
    destinos = servicio.get("destinos", [])
    num_origenes = len(origenes) if origenes else 1
    num_destinos = len(destinos) if destinos else 1
    
    etapas_pdf = ["espera"]
    etapa_config_dinamico = {}
    
    # Configurar etapa de espera - agregar número si hay múltiples cargas
    if num_origenes > 1:
        etapa_config_dinamico["espera"] = {
            "titulo": "ESPERA DE CARGA 1",
            "descripcion": "Evidencia durante la espera en el primer origen",
            "color": "#ecc94b"
        }
    else:
        etapa_config_dinamico["espera"] = {
            "titulo": "ESPERA DE CARGA",
            "descripcion": "Evidencia durante la espera",
            "color": "#ecc94b"
        }
    
    # Agregar etapas de carga
    if num_origenes == 1:
        etapas_pdf.append("carga")
        origen_nombre = origenes[0] if origenes else "Origen"
        etapa_config_dinamico["carga"] = {
            "titulo": f"CARGA - {origen_nombre[:30]}",
            "descripcion": "Evidencia del proceso de carga",
            "color": "#3182ce"
        }
    else:
        for i in range(num_origenes):
            # Agregar etapa de llegada para cargas 2+
            if i > 0:
                llegada_key = f"llegada_carga_{i+1}"
                etapas_pdf.append(llegada_key)
                origen_nombre = origenes[i] if i < len(origenes) else f"Origen {i+1}"
                etapa_config_dinamico[llegada_key] = {
                    "titulo": f"LLEGADA CARGA {i+1}",
                    "descripcion": f"Evidencia de llegada a {origen_nombre[:20]}",
                    "color": "#ed8936"
                }
            
            # Agregar etapa de carga
            key = f"carga_{i+1}"
            etapas_pdf.append(key)
            origen_nombre = origenes[i] if i < len(origenes) else f"Origen {i+1}"
            etapa_config_dinamico[key] = {
                "titulo": f"CARGA {i+1} - {origen_nombre[:25]}",
                "descripcion": f"Evidencia de carga en {origen_nombre[:20]}",
                "color": "#3182ce"
            }
    
    # Agregar etapas de descarga
    if num_destinos == 1:
        # Para 1 destino, usar 'entrega' o 'descarga_1' según lo que exista
        if "descarga_1" in fotos_etapas:
            etapas_pdf.append("descarga_1")
            destino_nombre = destinos[0] if destinos else "Destino"
            etapa_config_dinamico["descarga_1"] = {
                "titulo": f"ENTREGA - {destino_nombre[:30]}",
                "descripcion": "Evidencia de la entrega",
                "color": "#38a169"
            }
        else:
            etapas_pdf.append("entrega")
            destino_nombre = destinos[0] if destinos else "Destino"
            etapa_config_dinamico["entrega"] = {
                "titulo": f"ENTREGA - {destino_nombre[:30]}",
                "descripcion": "Evidencia de la entrega",
                "color": "#38a169"
            }
    else:
        # Múltiples destinos: verificar si el primero es 'entrega' (legacy) o 'descarga_1'
        for i in range(num_destinos):
            destino_nombre = destinos[i] if i < len(destinos) else f"Destino {i+1}"
            
            # Para el primer destino, verificar si usa formato legacy
            if i == 0 and "entrega" in fotos_etapas and "descarga_1" not in fotos_etapas:
                key = "entrega"
                titulo = f"DESCARGA 1 - {destino_nombre[:25]}"
            else:
                key = f"descarga_{i+1}"
                titulo = f"DESCARGA {i+1} - {destino_nombre[:25]}"
            
            etapas_pdf.append(key)
            etapa_config_dinamico[key] = {
                "titulo": titulo,
                "descripcion": f"Evidencia de descarga en {destino_nombre[:20]}",
                "color": "#38a169"
            }
    
    ETAPA_CONFIG = etapa_config_dinamico
    
    # Categorías en orden (documentación se procesa aparte)
    CATEGORIAS_GRID = ['evidencia', 'transporte', 'placas', 'temperatura', 'sello', 'licencia']
    CATEGORIA_DOCUMENTACION = 'documentacion'
    
    CATEGORIA_LABELS = {
        'evidencia': 'EVIDENCIA',
        'transporte': 'TRANSPORTE',
        'placas': 'PLACAS',
        'temperatura': 'TEMPERATURA',
        'sello': 'SELLO',
        'licencia': 'LICENCIA',
        'documentacion': 'DOCUMENTACIÓN'
    }
    
    CATEGORIA_COLORS = {
        'evidencia': '#3182ce',
        'transporte': '#48bb78',
        'placas': '#9f7aea',
        'temperatura': '#ed8936',
        'sello': '#f56565',
        'licencia': '#718096',
        'documentacion': '#dd6b20'
    }
    
    # Función para contar fotos en todas las etapas (dinámico)
    def contar_fotos_etapas():
        total = 0
        for etapa in etapas_pdf:
            etapa_data = fotos_etapas.get(etapa, {})
            if isinstance(etapa_data, dict):
                for cat, fotos_cat in etapa_data.items():
                    if isinstance(fotos_cat, list):
                        # Solo contar fotos activas
                        total += len([f for f in fotos_cat if f.get("active", True)])
            elif isinstance(etapa_data, list):
                total += len([f for f in etapa_data if f.get("active", True)])
        return total
    
    def filtrar_fotos_activas(fotos_list):
        """Retorna solo fotos con active=True y imagen_base64 válido"""
        if not isinstance(fotos_list, list):
            return []
        fotos_validas = []
        for f in fotos_list:
            if not isinstance(f, dict):
                continue
            if not f.get("active", True):
                continue
            # Validar que tenga imagen_base64 con contenido real
            img_data = f.get("imagen_base64")
            if not img_data or not isinstance(img_data, str) or len(img_data) < 100:
                continue
            fotos_validas.append(f)
        return fotos_validas
    
    total_fotos_etapas = contar_fotos_etapas()
    logger.info(f"PDF: Total {total_fotos_etapas} fotos en todas las etapas")
    
    if total_fotos_etapas > 0:
        # Procesar cada etapa (dinámico)
        for etapa in etapas_pdf:
            etapa_data = fotos_etapas.get(etapa, {})
            config = ETAPA_CONFIG.get(etapa, {
                "titulo": etapa.upper(),
                "descripcion": f"Evidencia de {etapa}",
                "color": "#718096"
            })
            
            # Convertir formato antiguo (lista) a nuevo (dict de categorías)
            if isinstance(etapa_data, list):
                etapa_data = {"evidencia": etapa_data}
            
            if not isinstance(etapa_data, dict):
                continue
            
            # Verificar si hay fotos ACTIVAS en esta etapa
            fotos_activas_en_etapa = sum(
                len(filtrar_fotos_activas(v)) if isinstance(v, list) else 0 
                for v in etapa_data.values()
            )
            if fotos_activas_en_etapa == 0:
                continue
            
            logger.info(f"PDF: Etapa {etapa} tiene {fotos_activas_en_etapa} fotos activas")
            
            # Recolectar documentos y fotos grid por separado (SOLO ACTIVAS)
            fotos_docs = []
            fotos_docs_raw = filtrar_fotos_activas(etapa_data.get(CATEGORIA_DOCUMENTACION, []))
            for foto_doc in fotos_docs_raw:
                if foto_doc.get("imagen_base64"):
                    fotos_docs.append(foto_doc)
            
            fotos_grid = []
            for cat in CATEGORIAS_GRID:
                fotos_cat = filtrar_fotos_activas(etapa_data.get(cat, []))
                for foto in fotos_cat:
                    if foto.get("imagen_base64"):
                        foto['_categoria_label'] = CATEGORIA_LABELS.get(cat, cat.upper())
                        foto['_categoria_color'] = CATEGORIA_COLORS.get(cat, '#718096')
                        fotos_grid.append(foto)
            
            # ===== PRIMERA PÁGINA: TÍTULO + PRIMERA IMAGEN =====
            story.append(PageBreak())
            story.append(Spacer(1, 0.2*inch))
            
            # Título compacto de etapa
            etapa_title_table = Table([[
                Paragraph(f'''<font size="18" color="{config["color"]}"><b>{config["titulo"]}</b></font><br/>
<font size="9" color="#718096">{config["descripcion"]} · <b>{fotos_activas_en_etapa}</b> foto{"s" if fotos_activas_en_etapa > 1 else ""}</font>''', 
                ParagraphStyle('EtapaTitle', alignment=1, leading=22))
            ]], colWidths=[6*inch])
            etapa_title_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f7fafc')),
                ('BOX', (0, 0), (-1, -1), 1.5, colors.HexColor(config["color"])),
                ('TOPPADDING', (0, 0), (-1, -1), 12),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 12),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ]))
            
            centered_title = Table([[etapa_title_table]], colWidths=[7*inch])
            centered_title.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_title)
            story.append(Spacer(1, 0.15*inch))
            
            # Determinar qué imagen mostrar en la primera página
            first_image_shown = False
            first_image_type = None  # 'doc' o 'grid'
            
            # Prioridad 1: Primer documento
            if fotos_docs:
                first_doc = fotos_docs[0]
                try:
                    img_data = first_doc.get("imagen_base64", "")
                    if img_data.startswith("data:"):
                        img_data = img_data.split(",")[1]
                    
                    img_bytes = base64.b64decode(img_data)
                    # OPTIMIZACIÓN: Comprimir imagen para PDF
                    img_bytes = optimize_image_for_pdf(img_bytes, max_width=800, quality=60)
                    pil_img = PILImage.open(BytesIO(img_bytes))
                    img_width, img_height = pil_img.size
                    
                    # Tamaño para primera página (más pequeño para dejar espacio al título)
                    FIRST_DOC_HEIGHT = 5.8 * inch
                    FIRST_DOC_WIDTH = 5.5 * inch
                    
                    width_ratio = FIRST_DOC_WIDTH / img_width
                    height_ratio = FIRST_DOC_HEIGHT / img_height
                    scale = min(width_ratio, height_ratio)
                    
                    final_width = img_width * scale
                    final_height = img_height * scale
                    
                    if pil_img.mode != 'RGB':
                        pil_img = pil_img.convert('RGB')
                    opt_buffer = BytesIO()
                    pil_img.save(opt_buffer, format='JPEG', quality=85, optimize=True)
                    opt_buffer.seek(0)
                    
                    img = RLImage(opt_buffer, width=final_width, height=final_height)
                    
                    # Label de documento
                    story.append(Paragraph(
                        f'<font color="#dd6b20" size="11"><b>DOCUMENTACIÓN</b></font> '
                        f'<font color="#718096" size="9">(1 de {len(fotos_docs)})</font>',
                        ParagraphStyle('DocLabel', alignment=1, spaceAfter=8)
                    ))
                    
                    # Imagen con borde
                    img_table = Table([[img]], colWidths=[7*inch])
                    img_table.setStyle(TableStyle([
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('BOX', (0, 0), (-1, -1), 1, CORPORATE_BORDER),
                        ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ]))
                    story.append(img_table)
                    
                    # Metadata
                    fecha_doc = first_doc.get("fecha", datetime.utcnow())
                    if isinstance(fecha_doc, str):
                        try:
                            fecha_doc = datetime.fromisoformat(fecha_doc.replace('Z', '+00:00'))
                        except:
                            fecha_doc = datetime.utcnow()
                    fecha_doc_mexico = to_mexico_time(fecha_doc)
                    fecha_str = fecha_doc_mexico.strftime("%d/%m/%Y %H:%M")
                    story.append(Paragraph(
                        f'<font size="8" color="#718096">{fecha_str}</font>',
                        ParagraphStyle('DocMeta', alignment=1)
                    ))
                    
                    first_image_shown = True
                    first_image_type = 'doc'
                    fotos_docs = fotos_docs[1:]  # Remover primer doc del flujo (ya mostrado)
                    
                except Exception as e:
                    logger.error(f"Error showing first doc in {etapa}: {e}")
            
            # Prioridad 2: Si no hay docs, mostrar primeras 4 fotos del grid
            if not first_image_shown and fotos_grid:
                # Mostrar hasta 4 fotos en la primera página
                first_grid_fotos = fotos_grid[:4]
                fotos_grid = fotos_grid[4:]  # Remover del flujo
                
                GRID_COLS = 2
                GRID_IMG_WIDTH = 2.8 * inch
                GRID_IMG_HEIGHT = 2.0 * inch
                
                grid_rows = []
                for row_idx in range(0, len(first_grid_fotos), GRID_COLS):
                    row_fotos = first_grid_fotos[row_idx:row_idx + GRID_COLS]
                    row_cells = []
                    
                    for foto in row_fotos:
                        try:
                            img_data = foto.get("imagen_base64", "")
                            
                            # VALIDACIÓN TEMPRANA: skip si no hay datos de imagen válidos
                            if not img_data or not isinstance(img_data, str) or len(img_data) < 100:
                                continue
                            
                            if img_data.startswith("data:"):
                                img_data = img_data.split(",")[1]
                            
                            img_bytes = base64.b64decode(img_data)
                            # OPTIMIZACIÓN: Comprimir imagen para PDF (800px, 60% calidad)
                            img_bytes = optimize_image_for_pdf(img_bytes, max_width=800, quality=60)
                            
                            pil_img = PILImage.open(BytesIO(img_bytes))
                            # Ya está convertido a RGB por optimize_image_for_pdf
                            
                            opt_buffer = BytesIO(img_bytes)  # Ya optimizado
                            
                            img = RLImage(opt_buffer, width=GRID_IMG_WIDTH, height=GRID_IMG_HEIGHT)
                            
                            cat_label = foto.get('_categoria_label', 'EVIDENCIA')
                            cat_color = foto.get('_categoria_color', '#718096')
                            
                            fecha_foto = foto.get("fecha", datetime.utcnow())
                            if isinstance(fecha_foto, str):
                                try:
                                    fecha_foto = datetime.fromisoformat(fecha_foto.replace('Z', '+00:00'))
                                except:
                                    fecha_foto = datetime.utcnow()
                            fecha_foto_mexico = to_mexico_time(fecha_foto)
                            fecha_str = fecha_foto_mexico.strftime("%d/%m/%Y %H:%M")
                            
                            meta_text = f'<font size="7" color="{cat_color}"><b>{cat_label}</b></font><br/>'
                            meta_text += f'<font size="7" color="#4a5568">{fecha_str}</font>'
                            
                            cell_content = [
                                [img],
                                [Paragraph(meta_text, ParagraphStyle('Meta', alignment=1, leading=9))]
                            ]
                            cell_table = Table(cell_content, colWidths=[GRID_IMG_WIDTH + 0.05*inch])
                            cell_table.setStyle(TableStyle([
                                ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                                ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BORDER),
                                ('TOPPADDING', (0, 0), (-1, -1), 3),
                                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                            ]))
                            row_cells.append(cell_table)
                            
                        except Exception as e:
                            logger.error(f"Error in first grid photo: {e}")
                            row_cells.append('')
                    
                    while len(row_cells) < GRID_COLS:
                        row_cells.append('')
                    grid_rows.append(row_cells)
                
                if grid_rows:
                    grid_table = Table(grid_rows, colWidths=[3.1*inch, 3.1*inch])
                    grid_table.setStyle(TableStyle([
                        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                        ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                        ('LEFTPADDING', (0, 0), (-1, -1), 3),
                        ('RIGHTPADDING', (0, 0), (-1, -1), 3),
                        ('TOPPADDING', (0, 0), (-1, -1), 4),
                        ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                    ]))
                    story.append(grid_table)
                    first_image_shown = True
                    first_image_type = 'grid'
            
            logger.info(f"PDF: Etapa {etapa} - Primera página con: {first_image_type or 'solo título'}")
            
            # ===== DOCUMENTOS RESTANTES (1 por página) =====
            if fotos_docs:
                MAX_DOC_HEIGHT = 7.0 * inch
                MAX_DOC_WIDTH = 6.5 * inch
                
                # Calcular total original de docs (incluyendo el ya mostrado)
                total_docs_original = len(fotos_docs) + (1 if first_image_type == 'doc' else 0)
                logger.info(f"PDF: Etapa {etapa} - {len(fotos_docs)} documentos restantes")
                
                for doc_idx, doc_foto in enumerate(fotos_docs):
                    try:
                        if not isinstance(doc_foto, dict) or not doc_foto.get("imagen_base64"):
                            continue
                        
                        story.append(PageBreak())
                        story.append(Spacer(1, 0.1*inch))
                        
                        # Índice correcto (2, 3, ... si el primero ya se mostró)
                        display_idx = doc_idx + 2 if first_image_type == 'doc' else doc_idx + 1
                        
                        # Header del documento
                        story.append(Paragraph(
                            f'<font color="{config["color"]}" size="12"><b>{config["titulo"]}</b></font> · '
                            f'<font color="#dd6b20" size="12"><b>DOCUMENTACIÓN</b></font> '
                            f'<font color="#718096" size="10">({display_idx} de {total_docs_original})</font>',
                            ParagraphStyle('DocHeader', alignment=1, spaceAfter=12)
                        ))
                        
                        img_data = doc_foto.get("imagen_base64", "")
                        if img_data.startswith("data:"):
                            img_data = img_data.split(",")[1]
                        
                        img_bytes = base64.b64decode(img_data)
                        # OPTIMIZACIÓN: Comprimir imagen para PDF (800px, 60% calidad)
                        img_bytes = optimize_image_for_pdf(img_bytes, max_width=800, quality=60)
                        
                        # Obtener dimensiones reales de imagen optimizada
                        pil_img = PILImage.open(BytesIO(img_bytes))
                        img_width, img_height = pil_img.size
                        
                        # Calcular escala para que quepa en la página (contain)
                        width_ratio = MAX_DOC_WIDTH / img_width
                        height_ratio = MAX_DOC_HEIGHT / img_height
                        scale = min(width_ratio, height_ratio)
                        
                        final_width = img_width * scale
                        final_height = img_height * scale
                        
                        # Ya está optimizada
                        opt_buffer = BytesIO(img_bytes)
                        
                        img = RLImage(opt_buffer, width=final_width, height=final_height)
                        
                        # Centrar imagen con borde
                        img_table = Table([[img]], colWidths=[7*inch])
                        img_table.setStyle(TableStyle([
                            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
                            ('BOX', (0, 0), (-1, -1), 1, CORPORATE_BORDER),
                            ('TOPPADDING', (0, 0), (-1, -1), 6),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                        ]))
                        story.append(img_table)
                        
                        # Metadata
                        fecha_doc = doc_foto.get("fecha", datetime.utcnow())
                        if isinstance(fecha_doc, str):
                            try:
                                fecha_doc = datetime.fromisoformat(fecha_doc.replace('Z', '+00:00'))
                            except:
                                fecha_doc = datetime.utcnow()
                        fecha_doc_mexico = to_mexico_time(fecha_doc)
                        fecha_str = fecha_doc_mexico.strftime("%d/%m/%Y %H:%M")
                        
                        story.append(Spacer(1, 0.08*inch))
                        story.append(Paragraph(
                            f'<font size="9" color="#718096">Capturado: {fecha_str}</font>',
                            ParagraphStyle('DocMeta', alignment=1)
                        ))
                        
                    except Exception as e:
                        logger.error(f"Error processing document in {etapa}: {e}")
            
            # ===== RESTO DE CATEGORÍAS (combinadas en grid 2x2) =====
            # Nota: fotos_grid ya fue procesado arriba y reducido (se removieron las primeras 4 si se mostraron)
            # Solo procesar si quedan fotos en fotos_grid
            
            if fotos_grid:
                logger.info(f"PDF: Etapa {etapa} - Procesando {len(fotos_grid)} fotos restantes en grid")
                
                # Configuración del grid
                GRID_COLS = 2
                PHOTOS_PER_PAGE = 4  # 2x2
                GRID_IMG_WIDTH = 3.0 * inch
                GRID_IMG_HEIGHT = 2.1 * inch
                
                # Procesar fotos en grupos de 4
                for page_idx in range(0, len(fotos_grid), PHOTOS_PER_PAGE):
                    # Nueva página SOLO si:
                    # - No es la primera página de fotos (page_idx > 0), O
                    # - Ya se mostró contenido en la primera página (first_image_shown es True)
                    if page_idx > 0 or first_image_shown:
                        story.append(PageBreak())
                        story.append(Spacer(1, 0.15*inch))
                        
                        # Header de continuación
                        story.append(Paragraph(
                            f'<font color="{config["color"]}" size="11"><b>{config["titulo"]}</b></font> - '
                            f'<font color="#718096" size="10">Fotos {page_idx + 1} a {min(page_idx + PHOTOS_PER_PAGE, len(fotos_grid))} de {len(fotos_grid)}</font>',
                            ParagraphStyle('PageHeader', alignment=1, spaceAfter=12)
                        ))
                    else:
                        # Primera página sin contenido previo - agregar header de evidencia
                        story.append(Spacer(1, 0.15*inch))
                        story.append(Paragraph(
                            f'<font color="{config["color"]}" size="12"><b>{config["titulo"]}</b></font> · '
                            f'<font color="#4a5568" size="11">Evidencia Fotográfica</font>',
                            ParagraphStyle('GridHeader', alignment=1, spaceAfter=12)
                        ))
                    
                    page_fotos = fotos_grid[page_idx:page_idx + PHOTOS_PER_PAGE]
                    grid_rows = []
                    
                    for row_idx in range(0, len(page_fotos), GRID_COLS):
                        row_fotos = page_fotos[row_idx:row_idx + GRID_COLS]
                        row_cells = []
                        
                        for foto in row_fotos:
                            try:
                                img_data = foto.get("imagen_base64", "")
                                
                                # VALIDACIÓN TEMPRANA: skip si no hay datos de imagen válidos
                                if not img_data or not isinstance(img_data, str) or len(img_data) < 100:
                                    logger.warning(f"Foto sin imagen válida en {etapa}, saltando...")
                                    continue
                                
                                if img_data.startswith("data:"):
                                    img_data = img_data.split(",")[1]
                                
                                img_bytes = base64.b64decode(img_data)
                                # OPTIMIZACIÓN: Comprimir imagen para PDF (800px, 60% calidad)
                                img_bytes = optimize_image_for_pdf(img_bytes, max_width=800, quality=60)
                                
                                opt_buffer = BytesIO(img_bytes)
                                
                                img = RLImage(opt_buffer, width=GRID_IMG_WIDTH, height=GRID_IMG_HEIGHT)
                                
                                # Metadata con etiqueta de categoría
                                fecha_foto = foto.get("fecha", datetime.utcnow())
                                if isinstance(fecha_foto, str):
                                    try:
                                        fecha_foto = datetime.fromisoformat(fecha_foto.replace('Z', '+00:00'))
                                    except:
                                        fecha_foto = datetime.utcnow()
                                fecha_foto_mexico = to_mexico_time(fecha_foto)
                                fecha_str = fecha_foto_mexico.strftime("%d/%m/%Y %H:%M")
                                
                                cat_label = foto.get('_categoria_label', 'EVIDENCIA')
                                cat_color = foto.get('_categoria_color', '#718096')
                                
                                ubicacion = foto.get("direccion", "")
                                if len(ubicacion) > 28:
                                    ubicacion = ubicacion[:28] + "..."
                                
                                # Celda con imagen + categoría + fecha
                                meta_text = f'<font size="8" color="{cat_color}"><b>{cat_label}</b></font><br/>'
                                meta_text += f'<font size="8" color="#4a5568">{fecha_str}</font>'
                                if ubicacion:
                                    meta_text += f'<br/><font size="7" color="#a0aec0">{ubicacion}</font>'
                                
                                cell_content = [
                                    [img],
                                    [Paragraph(meta_text, ParagraphStyle('Meta', alignment=1, leading=10))]
                                ]
                                cell_table = Table(cell_content, colWidths=[GRID_IMG_WIDTH + 0.1*inch])
                                cell_table.setStyle(TableStyle([
                                    ('BACKGROUND', (0, 0), (-1, -1), colors.white),
                                    ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BORDER),
                                    ('TOPPADDING', (0, 0), (-1, -1), 4),
                                    ('BOTTOMPADDING', (0, 0), (-1, -1), 4),
                                    ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                                ]))
                                row_cells.append(cell_table)
                                
                            except Exception as e:
                                logger.error(f"Error processing photo in {etapa}: {e}")
                                # NO mostrar error - simplemente omitir la foto
                                continue
                        
                        # Completar fila si está incompleta
                        while len(row_cells) < GRID_COLS:
                            row_cells.append('')
                        
                        grid_rows.append(row_cells)
                    
                    # Crear tabla grid
                    if grid_rows:
                        grid_table = Table(grid_rows, colWidths=[3.3*inch, 3.3*inch])
                        grid_table.setStyle(TableStyle([
                            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
                            ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                            ('LEFTPADDING', (0, 0), (-1, -1), 5),
                            ('RIGHTPADDING', (0, 0), (-1, -1), 5),
                            ('TOPPADDING', (0, 0), (-1, -1), 8),
                            ('BOTTOMPADDING', (0, 0), (-1, -1), 8),
                        ]))
                        story.append(grid_table)
    
    else:
        # No hay fotos en ninguna etapa
        story.append(PageBreak())
        story.append(Spacer(1, 0.5*inch))
        story.append(Paragraph("EVIDENCIA FOTOGRÁFICA", section_title_style))
        no_photos_data = [[Paragraph('<font color="#718096" size="12">No hay fotos registradas en este servicio.</font>', styles['Normal'])]]
        no_photos_table = Table(no_photos_data, colWidths=[7*inch])
        no_photos_table.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), CORPORATE_BG),
            ('BOX', (0, 0), (-1, -1), 1, CORPORATE_BORDER),
            ('TOPPADDING', (0, 0), (-1, -1), 30),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 30),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        story.append(no_photos_table)
    
    # Build PDF
    doc.build(story)
    buffer.seek(0)
    
    # Generar nombre de archivo limpio usando factura y referencia
    numero_factura = servicio.get("numero_factura", "")
    referencia_cliente = servicio.get("referencia_cliente", "")
    filename = generar_nombre_pdf(numero_factura, referencia_cliente)
    
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# ============ CATALOG ENDPOINTS ============

# Helper function to convert operador document to response
def operador_to_response(op: dict) -> dict:
    return {
        "id": str(op["_id"]),
        "nombre": op["nombre"],
        "telefono": op.get("telefono", ""),
        "licencia": op.get("licencia", ""),
        "vigencia_licencia": op.get("vigencia_licencia", ""),
        "rfc": op.get("rfc", ""),
        "id_operador": op.get("id_operador", ""),
        "foto_url": op.get("foto_url", None),
        "foto_base64": op.get("foto_base64", None),  # Foto comprimida en base64
        "status": op.get("status", "activo"),
        "fecha_creacion": op.get("fecha_creacion"),
        "fecha_actualizacion": op.get("fecha_actualizacion"),
    }

@api_router.get("/catalogo/operadores")
async def get_operadores(incluir_inactivos: bool = False):
    """Get operators from catalog (default: only active)"""
    query = {} if incluir_inactivos else {"$or": [{"status": "activo"}, {"status": {"$exists": False}}]}
    operadores = await db.operadores.find(query).to_list(100)
    return [operador_to_response(op) for op in operadores]

# ============ ADMIN: CRUD OPERADORES ============

@api_router.get("/admin/operadores")
async def admin_get_operadores(current_user: dict = Depends(get_current_user)):
    """Get ALL operators (including inactive) - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede ver todos los operadores")
    
    operadores = await db.operadores.find().sort("nombre", 1).to_list(200)
    return [operador_to_response(op) for op in operadores]

@api_router.post("/admin/operadores")
async def admin_create_operador(operador: OperadorCreate, current_user: dict = Depends(get_current_user)):
    """Create a new operator - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede crear operadores")
    
    # Verificar que id_operador no exista
    existing = await db.operadores.find_one({"id_operador": operador.id_operador})
    if existing:
        raise HTTPException(status_code=400, detail=f"Ya existe un operador con ID {operador.id_operador}")
    
    operador_doc = {
        "nombre": operador.nombre,
        "telefono": operador.telefono,
        "licencia": operador.licencia,
        "vigencia_licencia": operador.vigencia_licencia or "",
        "rfc": operador.rfc or "",
        "id_operador": operador.id_operador,
        "foto_url": operador.foto_url,
        "foto_base64": operador.foto_base64,  # Foto comprimida en base64
        "status": "activo",
        "fecha_creacion": datetime.utcnow(),
        "fecha_actualizacion": datetime.utcnow(),
    }
    
    result = await db.operadores.insert_one(operador_doc)
    operador_doc["_id"] = result.inserted_id
    
    return operador_to_response(operador_doc)

@api_router.put("/admin/operadores/{operador_id}")
async def admin_update_operador(operador_id: str, operador: OperadorUpdate, current_user: dict = Depends(get_current_user)):
    """Update an operator - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede editar operadores")
    
    # Construir update fields
    update_fields = {"fecha_actualizacion": datetime.utcnow()}
    
    if operador.nombre is not None:
        update_fields["nombre"] = operador.nombre
    if operador.telefono is not None:
        update_fields["telefono"] = operador.telefono
    if operador.licencia is not None:
        update_fields["licencia"] = operador.licencia
    if operador.vigencia_licencia is not None:
        update_fields["vigencia_licencia"] = operador.vigencia_licencia
    if operador.rfc is not None:
        update_fields["rfc"] = operador.rfc
    if operador.id_operador is not None:
        # Verificar que el nuevo id_operador no exista en otro operador
        existing = await db.operadores.find_one({
            "id_operador": operador.id_operador,
            "_id": {"$ne": ObjectId(operador_id)}
        })
        if existing:
            raise HTTPException(status_code=400, detail=f"Ya existe otro operador con ID {operador.id_operador}")
        update_fields["id_operador"] = operador.id_operador
    if operador.foto_url is not None:
        update_fields["foto_url"] = operador.foto_url
    if operador.foto_base64 is not None:
        update_fields["foto_base64"] = operador.foto_base64
    if operador.status is not None:
        update_fields["status"] = operador.status
    
    result = await db.operadores.update_one(
        {"_id": ObjectId(operador_id)},
        {"$set": update_fields}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Operador no encontrado")
    
    updated = await db.operadores.find_one({"_id": ObjectId(operador_id)})
    return operador_to_response(updated)

@api_router.put("/admin/operadores/{operador_id}/status")
async def admin_toggle_operador_status(operador_id: str, current_user: dict = Depends(get_current_user)):
    """Toggle operator status (activo/inactivo) - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede cambiar el status")
    
    operador = await db.operadores.find_one({"_id": ObjectId(operador_id)})
    if not operador:
        raise HTTPException(status_code=404, detail="Operador no encontrado")
    
    current_status = operador.get("status", "activo")
    new_status = "inactivo" if current_status == "activo" else "activo"
    
    await db.operadores.update_one(
        {"_id": ObjectId(operador_id)},
        {"$set": {"status": new_status, "fecha_actualizacion": datetime.utcnow()}}
    )
    
    return {"message": f"Operador {'desactivado' if new_status == 'inactivo' else 'activado'}", "status": new_status}

@api_router.delete("/admin/operadores/{operador_id}")
async def delete_operador(operador_id: str, current_user: dict = Depends(get_current_user)):
    """Eliminar un operador permanentemente"""
    try:
        oid = ObjectId(operador_id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID de operador inválido")
    
    result = await db.operadores.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Operador no encontrado")
    
    return {"message": "Operador eliminado"}

@api_router.get("/catalogo/camiones")
async def get_camiones(incluir_inactivos: bool = False):
    """Get trucks from catalog. By default only returns active trucks."""
    query = {} if incluir_inactivos else {"status": {"$ne": "inactivo"}}
    camiones = await db.camiones.find(query).to_list(100)
    return [
        {
            "id": str(c["_id"]),
            "nombre": c.get("nombre", ""),
            "numero": c.get("numero", 0),
            "placa": c.get("placa", ""),
            "tipo_caja": c.get("tipo_caja", ""),
            "status": c.get("status", "activo")
        }
        for c in camiones
    ]


# ============ CATÁLOGO DE CAJAS ============

@api_router.get("/catalogo/cajas")
async def get_cajas(incluir_inactivos: bool = False):
    """Get boxes from catalog. By default only returns active boxes."""
    query = {} if incluir_inactivos else {"status": {"$ne": "inactivo"}}
    cajas = await db.cajas.find(query).to_list(100)
    return [
        {
            "id": str(c["_id"]),
            "tipo_caja": c.get("tipo_caja", "THERMO"),
            "numero_entidad": c.get("numero_entidad", ""),
            "placa": c.get("placa", ""),
            "status": c.get("status", "activo")
        }
        for c in cajas
    ]


@api_router.get("/admin/cajas")
async def admin_get_cajas(current_user: dict = Depends(get_current_user)):
    """Get all boxes including inactive ones - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede ver todas las cajas")
    
    cajas = await db.cajas.find().to_list(100)
    return [
        {
            "id": str(c["_id"]),
            "tipo_caja": c.get("tipo_caja", "THERMO"),
            "numero_entidad": c.get("numero_entidad", ""),
            "placa": c.get("placa", ""),
            "status": c.get("status", "activo"),
            "fecha_creacion": c.get("fecha_creacion"),
            "fecha_actualizacion": c.get("fecha_actualizacion")
        }
        for c in cajas
    ]


@api_router.post("/admin/cajas")
async def admin_create_caja(caja: CajaCreate, current_user: dict = Depends(get_current_user)):
    """Create a new box - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede crear cajas")
    
    # Check if entidad already exists
    existing = await db.cajas.find_one({"numero_entidad": caja.numero_entidad})
    if existing:
        raise HTTPException(status_code=400, detail=f"Ya existe una caja con entidad {caja.numero_entidad}")
    
    caja_data = {
        "tipo_caja": caja.tipo_caja,
        "numero_entidad": caja.numero_entidad,
        "placa": caja.placa,
        "status": "activo",
        "fecha_creacion": datetime.utcnow(),
        "fecha_actualizacion": datetime.utcnow()
    }
    
    result = await db.cajas.insert_one(caja_data)
    
    return {
        "id": str(result.inserted_id),
        "tipo_caja": caja_data["tipo_caja"],
        "numero_entidad": caja_data["numero_entidad"],
        "placa": caja_data["placa"],
        "status": caja_data["status"],
        "fecha_creacion": caja_data["fecha_creacion"],
        "fecha_actualizacion": caja_data["fecha_actualizacion"]
    }


@api_router.put("/admin/cajas/{caja_id}")
async def admin_update_caja(caja_id: str, caja: CajaUpdate, current_user: dict = Depends(get_current_user)):
    """Update a box - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede editar cajas")
    
    update_data = {k: v for k, v in caja.dict().items() if v is not None}
    if not update_data:
        raise HTTPException(status_code=400, detail="No hay campos para actualizar")
    
    # Check if new entidad already exists (if changing)
    if "numero_entidad" in update_data:
        existing = await db.cajas.find_one({
            "numero_entidad": update_data["numero_entidad"],
            "_id": {"$ne": ObjectId(caja_id)}
        })
        if existing:
            raise HTTPException(status_code=400, detail=f"Ya existe otra caja con entidad {update_data['numero_entidad']}")
    
    update_data["fecha_actualizacion"] = datetime.utcnow()
    
    result = await db.cajas.update_one(
        {"_id": ObjectId(caja_id)},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Caja no encontrada")
    
    # Return updated caja
    updated = await db.cajas.find_one({"_id": ObjectId(caja_id)})
    return {
        "id": str(updated["_id"]),
        "tipo_caja": updated.get("tipo_caja"),
        "numero_entidad": updated.get("numero_entidad"),
        "placa": updated.get("placa"),
        "status": updated.get("status", "activo"),
        "fecha_actualizacion": updated.get("fecha_actualizacion")
    }


@api_router.put("/admin/cajas/{caja_id}/status")
async def admin_toggle_caja_status(caja_id: str, current_user: dict = Depends(get_current_user)):
    """Toggle box status (activo/inactivo) - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede cambiar el status")
    
    caja = await db.cajas.find_one({"_id": ObjectId(caja_id)})
    if not caja:
        raise HTTPException(status_code=404, detail="Caja no encontrada")
    
    current_status = caja.get("status", "activo")
    new_status = "inactivo" if current_status == "activo" else "activo"
    
    await db.cajas.update_one(
        {"_id": ObjectId(caja_id)},
        {"$set": {"status": new_status, "fecha_actualizacion": datetime.utcnow()}}
    )
    
    return {"message": f"Caja {'desactivada' if new_status == 'inactivo' else 'activada'}", "status": new_status}


@api_router.delete("/admin/cajas/{caja_id}")
async def delete_caja(caja_id: str, current_user: dict = Depends(get_current_user)):
    """Eliminar una caja permanentemente"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede eliminar cajas")
    
    try:
        oid = ObjectId(caja_id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID de caja inválido")
    
    result = await db.cajas.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Caja no encontrada")
    
    return {"message": "Caja eliminada permanentemente"}


# ============ ADMIN CAMIONES ============

@api_router.get("/admin/camiones")
async def admin_get_camiones(current_user: dict = Depends(get_current_user)):
    """Get all trucks including inactive ones - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede ver todos los camiones")
    
    camiones = await db.camiones.find().to_list(100)
    return [
        {
            "id": str(c["_id"]),
            "nombre": c.get("nombre", ""),
            "numero": c.get("numero", 0),
            "placa": c.get("placa", ""),
            "tipo_caja": c.get("tipo_caja", ""),
            "status": c.get("status", "activo"),
            "fecha_creacion": c.get("fecha_creacion"),
            "fecha_actualizacion": c.get("fecha_actualizacion")
        }
        for c in camiones
    ]


@api_router.post("/admin/camiones")
async def admin_create_camion(camion: CamionCreate, current_user: dict = Depends(get_current_user)):
    """Create a new truck - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede crear camiones")
    
    # Check if numero already exists
    existing = await db.camiones.find_one({"numero": camion.numero})
    if existing:
        raise HTTPException(status_code=400, detail=f"Ya existe un camión con número ECO {camion.numero}")
    
    camion_data = {
        "nombre": camion.nombre,
        "numero": camion.numero,
        "placa": camion.placa.upper(),
        "tipo_caja": camion.tipo_caja,
        "status": "activo",
        "fecha_creacion": datetime.utcnow(),
        "fecha_actualizacion": datetime.utcnow()
    }
    
    result = await db.camiones.insert_one(camion_data)
    
    return {
        "id": str(result.inserted_id),
        "nombre": camion_data["nombre"],
        "numero": camion_data["numero"],
        "placa": camion_data["placa"],
        "tipo_caja": camion_data["tipo_caja"],
        "status": camion_data["status"],
        "fecha_creacion": camion_data["fecha_creacion"]
    }


@api_router.put("/admin/camiones/{camion_id}")
async def admin_update_camion(camion_id: str, update_data: dict, current_user: dict = Depends(get_current_user)):
    """Update a truck - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede editar camiones")
    
    # If changing numero, check it's unique
    if "numero" in update_data:
        existing = await db.camiones.find_one({
            "numero": update_data["numero"],
            "_id": {"$ne": ObjectId(camion_id)}
        })
        if existing:
            raise HTTPException(status_code=400, detail=f"Ya existe un camión con número ECO {update_data['numero']}")
    
    # Uppercase placa if provided
    if "placa" in update_data:
        update_data["placa"] = update_data["placa"].upper()
    
    update_data["fecha_actualizacion"] = datetime.utcnow()
    
    result = await db.camiones.update_one(
        {"_id": ObjectId(camion_id)},
        {"$set": update_data}
    )
    
    if result.matched_count == 0:
        raise HTTPException(status_code=404, detail="Camión no encontrado")
    
    updated = await db.camiones.find_one({"_id": ObjectId(camion_id)})
    return {
        "id": str(updated["_id"]),
        "nombre": updated.get("nombre", ""),
        "numero": updated.get("numero", 0),
        "placa": updated.get("placa", ""),
        "tipo_caja": updated.get("tipo_caja", ""),
        "status": updated.get("status", "activo")
    }


@api_router.put("/admin/camiones/{camion_id}/status")
async def admin_toggle_camion_status(camion_id: str, current_user: dict = Depends(get_current_user)):
    """Toggle truck status (active/inactive) - Admin only"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede cambiar estado de camiones")
    
    camion = await db.camiones.find_one({"_id": ObjectId(camion_id)})
    if not camion:
        raise HTTPException(status_code=404, detail="Camión no encontrado")
    
    current_status = camion.get("status", "activo")
    new_status = "inactivo" if current_status == "activo" else "activo"
    
    await db.camiones.update_one(
        {"_id": ObjectId(camion_id)},
        {"$set": {"status": new_status, "fecha_actualizacion": datetime.utcnow()}}
    )
    
    return {"message": f"Camión {'desactivado' if new_status == 'inactivo' else 'activado'}", "status": new_status}


@api_router.delete("/admin/camiones/{camion_id}")
async def delete_camion(camion_id: str, current_user: dict = Depends(get_current_user)):
    """Eliminar un camión permanentemente"""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="Solo admin puede eliminar camiones")
    
    try:
        oid = ObjectId(camion_id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID de camión inválido")
    
    result = await db.camiones.delete_one({"_id": oid})
    if result.deleted_count == 0:
        raise HTTPException(status_code=404, detail="Camión no encontrado")
    
    return {"message": "Camión eliminado permanentemente"}


# ============ DISPONIBILIDAD DE RECURSOS ============

@api_router.get("/recursos/disponibilidad")
async def get_recursos_disponibilidad():
    """
    Get availability status of trucks (camiones) and boxes (cajas).
    A resource is OCCUPIED if it has a service in ACTIVE states.
    
    ACTIVE states (ocupan recursos):
    - pendiente
    - en_progreso
    - en_carga
    - en_ruta
    - en_descarga
    
    INACTIVE states (NO ocupan recursos):
    - completado
    - finalizado
    - cancelado
    
    NOTA V2: La restricción de disponibilidad ha sido ELIMINADA.
    Todas las unidades y cajas siempre aparecen como LIBRES.
    El admin puede seleccionar cualquier unidad/caja sin importar si tienen servicios activos.
    """
    # V2: SIN RESTRICCIONES - Devolver siempre vacío
    logger.info("[DISPONIBILIDAD] V2: Sin restricciones - Todas las unidades/cajas disponibles")
    
    return {
        "camiones_ocupados": {},
        "cajas_ocupadas": {},
        "servicios_activos_count": 0
    }


# ============ OPERATOR PHOTO UPDATE ============

class OperadorPhotoUpdate(BaseModel):
    foto_url: str  # URL de la foto del operador

@api_router.put("/catalogo/operadores/{operador_id}/foto")
async def update_operador_foto(operador_id: str, photo_data: OperadorPhotoUpdate):
    """Update operator photo URL"""
    try:
        result = await db.operadores.update_one(
            {"_id": ObjectId(operador_id)},
            {"$set": {"foto_url": photo_data.foto_url}}
        )
        if result.modified_count == 0:
            raise HTTPException(status_code=404, detail="Operador no encontrado")
        return {"message": "Foto actualizada exitosamente"}
    except Exception as e:
        logger.error(f"Error updating operator photo: {e}")
        raise HTTPException(status_code=500, detail="Error al actualizar foto")

# ============ HELPER: GENERAR PORTADA BASE64 ============

async def generate_portada_base64_internal(servicio: dict) -> Optional[str]:
    """
    Genera la portada PDF del servicio y la devuelve como string base64.
    Uso interno para guardar automáticamente al crear servicio.
    """
    try:
        import base64
        from io import BytesIO
        
        buffer = BytesIO()
        doc = SimpleDocTemplate(
            buffer, 
            pagesize=letter,
            leftMargin=0.5*inch,
            rightMargin=0.5*inch,
            topMargin=0.3*inch,
            bottomMargin=0.3*inch
        )
        story = []
        
        # Extraer datos básicos
        tipo_servicio = servicio.get("tipo_servicio", "SERVICIO")
        cliente = servicio.get("cliente") or tipo_servicio
        operador_nombre = servicio.get("operador_nombre", "N/A")
        camion = servicio.get("camion") or servicio.get("unidad", "N/A")
        placa_camion = servicio.get("placa_camion", "")
        tipo_caja = servicio.get("tipo_caja", "")
        entidad_caja = servicio.get("entidad_caja", "")
        placa_caja = servicio.get("placa_caja", "")
        operador_licencia = servicio.get("operador_licencia", "")
        
        origenes = servicio.get("origenes", [])
        destinos = servicio.get("destinos", [])
        if not origenes:
            origenes = [servicio.get("origen", "N/A")]
        if not destinos:
            destinos = [servicio.get("destino", "N/A")]
        
        origen_primario = origenes[0] if origenes else "N/A"
        destino_final = destinos[-1] if destinos else "N/A"
        
        fecha_creacion = servicio.get("fecha_creacion", datetime.utcnow())
        if isinstance(fecha_creacion, str):
            fecha_creacion = datetime.fromisoformat(fecha_creacion.replace('Z', '+00:00'))
        fecha_mexico = to_mexico_time(fecha_creacion)
        
        DIAS = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
        MESES = ['Enero', 'Febrero', 'Marzo', 'Abril', 'Mayo', 'Junio', 'Julio', 'Agosto', 'Septiembre', 'Octubre', 'Noviembre', 'Diciembre']
        fecha_str = f"{DIAS[fecha_mexico.weekday()]} {fecha_mexico.day} de {MESES[fecha_mexico.month - 1]} {fecha_mexico.year}"
        hora_str = fecha_mexico.strftime("%I:%M %p").lstrip("0")
        
        # ========== ENCABEZADO: SERVICIO Y CLIENTE ==========
        story.append(Spacer(1, 0.05*inch))
        story.append(Paragraph(
            '<font size="9" color="#a0aec0">VIRGO TRANSPORTES REFRIGERADOS</font>',
            ParagraphStyle('Company', alignment=1, spaceAfter=8)
        ))
        story.append(Paragraph(
            f'<font size="16" color="#4a5568">SERVICIO: </font><font size="22" color="#1a365d"><b>{tipo_servicio.upper()}</b></font>',
            ParagraphStyle('ServiceLine', alignment=1, spaceAfter=8)
        ))
        if cliente and cliente.upper() != tipo_servicio.upper():
            story.append(Paragraph(
                f'<font size="16" color="#4a5568">CLIENTE: </font><font size="22" color="#1a365d"><b>{cliente.upper()}</b></font>',
                ParagraphStyle('ClientLine', alignment=1, spaceAfter=6)
            ))
        story.append(Spacer(1, 0.1*inch))
        
        # ========== FOTO DEL OPERADOR ==========
        PHOTO_SIZE = 1.5 * inch
        operador_foto_url = servicio.get("operador_foto_url")
        operator_photo = None
        
        if operador_foto_url:
            foto_bytes = download_operator_photo(operador_foto_url, max_size=200, make_circular=True)
            if foto_bytes:
                try:
                    operator_photo = Image(BytesIO(foto_bytes), width=PHOTO_SIZE, height=PHOTO_SIZE)
                except:
                    pass
        
        # Tabla con foto y datos del operador
        if operator_photo:
            operator_content = f'''<font size="8" color="#718096">OPERADOR</font><br/>
<font size="12" color="#1a365d"><b>{operador_nombre.upper()}</b></font><br/>
<font size="8" color="#4a5568">Lic: {operador_licencia or "N/A"}</font>'''
            
            photo_data_table = Table([
                [operator_photo, Paragraph(operator_content, ParagraphStyle('OpData', alignment=0, leading=14))]
            ], colWidths=[PHOTO_SIZE + 0.1*inch, 3*inch])
            photo_data_table.setStyle(TableStyle([
                ('ALIGN', (0, 0), (0, 0), 'CENTER'),
                ('ALIGN', (1, 0), (1, 0), 'LEFT'),
                ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ]))
            
            centered_photo = Table([[photo_data_table]], colWidths=[7*inch])
            centered_photo.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_photo)
        else:
            story.append(Paragraph(
                f'<font size="8" color="#718096">OPERADOR</font><br/><font size="12" color="#1a365d"><b>{operador_nombre.upper()}</b></font>',
                ParagraphStyle('OpName', alignment=1)
            ))
        
        story.append(Spacer(1, 0.1*inch))
        
        # ========== TARJETAS: UNIDAD Y CAJA ==========
        unit_content = f'''<font size="7" color="#718096">UNIDAD</font><br/>
<font size="11" color="#1a365d"><b>{camion}</b></font><br/>
<font size="8" color="#4a5568">{placa_camion}</font>'''
        
        caja_content = f'''<font size="7" color="#718096">CAJA</font><br/>
<font size="11" color="#1a365d"><b>{tipo_caja or "N/A"}</b></font><br/>
<font size="8" color="#4a5568">{entidad_caja} {placa_caja}</font>'''
        
        unit_card = Table([[Paragraph(unit_content, ParagraphStyle('UnitCard', alignment=1, leading=11))]], colWidths=[2.5*inch])
        unit_card.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f7fafc')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        
        caja_card = Table([[Paragraph(caja_content, ParagraphStyle('CajaCard', alignment=1, leading=11))]], colWidths=[2.5*inch])
        caja_card.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f7fafc')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#e2e8f0')),
            ('TOPPADDING', (0, 0), (-1, -1), 6),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
        ]))
        
        cards_row = Table([[unit_card, caja_card]], colWidths=[3*inch, 3*inch])
        cards_row.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        
        centered_cards = Table([[cards_row]], colWidths=[7*inch])
        centered_cards.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        story.append(centered_cards)
        story.append(Spacer(1, 0.12*inch))
        
        # ========== RUTA Y CITAS (2 columnas) ==========
        # Columna 1: Ruta
        ruta_col = f'<font size="8" color="#718096"><b>ORIGEN</b></font><br/><font size="11" color="#1a365d"><b>{origen_primario[:40]}</b></font>'
        ruta_col += f'<br/><font size="14" color="#3182ce">↓</font><br/>'
        ruta_col += f'<font size="8" color="#718096"><b>DESTINO</b></font><br/><font size="11" color="#1a365d"><b>{destino_final[:40]}</b></font>'
        
        col1 = Table([[Paragraph(ruta_col, ParagraphStyle('Ruta', alignment=1, leading=12))]], colWidths=[3.2*inch])
        col1.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f0f5ff')),
            ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BLUE),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        
        # Columna 2: Citas
        cita_carga = servicio.get("cita_carga") or servicio.get("fecha_cita")
        cita_descarga = servicio.get("cita_descarga")
        
        citas_txt = '<font size="8" color="#d69e2e"><b>CITAS PROGRAMADAS</b></font><br/><br/>'
        if cita_carga:
            try:
                dt = datetime.fromisoformat(str(cita_carga).replace('Z', '').split('+')[0])
                citas_txt += f'<font size="8" color="#3182ce"><b>Carga:</b></font><br/><font size="9">{dt.strftime("%d/%m/%Y %I:%M %p")}</font>'
            except:
                pass
        if cita_descarga:
            try:
                dt = datetime.fromisoformat(str(cita_descarga).replace('Z', '').split('+')[0])
                citas_txt += f'<br/><br/><font size="8" color="#38a169"><b>Descarga:</b></font><br/><font size="9">{dt.strftime("%d/%m/%Y %I:%M %p")}</font>'
            except:
                pass
        if not cita_carga and not cita_descarga:
            citas_txt += '<font size="9" color="#a0aec0">Sin citas programadas</font>'
        
        col2 = Table([[Paragraph(citas_txt, ParagraphStyle('Citas', alignment=1, leading=12))]], colWidths=[3.2*inch])
        col2.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#fffbeb')),
            ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#d69e2e')),
            ('TOPPADDING', (0, 0), (-1, -1), 10),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ]))
        
        two_cols = Table([[col1, col2]], colWidths=[3.4*inch, 3.4*inch])
        two_cols.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        centered_cols = Table([[two_cols]], colWidths=[7.2*inch])
        centered_cols.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        story.append(centered_cols)
        story.append(Spacer(1, 0.12*inch))
        
        # ========== NÚMERO DE FACTURA Y REFERENCIA (SI EXISTEN) ==========
        numero_factura = servicio.get("numero_factura")
        referencia_cliente = servicio.get("referencia_cliente")
        
        # Solo mostrar si hay al menos un valor (no vacío, no None)
        has_factura = numero_factura and str(numero_factura).strip()
        has_referencia = referencia_cliente and str(referencia_cliente).strip()
        
        if has_factura or has_referencia:
            # Construir contenido en LÍNEAS SEPARADAS dentro del mismo recuadro
            factura_parts = []
            if has_factura:
                factura_parts.append(f'<font size="10" color="#000000">No. Factura:</font> <font size="13" color="#000000"><b>{numero_factura}</b></font>')
            if has_referencia:
                if has_factura:
                    factura_parts.append('<br/>')  # Salto de línea entre factura y referencia
                factura_parts.append(f'<font size="10" color="#000000">Ref. Cliente:</font> <font size="13" color="#000000"><b>{referencia_cliente}</b></font>')
            
            factura_table = Table(
                [[Paragraph(''.join(factura_parts), ParagraphStyle('Factura', alignment=1, leading=18))]],
                colWidths=[5.5*inch]
            )
            factura_table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f5f0ff')),
                ('BOX', (0, 0), (-1, -1), 1, colors.HexColor('#805ad5')),
                ('TOPPADDING', (0, 0), (-1, -1), 6),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 6),
                ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ]))
            centered_factura = Table([[factura_table]], colWidths=[7.2*inch])
            centered_factura.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
            story.append(centered_factura)
            story.append(Spacer(1, 0.05*inch))
        
        # ========== FECHA/HORA ==========
        story.append(Paragraph(
            f'<font size="10" color="#4a5568"><b>{fecha_str}</b></font>  |  <font size="10" color="#4a5568"><b>{hora_str}</b></font>',
            ParagraphStyle('DateTime', alignment=1)
        ))
        story.append(Spacer(1, 0.08*inch))
        story.append(Paragraph(
            f'<font size="7" color="#a0aec0">Documento generado el {datetime.now(MEXICO_TZ).strftime("%d/%m/%Y a las %H:%M")}</font>',
            ParagraphStyle('Footer', alignment=1)
        ))
        
        doc.build(story)
        buffer.seek(0)
        
        # Convertir a base64
        pdf_base64 = base64.b64encode(buffer.getvalue()).decode('utf-8')
        return f"data:application/pdf;base64,{pdf_base64}"
        
    except Exception as e:
        logger.error(f"Error generating portada base64: {e}")
        import traceback
        traceback.print_exc()
        return None

# ============ PORTADA PREVIEW ENDPOINT ============

@api_router.get("/servicios/{servicio_id}/portada-guardada")
async def get_portada_guardada(servicio_id: str):
    """
    Obtiene la portada guardada del servicio (base64).
    Si no existe, genera una nueva y la guarda.
    ENDPOINT PÚBLICO - No requiere autenticación.
    """
    servicio = await db.servicios.find_one({"_id": ObjectId(servicio_id)})
    if not servicio:
        raise HTTPException(status_code=404, detail="Servicio no encontrado")
    
    # Si ya tiene portada guardada, devolverla
    portada_url = servicio.get("portada_url")
    if portada_url:
        return {"portada_url": portada_url, "cached": True}
    
    # Si no tiene, generar una nueva y guardarla
    portada_base64 = await generate_portada_base64_internal(servicio)
    if portada_base64:
        await db.servicios.update_one(
            {"_id": ObjectId(servicio_id)},
            {"$set": {"portada_url": portada_base64}}
        )
        return {"portada_url": portada_base64, "cached": False}
    
    raise HTTPException(status_code=500, detail="Error generando portada")

@api_router.get("/servicios/{servicio_id}/portada")
async def generate_portada_preview(servicio_id: str):
    """
    Generate only the cover page (portada) as a PDF.
    Used for preview/sharing without evidence photos.
    ENDPOINT PÚBLICO - No requiere autenticación.
    El operador accede vía Linking.openURL que no permite headers.
    """
    # Validar ObjectId
    try:
        oid = ObjectId(servicio_id)
    except Exception:
        raise HTTPException(status_code=400, detail="ID de servicio inválido")
    
    try:
        servicio = await db.servicios.find_one({"_id": oid})
        if not servicio:
            raise HTTPException(status_code=404, detail="Servicio no encontrado")
    except Exception as e:
        logger.error(f"Error buscando servicio: {e}")
        raise HTTPException(status_code=500, detail="Error de conexión a base de datos")
    
    # Get additional data
    unidad_nombre = servicio.get("unidad", "") or servicio.get("camion", "")
    placas = servicio.get("placa_camion", "N/A")
    
    # tipo_caja: SIEMPRE del servicio (igual que en el PDF)
    tipo_caja = servicio.get("tipo_caja") or "N/A"
    
    # Si no tenemos placa del camión en el servicio, buscar en catálogo
    if placas == "N/A" and unidad_nombre:
        camion = await db.camiones.find_one({"nombre": unidad_nombre})
        if camion:
            placas = camion.get("placa", "N/A")
    
    # OBTENER FOTO DEL OPERADOR (primero servicio, luego catálogo)
    foto_url = await get_operador_foto_url(servicio, db)
    
    # Buscar teléfono del operador en el catálogo por nombre
    operador_nombre = servicio.get("operador_nombre", "")
    telefono_operador = ""
    if operador_nombre:
        operador_doc = await db.operadores.find_one({"nombre": operador_nombre})
        if operador_doc:
            telefono_operador = operador_doc.get("telefono", "")
    
    operador_data = {
        "nombre": operador_nombre,
        "foto_url": foto_url,  # Foto desde servicio o catálogo
        "licencia": servicio.get("operador_licencia", ""),
        "telefono": telefono_operador  # Teléfono desde catálogo de operadores
    }
    
    logger.info(f"Portada preview - Operator photo URL: {operador_data.get('foto_url')}")
    logger.info(f"Portada preview - Operator phone: {telefono_operador}")
    
    # Download logos
    header_logo = download_logo(LOGO_HEADER_URL)
    watermark_logo = download_logo(LOGO_WATERMARK_URL)
    
    buffer = BytesIO()
    doc = CorporatePDF(
        buffer,
        pagesize=letter,
        topMargin=1.1*inch,
        bottomMargin=1.0*inch,  # Space for legal footer
        leftMargin=0.6*inch,
        rightMargin=0.6*inch,
        header_logo=header_logo,
        watermark_logo=watermark_logo,
        servicio_info=servicio
    )
    
    styles = getSampleStyleSheet()
    story = []
    
    # ==========================================
    # PORTADA PROFESIONAL CENTRADA - PREVIEW
    # ==========================================
    
    tipo_servicio = servicio.get("tipo_servicio") or servicio.get("cliente") or "N/A"
    cliente = servicio.get("cliente")  # Nombre del cliente (opcional)
    unidad = servicio.get("unidad", "N/A")
    operador = servicio.get("operador_nombre", "N/A")
    estado = servicio.get("estado", "pendiente")
    fecha = servicio.get("fecha_creacion", datetime.utcnow())
    if isinstance(fecha, str):
        fecha = datetime.fromisoformat(fecha.replace('Z', '+00:00'))
    
    fecha_mexico = to_mexico_time(fecha)
    fecha_str = fecha_mexico.strftime("%d/%m/%Y")
    hora_str = fecha_mexico.strftime("%H:%M")
    
    # Handle multiple origins (backward compatible)
    origenes = servicio.get("origenes", [])
    if not origenes and servicio.get("origen"):
        origenes = [servicio.get("origen")]
    origen_primario = origenes[0] if origenes else "N/A"
    
    destinos = servicio.get("destinos", [])
    if not destinos and servicio.get("destino"):
        destinos = [servicio.get("destino")]
    destino_final = destinos[-1] if destinos else "N/A"
    
    # Licencia del operador
    licencia = operador_data.get("licencia", "") if operador_data else ""
    telefono_operador = operador_data.get("telefono", "") if operador_data else ""
    
    # Datos de la caja (nuevos campos)
    entidad_caja = servicio.get("entidad_caja") or "N/A"
    placa_caja = servicio.get("placa_caja") or "N/A"
    
    if estado == "completado":
        status_color = "#48bb78"
        status_text = "COMPLETADO"
    elif estado == "en_progreso":
        status_color = "#ecc94b"
        status_text = "EN PROGRESO"
    else:
        status_color = "#a0aec0"
        status_text = "PENDIENTE"
    
    # Unidad sin tipo de caja (el tipo de caja va en su propio campo)
    unidad_display = unidad
    
    # ========== ENCABEZADO: SERVICIO Y CLIENTE ==========
    story.append(Spacer(1, 0.05*inch))
    story.append(Paragraph(
        '<font size="9" color="#a0aec0">VIRGO TRANSPORTES REFRIGERADOS</font>',
        ParagraphStyle('Company', alignment=1, spaceAfter=8)
    ))
    
    # SERVICIO: {valor} - en una línea, centrado
    story.append(Paragraph(
        f'<font size="16" color="#4a5568">SERVICIO: </font><font size="22" color="#1a365d"><b>{tipo_servicio.upper()}</b></font>',
        ParagraphStyle('ServiceLine', alignment=1, spaceAfter=8)
    ))
    
    # CLIENTE: {valor} - en una línea, centrado (si existe cliente diferente)
    cliente_display = cliente if cliente and cliente.upper() != tipo_servicio.upper() else None
    if cliente_display:
        story.append(Paragraph(
            f'<font size="16" color="#4a5568">CLIENTE: </font><font size="22" color="#1a365d"><b>{cliente_display.upper()}</b></font>',
            ParagraphStyle('ClientLine', alignment=1, spaceAfter=6)
        ))
    else:
        story.append(Spacer(1, 0.02*inch))
    
    # Línea decorativa centrada
    line_table = Table([['']], colWidths=[3*inch])
    line_table.setStyle(TableStyle([
        ('LINEBELOW', (0, 0), (-1, -1), 1.5, CORPORATE_BLUE),
    ]))
    centered_line = Table([[line_table]], colWidths=[7*inch])
    centered_line.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    story.append(centered_line)
    story.append(Spacer(1, 0.12*inch))
    
    # ========== FOTO DEL OPERADOR CIRCULAR ==========
    PHOTO_SIZE = 1.5 * inch  # ~108px - reducido para layout compacto
    operator_photo = None
    
    if operador_data and operador_data.get("foto_url"):
        # Descargar con recorte circular
        foto_bytes = download_operator_photo(operador_data.get("foto_url"), max_size=200, make_circular=True)
        if foto_bytes:
            try:
                foto_buffer = BytesIO(foto_bytes)
                operator_photo = RLImage(foto_buffer, width=PHOTO_SIZE, height=PHOTO_SIZE)
                logger.info("Operator circular photo created for portada preview")
            except Exception as e:
                logger.error(f"Error creating operator photo: {e}")
    
    # Foto o ningún fallback (NO mostrar inicial)
    if operator_photo:
        # Contenedor con borde azul simulando círculo
        photo_container = Table([[operator_photo]], colWidths=[PHOTO_SIZE + 0.1*inch], rowHeights=[PHOTO_SIZE + 0.1*inch])
        photo_container.setStyle(TableStyle([
            ('BACKGROUND', (0, 0), (-1, -1), colors.white),
            ('BOX', (0, 0), (-1, -1), 3, CORPORATE_BLUE),
            ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
            ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
            ('LEFTPADDING', (0, 0), (-1, -1), 0),
            ('RIGHTPADDING', (0, 0), (-1, -1), 0),
            ('TOPPADDING', (0, 0), (-1, -1), 0),
            ('BOTTOMPADDING', (0, 0), (-1, -1), 0),
        ]))
        
        # Centrar foto
        centered_photo = Table([[photo_container]], colWidths=[7*inch])
        centered_photo.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
        story.append(centered_photo)
        story.append(Spacer(1, 0.1*inch))
    else:
        # Sin foto - espacio mínimo
        story.append(Spacer(1, 0.1*inch))
    
    # ========== NOMBRE DEL OPERADOR (MÁS DESTACADO) ==========
    story.append(Paragraph(
        f'<font size="18" color="#1a365d"><b>{operador.upper()}</b></font>',
        ParagraphStyle('Name', alignment=1, spaceAfter=6)
    ))
    story.append(Spacer(1, 0.05*inch))  # 3.6px spacing
    story.append(Paragraph(
        '<font size="9" color="#718096">OPERADOR ASIGNADO</font>',
        ParagraphStyle('Label', alignment=1, spaceAfter=2)
    ))
    # Teléfono del operador (si existe)
    if telefono_operador:
        story.append(Paragraph(
            f'<font size="10" color="#4a5568">Tel: {telefono_operador}</font>',
            ParagraphStyle('Phone', alignment=1, spaceAfter=10)
        ))
    else:
        story.append(Spacer(1, 0.05*inch))
    
    # ========== DATOS DEL VEHÍCULO (TARJETAS COMPACTAS EN FILA) ==========
    card_style = TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f7fafc')),
        ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BORDER),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('TOPPADDING', (0, 0), (-1, -1), 5),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 5),
        ('LEFTPADDING', (0, 0), (-1, -1), 4),
        ('RIGHTPADDING', (0, 0), (-1, -1), 4),
    ])
    
    # Tarjeta Tracto/Camion (antes UNIDAD)
    unidad_card = Table([[Paragraph(f'<font size="7" color="#718096">TRACTO/CAMION</font><br/><font size="10" color="#1a365d"><b>{unidad_display}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
    unidad_card.setStyle(card_style)
    
    # Tarjeta Placas
    placas_card = Table([[Paragraph(f'<font size="7" color="#718096">PLACAS</font><br/><font size="10" color="#1a365d"><b>{placas}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
    placas_card.setStyle(card_style)
    
    # Tarjeta Licencia
    licencia_display = licencia if licencia else "N/A"
    licencia_card = Table([[Paragraph(f'<font size="7" color="#718096">LICENCIA</font><br/><font size="10" color="#1a365d"><b>{licencia_display}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
    licencia_card.setStyle(card_style)
    
    # Fila de tarjetas compacta
    cards_row = Table([[unidad_card, placas_card, licencia_card]], colWidths=[2.2*inch, 2.2*inch, 2.2*inch], spaceBefore=0, spaceAfter=0)
    cards_row.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
    ]))
    
    centered_cards = Table([[cards_row]], colWidths=[7*inch])
    centered_cards.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    story.append(centered_cards)
    story.append(Spacer(1, 0.06*inch))
    
    # ========== FILA DE DATOS DE CAJA ==========
    tipo_caja_display = tipo_caja if tipo_caja else "N/A"
    
    # Tarjeta Tipo Caja
    tipo_caja_card = Table([[Paragraph(f'<font size="7" color="#718096">TIPO CAJA</font><br/><font size="10" color="#1a365d"><b>{tipo_caja_display}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
    tipo_caja_card.setStyle(card_style)
    
    # Tarjeta Entidad
    entidad_card = Table([[Paragraph(f'<font size="7" color="#718096">ENTIDAD</font><br/><font size="10" color="#1a365d"><b>{entidad_caja}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
    entidad_card.setStyle(card_style)
    
    # Tarjeta Placa Caja
    placa_caja_card = Table([[Paragraph(f'<font size="7" color="#718096">PLACA CAJA</font><br/><font size="10" color="#1a365d"><b>{placa_caja}</b></font>', ParagraphStyle('Card', alignment=1, leading=11))]], colWidths=[2.15*inch])
    placa_caja_card.setStyle(card_style)
    
    # Fila de tarjetas de caja
    caja_cards_row = Table([[tipo_caja_card, entidad_card, placa_caja_card]], colWidths=[2.2*inch, 2.2*inch, 2.2*inch], spaceBefore=0, spaceAfter=0)
    caja_cards_row.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 2),
        ('RIGHTPADDING', (0, 0), (-1, -1), 2),
    ]))
    
    centered_caja_cards = Table([[caja_cards_row]], colWidths=[7*inch])
    centered_caja_cards.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    story.append(centered_caja_cards)
    story.append(Spacer(1, 0.12*inch))
    
    # ==================================================================================
    # LAYOUT HORIZONTAL: 2 COLUMNAS (RUTA | CITAS) - SIN TRAZABILIDAD
    # La trazabilidad solo aparece en el PDF final del reporte completo
    # ==================================================================================
    
    # Preparar datos de CITAS
    cita_carga_raw = servicio.get("cita_carga") or servicio.get("fecha_cita")
    cita_descarga_raw = servicio.get("cita_descarga")
    
    def formatear_cita_portada(fecha_raw: str) -> str:
        if not fecha_raw:
            return None
        try:
            fecha_str_clean = str(fecha_raw).replace('Z', '').replace('+00:00', '').split('+')[0]
            fecha_dt = datetime.fromisoformat(fecha_str_clean) if 'T' in fecha_str_clean else datetime.strptime(fecha_str_clean, "%Y-%m-%d %H:%M")
            DIAS = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
            return f"{DIAS[fecha_dt.weekday()]} {fecha_dt.strftime('%d/%m/%Y')}<br/><b>{fecha_dt.strftime('%I:%M %p').lstrip('0')}</b>"
        except:
            return None
    
    cita_carga_fmt = formatear_cita_portada(cita_carga_raw)
    cita_descarga_fmt = formatear_cita_portada(cita_descarga_raw)
    
    # ========== COLUMNA 1: RUTA (Origen → Destino) ==========
    if len(origenes) > 1:
        origenes_txt = "<br/>".join([f'<font size="9">• {o[:35]}</font>' for o in origenes[:3]])
        ruta_col1 = f'<font size="8" color="#718096"><b>ORIGEN(ES)</b></font><br/>{origenes_txt}'
    else:
        ruta_col1 = f'<font size="8" color="#718096"><b>ORIGEN</b></font><br/><font size="11" color="#1a365d"><b>{origen_primario[:40]}</b></font>'
    
    ruta_col1 += f'<br/><font size="14" color="#3182ce">↓</font><br/>'
    
    if len(destinos) > 1:
        destinos_txt = "<br/>".join([f'<font size="9">• {d[:35]}</font>' for d in destinos[:3]])
        ruta_col1 += f'<font size="8" color="#718096"><b>DESTINO(S)</b></font><br/>{destinos_txt}'
    else:
        ruta_col1 += f'<font size="8" color="#718096"><b>DESTINO</b></font><br/><font size="11" color="#1a365d"><b>{destino_final[:40]}</b></font>'
    
    col1_table = Table([[Paragraph(ruta_col1, ParagraphStyle('Col1P', alignment=1, leading=12))]], colWidths=[3.2*inch])
    col1_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#f0f5ff')),
        ('BOX', (0, 0), (-1, -1), 0.5, CORPORATE_BLUE),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    
    # ========== COLUMNA 2: CITAS ==========
    citas_content = '<font size="8" color="#d69e2e"><b>CITAS PROGRAMADAS</b></font><br/><br/>'
    if cita_carga_fmt:
        citas_content += f'<font size="8" color="#3182ce"><b>Carga:</b></font><br/><font size="9" color="#1a365d">{cita_carga_fmt}</font>'
        if cita_descarga_fmt:
            citas_content += '<br/><br/>'
    if cita_descarga_fmt:
        citas_content += f'<font size="8" color="#38a169"><b>Descarga:</b></font><br/><font size="9" color="#1a365d">{cita_descarga_fmt}</font>'
    if not cita_carga_fmt and not cita_descarga_fmt:
        citas_content += '<font size="9" color="#a0aec0">Sin citas programadas</font>'
    
    col2_table = Table([[Paragraph(citas_content, ParagraphStyle('Col2P', alignment=1, leading=12))]], colWidths=[3.2*inch])
    col2_table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, -1), colors.HexColor('#fffbeb')),
        ('BOX', (0, 0), (-1, -1), 0.5, colors.HexColor('#d69e2e')),
        ('TOPPADDING', (0, 0), (-1, -1), 10),
        ('BOTTOMPADDING', (0, 0), (-1, -1), 10),
        ('LEFTPADDING', (0, 0), (-1, -1), 8),
        ('RIGHTPADDING', (0, 0), (-1, -1), 8),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
    ]))
    
    # ========== FILA HORIZONTAL DE 2 COLUMNAS ==========
    two_col_row = Table(
        [[col1_table, col2_table]], 
        colWidths=[3.4*inch, 3.4*inch],
        spaceBefore=0, 
        spaceAfter=0
    )
    two_col_row.setStyle(TableStyle([
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('VALIGN', (0, 0), (-1, -1), 'MIDDLE'),
        ('LEFTPADDING', (0, 0), (-1, -1), 5),
        ('RIGHTPADDING', (0, 0), (-1, -1), 5),
    ]))
    
    centered_two_col = Table([[two_col_row]], colWidths=[7.2*inch])
    centered_two_col.setStyle(TableStyle([('ALIGN', (0, 0), (-1, -1), 'CENTER')]))
    story.append(centered_two_col)
    story.append(Spacer(1, 0.12*inch))
    
    # ========== FECHA/HORA CREACIÓN ==========
    datetime_display = f'''<font size="10" color="#4a5568"><b>{fecha_str}</b></font>  <font size="9" color="#718096">|</font>  <font size="10" color="#4a5568"><b>{hora_str}</b></font>'''
    
    story.append(Paragraph(datetime_display, ParagraphStyle('DateTime', alignment=1, leading=12)))
    story.append(Spacer(1, 0.08*inch))
    
    # ========== PIE DISCRETO ==========
    story.append(Paragraph(
        f'<font size="7" color="#a0aec0">Documento generado el {datetime.now(MEXICO_TZ).strftime("%d/%m/%Y a las %H:%M")}</font>',
        ParagraphStyle('Footer', alignment=1)
    ))
    
    doc.build(story)
    buffer.seek(0)
    
    # Generar nombre de archivo usando factura y referencia
    numero_factura = servicio.get("numero_factura", "")
    referencia_cliente = servicio.get("referencia_cliente", "")
    # Para portada agregar sufijo "_PORTADA"
    base_filename = generar_nombre_pdf(numero_factura, referencia_cliente)
    filename = base_filename.replace(".pdf", "_PORTADA.pdf")
    
    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f"attachment; filename={filename}",
            "Cache-Control": "no-cache, no-store, must-revalidate",
            "Pragma": "no-cache",
            "Expires": "0"
        }
    )

# ============ HEALTH CHECK & ENVIRONMENT INFO ============

@api_router.get("/health")
async def health_check():
    """
    Health check endpoint para verificar conectividad.
    Útil para validar que dashboard y app móvil usan el mismo backend.
    """
    try:
        # Test DB connection
        await db.command("ping")
        db_status = "connected"
    except Exception as e:
        db_status = f"error: {str(e)}"
    
    # Contar documentos para verificar datos
    try:
        cajas_count = await db.cajas.count_documents({})
        camiones_count = await db.camiones.count_documents({})
        operadores_count = await db.operadores.count_documents({})
        servicios_count = await db.servicios.count_documents({})
    except:
        cajas_count = camiones_count = operadores_count = servicios_count = -1
    
    return {
        "status": "ok",
        "timestamp": datetime.utcnow().isoformat(),
        "database": db_status,
        "db_config": {
            "mongo_url": mongo_url,
            "db_name": db_name
        },
        "data_counts": {
            "cajas": cajas_count,
            "camiones": camiones_count,
            "operadores": operadores_count,
            "servicios": servicios_count
        },
        "version": "1.0.0",
        "environment": "production" if not os.environ.get("DEBUG") else "development"
    }

# ============ SEED DATA ============

@api_router.post("/seed")
async def seed_data():
    """
    Seed inicial de datos. 
    IMPORTANTE: NUNCA sobrescribe la contraseña del admin si ya existe.
    Solo crea el admin si no existe ninguno en la base de datos.
    """
    try:
        admin_exists = await db.users.find_one({"username": "transportesvirgo"})
        if admin_exists:
            # Admin ya existe - NO tocar la contraseña
            # Solo verificar si necesitan seed los catálogos
            operadores_count = await db.operadores.count_documents({})
            if operadores_count == 0:
                await seed_catalogs()
                return {"message": "Catálogos creados. Admin existente no modificado."}
            return {"message": "Datos ya existentes. Admin no modificado."}
        
        # Admin NO existe - crear nuevo admin
        admin_doc = {
            "username": "transportesvirgo",
            "password": hash_password("Virgo2019"),
            "nombre": "Administrador",
            "role": "admin",
            "created_at": datetime.utcnow()
        }
        await db.users.insert_one(admin_doc)
        
        # Seed catalogs
        await seed_catalogs()
        
        return {
            "message": "Datos iniciales creados exitosamente (nuevo admin)"
        }
    except Exception as e:
        print(f"[SEED] Error de conexión a base de datos: {e}")
        raise HTTPException(status_code=503, detail="Error de conexión a la base de datos. Intente nuevamente.")

# Endpoint para forzar re-seed de catálogos (SOLO para actualizar fotos de operadores)
@api_router.post("/seed/catalogs", tags=["System"])
async def reseed_catalogs():
    """Forzar re-seed de catálogos (operadores, camiones, cajas)"""
    await seed_catalogs()
    return {"message": "Catálogos actualizados exitosamente"}

async def seed_catalogs():
    """Seed operadores, camiones, and cajas catalogs"""
    # Clear existing catalogs
    await db.operadores.delete_many({})
    await db.camiones.delete_many({})
    await db.cajas.delete_many({})
    
    # URLs de fotos reales de Cloudinary (optimizadas - c_fit para mostrar completa)
    FOTO_LUIS = "https://res.cloudinary.com/dgp94fmou/image/upload/w_300,h_300,c_fit/v1776955926/luis_domingo_yds9eu.jpg"
    FOTO_EDDY = "https://res.cloudinary.com/dgp94fmou/image/upload/w_300,h_300,c_fit/v1776955926/eddy_garcia_rr3v1p.jpg"
    FOTO_JOSE_LUIS = "https://res.cloudinary.com/dgp94fmou/image/upload/w_300,h_300,c_fit/v1776955926/jose_luis_alanis_ibhcqe.jpg"
    FOTO_ISRAEL = "https://res.cloudinary.com/dgp94fmou/image/upload/w_300,h_300,c_fit/v1776955926/israel_espinoza_s1frju.jpg"
    FOTO_JAIME = "https://res.cloudinary.com/dgp94fmou/image/upload/w_300,h_300,c_fit/v1776955926/jaime_serrano_p3zjwt.jpg"
    FOTO_FERNANDO = "https://res.cloudinary.com/dgp94fmou/image/upload/w_300,h_300,c_fit/v1776916916/1a3eb9ba-8bba-4839-a02e-1a0fe717a7b8_wheqhl.jpg"
    
    # Operadores catalog with unique id_operador for quick access
    operadores_catalog = [
        {"nombre": "LUIS DOMINGO GARCIA", "telefono": "4622170584", "licencia": "GTO0014693", "vigencia_licencia": "08/12/2029", "rfc": "GACL800425", "id_operador": "A101", "foto_url": FOTO_LUIS},
        {"nombre": "EDDY GARCIA DURAN", "telefono": "4622645747", "licencia": "LFD00005502", "vigencia_licencia": "12/06/2029", "rfc": "GADE930410H90", "id_operador": "A102", "foto_url": FOTO_EDDY},
        {"nombre": "JOSE LUIS OLVERA ROSALES", "telefono": "4623758941", "licencia": "QRO10561", "vigencia_licencia": "16/02/2028", "rfc": "OERL570407", "id_operador": "A103", "foto_url": "https://res.cloudinary.com/dgp94fmou/image/upload/v1777571113/jose_luis_olvera_c1awvq.jpg"},
        {"nombre": "EDUARDO OLVERA PONCE", "telefono": "4621093169", "licencia": "LFD00065675", "vigencia_licencia": "20/05/2026", "rfc": "OEPJ9106012U0", "id_operador": "A104", "foto_url": "https://res.cloudinary.com/dgp94fmou/image/upload/v1777584369/jesus_eduardo_egxnmd.jpg"},
        {"nombre": "JOSE LUIS OLVERA ALANIS", "telefono": "4623241330", "licencia": "LFD00050237", "vigencia_licencia": "12/03/2030", "rfc": "OEAL020921FN2", "id_operador": "A105", "foto_url": FOTO_JOSE_LUIS},
        {"nombre": "ISRAEL ESPINOZA", "telefono": "4623692726", "licencia": "GTO0015373", "vigencia_licencia": "22/02/2027", "rfc": "IEMI760905H35", "id_operador": "B201", "foto_url": FOTO_ISRAEL},
        {"nombre": "JAIME SERRANO SANCHEZ", "telefono": "7203034300", "licencia": "LFD00014666", "vigencia_licencia": "06/08/2029", "rfc": "SES8001029U3", "id_operador": "B202", "foto_url": FOTO_JAIME},
        {"nombre": "FERNANDO RODRIGUEZ VEGA", "telefono": "5561901596", "licencia": "DF00225867", "vigencia_licencia": "25/10/2027", "rfc": "ROVF780304H40", "id_operador": "B203", "foto_url": FOTO_FERNANDO},
    ]
    await db.operadores.insert_many(operadores_catalog)
    
    # Camiones catalog
    camiones_catalog = [
        {"nombre": "ECO 01", "numero": 1, "placa": "12BJ3V", "tipo_caja": "THERMO"},
        {"nombre": "ECO 29", "numero": 29, "placa": "73BL9R", "tipo_caja": "THERMO"},
        {"nombre": "ECO 04", "numero": 4, "placa": "06BF8D", "tipo_caja": "THERMO"},
        {"nombre": "ECO 11", "numero": 11, "placa": "46BF3E", "tipo_caja": "THERMO"},
        {"nombre": "ECO 14", "numero": 14, "placa": "79BA9P", "tipo_caja": "THERMO"},
        {"nombre": "ECO 05", "numero": 5, "placa": "52BK3Y", "tipo_caja": "THERMO"},
        {"nombre": "ECO 12", "numero": 12, "placa": "38BL4P", "tipo_caja": "THERMO"},
        {"nombre": "ECO 22", "numero": 22, "placa": "96UZ6D", "tipo_caja": "THERMO"},
    ]
    await db.camiones.insert_many(camiones_catalog)
    
    # Cajas catalog - migrated from hardcoded CATALOGO_CAJAS
    cajas_catalog = [
        {"tipo_caja": "THERMO", "numero_entidad": "1141", "placa": "25UY7G", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "THERMO", "numero_entidad": "933", "placa": "58UW2J", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "THERMO", "numero_entidad": "14534", "placa": "50UW1K", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "THERMO", "numero_entidad": "929", "placa": "97UW2J", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "THERMO", "numero_entidad": "1151", "placa": "95UY6G", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "CAJA SECA", "numero_entidad": "153434", "placa": "15UT4H", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "CAJA SECA", "numero_entidad": "7", "placa": "85UZ4D", "status": "activo", "fecha_creacion": datetime.utcnow()},
        {"tipo_caja": "CAJA SECA", "numero_entidad": "1601", "placa": "96UZ6D", "status": "activo", "fecha_creacion": datetime.utcnow()},
    ]
    await db.cajas.insert_many(cajas_catalog)

@api_router.get("/")
async def root():
    return {"message": "Transport Evidence API", "version": "1.0.0"}

# Force re-seed catalogs (for updates)
@api_router.post("/reseed-catalogs")
async def reseed_catalogs():
    """Force re-seed operadores, camiones, and cajas catalogs"""
    await db.operadores.delete_many({})
    await db.camiones.delete_many({})
    await db.cajas.delete_many({})
    await seed_catalogs()
    return {"message": "Catálogos actualizados correctamente"}

# Include the router
app.include_router(api_router)

app.add_middleware(
    CORSMiddleware,
    allow_credentials=True,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
async def startup_event():
    """Build frontend and auto-seed data on startup"""
    # Auto-seed data if database is empty
    try:
        cajas_count = await db.cajas.count_documents({})
        print(f"[STARTUP] Verificando datos... Cajas: {cajas_count}")
        
        # Verificar si existe el admin - NUNCA sobrescribir contraseña existente
        admin_exists = await db.users.find_one({"username": "transportesvirgo"})
        
        if cajas_count == 0:
            print("[STARTUP] Base de datos vacía - Ejecutando seed automático...")
            # Seed admin user SOLO si no existe
            if not admin_exists:
                await db.users.insert_one({
                    "username": "transportesvirgo",
                    "nombre": "Administrador",
                    "password": hash_password("Virgo2019"),
                    "role": "admin",
                    "created_at": datetime.utcnow()
                })
                print("[STARTUP] Admin creado (nuevo)")
            else:
                print("[STARTUP] Admin ya existe - contraseña NO modificada")
            
            # Seed catalogs
            await seed_catalogs()
            print("[STARTUP] Seed completado exitosamente")
        else:
            # Base de datos con datos existentes
            if not admin_exists:
                # Caso raro: hay datos pero no hay admin - crear admin
                await db.users.insert_one({
                    "username": "transportesvirgo",
                    "nombre": "Administrador",
                    "password": hash_password("Virgo2019"),
                    "role": "admin",
                    "created_at": datetime.utcnow()
                })
                print("[STARTUP] Admin creado (faltaba)")
            else:
                print(f"[STARTUP] Datos existentes - Admin OK - No se requiere seed")
    except Exception as e:
        print(f"[STARTUP] Error en seed automático: {e}")

@app.on_event("shutdown")
async def shutdown_db_client():
    client.close()

# Frontend served separately via Netlify

# Ruta raíz "/" - DEBE ESTAR AL FINAL
@app.get("/")
def serve_root():
    index_path = "/app/frontend/dist/index.html"
    print(f"🔥 SERVE_ROOT - Checking: {index_path}")
    if os.path.isfile(index_path):
        print(f"🔥 SERVE_ROOT - Serving index.html")
        return FileResponse(index_path, media_type="text/html")
    print(f"🔥 SERVE_ROOT - index.html not found, returning API status")
    return {"status": "ok", "message": "API server running"}

# Ruta catch-all para SPA - DEBE ESTAR DESPUÉS DE "/"
@app.get("/{full_path:path}")
def serve_frontend(full_path: str):
    print(f"🔥 SERVE_FRONTEND - path: {full_path}")
    
    # Si es ruta de API, devolver 404
    if full_path.startswith("api/"):
        raise HTTPException(status_code=404, detail="Not Found")
    
    # Intentar servir archivo específico
    file_path = f"/app/frontend/dist/{full_path}"
    if os.path.isfile(file_path):
        return FileResponse(file_path)
    
    # Intentar con extensión .html
    html_path = f"/app/frontend/dist/{full_path}.html"
    if os.path.isfile(html_path):
        return FileResponse(html_path, media_type="text/html")
    
    # Fallback: servir index.html para rutas SPA (solo si existe)
    index_path = "/app/frontend/dist/index.html"
    if os.path.isfile(index_path):
        return FileResponse(index_path, media_type="text/html")
    
    # Si no existe index.html, devolver 404
    raise HTTPException(status_code=404, detail=f"File not found: {full_path}")
