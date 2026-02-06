#!/usr/bin/env python3
"""
Python WebSocket Signaling Server for RealDesk
Based on the reference documentation in signaling_server_example.md
"""

import asyncio
import json
import logging
import websockets
from collections import defaultdict
from typing import Set
import signal
import sys

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class SignalingServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 3000):
        self.host = host
        self.port = port
        self.rooms = defaultdict(set)  # room_id -> set of websockets
        self.client_rooms = {}  # websocket -> room_id mapping
        
    async def register_client(self, websocket, room_id: str):
        """Register a client in a room"""
        self.rooms[room_id].add(websocket)
        self.client_rooms[websocket] = room_id
        logger.info(f"Client {websocket.remote_address} joined room {room_id}")
        
        # Notify other clients in the room
        await self.broadcast_to_room(room_id, {
            'type': 'user-joined',
            'clientId': id(websocket),
            'timestamp': asyncio.get_event_loop().time()
        }, exclude=websocket)
        
    async def unregister_client(self, websocket):
        """Unregister a client from all rooms"""
        if websocket in self.client_rooms:
            room_id = self.client_rooms[websocket]
            self.rooms[room_id].discard(websocket)
            del self.client_rooms[websocket]
            
            # Clean up empty rooms
            if len(self.rooms[room_id]) == 0:
                del self.rooms[room_id]
                
            logger.info(f"Client {websocket.remote_address} left room {room_id}")
            
            # Notify other clients in the room
            await self.broadcast_to_room(room_id, {
                'type': 'user-left',
                'clientId': id(websocket),
                'timestamp': asyncio.get_event_loop().time()
            })
            
    async def broadcast_to_room(self, room_id: str, message: dict, exclude=None):
        """Broadcast a message to all clients in a room"""
        if room_id not in self.rooms:
            return
            
        message_str = json.dumps(message)
        disconnected = []
        
        for client in self.rooms[room_id]:
            if client != exclude:
                try:
                    await client.send(message_str)
                except websockets.exceptions.ConnectionClosed:
                    disconnected.append(client)
                except Exception as e:
                    logger.error(f"Error sending message to client: {e}")
                    disconnected.append(client)
        
        # Clean up disconnected clients
        for client in disconnected:
            await self.unregister_client(client)
            
    async def handle_client(self, websocket):
        """Handle a WebSocket client connection"""
        logger.info(f"New client connected: {websocket.remote_address}")
        
        try:
            async for message in websocket:
                try:
                    data = json.loads(message)
                    await self.handle_message(websocket, data)
                except json.JSONDecodeError as e:
                    logger.error(f"Invalid JSON from {websocket.remote_address}: {e}")
                    await websocket.send(json.dumps({
                        'type': 'error',
                        'message': 'Invalid JSON format'
                    }))
                except Exception as e:
                    logger.error(f"Error handling message from {websocket.remote_address}: {e}")
                    
        except websockets.exceptions.ConnectionClosed:
            logger.info(f"Client {websocket.remote_address} disconnected")
        except Exception as e:
            logger.error(f"Unexpected error with client {websocket.remote_address}: {e}")
        finally:
            await self.unregister_client(websocket)
            
    async def handle_message(self, websocket, data: dict):
        """Handle incoming messages from clients"""
        msg_type = data.get('type')
        
        if msg_type == 'join':
            room_id = data.get('roomId')
            if not room_id:
                await websocket.send(json.dumps({
                    'type': 'error',
                    'message': 'roomId is required for join'
                }))
                return
                
            await self.register_client(websocket, room_id)
            await websocket.send(json.dumps({
                'type': 'joined',
                'roomId': room_id,
                'clientId': id(websocket)
            }))
            
        elif msg_type == 'register':
            # Handle Ayame-style register message from RealDesk Flutter client
            room_id = data.get('roomId')
            client_id = data.get('clientId')
            if not room_id:
                await websocket.send(json.dumps({
                    'type': 'error',
                    'message': 'roomId is required for register'
                }))
                return
            
            # Check if there are existing users in the room
            is_exist_user = len(self.rooms.get(room_id, set())) > 0
                
            await self.register_client(websocket, room_id)
            # Send accept response in Ayame format with isExistUser flag
            await websocket.send(json.dumps({
                'type': 'accept',
                'roomId': room_id,
                'clientId': client_id,
                'isExistUser': is_exist_user,
                'iceServers': [
                    {'urls': ['stun:stun.l.google.com:19302']},
                    {'urls': ['stun:stun1.l.google.com:19302']}
                ]
            }))
            
        elif msg_type == 'offer':
            room_id = self.client_rooms.get(websocket)
            if room_id:
                await self.broadcast_to_room(room_id, {
                    'type': 'offer',
                    'sdp': data.get('sdp'),
                    'from': id(websocket)
                }, exclude=websocket)
            else:
                await websocket.send(json.dumps({
                    'type': 'error',
                    'message': 'Must join a room before sending offer'
                }))
                
        elif msg_type == 'answer':
            room_id = self.client_rooms.get(websocket)
            if room_id:
                await self.broadcast_to_room(room_id, {
                    'type': 'answer',
                    'sdp': data.get('sdp'),
                    'from': id(websocket)
                }, exclude=websocket)
            else:
                await websocket.send(json.dumps({
                    'type': 'error',
                    'message': 'Must join a room before sending answer'
                }))
                
        elif msg_type == 'candidate':
            room_id = self.client_rooms.get(websocket)
            if room_id:
                # Handle both direct candidate format and Ayame ICE format
                ice_data = data.get('ice')
                if ice_data:
                    # Ayame format: {'type': 'candidate', 'ice': {...}}
                    candidate_data = {
                        'type': 'candidate',
                        'ice': ice_data,
                        'from': id(websocket)
                    }
                else:
                    # Direct format: {'type': 'candidate', 'candidate': '...', ...}
                    candidate_data = {
                        'type': 'candidate',
                        'candidate': data.get('candidate'),
                        'sdpMid': data.get('sdpMid'),
                        'sdpMLineIndex': data.get('sdpMLineIndex'),
                        'from': id(websocket)
                    }
                    
                await self.broadcast_to_room(room_id, candidate_data, exclude=websocket)
            else:
                await websocket.send(json.dumps({
                    'type': 'error',
                    'message': 'Must join a room before sending candidate'
                }))
                
        elif msg_type == 'ping':
            await websocket.send(json.dumps({'type': 'pong'}))
            
        else:
            logger.warning(f"Unknown message type '{msg_type}' from {websocket.remote_address}")
            await websocket.send(json.dumps({
                'type': 'error',
                'message': f'Unknown message type: {msg_type}'
            }))
            
    async def start_server(self):
        """Start the signaling server"""
        logger.info(f"Starting signaling server on {self.host}:{self.port}")
        
        # Set up signal handlers for graceful shutdown
        def signal_handler(signum, frame):
            logger.info("Received shutdown signal, stopping server...")
            asyncio.create_task(self.stop_server())
            
        signal.signal(signal.SIGINT, signal_handler)
        signal.signal(signal.SIGTERM, signal_handler)
        
        async with websockets.serve(
            self.handle_client,
            self.host,
            self.port,
            ping_interval=20,
            ping_timeout=10,
            max_size=2**20,  # 1MB max message size
            compression=None  # Disable compression for lower latency
        ):
            logger.info(f"Signaling server running on ws://{self.host}:{self.port}")
            await asyncio.Future()  # Run forever
            
    async def stop_server(self):
        """Stop the server gracefully"""
        logger.info("Stopping signaling server...")
        # Close all client connections
        for websocket in list(self.client_rooms.keys()):
            await websocket.close()
        logger.info("Signaling server stopped")


async def main():
    """Main entry point"""
    import argparse
    
    parser = argparse.ArgumentParser(description='RealDesk Signaling Server')
    parser.add_argument('--host', default='0.0.0.0', help='Host to bind to (default: 0.0.0.0)')
    parser.add_argument('--port', type=int, default=3000, help='Port to bind to (default: 3000)')
    parser.add_argument('--log-level', default='INFO', 
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Log level (default: INFO)')
    
    args = parser.parse_args()
    
    # Set log level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    
    # Create and start server
    server = SignalingServer(args.host, args.port)
    
    try:
        await server.start_server()
    except KeyboardInterrupt:
        logger.info("Server interrupted by user")
    except Exception as e:
        logger.error(f"Server error: {e}")
    finally:
        await server.stop_server()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nServer stopped by user")
    except Exception as e:
        print(f"Failed to start server: {e}")
        sys.exit(1)
