import os

# Performance mode: set HEADLESS=1 in production (no display attached)
# Skips all drawing operations (draw_mesh, draw_iris, putText) to save CPU
HEADLESS = os.getenv('HEADLESS', '1').strip().lower() in {'1', 'true', 'yes', 'on'}

# Camera parameters
CAM_ID  = 0
FRAME_W = int(os.getenv('FRAME_W', '640'))
FRAME_H = int(os.getenv('FRAME_H', '480'))

# MediaPipe settings - disable iris refinement in production to save ~30% CPU
REFINE_LANDMARKS = os.getenv('REFINE_LANDMARKS', '0').strip().lower() in {'1', 'true', 'yes', 'on'}
MIN_DETECTION_CONFIDENCE = float(os.getenv('MIN_DETECTION_CONFIDENCE', '0.5'))
MIN_TRACKING_CONFIDENCE = float(os.getenv('MIN_TRACKING_CONFIDENCE', '0.5'))

# Target FPS for detection loop (lower = less CPU)
TARGET_DETECTION_FPS = int(os.getenv('TARGET_DETECTION_FPS', '10'))

# Plot configuration
TEXT_COLOR = (102,51,0)
LM_COLOR   = (51,255,51)
CT_COLOR   = (243,166,56)
WARN_COLOR = (76,76,255)

# Target landmarks
LEFT_EYE   = [263, 362, 386, 374, 473, 474, 475, 476, 477]
RIGHT_EYE  = [133,  33, 159, 145, 468, 469, 470, 471, 472]
LIPS       = [291,  61,  13,  14]

# Threshold
GAZE_LEFT  = 0.2
GAZE_RIGHT = 0.8
EYE_CLOSED = 0.20
MOUTH_OPEN = 0.65
FRAME_CLOSED = 4
FRAME_YAWN = 2
FRAME_TOLERANCE = 2
