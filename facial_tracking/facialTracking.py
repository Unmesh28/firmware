import cv2
import time
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips


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

        # Averaged eye ratio tracking — robust against asymmetric camera angles
        # Instead of requiring both eyes independently below threshold,
        # average L+R ratios which cancels out angle-induced asymmetry.
        self._avg_closed_frames = 0
        self._avg_gap_frames = 0

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

        # Average both eye ratios — cancels asymmetry from camera angle
        avg_ratio = (self.left_eye.eye_veti_to_hori + self.right_eye.eye_veti_to_hori) / 2.0

        # Diagnostic: log every 30 frames for threshold calibration
        if not hasattr(self, '_eye_log_count'):
            self._eye_log_count = 0
        self._eye_log_count += 1
        if self._eye_log_count % 30 == 0:
            print(f"EYE_AVG={avg_ratio:.3f} L={self.left_eye.eye_veti_to_hori:.3f} R={self.right_eye.eye_veti_to_hori:.3f} thresh={conf.EYE_CLOSED} closed_frames={self._avg_closed_frames}")

        if avg_ratio < conf.EYE_CLOSED:
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

