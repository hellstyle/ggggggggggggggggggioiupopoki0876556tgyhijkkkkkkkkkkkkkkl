import logging
from io import BytesIO
from typing import Optional, Any

try:
    from PIL import Image, UnidentifiedImageError
    import imagehash
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# --- Pillow version compatibility ---
# Image.ANTIALIAS was deprecated in Pillow 9.1.0 and removed in 10.0.0
# The new way is to use Image.Resampling.LANCZOS
if PIL_AVAILABLE:
    try:
        RESAMPLING_METHOD: Any = Image.Resampling.LANCZOS
    except AttributeError:
        # Fallback for older Pillow versions
        RESAMPLING_METHOD = Image.ANTIALIAS

logger = logging.getLogger(__name__)

async def calculate_phash(image_bytes: bytes) -> Optional[str]:
    """
    Calculates the perceptual hash (phash) of an image.
    This version is more robust and handles different image modes.
    Returns the hash as a string, or None if it fails.
    """
    if not PIL_AVAILABLE:
        logger.warning("Pillow or imagehash library not installed. Cannot calculate image hash.")
        return None

    try:
        image = Image.open(BytesIO(image_bytes))

        # --- Robust image handling ---
        # 1. If the image has an alpha channel (e.g., PNG, WEBP with transparency),
        #    convert it to RGB by pasting it on a white background.
        if image.mode in ('RGBA', 'LA') or (image.mode == 'P' and 'transparency' in image.info):
            # Create a white background
            background = Image.new('RGB', image.size, (255, 255, 255))
            # Paste the image on top of the background, using its alpha channel as a mask
            background.paste(image, mask=image.convert('RGBA').split()[3])
            image = background
        # 2. If it's not RGB, convert it. This handles palette-based images etc.
        elif image.mode != 'RGB':
            image = image.convert('RGB')

        # phash works on grayscale images.
        grayscale_image = image.convert("L")
        
        phash = imagehash.phash(grayscale_image)
        return str(phash)
        
    except UnidentifiedImageError:
        logger.error("Cannot identify image file. It might be corrupted or in an unsupported format (e.g., HEIC).")
        return None
    except Exception as e:
        logger.error(f"An unexpected error occurred while calculating phash: {e}", exc_info=True)
        return None

def compare_phashes(hash1_str: str, hash2_str: str, threshold: int) -> bool:
    """
    Compares two phash hex strings and returns True if their difference
    is within the threshold.
    """
    if not PIL_AVAILABLE or not hash1_str or not hash2_str:
        return False
        
    try:
        hash1 = imagehash.hex_to_hash(hash1_str)
        hash2 = imagehash.hex_to_hash(hash2_str)
        
        return (hash1 - hash2) <= threshold
    except (ValueError, TypeError) as e:
        logger.error(f"Error comparing phashes ('{hash1_str}', '{hash2_str}'): {e}")
        return False