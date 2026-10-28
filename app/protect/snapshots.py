"""Bounded frame selection by simple exposure/contrast heuristic, never identity."""
import asyncio
import io
import av


def quality(jpeg):
    if len(jpeg) > 10_000_000:
        return -1
    try:
        with av.open(io.BytesIO(jpeg), format='mjpeg') as source:
            frame = next(source.decode(video=0))
            if frame.width * frame.height > 16_000_000:
                return -1
            gray = frame.reformat(width=64, height=64, format='gray')
            plane = gray.planes[0]
            raw = bytes(plane)
            values = [v for row in range(64) for v in raw[row * plane.line_size:row * plane.line_size + 64]]
            mean = sum(values) / len(values)
            contrast = sum((v - mean) ** 2 for v in values) / len(values)
            clipped = sum(v < 10 or v > 245 for v in values) / len(values)
            return contrast * (1 - clipped)
    except Exception:
        return -1


async def best_snapshot(adapter, count=3, interval=0.2, score=quality):
    best = None
    best_score = float('-inf')
    for index in range(count):
        try:
            jpeg = await adapter.snapshot()
            value = score(jpeg)
            if len(jpeg) <= 10_000_000 and (best is None or value > best_score):
                best, best_score = jpeg, value
        except Exception:
            pass
        if index + 1 < count:
            await asyncio.sleep(interval)
    if best is None:
        raise ValueError('No camera frame available')
    return best
