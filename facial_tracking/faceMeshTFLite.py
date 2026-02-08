"""Drop-in replacement for MediaPipe FaceMesh using direct TFLite inference.

Uses tflite-runtime with BlazeFace (detection) + Face Landmark (468 points).
Provides the same interface as the MediaPipe-based FaceMesh class so that
Eye, Lips, Iris, and FacialTracker classes work unchanged.

Typical pipeline:
  Frame 1:  detection (40ms) + landmarks (58ms) = ~100ms
  Frame 2+: landmarks only (58ms) using tracked face box = ~58ms  (17 FPS)
  Re-detect every N frames or when face confidence drops.
"""

import os
import cv2
import numpy as np
import facial_tracking.conf as conf

try:
    from tflite_runtime.interpreter import Interpreter
except ImportError:
    from tensorflow.lite import Interpreter


# ---------------------------------------------------------------------------
# Compatibility objects — mimic MediaPipe's landmark interface
# ---------------------------------------------------------------------------

class _LandmarkPoint:
    """Single landmark with normalized x, y, z coordinates (0-1 range)."""
    __slots__ = ('x', 'y', 'z')

    def __init__(self, x, y, z=0.0):
        self.x = x
        self.y = y
        self.z = z


class _FaceLandmarks:
    """Container matching mediapipe face_landmarks.landmark[index] access."""
    __slots__ = ('landmark',)

    def __init__(self, landmarks):
        self.landmark = landmarks  # list of _LandmarkPoint


class _MeshResult:
    """Container matching mesh_result.multi_face_landmarks."""
    __slots__ = ('multi_face_landmarks',)

    def __init__(self, face_landmarks_list=None):
        self.multi_face_landmarks = face_landmarks_list


# ---------------------------------------------------------------------------
# BlazeFace anchor generation
# ---------------------------------------------------------------------------

def _generate_ssd_anchors(input_size=128, strides=(8, 16), anchors_per_loc=(2, 6)):
    """Generate SSD anchors for BlazeFace short-range model.

    Short range: 2 anchors at stride 8 (16x16 grid = 512)
                 6 anchors at stride 16 (8x8 grid = 384)
                 Total = 896 anchors.
    """
    anchors = []
    for stride, n in zip(strides, anchors_per_loc):
        grid = input_size // stride
        for y in range(grid):
            for x in range(grid):
                cx = (x + 0.5) / grid
                cy = (y + 0.5) / grid
                for _ in range(n):
                    anchors.append((cx, cy))
    return np.array(anchors, dtype=np.float32)


# ---------------------------------------------------------------------------
# FaceMesh — TFLite implementation
# ---------------------------------------------------------------------------

class FaceMesh:
    """TFLite-based face mesh that matches the MediaPipe FaceMesh interface.

    Args:
        max_num_faces: ignored (always 1 for performance)
        refine_landmarks: ignored (small model has 468 landmarks, no iris)
        min_detection_confidence: threshold for BlazeFace detection
        min_tracking_confidence: threshold for landmark face-presence score
    """

    # Model paths — configurable via env or default to models/ subdir
    _MODEL_DIR = os.getenv('FACE_MODEL_DIR',
                           os.path.join(os.path.dirname(os.path.dirname(__file__)), 'models'))
    _DET_MODEL = 'face_detection_short.tflite'
    _LM_MODEL = 'face_landmark_192.tflite'

    # How often to re-run face detection (every N frames)
    _DETECT_INTERVAL = int(os.getenv('DETECT_INTERVAL', '30'))

    # Margin around detection box for landmark crop (fraction of box size)
    _CROP_MARGIN = 0.35

    def __init__(self, max_num_faces=1, refine_landmarks=None,
                 min_detection_confidence=conf.MIN_DETECTION_CONFIDENCE,
                 min_tracking_confidence=conf.MIN_TRACKING_CONFIDENCE):

        self._det_threshold = min_detection_confidence
        self._lm_threshold = 0.0  # raw logit; >0 means face present (sigmoid > 0.5)

        # Load detection model
        det_path = os.path.join(self._MODEL_DIR, self._DET_MODEL)
        self._det = Interpreter(model_path=det_path, num_threads=4)
        self._det.allocate_tensors()
        self._det_inp = self._det.get_input_details()
        self._det_out = self._det.get_output_details()
        self._det_size = self._det_inp[0]['shape'][1]  # 128

        # Load landmark model
        lm_path = os.path.join(self._MODEL_DIR, self._LM_MODEL)
        self._lm = Interpreter(model_path=lm_path, num_threads=4)
        self._lm.allocate_tensors()
        self._lm_inp = self._lm.get_input_details()
        self._lm_out = self._lm.get_output_details()
        self._lm_size = self._lm_inp[0]['shape'][1]  # 192

        # Pre-compute BlazeFace anchors
        self._anchors = _generate_ssd_anchors(self._det_size)

        # State
        self._face_box = None  # [x1, y1, x2, y2] normalized 0-1
        self._frame_count = 0
        self.frame = None
        self.mesh_result = _MeshResult()

        # Drawing stubs (for compatibility)
        self.mp_drawing = None
        self.mp_drawing_styles = None
        self.mp_face_mesh = None

    # ------------------------------------------------------------------
    # Public API (matches MediaPipe FaceMesh interface)
    # ------------------------------------------------------------------

    def process_frame(self, frame):
        """Process a BGR frame and populate mesh_result."""
        self.frame = frame
        self._frame_count += 1

        # Decide whether to run face detection
        need_detect = (
            self._face_box is None
            or self._frame_count % self._DETECT_INTERVAL == 1
        )

        if need_detect:
            self._face_box = self._detect_face(frame)

        if self._face_box is None:
            self.mesh_result = _MeshResult()
            return

        # Run landmarks on the cropped face region
        landmarks = self._run_landmarks(frame, self._face_box)
        if landmarks is not None:
            fl = _FaceLandmarks(landmarks)
            self.mesh_result = _MeshResult([fl])
            # Update face box from landmarks for next-frame tracking
            self._face_box = self._box_from_landmarks(landmarks)
        else:
            # Lost face — force re-detection next frame
            self._face_box = None
            self.mesh_result = _MeshResult()

    # Drawing stubs — no-ops (HEADLESS mode typical on Pi)
    def draw_mesh(self):
        pass

    def draw_mesh_eyes(self):
        pass

    def draw_mesh_lips(self):
        pass

    # ------------------------------------------------------------------
    # Face detection (BlazeFace)
    # ------------------------------------------------------------------

    def _detect_face(self, frame):
        """Run BlazeFace and return the best face box [x1,y1,x2,y2] normalized."""
        h, w = frame.shape[:2]
        sz = self._det_size

        # Preprocess: resize, RGB, normalize to [-1, 1]
        img = cv2.resize(frame, (sz, sz))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = (img.astype(np.float32) - 127.5) / 127.5
        img = np.expand_dims(img, 0)

        self._det.set_tensor(self._det_inp[0]['index'], img)
        self._det.invoke()

        # Output 0: regressors [1, 896, 16], Output 1: classificators [1, 896, 1]
        raw_boxes = self._det.get_tensor(self._det_out[0]['index'])
        raw_scores = self._det.get_tensor(self._det_out[1]['index'])

        return self._decode_detections(raw_boxes, raw_scores)

    def _decode_detections(self, raw_boxes, raw_scores):
        """Decode BlazeFace SSD output to a face bounding box."""
        raw = np.clip(raw_scores[0, :, 0], -80.0, 80.0)
        scores = 1.0 / (1.0 + np.exp(-raw))  # sigmoid

        # Filter by threshold
        mask = scores > self._det_threshold
        if not np.any(mask):
            return None

        filtered_boxes = raw_boxes[0][mask]
        filtered_scores = scores[mask]
        filtered_anchors = self._anchors[mask]

        # Pick the highest confidence detection
        best = np.argmax(filtered_scores)
        box = filtered_boxes[best]
        anchor = filtered_anchors[best]

        # Decode box: offsets are relative to anchor, in pixel coords of 128x128
        sz = float(self._det_size)
        cx = box[0] / sz + anchor[0]
        cy = box[1] / sz + anchor[1]
        bw = box[2] / sz
        bh = box[3] / sz

        x1 = cx - bw / 2.0
        y1 = cy - bh / 2.0
        x2 = cx + bw / 2.0
        y2 = cy + bh / 2.0

        return self._expand_and_square(x1, y1, x2, y2)

    def _expand_and_square(self, x1, y1, x2, y2):
        """Expand box by margin and make it square (landmark model needs square input)."""
        w = x2 - x1
        h = y2 - y1
        margin = self._CROP_MARGIN

        # Expand
        x1 -= w * margin
        y1 -= h * margin
        x2 += w * margin
        y2 += h * margin

        # Make square (use the larger dimension)
        w = x2 - x1
        h = y2 - y1
        if w > h:
            diff = (w - h) / 2
            y1 -= diff
            y2 += diff
        else:
            diff = (h - w) / 2
            x1 -= diff
            x2 += diff

        # Clamp to [0, 1]
        x1 = max(0.0, x1)
        y1 = max(0.0, y1)
        x2 = min(1.0, x2)
        y2 = min(1.0, y2)

        return [x1, y1, x2, y2]

    # ------------------------------------------------------------------
    # Face landmarks
    # ------------------------------------------------------------------

    def _run_landmarks(self, frame, box):
        """Crop face, run landmark model, return list of _LandmarkPoint or None."""
        h, w = frame.shape[:2]
        x1, y1, x2, y2 = box

        # Pixel coordinates for crop
        px1 = max(0, int(x1 * w))
        py1 = max(0, int(y1 * h))
        px2 = min(w, int(x2 * w))
        py2 = min(h, int(y2 * h))

        if px2 - px1 < 10 or py2 - py1 < 10:
            return None

        crop = frame[py1:py2, px1:px2]

        sz = self._lm_size
        img = cv2.resize(crop, (sz, sz))
        img = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img = img.astype(np.float32) / 255.0
        img = np.expand_dims(img, 0)

        self._lm.set_tensor(self._lm_inp[0]['index'], img)
        self._lm.invoke()

        # Output 0: landmarks [1,1,1,1404] = 468*3 (x,y,z in 192x192 space)
        # Output 1: face presence [1,1,1,1] (logit)
        raw_lm = self._lm.get_tensor(self._lm_out[0]['index'])
        raw_conf = self._lm.get_tensor(self._lm_out[1]['index'])

        face_score = raw_conf.flat[0]
        if face_score < self._lm_threshold:
            return None

        # Decode landmarks: reshape to (468, 3), normalize to 0-1 in crop space,
        # then map to full-frame normalized coordinates.
        pts = raw_lm.reshape(468, 3)
        pts[:, 0] /= float(sz)  # x: 0-1 within crop
        pts[:, 1] /= float(sz)  # y: 0-1 within crop
        pts[:, 2] /= float(sz)  # z: depth (relative)

        # Map from crop-normalized to frame-normalized
        box_w = x2 - x1
        box_h = y2 - y1
        pts[:, 0] = pts[:, 0] * box_w + x1
        pts[:, 1] = pts[:, 1] * box_h + y1

        return [_LandmarkPoint(float(p[0]), float(p[1]), float(p[2])) for p in pts]

    def _box_from_landmarks(self, landmarks):
        """Compute a bounding box from landmarks for next-frame tracking."""
        xs = [lm.x for lm in landmarks]
        ys = [lm.y for lm in landmarks]
        x1 = min(xs)
        y1 = min(ys)
        x2 = max(xs)
        y2 = max(ys)
        return self._expand_and_square(x1, y1, x2, y2)
