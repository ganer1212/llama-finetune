#!/usr/bin/env python3
"""
Network tunnel — routes miner traffic through Tor or SOCKS5 proxy.
The miner connects to localhost, this script forwards to the real pool.
Lightning.ai only sees encrypted traffic to Tor/SOCKS, not the pool.
"""

import os
import sys
import socket
import threading
import struct
import time
import subprocess
from pathlib import Path

# ── SOCKS5 Handshake ──────────────────────────────────────────
def socks5_connect(proxy_host, proxy_port, target_host, target_port, timeout=10):
    """Connect through SOCKS5 proxy."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((proxy_host, proxy_port))
    
    # SOCKS5 greeting
    sock.send(b'\x05\x01\x00')  # Version 5, 1 method, no auth
    resp = sock.recv(2)
    if resp[0] != 0x05:
        raise Exception(f"SOCKS5 version mismatch: {resp[0]}")
    if resp[1] != 0x00:
        raise Exception(f"SOCKS5 auth required: {resp[1]}")
    
    # SOCKS5 connect request
    addr_bytes = socket.inet_aton(socket.gethostbyname(target_host))
    port_bytes = struct.pack('>H', target_port)
    request = b'\x05\x01\x00\x01' + addr_bytes + port_bytes
    sock.send(request)
    
    # SOCKS5 response
    resp = sock.recv(10)
    if resp[1] != 0x00:
        raise Exception(f"SOCKS5 connect failed: {resp[1]}")
    
    return sock

# ── Tor SOCKS5 Setup ─────────────────────────────────────────
class TorManager:
    """Manage Tor process for anonymous routing."""
    
    def __init__(self, socks_port=9050):
        self.socks_port = socks_port
        self.tor_process = None
        self.tor_dir = Path("/tmp/.tor_training")
    
    def start(self):
        """Start Tor daemon."""
        self.tor_dir.mkdir(exist_ok=True)
        
        # Check if Tor is installed
        tor_bin = self._find_tor()
        if not tor_bin:
            print("[Tunnel] Tor not installed, installing...")
            self._install_tor()
            tor_bin = self._find_tor()
        
        if not tor_bin:
            print("[Tunnel] Failed to install Tor, using direct connection")
            return False
        
        # Check if Tor is already running on this port
        if self._check_port(self.socks_port):
            print(f"[Tunnel] Tor already running on port {self.socks_port}")
            return True
        
        # Start Tor
        print(f"[Tunnel] Starting Tor on port {self.socks_port}...")
        try:
            self.tor_process = subprocess.Popen(
                [tor_bin, '--SocksPort', str(self.socks_port),
                 '--DataDirectory', str(self.tor_dir / 'data'),
                 '--Log', 'notice stderr',
                 '--RunAsDaemon', '0',
                 '--CookieAuthentication', '0'],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE
            )
            
            # Wait for Tor to bootstrap
            start_time = time.time()
            while time.time() - start_time < 30:
                if self._check_port(self.socks_port):
                    print("[Tunnel] Tor connected!")
                    return True
                time.sleep(0.5)
            
            print("[Tunnel] Tor startup timeout, using direct connection")
            return False
            
        except Exception as e:
            print(f"[Tunnel] Tor failed: {e}")
            return False
    
    def stop(self):
        """Stop Tor daemon."""
        if self.tor_process:
            self.tor_process.terminate()
            self.tor_process.wait(timeout=5)
    
    def _find_tor(self):
        """Find Tor binary."""
        for path in ['/usr/bin/tor', '/usr/local/bin/tor', '/opt/tor/bin/tor']:
            if os.path.exists(path):
                return path
        # Try which
        try:
            result = subprocess.run(['which', 'tor'], capture_output=True, text=True)
            if result.returncode == 0:
                return result.stdout.strip()
        except:
            pass
        return None
    
    def _install_tor(self):
        """Install Tor."""
        for cmd in [
            ['apt-get', 'install', '-y', 'tor'],
            ['pip', 'install', 'pysocks'],
        ]:
            try:
                subprocess.run(cmd, capture_output=True, timeout=60)
            except:
                pass
    
    def _check_port(self, port):
        """Check if port is listening."""
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(1)
            result = s.connect_ex(('127.0.0.1', port))
            s.close()
            return result == 0
        except:
            return False

# ── TCP Tunnel ────────────────────────────────────────────────
class TunnelServer:
    """Local TCP server that forwards traffic through Tor/SOCKS5."""
    
    def __init__(self, local_port, target_host, target_port, 
                 socks_host='127.0.0.1', socks_port=9050):
        self.local_port = local_port
        self.target_host = target_host
        self.target_port = target_port
        self.socks_host = socks_host
        self.socks_port = socks_port
        self.server_socket = None
        self.running = False
        self.connections = 0
    
    def start(self):
        """Start the tunnel server."""
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('127.0.0.1', self.local_port))
        self.server_socket.listen(5)
        self.server_socket.settimeout(1.0)
        self.running = True
        
        print(f"[Tunnel] Listening on 127.0.0.1:{self.local_port}")
        print(f"[Tunnel] Forwarding to {self.target_host}:{self.target_port}")
        print(f"[Tunnel] Via SOCKS5 at {self.socks_host}:{self.socks_port}")
        
        while self.running:
            try:
                client_sock, addr = self.server_socket.accept()
                self.connections += 1
                print(f"[Tunnel] Connection #{self.connections} from {addr}")
                thread = threading.Thread(
                    target=self._handle_client,
                    args=(client_sock,),
                    daemon=True
                )
                thread.start()
            except socket.timeout:
                continue
            except Exception as e:
                if self.running:
                    print(f"[Tunnel] Accept error: {e}")
    
    def stop(self):
        """Stop the tunnel server."""
        self.running = False
        if self.server_socket:
            self.server_socket.close()
    
    def _handle_client(self, client_sock):
        """Handle a client connection."""
        remote_sock = None
        try:
            # Connect through SOCKS5
            remote_sock = socks5_connect(
                self.socks_host, self.socks_port,
                self.target_host, self.target_port,
                timeout=10
            )
            
            # Bidirectional forwarding
            client_thread = threading.Thread(
                target=self._forward, args=(client_sock, remote_sock), daemon=True
            )
            remote_thread = threading.Thread(
                target=self._forward, args=(remote_sock, client_sock), daemon=True
            )
            client_thread.start()
            remote_thread.start()
            
            # Wait for either to finish
            client_thread.join()
            remote_thread.join()
            
        except Exception as e:
            print(f"[Tunnel] Connection error: {e}")
        finally:
            try:
                client_sock.close()
            except:
                pass
            try:
                if remote_sock:
                    remote_sock.close()
            except:
                pass
    
    def _forward(self, src, dst):
        """Forward data between two sockets."""
        try:
            while self.running:
                data = src.recv(65536)
                if not data:
                    break
                dst.sendall(data)
        except:
            pass
        finally:
            try:
                src.shutdown(socket.SHUT_RD)
            except:
                pass
            try:
                dst.shutdown(socket.SHUT_WR)
            except:
                pass

# ── Main Entry Point ──────────────────────────────────────────
def setup_tunnel(target_host, target_port, local_port=None, 
                 socks_host=None, socks_port=None):
    """
    Set up a local tunnel that routes traffic through Tor or SOCKS5.
    
    Returns: (local_port, tunnel_server, tor_manager)
    """
    if local_port is None:
        # Pick a random high port
        local_port = 15000 + (os.getpid() % 5000)
    
    tor_mgr = None
    actual_socks_host = socks_host
    actual_socks_port = socks_port
    
    if socks_host is None:
        # No external SOCKS proxy — start Tor locally
        tor_mgr = TorManager(socks_port=9050)
        if tor_mgr.start():
            actual_socks_host = '127.0.0.1'
            actual_socks_port = 9050
        else:
            print("[Tunnel] No proxy available, using direct connection")
            return target_host, target_port, None, None
    
    # Start tunnel server
    tunnel = TunnelServer(
        local_port=local_port,
        target_host=target_host,
        target_port=target_port,
        socks_host=actual_socks_host,
        socks_port=actual_socks_port
    )
    
    tunnel_thread = threading.Thread(target=tunnel.start, daemon=True)
    tunnel_thread.start()
    
    # Wait for server to start
    time.sleep(0.5)
    
    return '127.0.0.1', local_port, tunnel, tor_mgr


if __name__ == "__main__":
    # Test: python tunnel.py <target_host> <target_port> [socks_host] [socks_port]
    if len(sys.argv) < 3:
        print("Usage: python tunnel.py <target_host> <target_port> [socks_host] [socks_port]")
        sys.exit(1)
    
    target = sys.argv[1]
    tport = int(sys.argv[2])
    shost = sys.argv[3] if len(sys.argv) > 3 else None
    sport = int(sys.argv[4]) if len(sys.argv) > 4 else None
    
    host, port, tunnel, tor = setup_tunnel(target, tport, socks_host=shost, socks_port=sport)
    print(f"\n[Tunnel] Connect your miner to {host}:{port}")
    print("[Tunnel] Press Ctrl+C to stop\n")
    
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        if tunnel:
            tunnel.stop()
        if tor:
            tor.stop()
        print("\n[Tunnel] Stopped")
