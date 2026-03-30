#!/bin/bash
# ML Armado - Script de inicio
echo "Instalando dependencias..."
pip install fastapi uvicorn python-multipart pdfplumber pdf2image Pillow reportlab --break-system-packages -q

echo ""
echo "Iniciando servidor..."
echo "→ Abrí tu browser en: http://localhost:8000"
echo ""
cd "$(dirname "$0")"
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
