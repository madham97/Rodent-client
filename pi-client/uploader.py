#!/usr/bin/env python3
"""
Pi Monitoring Pipeline - Video Uploader Client
Monitors /outbox/ directory and uploads videos to remote server via GSM
"""

import os
import sys
import time
import logging
import requests
from pathlib import Path
from datetime import datetime
import shutil
import serial
import subprocess

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/monitoring-pipeline.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class GSMModem:
    """Manage GSM modem connection for GPRS uploads."""
    
    def __init__(self, device='/dev/serial0', baud=115200, pin="0427"):
        self.device = device
        self.baud = baud
        self.pin = pin
        self.connected = False
    
    def send_at_command(self, cmd, timeout=1):
        """Send AT command and return response."""
        try:
            with serial.Serial(self.device, self.baud, timeout=timeout) as ser:
                ser.write(f'{cmd}\r'.encode())
                time.sleep(0.2)
                resp = ser.read(ser.in_waiting or 500)
                return resp.decode('ascii', errors='ignore').strip()
        except Exception as e:
            logger.error(f"AT command '{cmd}' failed: {e}")
            return None
    
    def check_connection(self):
        """Check if modem is connected to network."""
        try:
            resp = self.send_at_command('AT+CREG?')
            if resp and '+CREG: 0,1' in resp or '+CREG: 0,5' in resp:
                logger.info("GSM modem: Connected to network")
                self.connected = True
                return True
            else:
                logger.warning(f"GSM modem: Not registered. Response: {resp}")
                return False
        except Exception as e:
            logger.error(f"Error checking GSM connection: {e}")
            return False
    
    def initialize(self):
        """Initialize modem and unlock SIM if needed."""
        try:
            # Check SIM status
            resp = self.send_at_command('AT+CPIN?')
            if resp:
                if '+CPIN: READY' in resp:
                    logger.info("SIM is ready")
                elif '+CPIN: SIM PIN' in resp:
                    logger.info(f"Unlocking SIM with PIN...")
                    resp = self.send_at_command(f'AT+CPIN="{self.pin}"')
                    if 'OK' in resp:
                        logger.info("SIM unlocked successfully")
                        time.sleep(2)
                    else:
                        logger.error("Failed to unlock SIM")
                        return False
            
            return self.check_connection()
        except Exception as e:
            logger.error(f"Error initializing GSM modem: {e}")
            return False
    
    def get_signal_strength(self):
        """Get current signal strength (0-31, where 31 is best)."""
        try:
            resp = self.send_at_command('AT+CSQ')
            if resp and '+CSQ:' in resp:
                # Extract signal strength value
                parts = resp.split('+CSQ: ')[1].split(',')[0]
                signal = int(parts)
                return signal
            return 0
        except:
            return 0


class VideoUploader:
    def __init__(self, config):
        """Initialize the uploader with configuration."""
        self.outbox_dir = Path(config.get('outbox_dir', '/outbox'))
        self.uploaded_dir = Path(config.get('uploaded_dir', '/uploaded'))
        self.server_url = config.get('server_url', 'http://localhost:5000')
        self.upload_endpoint = f"{self.server_url}/upload"
        self.max_retries = config.get('max_retries', 3)
        self.retry_delay = config.get('retry_delay', 300)  # 5 minutes
        self.poll_interval = config.get('poll_interval', 10)  # 10 seconds
        
        # GSM configuration
        self.gsm_device = config.get('gsm_device', '/dev/serial0')
        self.gsm_pin = config.get('gsm_pin', '0427')
        self.gsm_modem = None
        
        # Create necessary directories
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.uploaded_dir.mkdir(parents=True, exist_ok=True)
        
        # Initialize GSM (required)
        self.gsm_modem = GSMModem(device=self.gsm_device, pin=self.gsm_pin)
        if not self.gsm_modem.initialize():
            logger.error("FATAL: GSM modem initialization failed")
            raise RuntimeError("GSM modem initialization failed - cannot proceed without GSM")
        
        logger.info(f"GSM modem initialized and connected")
        signal = self.gsm_modem.get_signal_strength()
        logger.info(f"GSM signal strength: {signal}/31")
        
        logger.info(f"Uploader initialized. Server: {self.server_url}")
        logger.info(f"Outbox: {self.outbox_dir}, Uploaded: {self.uploaded_dir}")
        logger.info(f"Upload mode: GSM")

    def get_oldest_file(self):
        """Get the oldest file/directory in outbox.

        Ignores temporary/in-progress files (e.g., those ending with `.tmp`)."""
        try:
            items = [p for p in self.outbox_dir.glob('*') if not p.name.endswith('.tmp')]
            # ignore zero-length files
            items = [p for p in items if p.exists() and (not p.is_file() or p.stat().st_size > 0)]

            if not items:
                return None
            
            # Sort by modification time
            oldest = min(items, key=lambda x: x.stat().st_mtime)
            return oldest
        except Exception as e:
            logger.error(f"Error reading outbox directory: {e}")
            return None

    def upload_file(self, file_path, retry_count=0):
        """
        Upload a file to the server via GSM.
        
        Args:
            file_path: Path to file to upload
            retry_count: Current retry attempt
            
        Returns:
            True if successful, False otherwise
        """
        try:
            logger.info(f"Uploading: {file_path.name} (attempt {retry_count + 1})")
            
            # Log GSM signal strength
            signal = self.gsm_modem.get_signal_strength()
            logger.info(f"GSM signal strength: {signal}/31")
            
            with open(file_path, 'rb') as f:
                files = {'video': (file_path.name, f)}
                response = requests.post(
                    self.upload_endpoint,
                    files=files,
                    timeout=60  # Increased timeout for GSM which may be slower
                )
            
            if response.status_code == 200:
                logger.info(f"Successfully uploaded: {file_path.name}")
                return True
            else:
                logger.warning(
                    f"Upload failed with status {response.status_code}: {response.text}"
                )
                return False
                
        except requests.exceptions.RequestException as e:
            logger.error(f"Upload error for {file_path.name}: {e}")
            # Check GSM connection
            if not self.gsm_modem.check_connection():
                logger.warning("GSM connection lost, reconnecting...")
                self.gsm_modem.initialize()
            return False
        except Exception as e:
            logger.error(f"Unexpected error uploading {file_path.name}: {e}")
            return False

    def move_to_uploaded(self, file_path):
        """Move file to uploaded directory."""
        try:
            destination = self.uploaded_dir / file_path.name
            
            shutil.move(str(file_path), str(destination))
            
            logger.info(f"Moved to uploaded: {file_path.name}")
            return True
        except Exception as e:
            logger.error(f"Error moving file {file_path.name}: {e}")
            return False

    def process_oldest_event(self):
        """Process the oldest event in outbox."""
        oldest = self.get_oldest_file()
        
        if not oldest:
            return False  # No files to process
        
        logger.info(f"Processing: {oldest.name}")
        
        # Try uploading with retries
        for attempt in range(self.max_retries):
            if self.upload_file(oldest, attempt):
                # Success - move to uploaded directory
                self.move_to_uploaded(oldest)
                return True
            
            # Retry logic
            if attempt < self.max_retries - 1:
                logger.info(f"Retrying in {self.retry_delay} seconds...")
                time.sleep(self.retry_delay)
        
        logger.error(f"Failed to upload {oldest.name} after {self.max_retries} attempts")
        return False

    def run(self):
        """Main loop - continuously monitor and upload."""
        logger.info("Starting monitoring pipeline...")
        
        try:
            while True:
                # Try to process the oldest event
                processed = self.process_oldest_event()
                
                # Poll interval (shorter if we just processed something)
                sleep_time = self.poll_interval if not processed else 2
                time.sleep(sleep_time)
                
        except KeyboardInterrupt:
            logger.info("Uploader stopped by user")
            sys.exit(0)
        except Exception as e:
            logger.critical(f"Unexpected error in main loop: {e}")
            sys.exit(1)


def load_config(config_file=None):
    """Load configuration from file or use defaults."""
    if config_file is None:
        config_file = '/opt/monitoring-pipeline/config/client.json' 
    
    if os.path.exists(config_file):
        import json
        try:
            with open(config_file) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load config file: {e}, using defaults")
    
    # Default configuration
    return {
        'outbox_dir': '/outbox',
        'uploaded_dir': '/uploaded',
        'server_url': 'http://localhost:5000',
        'max_retries': 3,
        'retry_delay': 300,
        'poll_interval': 10
    }


if __name__ == '__main__':
    # Support custom config file via command line
    config_file = "config/client.json"
    if len(sys.argv) > 1:
        if sys.argv[1] == '--config' and len(sys.argv) > 2:
            config_file = sys.argv[2]
    
    config = load_config(config_file)
    uploader = VideoUploader(config)
    uploader.run()
