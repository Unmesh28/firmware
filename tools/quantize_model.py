#!/usr/bin/env python3
"""Quantize face landmark TFLite model from float32 to int8.

Int8 inference on ARM Cortex-A53 can be 2-3x faster than float32.

Usage:
    pip install tensorflow  # needed for quantization (run on desktop, not Pi)
    python3 quantize_model.py models/face_landmark_192.tflite models/face_landmark_192_int8.tflite
"""

import sys
import numpy as np


def quantize(input_path, output_path):
    try:
        import tensorflow as tf
    except ImportError:
        print("ERROR: tensorflow required for quantization. Install on desktop machine.")
        print("  pip install tensorflow")
        sys.exit(1)

    # Load the float model
    converter = tf.lite.TFLiteConverter.from_saved_model.__func__
    # Load from .tflite directly
    with open(input_path, 'rb') as f:
        model_content = f.read()

    interpreter = tf.lite.Interpreter(model_content=model_content)
    interpreter.allocate_tensors()
    inp = interpreter.get_input_details()[0]
    print(f"Input: {inp['shape']} {inp['dtype']}")

    # Representative dataset for calibration
    def representative_dataset():
        for _ in range(100):
            data = np.random.rand(*inp['shape']).astype(np.float32)
            yield [data]

    # Convert with dynamic range quantization (simplest, no calibration needed)
    converter = tf.lite.TFLiteConverter.experimental_from_buffer(model_content)
    converter.optimizations = [tf.lite.Optimize.DEFAULT]
    converter.representative_dataset = representative_dataset
    converter.target_spec.supported_ops = [tf.lite.OpsSet.TFLITE_BUILTINS_INT8]
    converter.inference_input_type = tf.uint8
    converter.inference_output_type = tf.float32

    quantized = converter.convert()

    with open(output_path, 'wb') as f:
        f.write(quantized)

    print(f"Quantized: {len(model_content)} -> {len(quantized)} bytes")
    print(f"Saved to: {output_path}")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"Usage: {sys.argv[0]} <input.tflite> <output.tflite>")
        sys.exit(1)
    quantize(sys.argv[1], sys.argv[2])
