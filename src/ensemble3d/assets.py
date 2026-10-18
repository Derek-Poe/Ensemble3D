"""Small deterministic GLB fixture engine and immutable local asset storage.

These are synthetic geometry helpers added for the portfolio demonstration, not
the original generation engines or a general-purpose mesh importer.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import struct
import tempfile

GENERATOR = "Ensemble3D synthetic fixture v1"
TRIANGLES = (0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7, 0, 1, 5, 0, 5, 4,
             3, 7, 6, 3, 6, 2, 0, 4, 7, 0, 7, 3, 1, 2, 6, 1, 6, 5)


def positive_size(value):
    value = float(value)
    if not math.isfinite(value) or not 0.001 <= value <= 1000:
        raise ValueError("Synthetic dimensions must be finite and between 0.001 and 1000")
    return value


def pack_glb(document, binary):
    description = json.dumps(document, separators=(",", ":"), sort_keys=True).encode("utf-8")
    description += b" " * (-len(description) % 4)
    binary += b"\0" * (-len(binary) % 4)
    return (struct.pack("<4sII", b"glTF", 2, 28 + len(description) + len(binary))
            + struct.pack("<I4s", len(description), b"JSON") + description
            + struct.pack("<I4s", len(binary), b"BIN\0") + binary)


def cube_glb(size=1.0):
    half = positive_size(size) / 2
    vertices = [(-half, -half, -half), (half, -half, -half), (half, half, -half), (-half, half, -half),
                (-half, -half, half), (half, -half, half), (half, half, half), (-half, half, half)]
    binary = struct.pack("<24f", *(v for vertex in vertices for v in vertex)) + struct.pack("<36H", *TRIANGLES)
    document = {
        "asset": {"version": "2.0", "generator": GENERATOR}, "scene": 0,
        "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0, "name": "Synthetic cube"}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "mode": 4}]}],
        "buffers": [{"byteLength": len(binary)}],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 96, "target": 34962},
                        {"buffer": 0, "byteOffset": 96, "byteLength": 72, "target": 34963}],
        "accessors": [{"bufferView": 0, "componentType": 5126, "count": 8, "type": "VEC3",
                       "min": [-half] * 3, "max": [half] * 3},
                      {"bufferView": 1, "componentType": 5123, "count": 36, "type": "SCALAR"}],
    }
    return pack_glb(document, binary)


def unpack_fixture(data):
    """Read the deliberately fixed fixture layout; arbitrary GLB is not accepted."""
    if len(data) < 28:
        raise ValueError("Truncated GLB")
    magic, version, length = struct.unpack_from("<4sII", data)
    json_length, json_type = struct.unpack_from("<I4s", data, 12)
    if magic != b"glTF" or version != 2 or length != len(data) or json_type != b"JSON" or json_length % 4:
        raise ValueError("Invalid GLB container")
    offset = 20 + json_length
    if offset + 8 > len(data):
        raise ValueError("Truncated GLB chunks")
    document = json.loads(data[20:offset])
    binary_length, binary_type = struct.unpack_from("<I4s", data, offset)
    if (document.get("asset", {}).get("generator") != GENERATOR or binary_type != b"BIN\0"
            or binary_length != 168 or offset + 8 + binary_length != len(data)):
        raise ValueError("Only this package's tiny synthetic mesh layout is supported")
    binary = data[offset + 8:]
    if tuple(struct.unpack_from("<36H", binary, 96)) != TRIANGLES:
        raise ValueError("Unexpected synthetic topology")
    values = struct.unpack_from("<24f", binary)
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Non-finite vertex")
    return document, binary, [values[i:i + 3] for i in range(0, 24, 3)]


def scale_glb(data, factor):
    factor = positive_size(factor)
    document, binary, vertices = unpack_fixture(data)
    scaled = [[value * factor for value in vertex] for vertex in vertices]
    minimum = [min(vertex[i] for vertex in scaled) for i in range(3)]
    maximum = [max(vertex[i] for vertex in scaled) for i in range(3)]
    for low, high in zip(minimum, maximum):
        positive_size(high - low)
    document["accessors"][0]["min"] = minimum
    document["accessors"][0]["max"] = maximum
    binary = struct.pack("<24f", *(value for vertex in scaled for value in vertex)) + binary[96:]
    return pack_glb(document, binary)


class AssetStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, data):
        digest = hashlib.sha256(data).hexdigest()
        target = self.root / (digest + ".glb")
        if target.exists():
            self.read(digest)
            return digest
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, prefix=".writing-", delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                # Publish once without replacing a file another reader may have
                # open (especially on Windows). Same-directory hard links keep
                # the fully flushed bytes atomic under concurrent deduplication.
                os.link(temporary, target)
            except FileExistsError:
                self.read(digest)
            if os.name != "nt":
                directory = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()
        return digest

    def read(self, digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Invalid asset digest")
        data = (self.root / (digest + ".glb")).read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Stored asset digest mismatch")
        return data
