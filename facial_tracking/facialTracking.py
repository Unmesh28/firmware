import cv2
import os
import time
from collections import deque
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips


# ── Face-height-normalized detection ───────────────────────────────
#
# PROBLEM: The old eye ratio formula (vertical / horizontal_eye_span)
# breaks at angles.  When the face is at 30-60° to the camera, the
# eye's horizontal span compresses due to perspective, inflating the
# ratio.  At 45° a closed eye produces ~0.18-0.22 — ABOVE a 0.15
# threshold.  No fixed threshold can work with this formula at angles.
#
# FIX: Normalize by face height (forehead→chin in Y) instead of eye
# horizontal span.  Face height is a Y-axis measurement unaffected by
# horizontal head rotation (yaw).  Eye vertical opening is also a
# Y-axis measurement.  The ratio eye_vertical/face_height is therefore
# yaw-invariant — it produces the same value whether the face is
# frontal or at 45°.
#
# Same fix applies to mouth/yawn detection.

# Face height landmarks
_FOREHEAD = 10
_CHIN = 152

# Minimum face height (pixels) to trust detection.
# Below this the face is too small/far for reliable landmarks.
_MIN_FACE_HEIGHT_PX = int(os.getenv('MIN_FACE_HEIGHT_PX', '30'))

# ── Eye detection ──────────────────────────────────────────────────
#
# Minimum horizontal span (pixels) for an eye to be trusted.
# When the driver turns their head, the far eye's corner landmarks
# compress together.  Below this span, the eye is considered occluded
# and excluded from detection (fallback to single-eye).
_MIN_EYE_SPAN_PX = int(os.getenv('MIN_EYE_SPAN_PX', '15'))

# Eye closed threshold (eye_vertical / face_height).
# Open eyes: ~0.04-0.07.  Closed eyes: ~0.005-0.020.
# Threshold at 0.028 — clear gap between open and closed.
# This is the face-height equivalent of the old 0.15 V/H threshold.
_EYE_CLOSED = float(os.getenv('EYE_CLOSED_RATIO', '0.028'))

# Cap: max realistic eye_vertical/face_height (very wide open eyes).
# TFLite jitter can spike the vertical span; cap prevents false opens.
_EYE_RATIO_CAP = 0.12

# ── Mouth/yawn detection ──────────────────────────────────────────
#
# Mouth open threshold (mouth_vertical / face_height).
# Normal closed: ~0.01-0.04.  Yawn: ~0.10-0.30.
# Threshold at 0.10 — equivalent to old 0.35 V/H threshold.
_MOUTH_OPEN_RATIO = float(os.getenv('MOUTH_OPEN_RATIO', '0.10'))

# Minimum mouth horizontal span (pixels) — secondary guard.
# At extreme angles mouth landmarks become unreliable.
_MIN_MOUTH_SPAN_PX = int(os.getenv('MIN_MOUTH_SPAN_PX', '12'))

# ── Head pose detection (distraction) ─────────────────────────────
#
# Yaw: Rolling median of nose position = driver's normal position (road).
#   Significant deviation from median = looking away (left/right).
#   Self-calibrating: adapts to any camera mounting position without
#   requiring initial calibration.
#
# Pitch: nose-bridge to chin vs forehead to chin ratio → looking down.
#   Fixed threshold (geometric ratio is camera-angle-independent).
#
# Key landmarks (MediaPipe FaceMesh 468-point):
#   1 = nose tip, 6 = nose bridge, 10 = forehead, 152 = chin,
#   234 = left face contour, 454 = right face contour.

_NOSE_TIP = 1
_NOSE_BRIDGE = 6
_LEFT_FACE = 234
_RIGHT_FACE = 454

# Yaw: rolling median deviation.
_YAW_WINDOW = int(os.getenv('YAW_WINDOW', '300'))       # ~30s at 10 FPS
_YAW_MIN_SAMPLES = int(os.getenv('YAW_MIN_SAMPLES', '30'))  # ~3s warmup
_YAW_DEVIATION = float(os.getenv('YAW_DEVIATION', '0.15'))

# Pitch threshold: (bridge-to-chin) / (forehead-to-chin).
_PITCH_DOWN_THRESHOLD = float(os.getenv('HEAD_PITCH_DOWN', '0.38'))

# Frames of sustained distraction before alerting (15 = 1.5s at 10 FPS).
_FRAME_DISTRACTED = int(os.getenv('FRAME_DISTRACTED', '15'))

# Clear yaw baseline after this many consecutive no-face frames (~10s).
_NO_FACE_RESET = int(os.getenv('NO_FACE_RESET', '100'))


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

        # Face geometry (set per-frame)
        self._face_height = 0

        # Eye state (fixed threshold, no calibration)
        self._closed_frames = 0
        self._open_frames = 0

        # Head pose distraction tracking
        self._yaw_window = deque(maxlen=_YAW_WINDOW)
        self._distracted_frames = 0
        self._attentive_frames = 0
        self._last_direction = ''
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
                # Compute face height (Y-axis, yaw-invariant) for normalization
                h = frame.shape[0]
                forehead = face_landmarks.landmark[_FOREHEAD]
                chin = face_landmarks.landmark[_CHIN]
                self._face_height = (chin.y - forehead.y) * h

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
                self._yaw_window.clear()
                self._no_face_frames = 0
            self._closed_frames = 0
            self._open_frames = 0
            self._distracted_frames = 0
            self._attentive_frames = 0

    # ── Eye: face-height-normalized ratio ──────────────────────────

    def _get_reliable_eye_ratio(self):
        """Return eye openness ratio normalized by face height.

        Uses eye_vertical_span / face_height instead of the old
        eye_vertical / eye_horizontal.  This is yaw-invariant because
        both measurements are in the Y-axis, unaffected by horizontal
        head rotation.

        Eye horizontal span is still checked to detect occluded eyes
        (landmark quality guard), but is NOT used in the ratio itself.

        Returns the ratio, or None if no eye is reliable this frame.
        """
        if self._face_height < _MIN_FACE_HEIGHT_PX:
            return None

        # Check eye horizontal span for landmark quality (occlusion guard)
        l_span = self.left_eye.pos[0][0] - self.left_eye.pos[1][0]
        r_span = self.right_eye.pos[0][0] - self.right_eye.pos[1][0]
        l_ok = l_span >= _MIN_EYE_SPAN_PX
        r_ok = r_span >= _MIN_EYE_SPAN_PX

        if l_ok and r_ok:
            # Both eyes visible — average for robustness
            l_v = max(0, self.left_eye.pos[3][1] - self.left_eye.pos[2][1])
            r_v = max(0, self.right_eye.pos[3][1] - self.right_eye.pos[2][1])
            l_r = min(l_v / self._face_height, _EYE_RATIO_CAP)
            r_r = min(r_v / self._face_height, _EYE_RATIO_CAP)
            return (l_r + r_r) / 2.0
        elif l_ok:
            # Only left eye reliable (head turned right)
            l_v = max(0, self.left_eye.pos[3][1] - self.left_eye.pos[2][1])
            return min(l_v / self._face_height, _EYE_RATIO_CAP)
        elif r_ok:
            # Only right eye reliable (head turned left)
            r_v = max(0, self.right_eye.pos[3][1] - self.right_eye.pos[2][1])
            return min(r_v / self._face_height, _EYE_RATIO_CAP)
        else:
            # Neither eye reliable — extreme angle or bad frame
            return None

    def _check_eyes_status(self):
        self.eyes_status = ''

        avg_ratio = self._get_reliable_eye_ratio()

        if avg_ratio is None:
            # No reliable eye data — conservative: assume awake
            self._open_frames += 1
            if self._open_frames > 2:
                self._closed_frames = 0
            return

        # Fixed threshold — no calibration, works for any person/angle
        is_closed = avg_ratio < _EYE_CLOSED

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

    # ── Mouth: face-height-normalized ratio ────────────────────────

    def _check_yawn_status(self):
        self.yawn_status = ''

        if self._face_height < _MIN_FACE_HEIGHT_PX:
            return

        # Secondary guard: mouth horizontal span for landmark quality
        mouth_span = self.lips.pos[0][0] - self.lips.pos[1][0]
        if abs(mouth_span) < _MIN_MOUTH_SPAN_PX:
            return

        # Mouth vertical opening normalized by face height (yaw-invariant)
        mouth_v = max(0, self.lips.pos[3][1] - self.lips.pos[2][1])
        mouth_ratio = mouth_v / self._face_height

        if mouth_ratio > _MOUTH_OPEN_RATIO:
            self.yawn_status = 'yawning'

    # ── Head pose (distraction) ──────────────────────────────────

    def _check_head_pose(self, face_landmarks):
        """Detect if driver is looking away or looking down.

        Yaw uses a rolling median of the nose position within the face.
        The median represents the driver's normal head position (looking at
        the road).  Any significant deviation = looking away.  This
        self-calibrates to any camera mounting position (left or right side
        of windshield) without requiring initial calibration.

        Pitch uses a fixed geometric ratio (camera-position-independent).
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

        # --- Yaw: deviation from rolling median ---
        face_w = right.x - left.x
        if face_w > 0.01:
            yaw_ratio = (nose.x - left.x) / face_w
            self._yaw_window.append(yaw_ratio)

            # Only detect after enough samples to establish baseline
            if len(self._yaw_window) >= _YAW_MIN_SAMPLES:
                sorted_yaw = sorted(self._yaw_window)
                median_yaw = sorted_yaw[len(sorted_yaw) // 2]

                if abs(yaw_ratio - median_yaw) > _YAW_DEVIATION:
                    direction = 'looking away'

        # --- Pitch: looking down (only if not already flagged by yaw) ---
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
