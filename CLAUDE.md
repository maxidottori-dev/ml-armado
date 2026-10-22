# ml-armado — instrucciones para Claude

## Acceso a internet / git push

Este sandbox accede a GitHub **únicamente** a través del proxy de CCR.
Antes de cualquier `git push` (o `git fetch`/`git pull`), configurar:

```bash
# Leer la config del proxy y exportar las variables
source <(cat /root/.ccr/proxy.env 2>/dev/null || true)

# Si proxy.env no existe, setear manualmente con el puerto activo:
export HTTPS_PROXY="http://localhost:$(curl -s "$HTTPS_PROXY/__agentproxy/status" | python3 -c "import sys,json;print(json.load(sys.stdin)['port'])" 2>/dev/null || echo 36745)"
export GIT_SSL_CAINFO=/root/.ccr/ca-bundle.crt
```

O más simple, usar `gh` CLI que ya viene preconfigurado:

```bash
gh repo sync          # en vez de git pull
# No hay gh equivalente para push, pero git push funciona con las vars de arriba
```

### Verificar que el proxy está activo

```bash
curl -sS "$HTTPS_PROXY/__agentproxy/status" | python3 -m json.tool
```

Si `enabled: true` → el proxy está disponible y solo falta exportar las variables.

## Stack

- Python / FastAPI
- pdfplumber, pdf2image, Pillow, reportlab
- DPI=150, SCALE=150/72
- Ver project instructions en Claude para las reglas de procesamiento de PDFs
