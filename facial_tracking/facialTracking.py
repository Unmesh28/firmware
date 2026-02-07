import cv2
import time
import facial_tracking.conf as conf

from facial_tracking.faceMesh import FaceMesh
from facial_tracking.eye import Eye
from facial_tracking.lips import Lips
from pygame import mixer
import time
from gpiozero import Buzzer
from time import sleep
from buzzer_controller import buzz_once

class FacialTracker:
    """
    The object of facial tracking, predicting status of eye, iris, and mouth.
    """

    def __init__(self):
        self.fm = FaceMesh()
        self.left_eye = None
        self.right_eye = None
        self.lips = None
        self.left_eye_closed_frames = 0
        self.right_eye_closed_frames = 0
        self.yawn_frames = 0
        self.yawn_gap_frames = 0
        self.detected = False  # Initialize the detected attribute
        
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
        #buzzer1 = Buzzer(17)  # for PCB, it's 12 & 13, and for normal device it's 17 & 22
        #buzzer2 = Buzzer(22)
        if self.left_eye.eye_closed():
            self.left_eye_closed_frames += 1
        else:
            self.left_eye_closed_frames = 0
            if not conf.HEADLESS and self.left_eye.iris:
                self.left_eye.iris.draw_iris(True)

        if self.right_eye.eye_closed():
            self.right_eye_closed_frames += 1
        else:
            self.right_eye_closed_frames = 0
            if not conf.HEADLESS and self.right_eye.iris:
                self.right_eye.iris.draw_iris(True)
        
        if self._left_eye_closed() and self._right_eye_closed():
            self.eyes_status = 'eye closed'
            buzz_once(0.12)
            
            #self.buzzer1.on()
            #self.buzzer2.on()
            #sleep(0.12)
            #self.buzzer1.off()
            #self.buzzer2.off()
            
            #buzzer1.beep()
            #buzzer2.beep()
            #sleep(0.12)
            #buzzer1.off()
            #buzzer2.off()
            return
        
        if not self.left_eye.eye_closed() and not self.right_eye.eye_closed():
            if self.left_eye.gaze_right() and self.right_eye.gaze_right():
                self.eyes_status = 'gazing right'
            elif self.left_eye.gaze_left() and self.right_eye.gaze_left():
                self.eyes_status = 'gazing left'
            elif self.left_eye.gaze_center() and self.right_eye.gaze_center():
                self.eyes_status = 'gazing center'

    def _check_yawn_status(self):
        self.yawn_status = ''
        
        #buzzer1 = Buzzer(17)
        #buzzer2 = Buzzer(22)
        if self.lips.mouth_open():
            self.yawn_frames += 1
            self.yawn_gap_frames = 0
        else:
            self.yawn_gap_frames += 1
            if self.yawn_gap_frames > conf.FRAME_TOLERANCE:
                self.yawn_frames = 0
        
        if self.yawn_frames > conf.FRAME_YAWN:
            self.yawn_status = 'yawning'
            buzz_once(0.12)
            
            #self.buzzer1.on()
            #self.buzzer2.on()
            #sleep(0.12)
            #self.buzzer1.off()
            #self.buzzer2.off()
            
            #buzzer1.beep()
            #buzzer2.beep()
            #sleep(0.12)
            #buzzer1.off()
            #buzzer2.off()
    
    def _left_eye_closed(self, threshold=conf.FRAME_CLOSED):
        return self.left_eye_closed_frames > threshold
    
    def _right_eye_closed(self, threshold=conf.FRAME_CLOSED):
        return self.right_eye_closed_frames > threshold
        

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

