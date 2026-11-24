from contextlib import asynccontextmanager
import hashlib
import math
import os
import re
from typing import Optional
import uuid

import cloudinary
import cloudinary.uploader
from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
import mysql.connector
from rapidocr_onnxruntime import RapidOCR

load_dotenv()

# =============================================================================
# CONFIGURACIÓN DE CLOUDINARY, BASE DE DATOS Y GEOCERCA UNIVERSITARIA
# =============================================================================
cloudinary.config(
    cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
    api_key=os.getenv("CLOUDINARY_API_KEY"),
    api_secret=os.getenv("CLOUDINARY_API_SECRET"),
    secure=True,
)

CAMPUS_LAT = 18.06750
CAMPUS_LON = -93.16850
RADIO_CAMPUS_METROS = 450
VALIDAR_PERIMETRO_SERVIDOR = False

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT") or 3306),
    "user": os.getenv("DB_USER", "root"),
    "password": os.getenv("DB_PASSWORD", ""),
    "database": os.getenv("DB_NAME", "unifix_found_db"),
}

motor_ocr = RapidOCR()


def get_db():
    return mysql.connector.connect(**DB_CONFIG)


def asegurar_esquema_db():
    """Verifica y actualiza columnas/enums en MySQL si la BD fue creada con una versión previa."""
    try:
        conn = get_db()
        cursor = conn.cursor()
        sentencias = [
            """
            ALTER TABLE espacios_campus
            MODIFY COLUMN tipo_espacio ENUM(
                'AULA', 'LABORATORIO', 'LAB_COMPUTO', 'LAB_REDES', 'LAB_ELECTRONICA', 'SOPORTE'
            ) NOT NULL DEFAULT 'AULA'
            """,
            "ALTER TABLE reportes_fallas ADD COLUMN fecha_resolucion TIMESTAMP NULL AFTER fecha_creacion",
            "ALTER TABLE reportes_fallas MODIFY COLUMN url_foto VARCHAR(500) NULL",
            "ALTER TABLE objetos_campus ADD COLUMN texto_ocr TEXT NULL AFTER descripcion",
            "ALTER TABLE objetos_campus ADD COLUMN fecha_entrega TIMESTAMP NULL AFTER fecha_creacion",
            "ALTER TABLE objetos_campus MODIFY COLUMN url_foto VARCHAR(500) NULL",
        ]
        for sql in sentencias:
            try:
                cursor.execute(sql)
            except mysql.connector.Error:
                pass
        conn.commit()
        cursor.close()
        conn.close()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    asegurar_esquema_db()
    yield


app = FastAPI(title="UniFix & Found API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# =============================================================================
# FUNCIONES AUXILIARES (GPS, ESPACIOS, CLOUDINARY Y OCR)
# =============================================================================
def calcular_distancia_metros(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def validar_ubicacion_campus(latitud: float, longitud: float):
    if latitud == 0.0 and longitud == 0.0:
        raise HTTPException(status_code=400, detail="Coordenadas GPS inválidas.")
    if VALIDAR_PERIMETRO_SERVIDOR:
        distancia = calcular_distancia_metros(latitud, longitud, CAMPUS_LAT, CAMPUS_LON)
        if distancia > RADIO_CAMPUS_METROS:
            raise HTTPException(
                status_code=403,
                detail=f"Fuera del perímetro universitario ({int(distancia)} m).",
            )


def obtener_o_crear_espacio(cursor, nivel: str, tipo_espacio: str, espacio: str) -> int:
    """Busca el aula o laboratorio escrito; si no existe aún, lo registra en el catálogo."""
    nombre_limpio = espacio.strip()
    tipo_limpio = tipo_espacio.strip().upper()
    if tipo_limpio not in ("AULA", "LABORATORIO"):
        tipo_limpio = "AULA"

    cursor.execute(
        """
        SELECT id_espacio FROM espacios_campus
        WHERE nivel = %s AND LOWER(nombre_espacio) = LOWER(%s)
        """,
        (nivel, nombre_limpio),
    )
    fila = cursor.fetchone()
    if fila:
        return fila[0]

    cursor.execute(
        """
        INSERT INTO espacios_campus (nivel, nombre_espacio, tipo_espacio)
        VALUES (%s, %s, %s)
        """,
        (nivel, nombre_limpio, tipo_limpio),
    )
    return cursor.lastrowid


async def guardar_imagen(foto: UploadFile, carpeta: str = "unifix_found") -> tuple[str, bytes]:
    """Sube la imagen a Cloudinary y retorna (url_segura_https, bytes_imagen)."""
    contenido = await foto.read()
    if not contenido:
        raise HTTPException(status_code=400, detail="El archivo de imagen está vacío.")
    try:
        resultado = cloudinary.uploader.upload(
            contenido,
            folder=carpeta,
            public_id=uuid.uuid4().hex,
            resource_type="image",
        )
        return resultado["secure_url"], contenido
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Error al subir la fotografía a Cloudinary: {str(e)}",
        )


def analizar_texto_imagen(imagen_bytes: bytes) -> dict:
    """Extrae texto legible desde bytes y detecta posibles matrículas universitarias."""
    try:
        resultado, _ = motor_ocr(imagen_bytes)
        if not resultado:
            return {"texto_completo": "", "matricula": None}

        lineas = [bloque[1].strip() for bloque in resultado if bloque[2] > 0.60]
        texto_unido = " | ".join(lineas)

        coincidencia_mat = re.search(
            r"\b\d{2,3}[A-Z]{1,2}\d{4,6}\b|\b\d{7,10}\b",
            texto_unido.upper(),
        )
        matricula = coincidencia_mat.group(0) if coincidencia_mat else None
        return {"texto_completo": texto_unido[:500], "matricula": matricula}
    except Exception:
        return {"texto_completo": "", "matricula": None}


# =============================================================================
# ENDPOINTS DE AUTENTICACIÓN (REGISTRO E INICIO DE SESIÓN)
# =============================================================================
@app.post("/api/registro")
def registrar_usuario(
    identificador: str = Form(...),
    nombre: str = Form(...),
    password: str = Form(...),
    rol: str = Form("ESTUDIANTE"),
):
    identificador_limpio = identificador.strip().upper()
    nombre_limpio = nombre.strip()
    rol_limpio = rol.strip().upper()

    if not identificador_limpio or not nombre_limpio or len(password.strip()) < 6:
        raise HTTPException(
            status_code=400,
            detail="Completa todos los campos (contraseña mínimo de 6 caracteres).",
        )

    if rol_limpio not in ("ESTUDIANTE", "TECNICO"):
        raise HTTPException(status_code=400, detail="Rol de usuario no válido.")

    pwd_hash = hashlib.sha256(password.strip().encode("utf-8")).hexdigest()
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            INSERT INTO usuarios (identificador, nombre, password_hash, rol)
            VALUES (%s, %s, %s, %s)
            """,
            (identificador_limpio, nombre_limpio, pwd_hash, rol_limpio),
        )
        conn.commit()
        return {
            "id_usuario": cursor.lastrowid,
            "mensaje": "Cuenta creada correctamente. Ya puedes iniciar sesión.",
        }
    except mysql.connector.IntegrityError:
        raise HTTPException(
            status_code=409,
            detail="Esa matrícula o ID ya se encuentra registrada.",
        )
    finally:
        cursor.close()
        conn.close()


@app.post("/api/login")
def iniciar_sesion(
    identificador: str = Form(...),
    password: str = Form(...),
):
    pwd_hash = hashlib.sha256(password.strip().encode("utf-8")).hexdigest()
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            """
            SELECT id_usuario, identificador, nombre, rol
            FROM usuarios
            WHERE identificador = %s AND password_hash = %s
            """,
            (identificador.strip(), pwd_hash),
        )
        usuario = cursor.fetchone()
        if not usuario:
            raise HTTPException(
                status_code=401,
                detail="Matrícula/ID o contraseña incorrectos.",
            )
        return usuario
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ENDPOINTS DE REPORTES DE FALLAS TI
# =============================================================================
@app.post("/api/fallas")
async def crear_reporte_falla(
    id_usuario: Optional[int] = Form(None),
    nivel: str = Form(...),
    tipo_espacio: str = Form("AULA"),
    espacio: str = Form(...),
    categoria: str = Form(...),
    numero_equipo: str = Form(""),
    descripcion: str = Form(""),
    latitud: float = Form(...),
    longitud: float = Form(...),
    foto: UploadFile = File(...),
):
    validar_ubicacion_campus(latitud, longitud)
    if not espacio.strip():
        raise HTTPException(
            status_code=400,
            detail="Escribe el nombre o número de aula/laboratorio.",
        )

    url_foto, _ = await guardar_imagen(foto, carpeta="unifix_found/fallas")

    conn = get_db()
    cursor = conn.cursor()
    try:
        id_espacio = obtener_o_crear_espacio(cursor, nivel, tipo_espacio, espacio)
        query = """
            INSERT INTO reportes_fallas
            (id_usuario, id_espacio, categoria, numero_equipo, descripcion, url_foto, latitud, longitud)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            query,
            (
                id_usuario,
                id_espacio,
                categoria,
                numero_equipo.strip() or None,
                descripcion.strip(),
                url_foto,
                latitud,
                longitud,
            ),
        )
        conn.commit()
        return {
            "id_falla": cursor.lastrowid,
            "url_foto": url_foto,
            "mensaje": f"Falla registrada en {espacio.strip()} ({nivel}).",
        }
    finally:
        cursor.close()
        conn.close()


@app.get("/api/fallas")
def listar_fallas(nivel: Optional[str] = None, estado: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        sql = """
            SELECT f.id_falla, e.nivel, e.tipo_espacio, e.nombre_espacio, f.categoria,
                   f.numero_equipo, f.descripcion, f.url_foto, f.estado,
                   COALESCE(u.nombre, 'Anónimo') AS reportado_por,
                   DATE_FORMAT(f.fecha_creacion, '%d/%m/%Y %H:%i') AS fecha,
                   DATE_FORMAT(f.fecha_resolucion, '%d/%m/%Y %H:%i') AS fecha_resolucion
            FROM reportes_fallas f
            JOIN espacios_campus e ON f.id_espacio = e.id_espacio
            LEFT JOIN usuarios u ON f.id_usuario = u.id_usuario
            WHERE 1=1
        """
        params = []
        if nivel and nivel != "Todos":
            sql += " AND e.nivel = %s"
            params.append(nivel)
        if estado and estado != "Todos":
            sql += " AND f.estado = %s"
            params.append(estado)
        sql += " ORDER BY f.fecha_creacion DESC"

        cursor.execute(sql, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


@app.patch("/api/fallas/{id_falla}/resolver")
def resolver_falla(
    id_falla: int,
    x_user_role: str = Header(default="ESTUDIANTE"),
):
    if x_user_role.strip().upper() != "TECNICO":
        raise HTTPException(
            status_code=403,
            detail="Solo el personal técnico puede marcar fallas como resueltas.",
        )

    conn = get_db()
    cursor = conn.cursor()
    try:
        try:
            cursor.execute(
                """
                UPDATE reportes_fallas
                SET estado = 'RESUELTO', fecha_resolucion = NOW()
                WHERE id_falla = %s
                """,
                (id_falla,),
            )
        except mysql.connector.Error:
            cursor.execute(
                """
                UPDATE reportes_fallas
                SET estado = 'RESUELTO'
                WHERE id_falla = %s
                """,
                (id_falla,),
            )
        conn.commit()
        if cursor.rowcount == 0:
            raise HTTPException(status_code=404, detail="Falla no encontrada.")
        return {"mensaje": "Falla marcada como resuelta."}
    finally:
        cursor.close()
        conn.close()


# =============================================================================
# ENDPOINTS DE OBJETOS PERDIDOS Y ENCONTRADOS (CON OCR Y CONTROL DE AUTORÍA)
# =============================================================================
@app.post("/api/objetos")
async def crear_reporte_objeto(
    id_usuario: Optional[int] = Form(None),
    tipo: str = Form(...),
    nivel: str = Form(...),
    tipo_espacio: str = Form("AULA"),
    espacio: str = Form(...),
    categoria: str = Form(...),
    descripcion: str = Form(""),
    latitud: float = Form(...),
    longitud: float = Form(...),
    foto: Optional[UploadFile] = File(None),
):
    validar_ubicacion_campus(latitud, longitud)
    if not espacio.strip():
        raise HTTPException(
            status_code=400,
            detail="Escribe el nombre o número de aula/laboratorio.",
        )

    url_foto = None
    texto_ocr = None
    matricula_detectada = None

    if foto and foto.filename:
        url_foto, bytes_foto = await guardar_imagen(foto, carpeta="unifix_found/objetos")
        datos_ocr = analizar_texto_imagen(bytes_foto)
        texto_ocr = datos_ocr["texto_completo"] or None
        matricula_detectada = datos_ocr["matricula"]

    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor_id = conn.cursor()
        id_espacio = obtener_o_crear_espacio(cursor_id, nivel, tipo_espacio, espacio)
        cursor_id.close()

        query = """
            INSERT INTO objetos_campus
            (id_usuario, id_espacio, tipo, categoria, descripcion, texto_ocr, url_foto, latitud, longitud)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
        """
        cursor.execute(
            query,
            (
                id_usuario,
                id_espacio,
                tipo,
                categoria,
                descripcion.strip(),
                texto_ocr,
                url_foto,
                latitud,
                longitud,
            ),
        )
        conn.commit()
        nuevo_id = cursor.lastrowid

        tipo_opuesto = (
            "OBJETO_PERDIDO" if tipo == "OBJETO_ENCONTRADO" else "OBJETO_ENCONTRADO"
        )
        aviso_extra = ""

        if matricula_detectada:
            cursor.execute(
                """
                SELECT id_objeto FROM objetos_campus
                WHERE tipo = %s AND estado = 'ACTIVO' AND texto_ocr LIKE %s
                """,
                (tipo_opuesto, f"%{matricula_detectada}%"),
            )
            if cursor.fetchone():
                aviso_extra = f" ¡Coincidencia exacta con matrícula {matricula_detectada}!"

        if not aviso_extra:
            cursor.execute(
                """
                SELECT o.id_objeto
                FROM objetos_campus o
                JOIN espacios_campus e ON o.id_espacio = e.id_espacio
                WHERE o.tipo = %s AND o.categoria = %s AND e.nivel = %s AND o.estado = 'ACTIVO'
                """,
                (tipo_opuesto, categoria, nivel),
            )
            coincidencias = cursor.fetchall()
            if coincidencias:
                aviso_extra = f" Hay {len(coincidencias)} reporte(s) similar(es) en {nivel}."

        return {
            "id_objeto": nuevo_id,
            "url_foto": url_foto,
            "matricula_ocr": matricula_detectada,
            "mensaje": f"Reporte registrado en {espacio.strip()}.{aviso_extra}",
        }
    finally:
        cursor.close()
        conn.close()


@app.get("/api/objetos")
def listar_objetos(nivel: Optional[str] = None):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        sql = """
            SELECT o.id_objeto, o.id_usuario, e.nivel, e.tipo_espacio, e.nombre_espacio, o.tipo,
                   o.categoria, o.descripcion, o.texto_ocr, o.url_foto, o.estado,
                   COALESCE(u.nombre, 'Anónimo') AS reportado_por,
                   u.identificador AS identificador_reporta,
                   DATE_FORMAT(o.fecha_creacion, '%d/%m/%Y %H:%i') AS fecha
            FROM objetos_campus o
            JOIN espacios_campus e ON o.id_espacio = e.id_espacio
            LEFT JOIN usuarios u ON o.id_usuario = u.id_usuario
            WHERE o.estado = 'ACTIVO'
        """
        params = []
        if nivel and nivel != "Todos":
            sql += " AND e.nivel = %s"
            params.append(nivel)
        sql += " ORDER BY o.fecha_creacion DESC"

        cursor.execute(sql, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()


@app.patch("/api/objetos/{id_objeto}/entregar")
def entregar_objeto(
    id_objeto: int,
    x_user_id: Optional[str] = Header(default=None),
):
    if not x_user_id or not x_user_id.strip().isdigit():
        raise HTTPException(
            status_code=401,
            detail="Debes iniciar sesión para confirmar la entrega del objeto.",
        )

    usuario_solicitante = int(x_user_id.strip())
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT id_usuario, estado FROM objetos_campus WHERE id_objeto = %s",
            (id_objeto,),
        )
        registro = cursor.fetchone()
        if not registro:
            raise HTTPException(status_code=404, detail="Objeto no encontrado.")

        if registro["id_usuario"] is None or int(registro["id_usuario"]) != usuario_solicitante:
            raise HTTPException(
                status_code=403,
                detail="Solo el usuario que publicó este reporte puede marcarlo como entregado.",
            )

        if registro["estado"] == "ENTREGADO":
            raise HTTPException(status_code=400, detail="Este objeto ya fue marcado como entregado.")

        try:
            cursor.execute(
                """
                UPDATE objetos_campus
                SET estado = 'ENTREGADO', fecha_entrega = NOW()
                WHERE id_objeto = %s
                """,
                (id_objeto,),
            )
        except mysql.connector.Error:
            cursor.execute(
                """
                UPDATE objetos_campus
                SET estado = 'ENTREGADO'
                WHERE id_objeto = %s
                """,
                (id_objeto,),
            )
        conn.commit()
        return {"mensaje": "Objeto marcado como entregado."}
    finally:
        cursor.close()
        conn.close()