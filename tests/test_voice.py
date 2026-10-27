import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from kit.tools import voice_note


class FfmpegArgs(unittest.TestCase):
    def test_command_timeout_and_no_shell(self):
        done = subprocess.CompletedProcess([], 0, stdout=b"RIFF....WAVE", stderr=b"")
        with mock.patch.object(voice_note.subprocess, "run", return_value=done) as run:
            self.assertEqual(voice_note.to_wav(b"OggS", ffmpeg="/x/ffmpeg"), b"RIFF....WAVE")
        args, kwargs = run.call_args
        cmd = args[0]
        self.assertIsInstance(cmd, list)
        self.assertEqual(cmd[0], "/x/ffmpeg")
        i = cmd.index("-i")
        before = cmd[:i]
        self.assertIn("-nostdin", before)
        self.assertEqual(before[before.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(before[before.index("-f") + 1], "ogg")
        self.assertEqual(cmd[i + 1], "pipe:0")
        after = cmd[i + 2:]
        self.assertEqual(after[after.index("-t") + 1], "120")
        self.assertEqual(after[after.index("-fs") + 1], str(voice_note.MAX_WAV_BYTES))
        self.assertEqual(cmd[-3:], ["-f", "wav", "pipe:1"])
        self.assertEqual(kwargs["timeout"], 30)
        self.assertIs(kwargs.get("shell", False), False)
        self.assertEqual(kwargs["input"], b"OggS")
        self.assertTrue(kwargs["check"])
        self.assertEqual((voice_note.MAX_SECONDS, voice_note.FFMPEG_TIMEOUT), (120, 30))
        self.assertLessEqual(voice_note.MAX_WAV_BYTES, 4 * 1000 * 1000)

    def test_output_over_the_cap_is_refused(self):
        big = subprocess.CompletedProcess([], 0, stdout=b"x" * (voice_note.MAX_WAV_BYTES + 1), stderr=b"")
        with mock.patch.object(voice_note.subprocess, "run", return_value=big):
            with self.assertRaises(ValueError):
                voice_note.to_wav(b"OggS", ffmpeg="/x/ffmpeg")


FFMPEG = shutil.which("ffmpeg") or (voice_note.DEFAULT_FFMPEG if Path(voice_note.DEFAULT_FFMPEG).exists() else None)


@unittest.skipUnless(FFMPEG, "ffmpeg is not installed")
class RealFfmpeg(unittest.TestCase):
    def make_ogg(self, seconds):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = Path(d) / "note.ogg"
        made = subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin", "-f", "lavfi",
                               "-i", "anullsrc=r=48000:cl=mono", "-t", str(seconds), "-c:a", "libopus",
                               "-b:a", "6k", str(path)], capture_output=True, timeout=60)
        if made.returncode != 0:
            self.skipTest("this ffmpeg cannot make an opus test file")
        return path.read_bytes()

    def test_tiny_ogg_becomes_wav(self):
        wav = voice_note.to_wav(self.make_ogg(1), ffmpeg=FFMPEG)
        self.assertEqual((wav[:4], wav[8:12]), (b"RIFF", b"WAVE"))
        self.assertAlmostEqual(len(wav), 32000, delta=2000)

    def test_long_note_stops_at_120_seconds(self):
        wav = voice_note.to_wav(self.make_ogg(130), ffmpeg=FFMPEG)
        self.assertLessEqual(len(wav), voice_note.MAX_WAV_BYTES)
        self.assertAlmostEqual(len(wav), 120 * 32000, delta=4000)

    def test_not_ogg_is_refused(self):
        with self.assertRaises(subprocess.CalledProcessError):
            voice_note.to_wav(b"#EXTM3U\n#EXT-X-ENDLIST\n", ffmpeg=FFMPEG)


if __name__ == "__main__":
    unittest.main()
