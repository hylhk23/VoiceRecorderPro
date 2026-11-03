import numpy as np
import pytest

from vrp.core.audio_backend import scripted_source
from vrp.core.tsm import Wsola
from vrp.core.vox import VoxGate, VoxParams


def run_gate(spans, total, block, params=VoxParams(4, 20, 20, 2.0, 300, 0.5)):
    src = scripted_source(spans)
    g = VoxGate(16000, 1, params)
    pos, out, cur = 0, [], None
    while pos < total * 16000:
        for op in g.process(src(pos, block, 16000, 1), pos):
            if op[0] == "begin":
                cur = [op[1], op[1]]
            elif op[0] == "write":
                assert op[2] == cur[1], "writes must be contiguous"
                cur[1] = op[2] + op[1].shape[0]
            elif op[0] == "end":
                out.append(((cur[1] - cur[0]) / 16000))
                cur = None
        pos += block
    return out


@pytest.mark.parametrize("block", [256, 1024, 777, 4096])
def test_tc02_two_segments(block):                                    # TC-02
    segs = run_gate([(2, 5, 0.2), (10, 11, 0.2)], 14, block)
    assert len(segs) == 2
    assert abs(segs[0] - 5.3) < 0.025 and abs(segs[1] - 3.3) < 0.025


@pytest.mark.parametrize("block", [1024, 777])
def test_tc03_click_dropped(block):                                   # TC-03, FR-VOX-006
    segs = run_gate([(2, 5, 0.2), (7.5, 7.7, 0.5), (10, 11, 0.2)], 14, block)
    assert len(segs) == 2 and abs(segs[0] - 5.3) < 0.025


def test_sound_within_hang_extends_segment():
    segs = run_gate([(2, 5, 0.2), (6, 6.2, 0.5)], 10, 1024)
    assert len(segs) == 1 and abs(segs[0] - (0.3 + 4.2 + 2.0)) < 0.025


def test_threshold_zero_records_any_sound():
    segs = run_gate([(1, 2, 0.001)], 5, 1024, VoxParams(0, 20, 0, 1.0, 0, 0))
    assert len(segs) == 1


@pytest.mark.parametrize("speed", [0.5, 0.75, 1.25, 2.0])
def test_wsola_length(speed):
    rate = 16000
    x = (0.3 * np.sin(2 * np.pi * 220 * np.arange(rate * 3) / rate)).astype(np.float32)[:, None]
    t = Wsola(rate, 1, speed)
    out = [t.process(x[i:i + 2048]) for i in range(0, len(x), 2048)]
    out.append(t.process(np.zeros((0, 1), np.float32), final=True))
    y = np.concatenate(out)
    assert abs(len(y) - len(x) / speed) < rate * 0.1
    assert np.isfinite(y).all() and np.max(np.abs(y)) < 0.6
