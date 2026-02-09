import cv2
import os
import time
from collections import deque
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips


# Clamp per-eye ratios before averaging.  Real V/H maxes at ~0.35;
# TFLite jitter can produce 0.85+ which corrupts any baseline.
_EYE_RATIO_CAP = 0.35

# ── Dual-mode eye-close detection ──────────────────────────────────
#
# Mode 1 — Absolute: avg_ratio below a hard floor = definitely closed
#   regardless of camera angle.  Averaged closed-eye ratios are
#   consistently 0.10-0.15 across all tested angles.
#
# Mode 2 — Relative: avg_ratio dropped significantly from the rolling
#   upper-percentile of recent ratios (= "normal open" baseline).
#   Adapts continuously to any camera angle; no fragile warmup period.
#
# Either mode triggers the closed-frame counter.

_ABSOLUTE_CLOSED = float(os.getenv('EYE_ABSOLUTE_CLOSED', '0.15'))

# Rolling window: ~15 seconds of face data at 10 FPS
_WINDOW_SIZE = int(os.getenv('EYE_WINDOW_SIZE', '150'))

# Need at least this many samples before relative mode activates (~2s)
_MIN_WINDOW_SAMPLES = 20

# 75th percentile of window = robust "eyes open" estimate
# (naturally filters out blinks & brief closures in the window)
_BASELINE_PERCENTILE = 0.75

# Eyes closed when ratio < baseline * this factor
_RELATIVE_FACTOR = float(os.getenv('EYE_RELATIVE_FACTOR', '0.75'))

# If face is lost for this many frames, clear stale baseline
# (driver may have moved camera / changed seating position)
_NO_FACE_RESET = int(os.getenv('EYE_NO_FACE_RESET', '100'))  # ~10s


class FacialTracker:
    """
    The object of facial tracking, predicting status of eye, iris, and mouth.
    """

    def __init__(self):
        self.fm = FaceMesh()
        self.left_eye = None
        self.right_eye = None
        self.lips = None
        self.detected = False

        # Rolling window of averaged eye ratios for adaptive baseline
        self._ratio_window = deque(maxlen=_WINDOW_SIZE)
        self._closed_frames = 0
        self._open_frames = 0
        self._no_face_frames = 0

    def process_frame(self, frame):
        """Process the frame to analyze facial status."""
        self.detected = False
        self.fm.process_frame(frame)
        self.fm.draw_mesh_lips()  # no-op in HEADLESS mode

        if self.fm.mesh_result.multi_face_landmarks:
            self.detected = True
            self._no_face_frames = 0
            for face_landmarks in self.fm.mesh_result.multi_face_landmarks:
                self.left_eye = Eye(frame, face_landmarks, conf.LEFT_EYE)
                self.right_eye = Eye(frame, face_landmarks, conf.RIGHT_EYE)
                self.lips = Lips(frame, face_landmarks, conf.LIPS)
                self._check_eyes_status()
                self._check_yawn_status()
                break
        else:
            self._no_face_frames += 1
            # Face lost for extended period — clear stale baseline
            if self._no_face_frames >= _NO_FACE_RESET:
                self._ratio_window.clear()
                self._no_face_frames = 0
            self._closed_frames = 0
            self._open_frames = 0

    def _check_eyes_status(self):
        self.eyes_status = ''

        l_ratio = min(self.left_eye.eye_veti_to_hori, _EYE_RATIO_CAP)
        r_ratio = min(self.right_eye.eye_veti_to_hori, _EYE_RATIO_CAP)
        avg_ratio = (l_ratio + r_ratio) / 2.0

        self._ratio_window.append(avg_ratio)

        # ── Dual-mode closed detection ──
        is_closed = False

        # Mode 1: Absolute — below hard floor = definitely closed
        if avg_ratio < _ABSOLUTE_CLOSED:
            is_closed = True

        # Mode 2: Relative — significant drop from rolling baseline
        if not is_closed and len(self._ratio_window) >= _MIN_WINDOW_SAMPLES:
            sorted_vals = sorted(self._ratio_window)
            baseline = sorted_vals[int(len(sorted_vals) * _BASELINE_PERCENTILE)]
            relative_thresh = baseline * _RELATIVE_FACTOR
            # Only use relative when it's stricter than absolute
            if relative_thresh > _ABSOLUTE_CLOSED and avg_ratio < relative_thresh:
                is_closed = True

        # Frame counter with 2-frame hysteresis (survives TFLite jitter)
        if is_closed:
            self._closed_frames += 1
            self._open_frames = 0
        else:
            self._open_frames += 1
            if self._open_frames > 2:
                self._closed_frames = 0

        if self._closed_frames > conf.FRAME_CLOSED:
            self.eyes_status = 'eye closed'

    def _check_yawn_status(self):
        self.yawn_status = ''
        if self.lips.mouth_open():
            self.yawn_status = 'yawning'
        

def main():
    cap = cv2.VideoCapture(conf.CAM_ID)
    cap.set(3, conf.FRAME_W)
    cap.set(4, conf.FRAME_H)
    facial_tracker = FacialTracker()
    ptime = 0
    ctime = 0

    while cap.isOpened():
        success, frame = cap.read()
        if not success:
            #print("Ignoring empty camera frame.")
            continue
        
        facial_tracker.process_frame(frame)

        ctime = time.time()
        fps = 1 / (ctime - ptime)
        ptime = ctime

        frame = cv2.flip(frame, 1)
        cv2.putText(frame, f'FPS: {int(fps)}', (30, 30), 0, 0.6, conf.TEXT_COLOR, 1, lineType=cv2.LINE_AA)
        
        if facial_tracker.detected:
            cv2.putText(frame, f'{facial_tracker.eyes_status}', (30, 70), 0, 0.8, conf.WARN_COLOR, 2, lineType=cv2.LINE_AA)
            cv2.putText(frame, f'{facial_tracker.yawn_status}', (30, 110), 0, 0.8, conf.WARN_COLOR, 2, lineType=cv2.LINE_AA)

        cv2.imshow('Facial tracking', frame)
        key = cv2.waitKey(1)
        if key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

if __name__ == '__main__':
    main()

