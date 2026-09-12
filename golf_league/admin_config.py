"""Admin configuration validation."""

from .config import settings

def validate_admin_config():
    """Validate admin bootstrap inputs."""
    # Check that admin email is set
    if not settings.admin_email:
        raise ValueError("Admin email must be configured")
    
    # Check that admin password is set
    if not settings.admin_password:
        raise ValueError("Admin password must be configured")
    
    # Validate maximum users
    if settings.max_users <= 0:
        raise ValueError("Maximum users must be positive")

# Validate on import
validate_admin_config()