import cv2
import os
import time
from collections import deque
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips


# ── Eye reliability ────────────────────────────────────────────────
#
# When the driver turns their head, the far eye's landmarks compress:
# the horizontal span shrinks toward 0 or goes negative.  This produces
# garbage blink ratios that poison the average → false sleeping triggers.
#
# Fix: measure each eye's horizontal span (px between corners).
# If span < minimum, that eye is occluded → exclude it.
# Use only the reliable eye(s) for detection.

# Minimum horizontal span (pixels) for an eye to be trusted.
# At 640px width a visible eye spans ~30-60px.  Below 15px the
# landmarks are too compressed for a meaningful V/H ratio.
_MIN_EYE_SPAN_PX = int(os.getenv('MIN_EYE_SPAN_PX', '15'))

# Clamp individual eye ratios to [0, CAP].
# Real V/H maxes at ~0.35; TFLite jitter can produce 0.85+.
# Floor at 0 catches negative ratios from landmark noise.
_EYE_RATIO_CAP = 0.35

# ── Dual-mode eye-close detection ──────────────────────────────────
#
# Mode 1 — Absolute: ratio below a hard floor = definitely closed
#   regardless of camera angle.  Works from frame 1.
#
# Mode 2 — Relative: ratio dropped below 75th-percentile of a rolling
#   15-second window × factor.  Adapts continuously; no warmup.
#
# Either mode triggers the closed-frame counter.

_ABSOLUTE_CLOSED = float(os.getenv('EYE_ABSOLUTE_CLOSED', '0.15'))

_WINDOW_SIZE = int(os.getenv('EYE_WINDOW_SIZE', '150'))  # ~15s at 10 FPS
_MIN_WINDOW_SAMPLES = 20                                   # ~2s
_BASELINE_PERCENTILE = 0.75
_RELATIVE_FACTOR = float(os.getenv('EYE_RELATIVE_FACTOR', '0.75'))

_NO_FACE_RESET = int(os.getenv('EYE_NO_FACE_RESET', '100'))  # ~10s

# ── Mouth reliability ──────────────────────────────────────────────
# Same issue: when head turns, mouth horizontal span shrinks, making
# the open ratio unstable.  Skip yawn detection when span is too small.
_MIN_MOUTH_SPAN_PX = int(os.getenv('MIN_MOUTH_SPAN_PX', '12'))

# ── Head pose detection (distraction) ─────────────────────────────
#
# Uses nose position relative to face edges to estimate head direction.
# Yaw: nose X offset from face center → looking left/right.
# Pitch: nose-bridge to chin vs nose-bridge to forehead ratio → looking down.
#
# Key landmarks (MediaPipe FaceMesh 468-point):
#   1 = nose tip, 6 = nose bridge, 10 = forehead, 152 = chin,
#   234 = left face contour, 454 = right face contour.

_NOSE_TIP = 1
_NOSE_BRIDGE = 6
_FOREHEAD = 10
_CHIN = 152
_LEFT_FACE = 234
_RIGHT_FACE = 454

# Yaw threshold: nose position as fraction of face width.
# 0.5 = center.  Below threshold = looking right, above (1-threshold) = looking left.
# 0.38 means ~24% off-center triggers.
_YAW_THRESHOLD = float(os.getenv('HEAD_YAW_THRESHOLD', '0.38'))

# Pitch threshold: ratio of (bridge-to-chin) / (forehead-to-chin).
# ~0.5 = level.  Below threshold = looking down.
_PITCH_DOWN_THRESHOLD = float(os.getenv('HEAD_PITCH_DOWN', '0.38'))

# Frames of sustained distraction before alerting.
# At 10 FPS: 15 frames = 1.5 seconds (ignores brief mirror glances).
_FRAME_DISTRACTED = int(os.getenv('FRAME_DISTRACTED', '15'))


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

        # Rolling window of reliable eye ratios for adaptive baseline
        self._ratio_window = deque(maxlen=_WINDOW_SIZE)
        self._closed_frames = 0
        self._open_frames = 0
        self._no_face_frames = 0

        # Head pose distraction tracking
        self._distracted_frames = 0
        self._attentive_frames = 0
        self._last_direction = ''

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
                self._check_head_pose(face_landmarks)
                break
        else:
            self._no_face_frames += 1
            if self._no_face_frames >= _NO_FACE_RESET:
                self._ratio_window.clear()
                self._no_face_frames = 0
            self._closed_frames = 0
            self._open_frames = 0
            self._distracted_frames = 0
            self._attentive_frames = 0

    # ── Eye reliability + single-eye fallback ──────────────────────

    def _get_reliable_eye_ratio(self):
        """Return blink ratio using only eyes with sufficient landmark span.

        When the driver turns their head, the far eye's corner landmarks
        compress together (small or negative horizontal span).  We detect
        this and exclude that eye, falling back to single-eye detection.

        Returns the ratio, or None if no eye is reliable this frame.
        """
        # Horizontal span = distance between eye corners (pixels).
        # pos[0] = outer corner, pos[1] = inner corner (from conf landmark IDs).
        l_span = self.left_eye.pos[0][0] - self.left_eye.pos[1][0]
        r_span = self.right_eye.pos[0][0] - self.right_eye.pos[1][0]

        l_ok = l_span >= _MIN_EYE_SPAN_PX
        r_ok = r_span >= _MIN_EYE_SPAN_PX

        if l_ok and r_ok:
            # Both eyes visible — average cancels mild angle asymmetry
            l_r = max(0.0, min(self.left_eye.eye_veti_to_hori, _EYE_RATIO_CAP))
            r_r = max(0.0, min(self.right_eye.eye_veti_to_hori, _EYE_RATIO_CAP))
            return (l_r + r_r) / 2.0
        elif l_ok:
            # Only left eye reliable (head turned right)
            return max(0.0, min(self.left_eye.eye_veti_to_hori, _EYE_RATIO_CAP))
        elif r_ok:
            # Only right eye reliable (head turned left)
            return max(0.0, min(self.right_eye.eye_veti_to_hori, _EYE_RATIO_CAP))
        else:
            # Neither eye reliable — extreme angle or bad frame
            return None

    def _check_eyes_status(self):
        self.eyes_status = ''

        avg_ratio = self._get_reliable_eye_ratio()

        if avg_ratio is None:
            # No reliable eye data — treat as inconclusive.
            # Increment open counter (conservative: assume awake).
            self._open_frames += 1
            if self._open_frames > 2:
                self._closed_frames = 0
            return

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
        # Guard: when head is turned, mouth horizontal span shrinks,
        # making the open ratio unstable / infinite.  Skip detection.
        mouth_span = self.lips.pos[0][0] - self.lips.pos[1][0]
        if abs(mouth_span) < _MIN_MOUTH_SPAN_PX:
            return
        if self.lips.mouth_open():
            self.yawn_status = 'yawning'

    # ── Head pose (distraction) ──────────────────────────────────

    def _check_head_pose(self, face_landmarks):
        """Detect if driver is looking down, left, or right.

        Uses nose position relative to face boundary landmarks to estimate
        head yaw (left/right) and pitch (down).  A frame counter with
        hysteresis prevents brief mirror glances from triggering alerts.
        """
        self.head_status = ''

        lm = face_landmarks.landmark
        nose = lm[_NOSE_TIP]
        bridge = lm[_NOSE_BRIDGE]
        forehead = lm[_FOREHEAD]
        chin = lm[_CHIN]
        left = lm[_LEFT_FACE]
        right = lm[_RIGHT_FACE]

        direction = ''

        # --- Yaw: looking left or right ---
        face_w = right.x - left.x
        if face_w > 0.01:
            yaw_ratio = (nose.x - left.x) / face_w
            if yaw_ratio < _YAW_THRESHOLD:
                direction = 'looking right'
            elif yaw_ratio > (1.0 - _YAW_THRESHOLD):
                direction = 'looking left'

        # --- Pitch: looking down (only if not already turned sideways) ---
        if not direction:
            upper = bridge.y - forehead.y
            lower = chin.y - bridge.y
            total = upper + lower
            if total > 0.01:
                pitch_ratio = lower / total
                if pitch_ratio < _PITCH_DOWN_THRESHOLD:
                    direction = 'looking down'

        # Frame counter with 2-frame hysteresis
        if direction:
            self._distracted_frames += 1
            self._attentive_frames = 0
            self._last_direction = direction
        else:
            self._attentive_frames += 1
            if self._attentive_frames > 2:
                self._distracted_frames = 0

        if self._distracted_frames > _FRAME_DISTRACTED:
            self.head_status = self._last_direction
        

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

