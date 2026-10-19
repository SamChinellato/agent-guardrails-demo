"""Build-time download of the classifier files (GRD-01), each verified by SHA-256.
Used by services/Dockerfile's model stage; does nothing when CLASSIFIER_MODEL is empty."""

import hashlib
import os
import sys
import urllib.request

model, rev = os.environ.get("CLASSIFIER_MODEL", ""), os.environ.get("CLASSIFIER_REVISION", "")
os.makedirs("/models/onnx", exist_ok=True)
if not model:
    sys.exit(0)
files = {
    "model.onnx": os.environ["CLASSIFIER_ONNX_SHA256"],
    "tokenizer.json": os.environ["CLASSIFIER_TOKENIZER_SHA256"],
    "config.json": os.environ["CLASSIFIER_CONFIG_SHA256"],
}
for name, want in files.items():
    path = f"/models/onnx/{name}"
    urllib.request.urlretrieve(f"https://huggingface.co/{model}/resolve/{rev}/onnx/{name}", path)
    with open(path, "rb") as f:
        got = hashlib.sha256(f.read()).hexdigest()
    if got != want:
        sys.exit(f"checksum mismatch for {name}: {got} != {want}")
    print(f"{name}: sha256 ok")
