from __future__ import annotations
from pathlib import Path
import os
from fastapi import HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from .app import create_app
from .config import Settings
from .routers import assistant_ext,research_ext,import_ext,operations_ext,account_ext

local_origins=('http://127.0.0.1:8788','http://localhost:8788','http://127.0.0.1:5178','http://localhost:5178')
settings=Settings.from_env(**({} if os.getenv('JIAOQI_ALLOWED_ORIGINS') else {'allowed_origins':local_origins}),plan_horizon_days=90)
app=create_app(settings)
app.include_router(assistant_ext.router)
app.include_router(research_ext.router)
app.include_router(import_ext.router)
app.include_router(operations_ext.router)
app.include_router(account_ext.router)
dist=Path(__file__).resolve().parent.parent/'frontend/dist'
if (dist/'assets').exists():app.mount('/assets',StaticFiles(directory=dist/'assets'),name='assets')
@app.get('/{path:path}',include_in_schema=False)
def spa(path:str):
    if path.startswith('api/') or path=='api':raise HTTPException(404,'接口不存在')
    if not (dist/'index.html').exists():raise HTTPException(503,'前端尚未构建')
    return FileResponse(dist/'index.html')
