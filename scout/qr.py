"""QR plant stakes: payload "AGRISCOUT:T-01". Encode (for printing) and decode (from a snapshot).

Only OpenCV is used (cv2.QRCodeEncoder / cv2.QRCodeDetector), no extra library.
"""

import re
from typing import Optional

import cv2
import numpy as np

PAYLOAD_PREFIX = "AGRISCOUT:"
PLANT_ID_RE = re.compile(r"^[A-Z]-\d{2,3}$")  # "T-01", "P-02", "T-104"
QUIET_ZONE_MODULES = 4  # white border around the code, in QR squares (the QR standard asks for 4)


def make_payload(plant_id: str) -> str:
    return PAYLOAD_PREFIX + plant_id


def parse_payload(text: Optional[str]) -> Optional[str]:
    """'AGRISCOUT:T-01' -> 'T-01'. Anything else (a web link, empty text) -> None."""
    if not text or not text.upper().startswith(PAYLOAD_PREFIX):
        return None
    plant_id = text[len(PAYLOAD_PREFIX):].strip().upper()
    return plant_id if PLANT_ID_RE.match(plant_id) else None


# ---------------------------------------------------------------- encode

def _encoder_params():
    # In the Python binding the nested C++ names are flattened: QRCodeEncoder::Params ->
    # cv2.QRCodeEncoder_Params, QRCodeEncoder::CORRECT_LEVEL_H -> cv2.QRCodeEncoder_CORRECT_LEVEL_H.
    params = cv2.QRCodeEncoder_Params()
    # Level H: the code still reads with up to ~30 % of it dirty or damaged (outdoor stakes).
    params.correction_level = cv2.QRCodeEncoder_CORRECT_LEVEL_H
    return params


def qr_modules(payload: str) -> np.ndarray:
    """The code itself, 1 pixel per square (0 = black, 255 = white), without any white border."""
    image = cv2.QRCodeEncoder.create(_encoder_params()).encode(payload)
    ys, xs = np.where(image == 0)  # the encoder adds a small border; crop to the black squares
    return image[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def make_qr(payload: str, min_code_px: int) -> np.ndarray:
    """Black-on-white QR code as a grayscale image.

    The code itself (black squares, without the white border) is at least min_code_px wide:
    every square gets the same whole number of pixels, so all edges stay sharp. Around it is a
    white quiet zone of QUIET_ZONE_MODULES squares, which the detectors need.
    """
    modules = qr_modules(payload)
    px_per_module = -(-min_code_px // modules.shape[0])  # ceiling division
    modules = cv2.copyMakeBorder(modules, QUIET_ZONE_MODULES, QUIET_ZONE_MODULES,
                                 QUIET_ZONE_MODULES, QUIET_ZONE_MODULES,
                                 cv2.BORDER_CONSTANT, value=255)
    size = modules.shape[0] * px_per_module
    # INTER_NEAREST copies pixels (no blending), so the squares stay pure black and white.
    return cv2.resize(modules, (size, size), interpolation=cv2.INTER_NEAREST)


# ---------------------------------------------------------------- decode

def _try(detector, gray: np.ndarray) -> Optional[str]:
    try:
        text = detector.detectAndDecode(gray)[0]
    except cv2.error:  # some odd images make the detector throw instead of returning ""
        return None
    return parse_payload(text)


def decode_plant_id(image: np.ndarray) -> Optional[str]:
    """Plant ID from the first AgriScout QR code found in a BGR (or gray) image, else None.

    Tries three ways, cheapest first, and stops at the first valid AgriScout payload:
      1. the normal detector on the photo
      2. the same on the photo scaled up 2x (a far-away stake is only a few pixels per square)
      3. the ArUco-based detector (copes better with rotation and perspective)
    If two stakes are in view, whichever is found first wins: aim the camera at one stake.
    """
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY) if image.ndim == 3 else image
    normal = cv2.QRCodeDetector()
    big = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    for detector, img in ((normal, gray), (normal, big), (cv2.QRCodeDetectorAruco(), gray)):
        plant_id = _try(detector, img)
        if plant_id:
            return plant_id
    return None
