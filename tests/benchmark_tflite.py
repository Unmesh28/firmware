#!/usr/bin/env python3
"""Benchmark TFLite face detection + landmark inference on Pi Zero 2W.

Usage:
    python3 benchmark_tflite.py

Requires: tflite-runtime, numpy, opencv-python (or python3-opencv)
Models:  models/face_detection_short.tflite
         models/face_landmark_192.tflite
         models/face_landmarks_detector.tflite (optional, from .task bundle)
"""

import time
import numpy as np
import cv2

try:
    from tflite_runtime.interpreter import Interpreter
except ImportError:
    import tensorflow.lite as tflite
    Interpreter = tflite.Interpreter

MODEL_DIR = "models"


def load_model(path, num_threads=4):
    """Load a TFLite model and allocate tensors."""
    interp = Interpreter(model_path=path, num_threads=num_threads)
    interp.allocate_tensors()
    inp = interp.get_input_details()
    out = interp.get_output_details()
    print(f"\nModel: {path}")
    print(f"  Input:  {inp[0]['shape']}  dtype={inp[0]['dtype']}")
    for i, o in enumerate(out):
        print(f"  Output[{i}]: {o['shape']}  dtype={o['dtype']}")
    return interp, inp, out


def benchmark_model(interp, inp_details, name, iterations=50):
    """Run dummy inference to measure raw model speed."""
    shape = inp_details[0]['shape']
    dtype = inp_details[0]['dtype']
    dummy = np.random.randint(0, 255, shape).astype(dtype)
    if dtype == np.float32:
        dummy = dummy / 255.0

    # Warmup
    for _ in range(5):
        interp.set_tensor(inp_details[0]['index'], dummy)
        interp.invoke()

    # Benchmark
    times = []
    for _ in range(iterations):
        t0 = time.monotonic()
        interp.set_tensor(inp_details[0]['index'], dummy)
        interp.invoke()
        times.append((time.monotonic() - t0) * 1000)

    avg = sum(times) / len(times)
    mn = min(times)
    mx = max(times)
    print(f"\n{name} ({iterations} iterations):")
    print(f"  Avg: {avg:.1f}ms  Min: {mn:.1f}ms  Max: {mx:.1f}ms")
    print(f"  FPS (model only): {1000/avg:.1f}")
    return avg


def benchmark_with_camera(interp_det, inp_det, out_det,
                          interp_lm, inp_lm, out_lm,
                          iterations=50):
    """Full pipeline: camera capture → detection → landmark."""
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

    det_shape = inp_det[0]['shape']  # e.g. [1, 128, 128, 3]
    lm_shape = inp_lm[0]['shape']   # e.g. [1, 192, 192, 3]

    times_total = []
    times_det = []
    times_lm = []
    times_preproc = []

    for i in range(iterations):
        t_start = time.monotonic()

        ret, frame = cap.read()
        if not ret:
            continue

        # --- Preprocess for detection ---
        t0 = time.monotonic()
        det_h, det_w = det_shape[1], det_shape[2]
        det_input = cv2.resize(frame, (det_w, det_h))
        det_input = cv2.cvtColor(det_input, cv2.COLOR_BGR2RGB)
        det_input = det_input.astype(np.float32) / 127.5 - 1.0
        det_input = np.expand_dims(det_input, axis=0)
        t_preproc = (time.monotonic() - t0) * 1000

        # --- Face detection ---
        t0 = time.monotonic()
        interp_det.set_tensor(inp_det[0]['index'], det_input)
        interp_det.invoke()
        t_det = (time.monotonic() - t0) * 1000

        # --- Preprocess for landmarks (use full frame resized) ---
        lm_h, lm_w = lm_shape[1], lm_shape[2]
        lm_input = cv2.resize(frame, (lm_w, lm_h))
        lm_input = cv2.cvtColor(lm_input, cv2.COLOR_BGR2RGB)
        lm_input = lm_input.astype(np.float32) / 255.0
        lm_input = np.expand_dims(lm_input, axis=0)

        # --- Face landmarks ---
        t0 = time.monotonic()
        interp_lm.set_tensor(inp_lm[0]['index'], lm_input)
        interp_lm.invoke()
        t_lm = (time.monotonic() - t0) * 1000

        t_total = (time.monotonic() - t_start) * 1000

        times_preproc.append(t_preproc)
        times_det.append(t_det)
        times_lm.append(t_lm)
        times_total.append(t_total)

    cap.release()

    print(f"\nFull pipeline with camera ({len(times_total)} frames):")
    print(f"  Preprocess:  {sum(times_preproc)/len(times_preproc):.1f}ms avg")
    print(f"  Detection:   {sum(times_det)/len(times_det):.1f}ms avg")
    print(f"  Landmarks:   {sum(times_lm)/len(times_lm):.1f}ms avg")
    print(f"  Total:       {sum(times_total)/len(times_total):.1f}ms avg")
    print(f"  Pipeline FPS: {1000/(sum(times_total)/len(times_total)):.1f}")


def benchmark_landmark_only(interp_lm, inp_lm, out_lm, iterations=50):
    """Landmark-only pipeline: skip detection, resize full frame to 192x192."""
    cap = cv2.VideoCapture(0, cv2.CAP_V4L2)
    cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
    cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    if not cap.isOpened():
        print("ERROR: Cannot open camera")
        return

    for _ in range(10):
        cap.read()

    lm_shape = inp_lm[0]['shape']
    lm_h, lm_w = lm_shape[1], lm_shape[2]

    times = []
    for i in range(iterations):
        t_start = time.monotonic()

        ret, frame = cap.read()
        if not ret:
            continue

        # Resize + color convert
        lm_input = cv2.resize(frame, (lm_w, lm_h))
        lm_input = cv2.cvtColor(lm_input, cv2.COLOR_BGR2RGB)
        lm_input = lm_input.astype(np.float32) / 255.0
        lm_input = np.expand_dims(lm_input, axis=0)

        # Landmark inference
        interp_lm.set_tensor(inp_lm[0]['index'], lm_input)
        interp_lm.invoke()

        times.append((time.monotonic() - t_start) * 1000)

    cap.release()

    # Get output details
    for i, o in enumerate(out_lm):
        tensor = interp_lm.get_tensor(o['index'])
        print(f"  Output[{i}] shape={tensor.shape} min={tensor.min():.3f} max={tensor.max():.3f}")

    avg = sum(times) / len(times)
    print(f"\nLandmark-only pipeline ({len(times)} frames):")
    print(f"  Avg: {avg:.1f}ms  Min: {min(times):.1f}ms  Max: {max(times):.1f}ms")
    print(f"  FPS: {1000/avg:.1f}")


def main():
    print("=" * 60)
    print("TFLite Face Mesh Benchmark")
    print("=" * 60)

    # Load models
    det_interp, det_inp, det_out = load_model(
        f"{MODEL_DIR}/face_detection_short.tflite", num_threads=4)

    lm_interp, lm_inp, lm_out = load_model(
        f"{MODEL_DIR}/face_landmark_192.tflite", num_threads=4)

    # Check for the larger model from .task bundle
    try:
        lm2_interp, lm2_inp, lm2_out = load_model(
            f"{MODEL_DIR}/face_landmarks_detector.tflite", num_threads=4)
        has_lm2 = True
    except Exception:
        has_lm2 = False

    # 1. Raw model benchmarks (no camera)
    print("\n" + "=" * 60)
    print("1. Raw model inference (dummy data)")
    print("=" * 60)
    benchmark_model(det_interp, det_inp, "Face Detection (BlazeFace)")
    t_lm1 = benchmark_model(lm_interp, lm_inp, "Face Landmark (192, small)")
    if has_lm2:
        t_lm2 = benchmark_model(lm2_interp, lm2_inp, "Face Landmark (detector, large)")

    # 2. Landmark-only with camera (the fast path)
    print("\n" + "=" * 60)
    print("2. Landmark-only pipeline (no detection step)")
    print("=" * 60)
    benchmark_landmark_only(lm_interp, lm_inp, lm_out, iterations=100)

    # 3. Full detection + landmark pipeline
    print("\n" + "=" * 60)
    print("3. Full pipeline: detection + landmarks")
    print("=" * 60)
    benchmark_with_camera(det_interp, det_inp, det_out,
                          lm_interp, lm_inp, lm_out,
                          iterations=100)

    print("\n" + "=" * 60)
    print("Done!")
    print("=" * 60)


if __name__ == "__main__":
    main()
