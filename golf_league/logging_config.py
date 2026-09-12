"""Logging configuration."""

import logging
from logging.handlers import RotatingFileHandler
import os

def setup_logging():
    """Setup application logging."""
    # Create logs directory if it doesn't exist
    os.makedirs("logs", exist_ok=True)
    
    # Configure root logger
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s %(message)s',
        handlers=[
            RotatingFileHandler('logs/app.log', maxBytes=1024*1024*10, backupCount=5),
            logging.StreamHandler()
        ]
    )