# ML Armado · Procesador

## Deploy en Railway (gratis)

1. Crear cuenta en https://railway.app (con tu cuenta de GitHub o Google)
2. En Railway: New Project → Deploy from GitHub repo
3. Subir esta carpeta a GitHub (ver instrucciones abajo)
4. Railway detecta automáticamente Python y despliega

## Subir a GitHub (necesario para Railway)

1. Crear cuenta en https://github.com
2. New repository → nombre: `ml-armado`
3. Subir todos los archivos de esta carpeta

## Correr localmente (Windows)

```
py -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

Abrir: http://localhost:8000
