import cv2
import os
import time
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips


# Clamp per-eye ratios before averaging. Real eye V/H ratio maxes at ~0.35.
# TFLite landmarks can jitter to 0.85+ which inflates the adaptive baseline.
_EYE_RATIO_CAP = 0.35

# Adaptive eye close detection: eyes "closed" when avg ratio drops
# below this fraction of the running "open" baseline.
# 0.65 = closed when ratio drops to < 65% of normal open value.
_EYE_CLOSE_RATIO = float(os.getenv('EYE_CLOSE_RATIO', '0.65'))

# Warmup: frames to establish baseline (~3s at 10 FPS)
_EYE_WARMUP = int(os.getenv('EYE_WARMUP', '30'))


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

        # Adaptive eye ratio tracking
        # Averages L+R eye ratios (cancels camera-angle asymmetry),
        # then compares to a running baseline that adapts to any mount position.
        self._avg_closed_frames = 0
        self._avg_gap_frames = 0
        self._eye_baseline = 0.0
        self._warmup_count = 0

    def process_frame(self, frame):
        """Process the frame to analyze facial status."""
        self.detected = False  # Reset detected status for each frame
        self.fm.process_frame(frame)
        self.fm.draw_mesh_lips()  # no-op in HEADLESS mode

        if self.fm.mesh_result.multi_face_landmarks:
            self.detected = True
            for face_landmarks in self.fm.mesh_result.multi_face_landmarks:
                self.left_eye = Eye(frame, face_landmarks, conf.LEFT_EYE)
                self.right_eye = Eye(frame, face_landmarks, conf.RIGHT_EYE)
                self.lips = Lips(frame, face_landmarks, conf.LIPS)
                self._check_eyes_status()
                self._check_yawn_status()
                break  # Only process the first face (skip multi-face loop overhead)

    def _check_eyes_status(self):
        self.eyes_status = ''

        # Clamp per-eye ratios to reject landmark jitter outliers.
        # Real eye V/H ratio is 0.05-0.35; values like 0.85 are noise.
        l_ratio = min(self.left_eye.eye_veti_to_hori, _EYE_RATIO_CAP)
        r_ratio = min(self.right_eye.eye_veti_to_hori, _EYE_RATIO_CAP)
        avg_ratio = (l_ratio + r_ratio) / 2.0

        # --- Adaptive baseline ---
        # During warmup: fast convergence to establish "open" baseline.
        # After warmup: slow tracking, only updated when eyes appear open.
        # Threshold = baseline * CLOSE_RATIO (adapts to any camera angle).
        if self._warmup_count < _EYE_WARMUP:
            self._warmup_count += 1
            if self._eye_baseline == 0:
                self._eye_baseline = avg_ratio
            else:
                # Fast EMA (alpha=0.15) during warmup
                self._eye_baseline += 0.15 * (avg_ratio - self._eye_baseline)
            # Use fixed threshold during warmup (from conf / env var)
            threshold = conf.EYE_CLOSED
        else:
            # Adaptive threshold: closed = below 65% of open baseline
            threshold = self._eye_baseline * _EYE_CLOSE_RATIO
            threshold = max(threshold, 0.06)  # safety floor
            # Slow EMA (alpha=0.02) — only update when eyes are open
            if avg_ratio >= threshold:
                self._eye_baseline += 0.02 * (avg_ratio - self._eye_baseline)

        if avg_ratio < threshold:
            self._avg_closed_frames += 1
            self._avg_gap_frames = 0
        else:
            # 1-frame hysteresis: single noisy "open" frame doesn't reset
            self._avg_gap_frames += 1
            if self._avg_gap_frames > 1:
                self._avg_closed_frames = 0

        if self._avg_closed_frames > conf.FRAME_CLOSED:
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

