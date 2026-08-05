import os, re, json, io, shutil, hashlib, secrets
import firebase_admin
from firebase_admin import credentials, firestore
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import pdfplumber
from pypdf import PdfReader, PdfWriter
from pdf2image import convert_from_path
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ── Firebase ──────────────────────────────────────────────
_fb_app = None
def get_firestore():
    global _fb_app
    if _fb_app is None:
        pk = os.environ.get("FIREBASE_PRIVATE_KEY", "").replace("\\n", "\n")
        cred = credentials.Certificate({
            "type": "service_account",
            "project_id": os.environ.get("FIREBASE_PROJECT_ID", ""),
            "private_key": pk,
            "client_email": os.environ.get("FIREBASE_CLIENT_EMAIL", ""),
            "token_uri": "https://oauth2.googleapis.com/token",
        })
        _fb_app = firebase_admin.initialize_app(cred)
    return firestore.client()

def get_keywords_from_fb():
    try:
        db = get_firestore()
        doc = db.collection("config").document("mlArmado").get()
        if doc.exists:
            return doc.to_dict().get("keywords", "")
        return ""
    except Exception:
        return ""

def save_keywords_to_fb(keywords: str):
    try:
        db = get_firestore()
        db.collection("config").document("mlArmado").set({"keywords": keywords}, merge=True)
    except Exception:
        pass

DPI   = 150
SCALE = DPI / 72.0
BASE  = Path(__file__).parent
UPLOADS    = BASE / "uploads"
OUTPUTS    = BASE / "outputs"
STATE_FILE = BASE / "state.json"
USERS_FILE = BASE / "users.json"

UPLOADS.mkdir(exist_ok=True)
OUTPUTS.mkdir(exist_ok=True)

SESSIONS: dict = {}
SESSIONS_FILE = BASE / "sessions.json"

def load_sessions():
    global SESSIONS
    if SESSIONS_FILE.exists():
        try:
            SESSIONS = json.loads(SESSIONS_FILE.read_text())
        except Exception:
            SESSIONS = {}

def save_sessions():
    SESSIONS_FILE.write_text(json.dumps(SESSIONS, ensure_ascii=False))

load_sessions()

app = FastAPI(title="ML Armado Processor")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
app.mount("/static",  StaticFiles(directory=str(BASE / "static")),  name="static")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS)),           name="outputs")

# ── Auth ──────────────────────────────────────────────────
def load_users():
    if not USERS_FILE.exists():
        default = {
            "admin":    hashlib.sha256("admin123".encode()).hexdigest(),
            "operador": hashlib.sha256("ml2024".encode()).hexdigest(),
        }
        USERS_FILE.write_text(json.dumps(default, indent=2))
    return json.loads(USERS_FILE.read_text())

def hash_password(p): return hashlib.sha256(p.encode()).hexdigest()
def verify_login(u, p): return load_users().get(u) == hash_password(p)
def get_current_user(request: Request): return SESSIONS.get(request.cookies.get("session_token"))
def require_auth(request: Request):
    u = get_current_user(request)
    if not u: raise HTTPException(status_code=401, detail="No autorizado")
    return u

# ── State ─────────────────────────────────────────────────
def default_state(today):
    return {
        "date": today,
        "flex_count": 0,    "flex_next": 1,
        "colecta_count": 0, "colecta_next": 1,
        "history": [],
        "keywords": "agitador,agitadores,collarin,collarín,collarines"
    }

def load_state():
    """Lee estado desde Firebase. Si no existe o es de otro día, retorna default."""
    today = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    try:
        db = get_firestore()
        doc = db.collection("config").document("mlArmadoState").get()
        if doc.exists:
            s = doc.to_dict()
            if s.get("date") != today:
                # Nuevo día: preservar keywords pero resetear contadores
                keywords = s.get("keywords", "agitador,agitadores,collarin,collarín,collarines")
                s = default_state(today)
                s["keywords"] = keywords
                save_state(s)
            return s
    except Exception as e:
        print(f"Firebase load_state error: {e}")
    return default_state(today)

def save_state(s):
    """Guarda estado en Firebase (persistente) y opcionalmente en archivo local."""
    try:
        db = get_firestore()
        db.collection("config").document("mlArmadoState").set(s)
    except Exception as e:
        print(f"Firebase save_state error: {e}")
    # Fallback local también
    try:
        STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))
    except Exception:
        pass

# ── Helpers ───────────────────────────────────────────────
def normalize(text):
    return text.lower().replace('í','i').replace('ó','o').replace('á','a').replace('é','e').replace('ú','u')

def extract_label_info_from_text(text):
    """Extract order ID, barcode, and recipient from a colecta label page text."""
    m = re.search(r'(?:Pack ID:|Venta:)\s*(\d+)', text)
    order_id = m.group(1) if m else ""
    m = re.search(r'\b(\d{11})\b', text)
    barcode = m.group(1) if m else ""
    lines = text.split('\n')
    recipient = ""
    for i, line in enumerate(lines):
        if 'Domicilio:' in line:
            for j in range(i - 1, -1, -1):
                candidate = re.sub(r'\s*\([^)]+\)', '', lines[j]).strip()
                if candidate and len(candidate) > 4 and not re.match(r'^[\d\s:>]+$', candidate):
                    recipient = candidate
                    break
            break
    return order_id, barcode, recipient

def keywords_to_label(labels):
    if not labels:
        return ""
    has_agit = any('agitador' in normalize(k) for k in labels)
    has_coll = any('collarin' in normalize(k) for k in labels)
    # Palabras especiales con cartel propio
    if has_agit and has_coll: return "! AGITADORES + COLLARÍN !"
    if has_agit:              return "! AGITADORES !"
    if has_coll:              return "! COLLARÍN !"
    # Cualquier otra keyword: mostrar la palabra encontrada en el cartel
    words = sorted(set(normalize(k) for k in labels))
    label = " + ".join(f"! {w.upper()} !" for w in words)
    return label

def load_font(size):
    candidates = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "C:/Windows/Fonts/arialbd.ttf",
        "C:/Windows/Fonts/calibrib.ttf",
        "C:/Windows/Fonts/verdanab.ttf",
        "C:/Windows/Fonts/tahoma.ttf",
    ]
    for path in candidates:
        try: return ImageFont.truetype(path, size)
        except: continue
    return ImageFont.load_default()

# ── UUID detection ────────────────────────────────────────
def is_uuid(text):
    """Detecta UUIDs tolerando ligadura 'fi' del extractor de PDF."""
    if text.startswith('SKU:') or text.startswith('MEL'):
        return False
    if text.count('-') != 4:
        return False
    if len(text) < 30 or len(text) > 50:
        return False
    allowed = set('0123456789abcdefABCDEFfi-')
    return all(c in allowed for c in text)

# ── PDF Analysis ──────────────────────────────────────────
def split_pages(pdf_path):
    label_pages, order_pages = [], []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if "Identif" in text and "Producto" in text:  # cubre "Producto" y "Productos"
                order_pages.append(i)
            else:
                label_pages.append(i)
    return label_pages, order_pages

def extract_ids_from_labels(pdf_path, label_pages):
    """
    Detecta tipo de envío y extrae IDs de etiquetas.
    Colecta: UUIDs están solo en páginas de armado, no en etiquetas.
    Flex: los IDs numéricos (466xxxxxxx) aparecen en etiquetas.
    """
    envio_ids  = set()
    envio_type = "Colecta"

    with pdfplumber.open(pdf_path) as pdf:
        for i in label_pages:
            text = pdf.pages[i].extract_text() or ""
            words = pdf.pages[i].extract_words()

            if any(k in text for k in ["Envío Flex", "Envio Flex", "FLEX"]):
                envio_type = "Flex"

            for w in words:
                clean = re.sub(r'\s+', '', w['text'])
                nums = re.findall(r'\d{9,12}', clean)
                for n in nums:
                    if len(n) == 11 and n.startswith('4'):  # IDs Flex: 11 dígitos que empiezan con 4x
                        envio_ids.add(n)

    return envio_ids, envio_type

def extract_envio_id_per_label(pdf_path, label_pages):
    """
    Devuelve una lista con el envio ID (str) de cada pagina de etiqueta, en orden.
    Si no se detecta ID en una pagina, devuelve "" para ese indice.
    Solo aplica a Flex (IDs numericos que empiezan con 46).
    """
    result = []
    with pdfplumber.open(pdf_path) as pdf:
        for i in label_pages:
            found = ""
            words = pdf.pages[i].extract_words()
            for w in words:
                clean = re.sub(r'\s+', '', w['text'])
                nums = re.findall(r'\d{9,12}', clean)
                for n in nums:
                    if len(n) == 11 and n.startswith('4'):  # IDs Flex: 11 dígitos que empiezan con 4x
                        found = n
                        break
                if found:
                    break
            result.append(found)
    return result

GAP_THRESHOLD = 8  # pt minimo de separacion vertical para nuevo pedido

def is_order_header(word_text, next_left_word_text):
    """
    Un word de la col izquierda es encabezado de pedido si:
    - es UUID, O
    - la siguiente palabra de la col izquierda empieza con Pack o Venta:
    (esto detecta códigos de transportista como EC3EX20370482, MEL..., etc.)
    """
    if is_uuid(word_text):
        return True
    if next_left_word_text and next_left_word_text.startswith(('Pack', 'Venta:')):
        # Excluir palabras que claramente no son IDs de pedido
        t = word_text
        if any(t.startswith(x) for x in ('SKU:', 'Color:', 'Cantidad:', 'ID:', 'Nombre')):
            return False
        if len(t) < 5:
            return False
        return True
    return False

def get_orders(page, known_ids, keywords, envio_type="Flex"):
    words  = page.extract_words()
    page_h = page.height
    order_ids = []

    if envio_type == "Colecta":
        # Columna izquierda ordenada por posición vertical
        left_words = sorted(
            [w for w in words if w['x0'] < 220],
            key=lambda w: w['top']
        )
        prev_bot = None
        for idx, w in enumerate(left_words):
            next_w = left_words[idx + 1] if idx + 1 < len(left_words) else None
            next_text = next_w['text'] if next_w else None

            if is_order_header(w['text'], next_text):
                gap = (w['top'] - prev_bot) if prev_bot is not None else 999.0
                already = any(o['id'] == w['text'] for o in order_ids)
                if (gap >= GAP_THRESHOLD or not order_ids) and not already:
                    order_ids.append({'id': w['text'], 'top': w['top'],
                                      'id_x1': w['x1']})
            # Actualizar prev_bot con cualquier word de columna izquierda
            if prev_bot is None or w['bottom'] > prev_bot:
                prev_bot = w['bottom']
    else:
        # Flex: anclar en IDs numéricos conocidos de las etiquetas
        for w in words:
            text = w['text']
            nums = re.findall(r'\d{9,12}', text)
            for num in nums:
                if num in known_ids and len(num) == 11 and num.startswith('4') and w['x0'] < 200:
                    if not any(o['id'] == num for o in order_ids):
                        order_ids.append({'id': num, 'top': w['top'],
                                          'id_x1': w.get('x1', 90)})

    order_ids.sort(key=lambda x: x['top'])

    for i, order in enumerate(order_ids):
        order['box_top'] = order['top'] - 6
        if i + 1 < len(order_ids):
            my_words = [w for w in words if order['top'] - 2 <= w['top'] < order_ids[i+1]['top'] - 2]
        else:
            my_words = [w for w in words if w['top'] >= order['top'] - 2]
        order['box_bot'] = (max(w['bottom'] for w in my_words) + 5) if my_words else page_h

        flagged = set()
        for w in my_words:
            tl = normalize(w['text'])
            for kw in keywords:
                if normalize(kw) in tl:
                    flagged.add(kw)
        order['labels'] = flagged

    return order_ids

# ── Rendering ─────────────────────────────────────────────
def add_header_overlay(img, date_str, page_num, total_pages, total_orders, envio_type, offset_y=20):
    draw  = ImageDraw.Draw(img, 'RGBA')
    img_w = img.width
    hh    = int(28 * SCALE)
    y0    = offset_y
    draw.rectangle([0, y0, img_w, y0 + hh], fill=(220, 220, 220, 240))
    draw.line([(0, y0 + hh), (img_w, y0 + hh)], fill=(60, 60, 60, 255), width=2)
    font = load_font(24)
    cy = y0 + hh // 2; pad = 18

    def txt(text, x, anchor="left"):
        bb = font.getbbox(text); th = bb[3]-bb[1]; tw = bb[2]-bb[0]
        if anchor == "right":    x -= tw
        elif anchor == "center": x -= tw // 2
        draw.text((x, cy - th // 2), text, fill=(0, 0, 0, 255), font=font)

    txt(f"Pág. {page_num} de {total_pages}", pad)
    txt(f"{total_orders} pedidos  ·  {envio_type}", img_w // 2, "center")
    txt(date_str, img_w - pad, "right")
    return img

def annotate_page(img, orders, order_number_start=1, font_size_num=30, font_size_lbl=25):
    draw    = ImageDraw.Draw(img, 'RGBA')
    x_left  = int(28.3  * SCALE) - 4
    x_right = int(566.9 * SCALE) + 4
    PROD_COL_X = 260  # products column always starts at ~260pt
    font_num = load_font(font_size_num)
    font_lbl = load_font(font_size_lbl)
    font_log = load_font(max(8, int(font_size_num * 0.38)))  # logistica: 38% del tamaño del número

    for idx, order in enumerate(orders):
        num      = order_number_start + idx
        logistica = order.get('logistica', '')
        y_top  = int(order['box_top'] * SCALE)
        y_bot  = int(order['box_bot'] * SCALE)
        num_cy = y_top + (y_bot - y_top) // 2
        # Center number in the gap between ID col end and products col start
        id_x1  = order.get('id_x1', 88)
        num_cx = int(((id_x1 + PROD_COL_X) / 2) * SCALE)

        def draw_num_and_log(color=(0,0,0,255)):
            nb = font_num.getbbox(str(num)); nw, nh = nb[2]-nb[0], nb[3]-nb[1]
            if logistica:
                lb = font_log.getbbox(logistica); lw, lh = lb[2]-lb[0], lb[3]-lb[1]
                gap = int(2 * SCALE)
                total_h = nh + gap + lh
                num_y = num_cy - total_h // 2
                log_y = num_y + nh + gap
                draw.text((num_cx - nw // 2, num_y), str(num), fill=color, font=font_num)
                draw.text((num_cx - lw // 2, log_y), logistica, fill=color, font=font_log)
            else:
                draw.text((num_cx - nw // 2, num_cy - nh // 2), str(num), fill=color, font=font_num)

        if order['labels']:
            draw.rectangle([x_left, y_top, x_right, y_bot], fill=(200, 200, 200, 80))
            draw.rectangle([x_left, y_top, x_right, y_bot], outline=(0, 0, 0, 255), width=3)
            badge = keywords_to_label(order['labels'])
            if badge:
                bb = font_lbl.getbbox(f"  {badge}  ")
                tw, th = bb[2]-bb[0], bb[3]-bb[1]; pad = 4
                bx2 = x_right; bx1 = bx2 - tw - pad*2
                by2 = y_top;   by1 = by2 - th - pad*2
                if by1 < 0: by1 = y_top; by2 = y_top + th + pad*2
                draw.rectangle([bx1, by1, bx2, by2], fill=(0, 0, 0, 255))
                draw.text((bx1+pad, by1+pad), f"  {badge}  ", fill=(255,255,255), font=font_lbl)
            draw_num_and_log()
        else:
            draw.line([(x_left, y_bot), (x_right, y_bot)], fill=(130,130,130,220), width=2)
            draw_num_and_log()
    return img


def annotate_label_page(img, order_num, font_size_num=30):
    """
    Agrega el número de pedido centrado en el espacio libre superior derecho
    de la etiqueta (derecha del bloque de texto del remitente, arriba de
    la fila FLEX/XBA3-Despachar).
    Zona libre: x 170-283.5pt, y 10-75pt (igual en Flex y Colecta).
    """
    draw = ImageDraw.Draw(img, 'RGBA')
    # Fuente 20% más grande que el armado; auto-reduce si no entra
    font_size = int(font_size_num * 1.2)
    font = load_font(font_size)

    text = str(order_num)

    # Zona libre en píxeles
    zone_x1 = int(170 * SCALE)
    zone_x2 = img.width - int(4 * SCALE)
    zone_y1 = int(10 * SCALE)
    zone_y2 = int(74 * SCALE)
    zone_w = zone_x2 - zone_x1
    zone_h = zone_y2 - zone_y1

    # Auto-reducir fuente si el número no entra (ej. 3 cifras con fuente grande)
    while font_size > 10:
        bb = font.getbbox(text)
        tw, th = bb[2] - bb[0], bb[3] - bb[1]
        if tw <= zone_w and th <= zone_h:
            break
        font_size -= 2
        font = load_font(font_size)

    bb = font.getbbox(text)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]

    # Centrar en la zona libre
    x = zone_x1 + (zone_w - tw) // 2
    y = zone_y1 + (zone_h - th) // 2

    draw.text((x, y), text, fill=(0, 0, 0, 255), font=font)
    return img


def make_number_overlay(number, page_width_pt, page_height_pt, font_size_pt=36, logistica=""):
    """
    Genera un PDF de una página con el número superpuesto como texto vectorial
    en la zona libre superior derecha de la etiqueta.
    Zona libre aprox: x 170-283.5pt, y (page_height - 74) a (page_height - 10)pt
    (reportlab usa origen abajo-izquierda, PDF usa origen arriba-izquierda)
    """
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(page_width_pt, page_height_pt))

    # Zona libre en coordenadas PDF (origen abajo-izquierda)
    zone_x1 = 170
    zone_x2 = page_width_pt - 4
    zone_y1 = page_height_pt - 74   # parte inferior de la zona (top=74 en coords arriba)
    zone_y2 = page_height_pt - 10   # parte superior (top=10 en coords arriba)
    zone_w = zone_x2 - zone_x1
    zone_h = zone_y2 - zone_y1

    text = str(number)

    # Auto-ajustar tamaño para que entre en la zona
    fs = font_size_pt
    while fs > 8:
        c.setFont("Helvetica-Bold", fs)
        tw = c.stringWidth(text, "Helvetica-Bold", fs)
        th = fs * 1.1
        if tw <= zone_w and th <= zone_h:
            break
        fs -= 2

    tw = c.stringWidth(text, "Helvetica-Bold", fs)
    th = fs * 1.1

    # Centrar en la zona
    x = zone_x1 + (zone_w - tw) / 2
    y = zone_y1 + (zone_h - th) / 2

    # Fondo blanco para que el número sea visible sobre cualquier contenido
    pad = 4
    c.setFillColorRGB(1, 1, 1)
    c.rect(x - pad, y - pad * 0.5, tw + pad * 2, th + pad, fill=1, stroke=0)
    # Borde negro fino
    c.setStrokeColorRGB(0, 0, 0)
    c.setLineWidth(0.5)
    c.rect(x - pad, y - pad * 0.5, tw + pad * 2, th + pad, fill=0, stroke=1)

    c.setFillColorRGB(0, 0, 0)
    c.drawString(x, y, text)

    # Logistica debajo del numero (opcional)
    if logistica:
        fs_log = max(8, int(fs * 0.38))
        c.setFont("Helvetica-Bold", fs_log)
        tw_log = c.stringWidth(logistica, "Helvetica-Bold", fs_log)
        x_log = zone_x1 + (zone_w - tw_log) / 2
        y_log = y - fs_log * 1.3
        c.setFillColorRGB(0, 0, 0)
        c.drawString(x_log, y_log, logistica)

    c.save()
    buf.seek(0)
    return buf


def overlay_number_on_label(input_pdf_bytes, page_idx, number, font_size_pt=36, logistica=""):
    """
    Superpone el número como texto vectorial sobre la página de etiqueta indicada.
    Devuelve bytes del PDF resultante.
    """
    reader = PdfReader(io.BytesIO(input_pdf_bytes))
    writer = PdfWriter()

    for i, page in enumerate(reader.pages):
        if i == page_idx:
            w = float(page.mediabox.width)
            h = float(page.mediabox.height)
            overlay_buf = make_number_overlay(number, w, h, font_size_pt=font_size_pt, logistica=logistica)
            overlay_reader = PdfReader(overlay_buf)
            page.merge_page(overlay_reader.pages[0])
        writer.add_page(page)

    out_buf = io.BytesIO()
    writer.write(out_buf)
    out_buf.seek(0)
    return out_buf.read()

# ── Listado generator (labels-only format) ────────────────
def generate_listado_pdf(items, date_str, start_number):
    """Generate a PDF listado from a list of label dicts. Returns bytes."""
    A4_W, A4_H = 595, 842
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(A4_W, A4_H))

    total = len(items)
    end_num = start_number + total - 1
    y = A4_H - 40

    # Title
    c.setFont("Helvetica-Bold", 14)
    c.drawString(30, y, f"Listado de Colecta — {date_str}")
    y -= 18
    c.setFont("Helvetica", 10)
    c.drawString(30, y, f"Total: {total} envíos  |  Números {start_number}–{end_num}")
    y -= 22

    # Column headers
    c.setFillColorRGB(0.3, 0.3, 0.3)
    c.rect(30, y - 4, A4_W - 60, 15, fill=1, stroke=0)
    c.setFillColorRGB(1, 1, 1)
    c.setFont("Helvetica-Bold", 9)
    c.drawString(33, y, "#")
    c.drawString(55, y, "Pack ID / Venta")
    c.drawString(220, y, "Código de barras")
    c.drawString(335, y, "Destinatario")
    c.setFillColorRGB(0, 0, 0)
    y -= 14

    c.setFont("Helvetica", 8)
    for i, item in enumerate(items):
        if y < 40:
            c.showPage()
            y = A4_H - 40
            c.setFont("Helvetica", 8)
        if i % 2 == 0:
            c.setFillColorRGB(0.94, 0.94, 0.94)
            c.rect(30, y - 3, A4_W - 60, 11, fill=1, stroke=0)
            c.setFillColorRGB(0, 0, 0)
        c.drawString(33, y, str(item['num']))
        c.drawString(55, y, str(item['order_id'])[:25])
        c.drawString(220, y, str(item['barcode']))
        c.drawString(335, y, str(item['recipient'])[:38])
        y -= 11

    c.save()
    buf.seek(0)
    return buf.read()


def process_labels_only_pdf(pdf_path, keywords, start_number=1, font_size_num=30, font_size_lbl=25, logistica_map=None):
    """
    Handles colecta PDFs with NO armado pages (labels-only format).
    Numbers each non-blank label page sequentially and appends a generated listado.
    """
    if logistica_map is None:
        logistica_map = {}

    tz = timezone(timedelta(hours=-3))
    now_str = datetime.now(tz).strftime("%d/%m/%Y  %H:%M")

    # Collect non-blank label pages with their info
    page_infos = []
    with pdfplumber.open(pdf_path) as pdf:
        for i, page in enumerate(pdf.pages):
            text = page.extract_text() or ""
            if not text.strip():
                continue
            order_id, barcode, recipient = extract_label_info_from_text(text)
            if order_id:
                page_infos.append({
                    'page_idx': i,
                    'order_id': order_id,
                    'barcode': barcode,
                    'recipient': recipient,
                })

    if not page_infos:
        raise ValueError("No se encontraron etiquetas válidas en el PDF.")

    total_orders = len(page_infos)

    # Overlay sequential numbers on each label page (vectorial)
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    items = []
    for idx, info in enumerate(page_infos):
        num = start_number + idx
        info['num'] = num
        items.append(info)
        logistica = logistica_map.get(info['order_id'], '')
        pdf_bytes = overlay_number_on_label(
            pdf_bytes, info['page_idx'], num,
            font_size_pt=int(font_size_num * 0.9),
            logistica=logistica
        )

    # Generate listado page and append
    listado_bytes = generate_listado_pdf(items, now_str, start_number)

    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    for page in PdfReader(io.BytesIO(listado_bytes)).pages:
        writer.add_page(page)

    out_name = f"armado_{datetime.now(tz).strftime('%Y%m%d_%H%M%S')}.pdf"
    out_path = str(OUTPUTS / out_name)
    with open(out_path, "wb") as f:
        writer.write(f)

    return out_path, {
        "envio_type": "Colecta",
        "total_orders": total_orders,
        "total_pages": 1,
        "start_number": start_number,
        "end_number": start_number + total_orders - 1,
        "flagged": [],
        "filename": out_name,
    }


# ── Main processor ────────────────────────────────────────
def process_pdf(pdf_path, keywords, start_number=1, header_offset=20, font_size_num=30, font_size_lbl=25, logistica_map=None):
    label_pages, order_page_idxs = split_pages(pdf_path)
    if not order_page_idxs:
        # Labels-only colecta format (no armado page) — handle gracefully
        return process_labels_only_pdf(pdf_path, keywords, start_number, font_size_num, font_size_lbl, logistica_map)

    known_ids, envio_type = extract_ids_from_labels(pdf_path, label_pages)
    # Para Colecta los UUIDs están en la página de armado, no en etiquetas → ok si known_ids está vacío
    if not known_ids and envio_type == "Flex":
        raise ValueError("No se pudieron detectar IDs de pedido en las etiquetas (Flex).")

    tz      = timezone(timedelta(hours=-3))
    now_str = datetime.now(tz).strftime("%d/%m/%Y  %H:%M")

    with pdfplumber.open(pdf_path) as pdf:
        all_orders = [get_orders(pdf.pages[i], known_ids, keywords, envio_type) for i in order_page_idxs]

    total_orders = sum(len(o) for o in all_orders)
    total_pages  = len(order_page_idxs)

    if logistica_map is None:
        logistica_map = {}

    # Agregar logistica a cada orden (para mostrarla en el listado de armado)
    for orders in all_orders:
        for order in orders:
            order['logistica'] = logistica_map.get(order['id'], '')

    # Mapa etiqueta → envio ID (para lookup de logistica)
    ids_per_label = extract_envio_id_per_label(pdf_path, label_pages)
    label_page_to_envio_id = {label_pages[i]: ids_per_label[i] for i in range(len(label_pages))}

    # Mapa etiqueta → número de pedido
    label_to_order = {}
    order_counter = start_number
    for orders in all_orders:
        for _ in orders:
            if len(label_to_order) < len(label_pages):
                label_to_order[label_pages[len(label_to_order)]] = order_counter
            order_counter += 1

    # Leer el PDF original como bytes (para overlay vectorial en etiquetas)
    with open(pdf_path, "rb") as f:
        original_pdf_bytes = f.read()

    # Renderizar solo las páginas de armado a imagen (necesitan anotaciones complejas)
    armado_imgs = convert_from_path(pdf_path, dpi=DPI,
                                    first_page=order_page_idxs[0] + 1,
                                    last_page=order_page_idxs[-1] + 1)

    out_name = f"armado_{datetime.now(tz).strftime('%Y%m%d_%H%M%S')}.pdf"
    out_path = str(OUTPUTS / out_name)

    # ── Paso 1: superponer números en etiquetas (vectorial, sin rasterizar) ──
    pdf_bytes = original_pdf_bytes
    for page_idx in label_pages:
        order_num = label_to_order.get(page_idx, "?")
        envio_id = label_page_to_envio_id.get(page_idx, "")
        logistica = logistica_map.get(envio_id, "")
        pdf_bytes = overlay_number_on_label(pdf_bytes, page_idx, order_num,
                                            font_size_pt=int(font_size_num * 0.9),
                                            logistica=logistica)

    # ── Paso 2: reemplazar páginas de armado con versión anotada (imagen) ──
    reader = PdfReader(io.BytesIO(pdf_bytes))
    writer = PdfWriter()

    armado_counter = start_number
    armado_page_num = 0
    armado_img_idx = 0

    for page_idx in range(len(reader.pages)):
        if page_idx in order_page_idxs:
            orders = all_orders[order_page_idxs.index(page_idx)]
            img = armado_imgs[armado_img_idx]
            ann = annotate_page(img.copy(), orders, order_number_start=armado_counter,
                                font_size_num=font_size_num, font_size_lbl=font_size_lbl)
            ann = add_header_overlay(ann, now_str, armado_page_num + 1, total_pages,
                                     total_orders, envio_type, offset_y=header_offset)
            armado_counter += len(orders)
            armado_page_num += 1
            armado_img_idx += 1

            # Convertir imagen anotada a página PDF
            iw_pt = img.width * 72 / DPI
            ih_pt = img.height * 72 / DPI
            img_buf = io.BytesIO()
            ann.save(img_buf, format='PNG')
            img_buf.seek(0)
            page_buf = io.BytesIO()
            pc = canvas.Canvas(page_buf, pagesize=(iw_pt, ih_pt))
            pc.drawImage(ImageReader(img_buf), 0, 0, width=iw_pt, height=ih_pt)
            pc.save()
            page_buf.seek(0)
            armado_page = PdfReader(page_buf).pages[0]
            writer.add_page(armado_page)
        else:
            writer.add_page(reader.pages[page_idx])

    with open(out_path, "wb") as f:
        writer.write(f)

    flagged = []
    num = start_number
    for orders in all_orders:
        for o in orders:
            if o['labels']:
                flagged.append({"num": num, "id": o['id'], "labels": list(o['labels'])})
            num += 1

    return out_path, {
        "envio_type": envio_type, "total_orders": total_orders,
        "total_pages": total_pages, "start_number": start_number,
        "end_number": start_number + total_orders - 1,
        "flagged": flagged, "filename": out_name,
    }

# ── API Routes ────────────────────────────────────────────
@app.get("/")
def index(request: Request):
    return FileResponse(str(BASE / "static" / "index.html"))

@app.post("/api/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not verify_login(username, password):
        # Volver al login con error en query param
        return RedirectResponse(url="/login?error=1", status_code=303)
    token = secrets.token_hex(32)
    SESSIONS[token] = username
    save_sessions()
    response = RedirectResponse(url="/", status_code=303)
    response.set_cookie("session_token", token, httponly=True, max_age=60*60*12, samesite="lax")
    return response

@app.get("/login")
def login_page(request: Request):
    return FileResponse(str(BASE / "static" / "login.html"))

@app.post("/api/logout")
def logout(request: Request):
    token = request.cookies.get("session_token")
    if token:
        SESSIONS.pop(token, None)
        save_sessions()
    response = JSONResponse({"ok": True})
    response.delete_cookie("session_token")
    return response

@app.get("/api/me")
def me(request: Request):
    user = get_current_user(request)
    if not user: raise HTTPException(status_code=401)
    return {"username": user}

@app.get("/api/state")
def get_state():
    s = load_state()
    # Traer keywords de Firebase (tiene prioridad sobre state.json)
    fb_kw = get_keywords_from_fb()
    if fb_kw:
        s["keywords"] = fb_kw
    return s

@app.post("/api/process")
async def process(
    request: Request,
    file: UploadFile = File(...),
    keywords: str      = Form(default="agitador,agitadores,collarin,collarín,collarines"),
    start_number: int  = Form(default=1),
    header_offset: int = Form(default=20),
    font_size_num: int = Form(default=30),
    font_size_lbl: int = Form(default=25),
    logistica_map: str = Form(default="{}"),
):
    pdf_path = str(UPLOADS / file.filename)
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    try:
        lmap = json.loads(logistica_map) if logistica_map else {}
    except Exception:
        lmap = {}
    try:
        out_path, info = process_pdf(pdf_path, kw_list, start_number, header_offset,
                                     font_size_num, font_size_lbl, logistica_map=lmap)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    state = load_state()
    if info["envio_type"] == "Flex":
        state["flex_count"] += info["total_orders"]
        state["flex_next"]   = info["end_number"] + 1
    else:
        state["colecta_count"] += info["total_orders"]
        state["colecta_next"]   = info["end_number"] + 1

    state["history"].insert(0, {
        "filename":     file.filename,
        "output":       info["filename"],
        "envio_type":   info["envio_type"],
        "total_orders": info["total_orders"],
        "start_number": info["start_number"],
        "end_number":   info["end_number"],
        "flagged_count":len(info["flagged"]),
        "time": datetime.now(timezone(timedelta(hours=-3))).strftime("%H:%M"),
    })
    state["history"] = state["history"][:20]
    save_state(state)
    return JSONResponse({**info, "state": state})

@app.get("/api/download/{filename}")
def download(filename: str):
    path = OUTPUTS / filename
    if not path.exists(): raise HTTPException(status_code=404)
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.put("/api/keywords")
async def update_keywords(request: Request):
    body = await request.json()
    kw = body.get("keywords", "")
    # Guardar en Firebase (persistente) y en state.json (local)
    save_keywords_to_fb(kw)
    s = load_state()
    s["keywords"] = kw
    save_state(s)
    return {"ok": True}


@app.post("/api/reset")
def reset_state():
    today = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    save_state(default_state(today))
    return {"ok": True}

@app.put("/api/next-number")
def set_next_number(body: dict):
    state = load_state()
    tipo = body.get("tipo", "flex")
    num  = int(body.get("number", 1))
    if tipo == "flex":
        state["flex_next"] = num
    else:
        state["colecta_next"] = num
    save_state(state)
    return state

