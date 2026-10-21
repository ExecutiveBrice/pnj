"""AudioPlayer: one persistent stream, block writes, cross-thread stop flag."""

import sys
from types import SimpleNamespace

import numpy as np

from npc.audio.player import BLOCK_FRAMES, AudioPlayer


class FakeStream:
    def __init__(self, samplerate, channels, dtype):
        self.samplerate = samplerate
        self.active = False
        self.written = 0
        self.aborted = 0
        self.started = 0
        self.stopped = 0
        self.pending = 0
        self.played = 0
        self.closed = False
        self.on_write = None

    def start(self):
        self.active = True
        self.started += 1

    def write(self, block):
        self.written += len(block)
        self.pending += len(block)
        if self.on_write:
            self.on_write()

    def stop(self):
        self.played += self.pending
        self.pending = 0
        self.active = False
        self.stopped += 1

    def abort(self):
        self.pending = 0
        self.active = False
        self.aborted += 1

    def close(self):
        self.closed = True


def make_player(monkeypatch):
    streams = []

    def output_stream(**kwargs):
        stream = FakeStream(**kwargs)
        streams.append(stream)
        return stream

    monkeypatch.setitem(sys.modules, "sounddevice",
                        SimpleNamespace(OutputStream=output_stream))
    return AudioPlayer(), streams


def test_stream_is_opened_once_and_reused(monkeypatch):
    player, streams = make_player(monkeypatch)
    clip = np.zeros(BLOCK_FRAMES * 3, dtype=np.int16)
    player.play(clip, 22_050)
    player.play(clip, 22_050)
    assert len(streams) == 1                       # no per-play open/close churn
    assert streams[0].written == len(clip) * 2
    assert not streams[0].closed
    assert streams[0].started == streams[0].stopped == 2
    assert streams[0].played == len(clip) * 2
    assert streams[0].pending == 0
    assert not streams[0].active


def test_sample_rate_change_is_the_only_reopen(monkeypatch):
    player, streams = make_player(monkeypatch)
    player.play(np.zeros(10, dtype=np.int16), 22_050)
    player.play(np.zeros(10, dtype=np.int16), 16_000)
    assert len(streams) == 2
    assert streams[0].closed
    assert streams[1].samplerate == 16_000


def test_stop_aborts_within_one_block_and_playback_recovers(monkeypatch):
    player, streams = make_player(monkeypatch)
    clip = np.zeros(BLOCK_FRAMES * 10, dtype=np.int16)

    player.play(np.zeros(4, dtype=np.int16), 22_050)
    stream = streams[0]
    stream.on_write = player.stop                  # barge-in during the 1st block
    player.play(clip, 22_050)
    assert stream.written <= 4 + BLOCK_FRAMES
    assert stream.aborted == 1

    stream.on_write = None                         # next reply plays fully again
    before = stream.written
    player.play(clip, 22_050)
    assert stream.written == before + len(clip)
    assert not stream.active


def test_short_clip_finishes_before_player_returns(monkeypatch):
    player, streams = make_player(monkeypatch)
    player.play(np.ones(100, dtype=np.int16), 22_050)
    stream = streams[0]
    assert stream.played == 100
    assert stream.pending == 0
    assert not stream.active


def test_stop_during_final_block_discards_buffered_tail(monkeypatch):
    player, streams = make_player(monkeypatch)
    player.play(np.zeros(4, dtype=np.int16), 22_050)
    stream = streams[0]
    before = stream.played
    stream.on_write = player.stop
    player.play(np.ones(100, dtype=np.int16), 22_050)
    assert stream.aborted == 1
    assert stream.played == before
    assert stream.pending == 0
    assert not stream.active


def test_write_failure_discards_audio_and_allows_reuse(monkeypatch):
    import pytest

    player, streams = make_player(monkeypatch)
    player.play(np.zeros(4, dtype=np.int16), 22_050)
    stream = streams[0]

    def fail():
        raise OSError("output disconnected")

    stream.on_write = fail
    with pytest.raises(OSError, match="output disconnected"):
        player.play(np.ones(100, dtype=np.int16), 22_050)
    assert stream.aborted == 1
    assert stream.pending == 0
    assert not stream.active

    stream.on_write = None
    player.play(np.ones(100, dtype=np.int16), 22_050)
    assert stream.played == 104
