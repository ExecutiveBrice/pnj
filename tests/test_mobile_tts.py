"""The audio observer sees played PCM without repeating Piper synthesis."""

import threading

import numpy as np

from test_tts import RecordingPlayer, StubPiperSpeaker


def test_plain_say_observes_audio_and_preserves_local_playback():
    player = RecordingPlayer()
    speaker = StubPiperSpeaker(player)
    audio = []
    speaker.on_audio = lambda samples, rate: audio.append((samples.copy(), rate))
    speaker.say("Hello")
    assert player.played == [5]
    assert len(audio) == 1
    assert audio[0][1] == 22050
    np.testing.assert_array_equal(audio[0][0], np.ones(5, dtype=np.int16))


def test_stream_observes_each_played_sentence_in_order():
    player = RecordingPlayer()
    speaker = StubPiperSpeaker(player)
    observed = []
    speaker.on_audio = lambda samples, rate: observed.append(len(samples))
    assert speaker.say_stream(iter(["one", "four", "fives"])) == (["one", "four", "fives"], False)
    assert observed == player.played == [3, 4, 5]


def test_observer_failure_cannot_break_local_audio():
    speaker = StubPiperSpeaker(RecordingPlayer())

    def fail(samples, rate):
        raise OSError("network unavailable")

    speaker.on_audio = fail
    speaker.say("one")
    assert speaker.say_stream(iter(["two"])) == (["two"], False)
    assert speaker._player.played == [3, 3]


def test_client_only_output_publishes_once_without_pc_audio():
    speaker = StubPiperSpeaker(RecordingPlayer())
    speaker.local_playback = False
    observed = []
    speaker.on_audio = lambda samples, rate: observed.append((len(samples), rate))
    speaker.say("Hello")
    assert speaker.say_stream(iter(["one", "four"])) == (["one", "four"], False)
    assert observed == [(5, 22050), (3, 22050), (4, 22050)]
    assert speaker._player.played == []


def test_cancelled_sentences_are_not_broadcast():
    playing = threading.Event()

    class BlockingPlayer(RecordingPlayer):
        def __init__(self):
            super().__init__()
            self.release = threading.Event()

        def play(self, samples, sample_rate):
            super().play(samples, sample_rate)
            playing.set()
            assert self.release.wait(timeout=2)

        def stop(self):
            self.release.set()

    speaker = StubPiperSpeaker(BlockingPlayer())
    observed = []
    speaker.on_audio = lambda samples, rate: observed.append(len(samples))
    worker = threading.Thread(target=speaker.say_stream, args=(iter(["one", "four", "fives"]),))
    worker.start()
    assert playing.wait(timeout=2)
    speaker.stop()
    worker.join(timeout=2)
    assert not worker.is_alive()
    assert observed == speaker._player.played == [3]
