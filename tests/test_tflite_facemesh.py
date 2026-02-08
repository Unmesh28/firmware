#!/usr/bin/env python3
"""Test the TFLite FaceMesh replacement end-to-end with camera.

Runs the facial tracking pipeline (detection + landmarks + eye/lip ratios)
using direct TFLite inference. Reports per-frame timing and detection results.

Usage:
    cd /home/pi/facial-tracker-firmware
    FACE_MODEL_DIR=models REFINE_LANDMARKS=0 python3 test_tflite_facemesh.py
"""

import sys
import os
import time

# Add parent dir to path so we can import facial_tracking
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
os.environ.setdefault('HEADLESS', '1')
os.environ.setdefault('REFINE_LANDMARKS', '0')  # No iris with small model

import cv2
import numpy as np
import facial_tracking.conf as conf
from facial_tracking.faceMesh import FaceMesh

def main():
    print("=" * 60)
    print("TFLite FaceMesh End-to-End Test")
    print(f"  REFINE_LANDMARKS = {conf.REFINE_LANDMARKS}")
    print(f"  HEADLESS = {conf.HEADLESS}")
    print(f"  Model dir = {os.getenv('FACE_MODEL_DIR', 'models')}")
    print("=" * 60)

    # Initialize
    fm = FaceMesh()
    print(f"FaceMesh class: {type(fm).__module__}.{type(fm).__name__}")

    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print("ERROR: Cannot open camera")
        return

    # Warmup
    for _ in range(10):
        cap.read()

    print("\nRunning 200 frames...")
    times = []
    detected = 0
    eye_closed_count = 0

    for i in range(200):
        t0 = time.monotonic()

        ret, frame = cap.read()
        if not ret:
            continue

        fm.process_frame(frame)

        elapsed = (time.monotonic() - t0) * 1000
        times.append(elapsed)

        if fm.mesh_result.multi_face_landmarks:
            detected += 1
            face_lm = fm.mesh_result.multi_face_landmarks[0]

            # Test eye ratio (same logic as Eye class)
            # LEFT_EYE = [263, 362, 386, 374]
            h, w = frame.shape[:2]
            lm = face_lm.landmark
            l_top = lm[386].y * h
            l_bot = lm[374].y * h
            l_left = lm[263].x * w
            l_right = lm[362].x * w
            l_ratio = (l_bot - l_top) / max(l_left - l_right, 1)

            # LIPS = [291, 61, 13, 14]
            lip_top = lm[13].y * h
            lip_bot = lm[14].y * h
            lip_left = lm[291].x * w
            lip_right = lm[61].x * w
            lip_ratio = (lip_bot - lip_top) / max(lip_left - lip_right, 1)

            if i % 20 == 0:
                print(f"  Frame {i:3d}: {elapsed:5.1f}ms  "
                      f"eye_ratio={l_ratio:.3f} lip_ratio={lip_ratio:.3f}")

            if l_ratio < conf.EYE_CLOSED:
                eye_closed_count += 1
        else:
            if i % 20 == 0:
                print(f"  Frame {i:3d}: {elapsed:5.1f}ms  NO FACE")

    cap.release()

    # Results
    avg = sum(times) / len(times)
    print(f"\n{'=' * 60}")
    print(f"Results ({len(times)} frames):")
    print(f"  Avg: {avg:.1f}ms  Min: {min(times):.1f}ms  Max: {max(times):.1f}ms")
    print(f"  FPS: {1000/avg:.1f}")
    print(f"  Face detected: {detected}/{len(times)} ({100*detected/len(times):.0f}%)")
    print(f"  Eye closed frames: {eye_closed_count}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
