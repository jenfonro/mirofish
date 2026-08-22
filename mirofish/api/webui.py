"""Serve the built WebUI (webui/ Vite output copied into mirofish/static)."""

from __future__ import annotations

import pathlib

from fastapi import APIRouter
from fastapi.responses import FileResponse, HTMLResponse

STATIC_DIR = pathlib.Path(__file__).resolve().parent.parent / "static"

FALLBACK_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Mirofish Relay</title></head>
<body style="font-family:sans-serif;max-width:640px;margin:80px auto;line-height:1.6">
<h1>Mirofish Relay</h1>
<p>WebUI 静态文件尚未构建。请在仓库根目录执行：</p>
<pre>cd webui &amp;&amp; npm install &amp;&amp; npm run build</pre>
<p>API 本身已可用（需要 <code>X-Mirofish-Proxy-Key</code>）。</p>
</body></html>"""

router = APIRouter()


@router.get("/", include_in_schema=False, response_model=None)
@router.get("/index.html", include_in_schema=False, response_model=None)
async def index() -> HTMLResponse | FileResponse:
    index_path = STATIC_DIR / "index.html"
    if index_path.is_file():
        return FileResponse(index_path, media_type="text/html")
    return HTMLResponse(FALLBACK_HTML)
