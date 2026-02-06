#!/usr/bin/env python3
"""
Python Remote Host for RealDesk
Captures screen and streams via WebRTC
Based on the reference documentation in remote_host_setup.md
"""

import asyncio
import json
import logging
import websockets
import argparse
import signal
import sys
from aiortc import RTCPeerConnection, RTCSessionDescription, VideoStreamTrack, RTCIceCandidate, RTCConfiguration, RTCIceServer
from aiortc.contrib.media import MediaPlayer
import cv2
import numpy as np
from av import VideoFrame
import time
import mss
import threading
from queue import Queue

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

class ScreenCaptureTrack(VideoStreamTrack):
    """
    Custom video track for screen capture using mss (cross-platform screen capture)
    """
    
    def __init__(self, fps: int = 30, resolution: tuple = None):
        super().__init__()
        self.fps = fps
        self.resolution = resolution
        self.frame_time = 1.0 / fps
        self.last_frame_time = 0
        self.pts = 0  # Initialize presentation timestamp counter
        
        # Initialize screen capture
        self.sct = mss.mss()
        
        # Get primary monitor info
        self.monitor = self.sct.monitors[1]  # Monitor 1 is usually the primary display
        logger.info(f"Screen capture initialized: {self.monitor['width']}x{self.monitor['height']}")
        
        if self.resolution:
            logger.info(f"Will resize to: {self.resolution[0]}x{self.resolution[1]}")
    
    async def recv(self):
        """Capture and return a video frame"""
        current_time = time.time()
        
        # Maintain target framerate
        time_since_last = current_time - self.last_frame_time
        if time_since_last < self.frame_time:
            await asyncio.sleep(self.frame_time - time_since_last)
        
        try:
            # Capture screen
            screenshot = self.sct.grab(self.monitor)
            
            # Convert to numpy array
            img = np.array(screenshot)
            
            # Convert BGRA to RGB
            img = cv2.cvtColor(img, cv2.COLOR_BGRA2RGB)
            
            # Resize if needed
            if self.resolution:
                img = cv2.resize(img, self.resolution)
            
            # Create video frame
            frame = VideoFrame.from_ndarray(img, format="rgb24")
            frame.pts = self.pts
            frame.time_base = self.fps  # Use fps as time base denominator
            
            # Increment pts for next frame
            self.pts += 1
            self.last_frame_time = current_time
            return frame
            
        except Exception as e:
            logger.error(f"Error capturing screen: {e}")
            # Return a black frame on error
            height, width = (720, 1280) if self.resolution is None else self.resolution[::-1]
            black_frame = np.zeros((height, width, 3), dtype=np.uint8)
            frame = VideoFrame.from_ndarray(black_frame, format="rgb24")
            frame.pts = self.pts
            frame.time_base = self.fps
            
            # Increment pts for next frame
            self.pts += 1
            self.last_frame_time = current_time
            return frame


class RemoteHost:
    """Remote Host implementation for RealDesk"""
    
    def __init__(self, signaling_url: str, room_id: str, client_id: str = None):
        self.signaling_url = signaling_url
        self.room_id = room_id
        self.client_id = client_id or f"host-{int(time.time())}"
        
        self.websocket = None
        self.pc = None
        self.screen_track = None
        self.running = False
        
        # Configuration
        self.fps = 30
        self.resolution = None  # Use native resolution by default
        self.video_codec = "VP8"  # VP8, VP9, H264
        self.video_bitrate = 2000  # kbps
        
    def configure(self, fps: int = 30, resolution: tuple = None, 
                 video_codec: str = "VP8", video_bitrate: int = 2000):
        """Configure video settings"""
        self.fps = fps
        self.resolution = resolution
        self.video_codec = video_codec
        self.video_bitrate = video_bitrate
        logger.info(f"Configuration: {fps}fps, {resolution}, {video_codec}, {video_bitrate}kbps")
        
    async def connect_signaling(self):
        """Connect to the signaling server"""
        logger.info(f"Connecting to signaling server: {self.signaling_url}")
        
        try:
            self.websocket = await websockets.connect(
                self.signaling_url,
                ping_interval=20,
                ping_timeout=10
            )
            logger.info("Connected to signaling server")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to signaling server: {e}")
            return False
    
    async def join_room(self):
        """Join the specified room"""
        if not self.websocket:
            return False
            
        join_message = {
            "type": "join",
            "roomId": self.room_id,
            "clientCaps": {
                "video": True,
                "audio": False,  # Audio not implemented in this example
                "role": "host"
            }
        }
        
        await self.websocket.send(json.dumps(join_message))
        logger.info(f"Joined room: {self.room_id}")
        return True
    
    def create_peer_connection(self):
        """Create and configure RTCPeerConnection"""
        # ICE servers configuration
        ice_servers = [
            RTCIceServer(urls=["stun:stun.l.google.com:19302"]),
            RTCIceServer(urls=["stun:stun1.l.google.com:19302"]),
        ]
        
        configuration = RTCConfiguration(iceServers=ice_servers)
        self.pc = RTCPeerConnection(configuration=configuration)
        
        # Add screen capture track
        self.screen_track = ScreenCaptureTrack(
            fps=self.fps,
            resolution=self.resolution
        )
        self.pc.addTrack(self.screen_track)
        
        # Handle ICE candidates
        @self.pc.on("icecandidate")
        def on_icecandidate(candidate):
            if candidate:
                asyncio.create_task(self.send_ice_candidate(candidate))
        
        # Handle connection state changes
        @self.pc.on("connectionstatechange")
        async def on_connectionstatechange():
            logger.info(f"Connection state changed to: {self.pc.connectionState}")
            
        # Handle ICE connection state changes
        @self.pc.on("iceconnectionstatechange")
        async def on_iceconnectionstatechange():
            logger.info(f"ICE connection state: {self.pc.iceConnectionState}")
            
        logger.info("Peer connection created")
    
    async def send_ice_candidate(self, candidate):
        """Send ICE candidate to signaling server"""
        if not self.websocket:
            return
            
        candidate_message = {
            "type": "candidate",
            "candidate": candidate.candidate,
            "sdpMid": candidate.sdpMid,
            "sdpMLineIndex": candidate.sdpMLineIndex
        }
        
        await self.websocket.send(json.dumps(candidate_message))
    
    async def handle_signaling_message(self, message):
        """Handle incoming signaling messages"""
        try:
            data = json.loads(message)
            msg_type = data.get("type")
            
            if msg_type == "joined":
                logger.info(f"Successfully joined room {data.get('roomId')}")
                
            elif msg_type == "user-joined":
                logger.info(f"New user joined: {data.get('clientId')}")
                
            elif msg_type == "user-left":
                logger.info(f"User left: {data.get('clientId')}")
                
            elif msg_type == "offer":
                await self.handle_offer(data)
                
            elif msg_type == "answer":
                await self.handle_answer(data)
                
            elif msg_type == "candidate":
                await self.handle_ice_candidate(data)
                
            elif msg_type == "pong":
                # Response to ping
                pass
                
            elif msg_type == "error":
                logger.error(f"Signaling error: {data.get('message')}")
                
            else:
                logger.warning(f"Unknown message type: {msg_type}")
                
        except json.JSONDecodeError as e:
            logger.error(f"Invalid JSON received: {e}")
        except Exception as e:
            logger.error(f"Error handling signaling message: {e}")
    
    async def handle_offer(self, data):
        """Handle incoming WebRTC offer"""
        logger.info("Received offer, creating answer...")
        
        # Check if peer connection is in a valid state
        if not self.pc or self.pc.connectionState == "closed":
            logger.error("Cannot handle offer in signaling state \"closed\"")
            return
        
        # Set remote description
        offer = RTCSessionDescription(sdp=data["sdp"], type="offer")
        await self.pc.setRemoteDescription(offer)
        
        # Create and send answer
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        
        answer_message = {
            "type": "answer",
            "sdp": self.pc.localDescription.sdp
        }
        
        await self.websocket.send(json.dumps(answer_message))
        logger.info("Sent answer")
    
    async def handle_answer(self, data):
        """Handle incoming WebRTC answer"""
        logger.info("Received answer")
        
        answer = RTCSessionDescription(sdp=data["sdp"], type="answer")
        await self.pc.setRemoteDescription(answer)
    
    async def handle_ice_candidate(self, data):
        """Handle incoming ICE candidate"""
        # Check if peer connection is in a valid state
        if not self.pc or self.pc.connectionState == "closed":
            logger.error("Cannot handle candidate in signaling state \"closed\"")
            return
            
        try:
            # Handle both Ayame format and direct format
            ice_data = data.get("ice")
            if ice_data:
                # Ayame format: {'type': 'candidate', 'ice': {...}}
                candidate_string = ice_data["candidate"]
                sdp_mid = ice_data["sdpMid"]
                sdp_mline_index = ice_data["sdpMLineIndex"]
            else:
                # Direct format: {'type': 'candidate', 'candidate': '...', ...}
                candidate_string = data["candidate"]
                sdp_mid = data["sdpMid"]
                sdp_mline_index = data["sdpMLineIndex"]
            
            # For aiortc, create RTCIceCandidate with proper parameters
            # Parse the candidate string to extract required fields
            parts = candidate_string.split()
            if len(parts) >= 8:
                component = int(parts[1])
                protocol = parts[2]
                priority = int(parts[3])
                ip = parts[4]
                port = int(parts[5])
                typ = parts[7] if len(parts) > 7 else "host"
                
                candidate = RTCIceCandidate(
                    component=component,
                    foundation=parts[0],
                    ip=ip,
                    port=port,
                    priority=priority,
                    protocol=protocol,
                    type=typ
                )
                candidate.sdpMid = sdp_mid
                candidate.sdpMLineIndex = sdp_mline_index
            else:
                logger.warning(f"Invalid candidate format: {candidate_string}")
                return
            
            await self.pc.addIceCandidate(candidate)
        except Exception as e:
            logger.error(f"Failed to add ICE candidate: {e}")
            # Skip this candidate and continue
    
    async def create_offer(self):
        """Create and send WebRTC offer"""
        logger.info("Creating offer...")
        
        offer = await self.pc.createOffer()
        await self.pc.setLocalDescription(offer)
        
        offer_message = {
            "type": "offer",
            "sdp": self.pc.localDescription.sdp
        }
        
        await self.websocket.send(json.dumps(offer_message))
        logger.info("Sent offer")
    
    async def signaling_loop(self):
        """Main signaling message loop"""
        try:
            async for message in self.websocket:
                await self.handle_signaling_message(message)
        except websockets.exceptions.ConnectionClosed:
            logger.info("Signaling connection closed")
        except Exception as e:
            logger.error(f"Signaling loop error: {e}")
    
    async def run(self):
        """Main run loop"""
        self.running = True
        
        try:
            # Connect to signaling server
            if not await self.connect_signaling():
                return False
            
            # Create peer connection
            self.create_peer_connection()
            
            # Join room
            await self.join_room()
            
            # Wait for clients to connect and initiate offers
            # RealDesk clients will create offers when they detect existing users
            logger.info("Remote host ready, waiting for client offers...")
            
            # Handle signaling messages
            await self.signaling_loop()
            
        except Exception as e:
            logger.error(f"Runtime error: {e}")
        finally:
            await self.cleanup()
    
    async def cleanup(self):
        """Clean up resources"""
        logger.info("Cleaning up...")
        
        self.running = False
        
        if self.pc:
            await self.pc.close()
            
        if self.websocket:
            await self.websocket.close()
        
        if hasattr(self, 'screen_track') and self.screen_track:
            self.screen_track = None
            
        logger.info("Cleanup completed")
    
    async def stop(self):
        """Stop the remote host"""
        await self.cleanup()


async def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(description='RealDesk Remote Host')
    parser.add_argument('--signaling-url', default='ws://localhost:3000',
                       help='Signaling server URL (default: ws://localhost:3000)')
    parser.add_argument('--room-id', default='test-room',
                       help='Room ID to join (default: test-room)')
    parser.add_argument('--client-id',
                       help='Client ID (default: auto-generated)')
    parser.add_argument('--fps', type=int, default=30,
                       help='Target framerate (default: 30)')
    parser.add_argument('--resolution', 
                       help='Target resolution (e.g., 1920x1080)')
    parser.add_argument('--video-codec', default='VP8',
                       choices=['VP8', 'VP9', 'H264'],
                       help='Video codec (default: VP8)')
    parser.add_argument('--video-bitrate', type=int, default=2000,
                       help='Video bitrate in kbps (default: 2000)')
    parser.add_argument('--log-level', default='INFO',
                       choices=['DEBUG', 'INFO', 'WARNING', 'ERROR'],
                       help='Log level (default: INFO)')
    
    args = parser.parse_args()
    
    # Set log level
    logging.getLogger().setLevel(getattr(logging, args.log_level))
    
    # Parse resolution
    resolution = None
    if args.resolution:
        try:
            width, height = map(int, args.resolution.split('x'))
            resolution = (width, height)
        except ValueError:
            logger.error(f"Invalid resolution format: {args.resolution}")
            sys.exit(1)
    
    # Create remote host
    host = RemoteHost(
        signaling_url=args.signaling_url,
        room_id=args.room_id,
        client_id=args.client_id
    )
    
    # Configure video settings
    host.configure(
        fps=args.fps,
        resolution=resolution,
        video_codec=args.video_codec,
        video_bitrate=args.video_bitrate
    )
    
    # Set up signal handlers for graceful shutdown
    def signal_handler(signum, frame):
        logger.info("Received shutdown signal, stopping host...")
        asyncio.create_task(host.stop())
        
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    # Run the host
    try:
        logger.info("Starting RealDesk Remote Host...")
        await host.run()
    except KeyboardInterrupt:
        logger.info("Host interrupted by user")
    except Exception as e:
        logger.error(f"Host error: {e}")
    finally:
        await host.cleanup()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nHost stopped by user")
    except Exception as e:
        print(f"Failed to start host: {e}")
        sys.exit(1)
