"""Deterministic recognition benchmark; never captures screens or sends input.

Run from any directory with the project's Python environment::

    python benchmarks/vision_benchmark.py --output build/benchmark/current.json
    python benchmarks/vision_benchmark.py --include-4k --repeats 5

An optional ``--engine-source`` loads a saved vision_engine.py for a same-machine
before/after comparison. Wall-clock timings include frame conversion, matching,
and a no-op callback, but exclude real screenshot/click latency and UI work.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import platform
import statistics
import sys
import time
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SEED = 20260919


def load_engine(path):
    spec = importlib.util.spec_from_file_location("benchmark_vision_engine", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def template_image(index=0):
    """UI-like coloured buttons with nonconstant borders, labels, and icons."""
    colors = [(30, 170, 240), (200, 100, 35), (70, 180, 65), (130, 40, 180)]
    image = np.full((48, 128, 3), colors[index % len(colors)], dtype=np.uint8)
    cv2.rectangle(image, (3, 3), (124, 44), (10, 40, 70), 2)
    labels = ["START", "NEXT", "STOP", "BACK"]
    cv2.putText(image, labels[index % len(labels)], (12, 33),
                cv2.FONT_HERSHEY_SIMPLEX, .8, (250, 250, 250), 2)
    return image


def background(width, height):
    rng = np.random.default_rng(SEED)
    image = rng.integers(24, 64, (height, width, 3), dtype=np.uint8)
    # Ordinary window-like edges keep this from being a blank-capture shortcut.
    for x in range(20, width, 240):
        cv2.rectangle(image, (x, 20), (min(x + 210, width - 1), height - 30),
                      (80, 85, 90), 1)
    return image


def cases(module, width, height):
    images = [template_image(index) for index in range(4)]
    base = background(width, height)

    def make(name, placements=(), templates=(0,), *, immediate=False,
             max_matches=1):
        frame = base.copy()
        expected = []
        for index, scale, x, y in placements:
            original = images[index]
            w, h = round(original.shape[1] * scale), round(original.shape[0] * scale)
            resized = cv2.resize(original, (w, h),
                                 interpolation=cv2.INTER_AREA if scale < 1 else cv2.INTER_LINEAR)
            frame[y:y + h, x:x + w] = resized
            expected.append((f"target-{index}", x + w // 2, y + h // 2))
        specs = [module.TemplateSpec(id=f"target-{index}", image=images[index],
                                    threshold=.94, cooldown=0, max_matches=max_matches)
                 for index in templates]
        engine = module.VisionEngine(specs, capture_fn=lambda *_: frame,
                                     immediate_click=immediate, auto_click=False,
                                     on_match=lambda match: None)
        return name, engine, expected, immediate

    x, y = width // 3 + 3, height // 3 + 1
    yield make("native", [(0, 1., x, y)])
    yield make("scaled_075", [(0, .75, x, y)])
    yield make("scaled_125", [(0, 1.25, x, y)])
    yield make("scaled_165", [(0, 1.65, x, y)])
    yield make("absent")
    yield make("multiple_templates", [(0, 1., 40, 80), (1, 1., x, y),
                                       (2, 1., width - 200, height - 160)], templates=(0, 1, 2))
    yield make("multiple_occurrences", [(0, 1., 40, 80), (0, 1., x, y),
                                         (0, 1., width - 200, height - 160)], max_matches=3)
    yield make("immediate_native", [(0, 1., x, y)], immediate=True)
    yield make("immediate_after_absent", [(0, 1., x, y)], templates=(1, 0), immediate=True)
    yield make("immediate_scaled_165", [(0, 1.65, x, y)], immediate=True)
    yield make("immediate_absent", templates=(0, 1, 2), immediate=True)

    def moving(name, scale, immediate=False):
        original = images[0]
        w, h = round(original.shape[1] * scale), round(original.shape[0] * scale)
        target = cv2.resize(original, (w, h), interpolation=cv2.INTER_LINEAR)
        positions = [(40, 80), (width - w - 50, height - h - 60),
                     (width // 2 + 3, 60), (60, height - h - 80)]
        frames = []
        for px, py in positions:
            frame = base.copy()
            frame[py:py + h, px:px + w] = target
            frames.append(frame)
        state = [-1]

        def capture(*_):
            state[0] = (state[0] + 1) % len(frames)
            return frames[state[0]]

        def expected():
            px, py = positions[state[0]]
            return [("target-0", px + w // 2, py + h // 2)]

        spec = module.TemplateSpec(id="target-0", image=original, threshold=.94, cooldown=0)
        engine = module.VisionEngine([spec], capture_fn=capture,
                                     immediate_click=immediate, auto_click=False,
                                     on_match=lambda match: None)
        return name, engine, expected, immediate

    # Every frame moves far outside the previous location; a location cache
    # must fall back within this scan and may never return a stale coordinate.
    yield moving("moving_native", 1.)
    yield moving("moving_scaled_165", 1.65)
    yield moving("immediate_moving_scaled_165", 1.65, immediate=True)


def correct_matches(matches, expected):
    remaining = list(expected() if callable(expected) else expected)
    for match in matches:
        found = next((index for index, (key, x, y) in enumerate(remaining)
                      if match.template_id == key and abs(match.center_x - x) <= 2
                      and abs(match.center_y - y) <= 2), None)
        if found is None:
            return False
        remaining.pop(found)
    return not remaining


def measure(name, engine, expected, immediate, repeats, warmups, max_discovery_scans):
    def scan():
        started = time.perf_counter()
        matches = engine.scan_once(trigger=immediate)
        return (time.perf_counter() - started) * 1000, matches

    first_ms, first_matches = scan()
    first_correct = correct_matches(first_matches, expected)
    discovery_ms = first_ms
    discovery_scans = 1
    discovered = first_correct
    matches = first_matches
    # Immediate mode spreads scale discovery over captures. Report its total
    # first-hit latency separately so warm-cache timings cannot hide that cost.
    while immediate and expected and not discovered and discovery_scans < max_discovery_scans:
        elapsed, matches = scan()
        discovery_ms += elapsed
        discovery_scans += 1
        discovered = correct_matches(matches, expected)
    for _ in range(warmups):
        scan()
    samples = []
    steady_correct = []
    errors = []
    for _ in range(repeats):
        elapsed, matches = scan()
        samples.append(elapsed)
        steady_correct.append(correct_matches(matches, expected))
        if engine.last_error is not None:
            errors.append(str(engine.last_error))
    return {
        "name": name,
        "first_scan_ms": round(first_ms, 3),
        "first_scan_correct": first_correct,
        "first_hit_ms": round(discovery_ms, 3) if expected and discovered else None,
        "first_hit_scans": discovery_scans if expected and discovered else None,
        "median_ms": round(statistics.median(samples), 3),
        "min_ms": round(min(samples), 3),
        "max_ms": round(max(samples), 3),
        "samples_ms": [round(value, 3) for value in samples],
        "correct": discovered and all(steady_correct) and not errors,
        "correct_steady_scans": sum(steady_correct),
        "expected_matches": len(expected() if callable(expected) else expected),
        "last_matches": [match.as_dict() for match in matches],
        "errors": errors,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-source", type=Path, default=ROOT / "vision_engine.py")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--max-discovery-scans", type=int, default=24)
    parser.add_argument("--include-4k", action="store_true")
    parser.add_argument("--case", action="append", help="Run only named cases; repeat to select several.")
    parser.add_argument("--threads", type=int, help="Explicit OpenCV thread count, otherwise keep its default.")
    args = parser.parse_args()
    if args.repeats < 1 or args.warmups < 0 or args.max_discovery_scans < 1:
        parser.error("repeats/discovery scans must be positive and warmups nonnegative")
    if args.threads is not None:
        cv2.setNumThreads(args.threads)
    source = args.engine_source.resolve()
    module = load_engine(source)
    report = {
        "metadata": {
            "engine_source": str(source),
            "engine_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
            "python": platform.python_version(), "platform": platform.platform(),
            "opencv": cv2.__version__, "numpy": np.__version__,
            "opencv_threads": cv2.getNumThreads(), "seed": SEED,
            "repeats": args.repeats, "warmups": args.warmups,
            "scope": "Synthetic in-memory captures; excludes OS capture, input, UI, and polling waits.",
        },
        "results": [],
    }
    resolutions = [(1920, 1080)] + ([(3840, 2160)] if args.include_4k else [])
    for width, height in resolutions:
        for name, engine, expected, immediate in cases(module, width, height):
            if args.case and name not in args.case:
                continue
            result = measure(name, engine, expected, immediate, args.repeats,
                             args.warmups, args.max_discovery_scans)
            result["resolution"] = [width, height]
            report["results"].append(result)
            print(f"{width}x{height} {name:26s} first={result['first_scan_ms']:8.2f} ms "
                  f"median={result['median_ms']:8.2f} ms correct={result['correct']} "
                  f"first_hit={result['first_hit_ms']} ms/{result['first_hit_scans']} scans", flush=True)
            # Checkpoint after each case, so a slow 4K sweep remains reviewable.
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    if not report["results"]:
        parser.error("no benchmark cases selected")
    return 0 if all(row["correct"] for row in report["results"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
