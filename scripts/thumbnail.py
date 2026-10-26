"""Picks the episode thumbnail frame.

Looks at frames across the middle of the video and keeps the sharpest one
where the teacher's face is found with both eyes open (so no mid-blink
frames). Falls back to the exact middle frame if nothing qualifies or
OpenCV isn't installed.
"""
import os
import subprocess
import tempfile


def _grab(mp4, second, out):
    subprocess.run(["ffmpeg", "-y", "-ss", f"{second:.2f}", "-i", mp4,
                    "-frames:v", "1", "-q:v", "2", out],
                   check=True, capture_output=True)


def _score(path, cv2):
    img = cv2.imread(path)
    if img is None:
        return None
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if gray.mean() < 40:  # black / near-black frame
        return None
    faces = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml") \
        .detectMultiScale(gray, 1.1, 6, minSize=(gray.shape[1] // 10, gray.shape[1] // 10))
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
    upper = gray[y:y + h // 2 + h // 10, x:x + w]
    eyes = 0
    for name in ("haarcascade_eye_tree_eyeglasses.xml", "haarcascade_eye.xml"):
        found = cv2.CascadeClassifier(cv2.data.haarcascades + name) \
            .detectMultiScale(upper, 1.1, 5, minSize=(w // 10, w // 10))
        eyes = max(eyes, len(found))
    if eyes < 2:  # eyes closed (or face turned away)
        return None
    face = gray[y:y + h, x:x + w]
    return cv2.Laplacian(face, cv2.CV_64F).var()  # sharpness


def choose_frame(mp4, duration, out_jpg, at_seconds=None):
    """Writes the chosen frame to out_jpg. Returns the second it came from."""
    if at_seconds is not None:
        _grab(mp4, at_seconds, out_jpg)
        return at_seconds
    mid = max(1.0, duration / 2)
    try:
        import cv2
    except ImportError:
        _grab(mp4, mid, out_jpg)
        return mid
    start, end = duration * 0.35, duration * 0.65
    step = max(1.0, (end - start) / 40)
    best = None
    with tempfile.TemporaryDirectory() as d:
        t = start
        while t <= end:
            p = os.path.join(d, f"{t:.2f}.jpg")
            try:
                _grab(mp4, t, p)
                s = _score(p, cv2)
            except Exception:
                s = None
            if s is not None and (best is None or s > best[0]):
                best = (s, t)
            t += step
    chosen = best[1] if best else mid
    _grab(mp4, chosen, out_jpg)
    return chosen
