################################################################################
## Copyright 2025 Lawrence Livermore National Security, LLC.
## See the top-level LICENSE file for details.
##
## SPDX-License-Identifier: Apache-2.0
################################################################################
"""
Web UI (``/``) routing
"""
from fastapi import Request, APIRouter
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import os
from loguru import logger

router = APIRouter(tags=["webui"])

BUILD_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "flask-app", "build")
STATIC_PATH = os.path.join(BUILD_PATH, "static")

if os.path.exists(STATIC_PATH):
    # Serve the frontend
    router.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")

    @router.get("/")
    async def root(request: Request):
        logger.info(f"Request for Web UI received. Headers: {str(request.headers)}")
        return FileResponse(os.path.join(BUILD_PATH, "index.html"))
