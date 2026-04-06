import os, re, json, io, shutil, hashlib, secrets
from datetime import datetime, timezone, timedelta
from pathlib import Path

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, Depends
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware

import pdfplumber
from pdf2image import convert_from_path
from PIL import Image, ImageDraw, ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

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
def load_state():
    today = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    if STATE_FILE.exists():
        s = json.loads(STATE_FILE.read_text())
        if s.get("date") != today:
            s = default_state(today)
    else:
        s = default_state(today)
    return s

def default_state(today):
    return {
        "date": today,
        "flex_count": 0,    "flex_next": 1,
        "colecta_count": 0, "colecta_next": 1,
        "history": []
    }

def save_state(s): STATE_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2))

# ── Helpers ───────────────────────────────────────────────
def normalize(text):
    return text.lower().replace('í','i').replace('ó','o').replace('á','a').replace('é','e').replace('ú','u')

def keywords_to_label(labels):
    has_agit = any('agitador' in normalize(k) for k in labels)
    has_coll = any('collarin' in normalize(k) for k in labels)
    if has_agit and has_coll: return "! AGITADORES + COLLARÍN !"
    if has_agit:              return "! AGITADORES !"
    if has_coll:              return "! COLLARÍN !"
    return ""

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
                    if n.startswith('46'):  # solo IDs Flex reales
                        envio_ids.add(n)

    return envio_ids, envio_type

GAP_THRESHOLD = 8  # pt mínimo de separación vertical para nuevo pedido

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
                if num in known_ids and num.startswith('46') and w['x0'] < 200:
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

    for idx, order in enumerate(orders):
        num    = order_number_start + idx
        y_top  = int(order['box_top'] * SCALE)
        y_bot  = int(order['box_bot'] * SCALE)
        num_cy = y_top + (y_bot - y_top) // 2
        # Center number in the gap between ID col end and products col start
        id_x1  = order.get('id_x1', 88)
        num_cx = int(((id_x1 + PROD_COL_X) / 2) * SCALE)

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
            nb = font_num.getbbox(str(num)); nw,nh = nb[2]-nb[0], nb[3]-nb[1]
            draw.text((num_cx-nw//2, num_cy-nh//2), str(num), fill=(0,0,0,255), font=font_num)
        else:
            draw.line([(x_left, y_bot), (x_right, y_bot)], fill=(130,130,130,220), width=2)
            nb = font_num.getbbox(str(num)); nw,nh = nb[2]-nb[0], nb[3]-nb[1]
            draw.text((num_cx-nw//2, num_cy-nh//2), str(num), fill=(0,0,0,255), font=font_num)
    return img

# ── Main processor ────────────────────────────────────────
def process_pdf(pdf_path, keywords, start_number=1, header_offset=20, font_size_num=30, font_size_lbl=25):
    label_pages, order_page_idxs = split_pages(pdf_path)
    if not order_page_idxs:
        raise ValueError("No se encontraron páginas de armado en el PDF.")

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

    pages_img = convert_from_path(pdf_path, dpi=DPI,
                                  first_page=order_page_idxs[0]+1,
                                  last_page=order_page_idxs[-1]+1)

    PAGE_W, PAGE_H = 595.28, 841.89
    out_name = f"armado_{datetime.now(tz).strftime('%Y%m%d_%H%M%S')}.pdf"
    out_path = str(OUTPUTS / out_name)
    c = canvas.Canvas(out_path, pagesize=(PAGE_W, PAGE_H))

    counter = start_number
    for idx, (img, orders) in enumerate(zip(pages_img, all_orders)):
        ann = annotate_page(img.copy(), orders, order_number_start=counter,
                            font_size_num=font_size_num, font_size_lbl=font_size_lbl)
        ann = add_header_overlay(ann, now_str, idx+1, total_pages,
                                 total_orders, envio_type, offset_y=header_offset)
        iw, ih = ann.size
        scale  = min(PAGE_W/iw, PAGE_H/ih)
        dw, dh = iw*scale, ih*scale
        buf = io.BytesIO(); ann.save(buf, format='PNG'); buf.seek(0)
        c.drawImage(ImageReader(buf), (PAGE_W-dw)/2, (PAGE_H-dh)/2, width=dw, height=dh)
        c.showPage()
        counter += len(orders)
    c.save()

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
    if not get_current_user(request):
        return FileResponse(str(BASE / "static" / "login.html"))
    return FileResponse(str(BASE / "static" / "index.html"))

@app.post("/api/login")
async def login(request: Request, username: str = Form(...), password: str = Form(...)):
    if not verify_login(username, password):
        return JSONResponse({"ok": False, "error": "Usuario o contraseña incorrectos"}, status_code=401)
    token = secrets.token_hex(32)
    SESSIONS[token] = username
    save_sessions()
    response = JSONResponse({"ok": True, "username": username})
    response.set_cookie("session_token", token, httponly=True, max_age=60*60*12, samesite="lax")
    return response

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
def get_state(user: str = Depends(require_auth)):
    return load_state()

@app.post("/api/process")
async def process(
    request: Request,
    file: UploadFile = File(...),
    keywords: str      = Form(default="agitador,agitadores,collarin,collarín,collarines"),
    start_number: int  = Form(default=1),
    header_offset: int = Form(default=20),
    font_size_num: int = Form(default=30),
    font_size_lbl: int = Form(default=25),
    user: str = Depends(require_auth),
):
    pdf_path = str(UPLOADS / file.filename)
    with open(pdf_path, "wb") as f:
        shutil.copyfileobj(file.file, f)
    kw_list = [k.strip() for k in keywords.split(",") if k.strip()]
    try:
        out_path, info = process_pdf(pdf_path, kw_list, start_number, header_offset,
                                     font_size_num, font_size_lbl)
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
def download(filename: str, user: str = Depends(require_auth)):
    path = OUTPUTS / filename
    if not path.exists(): raise HTTPException(status_code=404)
    return FileResponse(str(path), media_type="application/pdf",
                        headers={"Content-Disposition": f"attachment; filename={filename}"})

@app.post("/api/reset")
def reset_state(user: str = Depends(require_auth)):
    today = datetime.now(timezone(timedelta(hours=-3))).strftime("%Y-%m-%d")
    save_state(default_state(today))
    return {"ok": True}

@app.put("/api/next-number")
def set_next_number(body: dict, user: str = Depends(require_auth)):
    state = load_state()
    tipo = body.get("tipo", "flex")
    num  = int(body.get("number", 1))
    if tipo == "flex":
        state["flex_next"] = num
    else:
        state["colecta_next"] = num
    save_state(state)
    return state
