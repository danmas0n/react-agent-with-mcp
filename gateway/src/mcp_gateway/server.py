"""MCP Gateway Server.

This module implements a gateway server that:
1. Exposes an SSE endpoint for clients to connect
2. Reads MCP server configurations
3. Forwards requests to appropriate MCP servers
4. Aggregates responses back to clients
"""

import asyncio
import json
import os
import logging
import signal
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from mcp.types import Tool

# Set up logging
logging.basicConfig(level=logging.DEBUG)  # Set to DEBUG for more verbose output
logger = logging.getLogger(__name__)

app = FastAPI()


@dataclass
class MCPServerConfig:
    """Configuration for an MCP server."""
    command: str
    args: List[str]


@dataclass
class MCPServer:
    """Represents a running MCP server."""
    name: str
    config: MCPServerConfig
    process: asyncio.subprocess.Process
    tools: List[Dict] = field(default_factory=list)


class Gateway:
    """MCP Gateway that manages server connections and forwards requests."""
    
    def __init__(self):
        self.servers: Dict[str, MCPServer] = {}
        
    async def _communicate_with_server(self, server: MCPServer, method: str, params: dict = None) -> Any:
        """Send a request to a server and get the response."""
        if not server.process.stdin or not server.process.stdout:
            raise Exception("Server process pipes not available")
            
        try:
            # Prepare request
            request = {
                "jsonrpc": "2.0",
                "method": method,
                "params": params or {},
                "id": 1
            }
            request_str = json.dumps(request) + "\n"
            logger.debug(f"Sending request to {server.name}: {request_str.strip()}")
            
            # Send request
            server.process.stdin.write(request_str.encode())
            await server.process.stdin.drain()
            
            # Read response
            response_line = await server.process.stdout.readline()
            if not response_line:
                raise Exception("Empty response")
                
            response_str = response_line.decode().strip()
            logger.debug(f"Received response from {server.name}: {response_str}")
            
            response = json.loads(response_str)
            if "error" in response:
                raise Exception(response["error"])
                
            return response.get("result")
            
        except Exception as e:
            logger.error(f"Error communicating with {server.name}: {str(e)}")
            raise
        
    async def start_server(self, name: str, config: MCPServerConfig) -> MCPServer:
        """Start an MCP server and initialize its client session."""
        try:
            logger.info(f"Starting MCP server: {name}")
            
            # Construct command
            cmd = f"{config.command} {' '.join(config.args)}"
            logger.debug(f"Running command: {cmd}")
            
            # Start the server process in the background
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                preexec_fn=os.setsid  # Create new process group
            )
            
            # Create server object
            server = MCPServer(
                name=name,
                config=config,
                process=process
            )
            
            # Wait a bit for server to initialize
            await asyncio.sleep(2)
            
            # Query available tools
            try:
                result = await self._communicate_with_server(server, "tools/list")
                server.tools = result.get("tools", [])
                logger.info(f"Server {name} provides tools: {[t['name'] for t in server.tools]}")
            except Exception as e:
                logger.error(f"Error querying tools from {name}: {str(e)}")
                server.tools = []
            
            self.servers[name] = server
            
            # Start monitoring stderr in background
            asyncio.create_task(self._monitor_stderr(server))
            
            return server
            
        except Exception as e:
            logger.error(f"Error starting server {name}: {str(e)}")
            raise
    
    async def _monitor_stderr(self, server: MCPServer):
        """Monitor server's stderr output."""
        while True:
            if server.process.stderr:
                try:
                    line = await server.process.stderr.readline()
                    if line:
                        logger.debug(f"[{server.name}] {line.decode().strip()}")
                    else:
                        break
                except Exception as e:
                    logger.error(f"Error reading stderr from {server.name}: {str(e)}")
                    break
    
    async def start_all_servers(self, config_path: str) -> None:
        """Start all configured MCP servers."""
        try:
            logger.info(f"Loading config from: {config_path}")
            # Read config file
            with open(config_path) as f:
                config = json.load(f)
            
            if not config.get('mcp', {}).get('servers'):
                raise ValueError("No MCP servers configured in config file")
                
            # Start each configured server in parallel
            tasks = []
            for name, server_config in config['mcp']['servers'].items():
                task = asyncio.create_task(
                    self.start_server(
                        name,
                        MCPServerConfig(**server_config)
                    )
                )
                tasks.append(task)
            
            # Wait for all servers to start
            await asyncio.gather(*tasks, return_exceptions=True)
            
        except Exception as e:
            logger.error(f"Error starting servers: {str(e)}")
            raise
    
    async def list_all_tools(self) -> List[Dict[str, Any]]:
        """Get all available tools from all servers."""
        tools = []
        for server in self.servers.values():
            for tool in server.tools:
                tool_dict = {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "server": server.name
                }
                if "input_schema" in tool:
                    tool_dict["input_schema"] = tool["input_schema"]
                tools.append(tool_dict)
        return tools
    
    async def call_tool(self, tool_name: str, arguments: dict) -> Any:
        """Call a tool on the appropriate server."""
        # Find server that has this tool
        for server in self.servers.values():
            if any(t["name"] == tool_name for t in server.tools):
                try:
                    return await self._communicate_with_server(
                        server,
                        "tools/call",
                        {
                            "name": tool_name,
                            "arguments": arguments
                        }
                    )
                except Exception as e:
                    logger.error(f"Error calling tool {tool_name}: {str(e)}")
                    raise
        raise ValueError(f"Tool {tool_name} not found")
    
    async def shutdown(self) -> None:
        """Shutdown all MCP servers."""
        for server in self.servers.values():
            if server.process:
                try:
                    # Kill entire process group
                    os.killpg(os.getpgid(server.process.pid), signal.SIGTERM)
                    await server.process.wait()
                except Exception as e:
                    logger.error(f"Error shutting down server {server.name}: {str(e)}")
                    try:
                        os.killpg(os.getpgid(server.process.pid), signal.SIGKILL)
                    except:
                        pass
        self.servers.clear()


# Global gateway instance
gateway = Gateway()


@app.on_event("startup")
async def startup():
    """Initialize the gateway on startup."""
    config_path = os.environ.get("MCP_CONFIG", "langgraph.json")
    logger.info("Starting MCP Gateway Server")
    await gateway.start_all_servers(config_path)


@app.on_event("shutdown")
async def shutdown():
    """Cleanup on shutdown."""
    logger.info("Shutting down MCP Gateway Server")
    await gateway.shutdown()


@app.post("/message")
async def message_endpoint(request: Request):
    """Handle incoming messages from clients."""
    try:
        msg = await request.json()
        logger.debug(f"Received message: {msg}")
        
        if msg.get("method") == "tools/list":
            tools = await gateway.list_all_tools()
            response = {"tools": tools}
            logger.debug(f"Returning tools: {response}")
            return JSONResponse(response)
        
        elif msg.get("method") == "tools/call":
            params = msg.get("params", {})
            result = await gateway.call_tool(
                params.get("name"),
                params.get("arguments", {})
            )
            logger.debug(f"Tool call result: {result}")
            return JSONResponse(result)
        
        return JSONResponse({"error": "Unknown method"}, status_code=400)
    except Exception as e:
        logger.error(f"Error handling message: {str(e)}")
        return JSONResponse({"error": str(e)}, status_code=500)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("MCP_PORT", "8808"))
    uvicorn.run(app, host="0.0.0.0", port=port)
