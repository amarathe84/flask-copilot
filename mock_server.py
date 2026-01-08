"""
WebSocket server that can send mock data to the FLASK Copilot web UI.
Will serve the web app, if exists.

Supported messages from server to frontend:
    * ``node``: New node
    * ``edge``: New edge
    * ``edge_update``: Update existing edge properties
    * ``complete``: Free up UI for user input
    * ``response``: Server response to user prompt
    * ``error``: Server error message

Supported messages from frontend to server:
    * ``compute``: Start the given computation
    * ``custom_query``: Execute custom user query
"""

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager
import asyncio
import os
import sys
import random
import uuid
from typing import Any
from dataclasses import dataclass, field
from datetime import datetime, timedelta

# Import database components
sys.path.insert(0, os.path.dirname(__file__))
from backend.database.engine import engine, Base
from backend.routers import projects as projrouter
from backend.routers import webui
from backend.routers import sessions as sessionsrouter
from loguru import logger

# Session management for persistent connections
@dataclass
class ComputationSession:
    """Tracks the state of a computation session for resumption."""
    session_id: str
    smiles: str
    problem_type: str
    depth: int = 3
    created_at: datetime = field(default_factory=datetime.now)
    is_complete: bool = False
    is_cancelled: bool = False
    sent_nodes: list = field(default_factory=list)  # IDs of nodes already sent
    sent_edges: list = field(default_factory=list)  # IDs of edges already sent
    pending_nodes: list = field(default_factory=list)  # Positioned nodes to send
    pending_edges: list = field(default_factory=list)  # Edges to send
    current_index: int = 0  # Current position in streaming
    websocket: WebSocket = None  # Current connected websocket

# Global session storage (in production, use Redis or database)
active_sessions: dict[str, ComputationSession] = {}
SESSION_TIMEOUT_HOURS = 24

def cleanup_old_sessions():
    """Remove sessions older than TIMEOUT."""
    cutoff = datetime.now() - timedelta(hours=SESSION_TIMEOUT_HOURS)
    expired = [sid for sid, sess in active_sessions.items() if sess.created_at < cutoff]
    for sid in expired:
        del active_sessions[sid]
        logger.info(f"Cleaned up expired session: {sid}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage database lifecycle"""
    if engine is not None:
        # Startup: Create tables
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            logger.info("Database tables created/verified")

    yield

    if engine is not None:
        # Shutdown: Close connections
        await engine.dispose()
        logger.info("Database connections closed")

app = FastAPI(title="FLASK Copilot Mock Backend", lifespan=lifespan)

# Include database API routes
app.include_router(projrouter.router)
app.include_router(webui.router)
app.include_router(sessionsrouter.router)

# CORS for development
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Note: Frontend serving is now handled by webui router
BUILD_PATH = os.path.join(os.path.dirname(__file__), "flask-app", "build")
STATIC_PATH = os.path.join(BUILD_PATH, "static")

# if os.path.exists(STATIC_PATH):
#     # Serve the frontend
#     app.mount("/static", StaticFiles(directory=STATIC_PATH), name="static")
#
#     @app.get("/")
#     async def root():
#         return FileResponse(os.path.join(BUILD_PATH, "index.html"))


def generate_tree_structure(start_smiles: str, depth: int = 3):
    """
    Generate entire tree structure upfront.
    """
    nodes = []
    edges = []
    node_counter = 0

    ATOMS = ["C", "N", "O", "Br"]

    def build_subtree(parent_smiles, parent_id, level):
        nonlocal node_counter
        if level > depth:
            return

        num_children = random.choice([1, 2])

        for i in range(num_children):
            node_id = f"node_{node_counter}"
            node_counter += 1
            child_smiles = f"{parent_smiles}{ATOMS[i]}"

            node = {
                "id": node_id,
                "smiles": child_smiles,
                "label": f"Molecule-{level}{i}",
                "cost": random.uniform(10, 110),
                "energy": random.uniform(100, 600),
                "yield": random.uniform(0, 100),
                "level": level,
                "parentId": parent_id,
                "hoverInfo": f"# Molecule {level}-{i}\n**SMILES:** `{child_smiles}`\n**Level:** {level}",
            }
            nodes.append(node)

            edge = {
                "id": f"edge_{parent_id}_{node_id}",
                "from": parent_id,
                "to": node_id,
                "reactionType": random.choice(
                    ["Hydrogenation", "Oxidation", "Methylation", "Reduction", "Cyclization"]
                ),
            }
            edges.append(edge)

            build_subtree(child_smiles, node_id, level + 1)

    # Root node
    root_id = "root"
    root = {
        "id": root_id,
        "smiles": start_smiles,
        "label": "Root Molecule",
        "cost": random.uniform(10, 110),
        "energy": random.uniform(100, 600),
        "yield": 2.0,
        "level": 0,
        "parentId": None,
        "hoverInfo": f"# Root Molecule\n**SMILES:** `{start_smiles}`",
    }
    nodes.insert(0, root)

    build_subtree(start_smiles, root_id, 1)

    return nodes, edges


def calculate_positions(nodes: list[dict[str, Any]]):
    """
    Calculate positions for all nodes (matching frontend logic).
    """
    BOX_WIDTH = 220  # Must match with javascript!
    BOX_GAP = 160  # Must match with javascript!
    level_gap = BOX_WIDTH + BOX_GAP
    node_spacing = 150

    # Group by level
    levels = {}
    for node in nodes:
        level = node["level"]
        if level not in levels:
            levels[level] = []
        levels[level].append(node)

    # Position nodes
    positioned = []
    for node in nodes:
        level_nodes = levels[node["level"]]
        index_in_level = level_nodes.index(node)

        positioned_node = {**node, "x": 100 + node["level"] * level_gap, "y": 100 + index_in_level * node_spacing}
        positioned.append(positioned_node)

    return positioned


async def generate_molecules(start_smiles: str, depth: int = 3, websocket: WebSocket = None, session: ComputationSession = None):
    """
    Stream positioned nodes and edges for the retrosynthesis sample.
    Supports session-based resumption.
    """
    
    # Check if resuming existing session
    if session and session.pending_nodes:
        positioned_nodes = session.pending_nodes
        edges = session.pending_edges
        start_index = session.current_index
        logger.info(f"Resuming session {session.session_id} from index {start_index}")
    else:
        # Generate and position entire tree upfront
        nodes, edges_list = generate_tree_structure(start_smiles, depth)
        positioned_nodes = calculate_positions(nodes)
        edges = edges_list
        start_index = 0
        
        # Store in session for potential resume
        if session:
            session.pending_nodes = positioned_nodes
            session.pending_edges = edges

    # Create node map
    node_map = {node["id"]: node for node in positioned_nodes}

    # Stream root first (if not already sent)
    if start_index == 0:
        root = positioned_nodes[0]
        await websocket.send_json({"type": "node", **root})
        if session:
            session.sent_nodes.append(root["id"])
            session.current_index = 1
        await asyncio.sleep(0.8)
        start_index = 1

    # Stream remaining nodes with edges
    for i in range(start_index, len(positioned_nodes)):
        # Check if session was cancelled
        if session and session.is_cancelled:
            logger.info(f"Session {session.session_id} was cancelled")
            return
            
        node = positioned_nodes[i]

        # Find edge for this node
        edge = next((e for e in edges if e["to"] == node["id"]), None)

        if edge:
            # Send edge with computing status
            edge_data = {
                "type": "edge",
                **edge,
                "status": "computing",
                "label": f"Computing: {edge['reactionType']}",
                "fromNode": node_map[edge["from"]],
                "toNode": node,
            }
            await websocket.send_json(edge_data)

            await asyncio.sleep(0.6)

            # Send node
            await websocket.send_json({"type": "node", **node})
            
            if session:
                session.sent_nodes.append(node["id"])
                session.sent_edges.append(edge["id"])
                session.current_index = i + 1

            # Update edge to complete
            edge_complete = {
                "type": "edge_update",
                "id": edge["id"],
                "status": "complete",
                "label": edge["reactionType"],
                "fromNode": node_map[edge["from"]],
                "toNode": node,
            }
            await websocket.send_json(edge_complete)

            await asyncio.sleep(0.2)

    if session:
        session.is_complete = True
    await websocket.send_json({"type": "complete"})


async def lead_molecule(start_smiles: str, depth: int = 3, websocket: WebSocket = None, session: ComputationSession = None):
    """
    Stream positioned nodes and edges for the lead molecule optimization sample.
    Supports session-based resumption.
    """
    
    start_index = session.current_index if session else 0
    
    if session and start_index > 0:
        logger.info(f"Resuming lead_molecule session {session.session_id} from index {start_index}")

    # Generate one node at a time
    for i in range(start_index, depth):
        # Check if session was cancelled
        if session and session.is_cancelled:
            logger.info(f"Session {session.session_id} was cancelled")
            return
            
        if i > 0:
            edge_complete = {
                "type": "edge_update",
                "id": f"edge_{i-1}_{i}",
                "status": "complete",
                "label": "",
                "fromNode": {"id": f"node_{i-1}", "x": 0, "y": 0},
                "toNode": {"id": f"node_{i}", "x": 0, "y": 0},
            }
            await websocket.send_json(edge_complete)
        node = dict(
            id=f"node_{i}",
            smiles=start_smiles + "C" * i,
            label="Water",
            energy=i * random.uniform(0, 16),
            level=0,
            hoverInfo="This is some markdown\n# Hej",
            x=0,
            y=i * 150,
        )
        await websocket.send_json({"type": "node", **node})
        
        if session:
            session.sent_nodes.append(node["id"])
            session.current_index = i + 1
            
        if i == depth - 1:
            break
        edge_data = {
            "type": "edge",
            "id": f"edge_{i}_{i+1}",
            "status": "computing",
            "label": "Optimizing",
            "fromNode": {"id": f"node_{i}", "x": 0, "y": 0},
            "toNode": {"id": f"node_{i+1}", "x": 0, "y": 0},
        }
        await websocket.send_json(edge_data)
        if session:
            session.sent_edges.append(edge_data["id"])
        # TODO: Compute here
        await asyncio.sleep(0.8)

    if session:
        session.is_complete = True
    await websocket.send_json({"type": "complete"})


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    current_session_id = None
    
    # Cleanup old sessions periodically
    cleanup_old_sessions()
    
    try:
        while True:
            data = await websocket.receive_json()
            
            # Handle session resume request
            if data["action"] == "resume_session":
                session_id = data.get("sessionId")
                if session_id and session_id in active_sessions:
                    session = active_sessions[session_id]
                    if not session.is_complete and not session.is_cancelled:
                        current_session_id = session_id
                        session.websocket = websocket
                        logger.info(f"Resuming session {session_id}")
                        
                        # Send session_resumed message with current state
                        await websocket.send_json({
                            "type": "session_resumed",
                            "sessionId": session_id,
                            "sentNodes": len(session.sent_nodes),
                            "totalNodes": len(session.pending_nodes) if session.pending_nodes else 0,
                            "isComplete": session.is_complete
                        })
                        
                        # Resume the computation
                        if session.problem_type == "optimization":
                            await lead_molecule(session.smiles, session.depth, websocket=websocket, session=session)
                        elif session.problem_type == "retrosynthesis":
                            await generate_molecules(session.smiles, session.depth, websocket=websocket, session=session)
                    else:
                        # Session was already complete or cancelled
                        await websocket.send_json({
                            "type": "session_status",
                            "sessionId": session_id,
                            "status": "complete" if session.is_complete else "cancelled"
                        })
                else:
                    # Session not found or expired
                    await websocket.send_json({
                        "type": "session_not_found",
                        "sessionId": session_id
                    })
                continue

            if data["action"] == "compute":
                # Create new session
                session_id = data.get("sessionId") or str(uuid.uuid4())
                depth = data.get("depth", 10 if data["problemType"] == "optimization" else 3)
                
                session = ComputationSession(
                    session_id=session_id,
                    smiles=data["smiles"],
                    problem_type=data["problemType"],
                    depth=depth,
                    websocket=websocket
                )
                active_sessions[session_id] = session
                current_session_id = session_id
                
                # Send session ID to client
                await websocket.send_json({
                    "type": "session_started",
                    "sessionId": session_id
                })
                
                logger.info(f"Started new session {session_id} for {data['problemType']}")
                
                if data["problemType"] == "optimization":
                    await lead_molecule(data["smiles"], depth, websocket=websocket, session=session)
                elif data["problemType"] == "retrosynthesis":
                    await generate_molecules(data["smiles"], depth, websocket=websocket, session=session)
                else:
                    await websocket.send_json(
                        {"type": "error", "message": f"Unsupported problem type {data['problemType']}"}
                    )
                    
            if data["action"] == "stop":
                if current_session_id and current_session_id in active_sessions:
                    active_sessions[current_session_id].is_cancelled = True
                    logger.info(f"Cancelled session {current_session_id}")
                    
            if data["action"] == "custom_query":
                await websocket.send_json(
                    {"type": "response", "message": f"Processing query: {data['query']} for node {data['nodeId']}"}
                )
                await asyncio.sleep(3)  # Random wait
                await websocket.send_json({"type": "complete"})
    except WebSocketDisconnect:
        # Don't delete session on disconnect - allow resume
        if current_session_id:
            logger.info(f"Client disconnected from session {current_session_id} (session preserved for resume)")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8001)
