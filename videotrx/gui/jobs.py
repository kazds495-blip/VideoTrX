from __future__ import annotations

import codecs
import re
import sys
from pathlib import Path

from PySide6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, Signal

from videotrx import config, paths

# 烧录用的视频编码器，按优先级排列；检测结果在进程内缓存
VIDEO_ENCODERS = (
    ("NVIDIA", ["-c:v", "hevc_nvenc", "-preset", "p4", "-rc", "constqp", "-qp", "23"]),
    ("AMD", ["-c:v", "hevc_amf", "-quality", "speed", "-rc", "cqp", "-qp_i", "23", "-qp_p", "23", "-qp_b", "23"]),
    ("CPU", ["-c:v", "libx265", "-preset", "fast", "-crf", "23"]),
)
_detected_encoder = None


class ProcessJob(QObject):
    """单个 QProcess 子进程任务：合并输出通道、按行解析输出，完成信号只发一次。"""

    log = Signal(str)
    finished = Signal(bool, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._process = QProcess(self)
        self._process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self._process.readyReadStandardOutput.connect(self._read_output)
        self._process.finished.connect(self._on_process_finished)
        self._process.errorOccurred.connect(self._on_process_error)
        self._reset()

    @property
    def stopped(self) -> bool:
        """本次运行是否由 stop() 手动停止。"""
        return self._stopped

    def is_running(self) -> bool:
        return self._process.state() != QProcess.ProcessState.NotRunning

    def stop(self):
        if not self.is_running():
            return
        self._stopped = True
        self._process.terminate()
        QTimer.singleShot(1500, self._kill_if_running)

    def _reset(self):
        self._buffer = ""
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._completion_emitted = False
        self._stopped = False

    def _kill_if_running(self):
        if self.is_running():
            self._process.kill()

    def _read_output(self):
        text = self._decoder.decode(bytes(self._process.readAllStandardOutput()))
        self._buffer += text.replace("\r", "\n")
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            line = line.strip()
            if line:
                self._handle_line(line)

    def _on_process_finished(self, exit_code: int, exit_status):
        self._read_output()
        rest = (self._buffer + self._decoder.decode(b"", final=True)).strip()
        self._buffer = ""
        if rest:
            self._handle_line(rest)
        if not self._completion_emitted:
            self._on_exit(exit_code, exit_status == QProcess.ExitStatus.NormalExit)

    def _on_process_error(self, error):
        if error == QProcess.ProcessError.FailedToStart and not self._completion_emitted:
            self._on_failed_to_start()

    def _emit_finished(self, success: bool, message: str):
        if self._completion_emitted:
            return
        self._completion_emitted = True
        self.finished.emit(success, message)

    def _handle_line(self, line: str):
        self.log.emit(line)

    def _on_exit(self, exit_code: int, normal_exit: bool):
        raise NotImplementedError

    def _on_failed_to_start(self):
        raise NotImplementedError


class YtDlpJob(ProcessJob):
    progress = Signal(int)

    def __init__(self, kind: str, parent: QObject | None = None):
        super().__init__(parent)
        self.kind = kind
        self._final_path = ""

    def start(self, url: str, output_dir: str):
        if self.is_running():
            return

        self._reset()
        self._final_path = ""

        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        try:
            args = self.build_args(url, output_path)
        except Exception as error:
            self.log.emit(f"读取 config.yaml 失败：{error}")
            self._emit_finished(False, "")
            return

        label = "最佳音频" if self.kind == "audio" else "最佳音视频"
        self.log.emit(f"开始下载{label}")
        self._process.start(sys.executable, args)

    def build_args(self, url: str, output_path: Path) -> list[str]:
        media_format = "bestaudio/best" if self.kind == "audio" else "bestvideo+bestaudio/best"
        suffix = "_audio" if self.kind == "audio" else "_best"
        output_template = str(output_path / f"%(title).180B{suffix}.%(ext)s")

        args = [
            "-m",
            "yt_dlp",
            "--format",
            media_format,
            "--output",
            output_template,
            "--no-playlist",
            "--progress",
            "--newline",
            "--no-color",
            "--encoding",
            "utf-8",
            "--windows-filenames",
            "--retries",
            "0",
            "--fragment-retries",
            "0",
            "--extractor-retries",
            "0",
            "--print",
            "after_move:__FINAL_PATH__:%(filepath)s",
        ]
        proxy = str(config.get("download.proxy", "") or "").strip()
        if proxy:
            args.extend(["--proxy", proxy])
        cookies = str(config.get("download.cookies_from_browser", "") or "").strip()
        if cookies:
            args.extend(["--cookies-from-browser", cookies])
        # paths.tool 找不到 bin/ 下的程序时原样返回名字
        if paths.tool("ffmpeg") != "ffmpeg":
            args.extend(["--ffmpeg-location", str(paths.BIN_DIR)])
        deno = paths.tool("deno")
        if deno != "deno":
            args.extend(["--js-runtimes", f"deno:{deno}"])
        if self.kind == "video":
            args.extend(["--write-thumbnail", "--convert-thumbnails", "png"])
        args.append(url)
        return args

    def stop(self):
        if self.is_running():
            self.log.emit("正在停止 yt-dlp")
        super().stop()

    def _handle_line(self, line: str):
        if line.startswith("__FINAL_PATH__:"):
            path = line.removeprefix("__FINAL_PATH__:").strip()
            if Path(path).suffix.lower() not in {".jpg", ".jpeg", ".png", ".webp", ".avif", ".gif"}:
                self._final_path = path
            return

        match = re.search(r"\[download\]\s+(\d+(?:\.\d+)?)%", line)
        if match:
            self.progress.emit(min(100, int(float(match.group(1)))))

        self.log.emit(line)

    def _on_exit(self, exit_code: int, normal_exit: bool):
        if exit_code == 0 and normal_exit:
            self.progress.emit(100)
            self._emit_finished(True, self._final_path)
            return

        self.log.emit("下载已停止" if self._stopped else f"yt-dlp 退出，代码 {exit_code}")
        self._emit_finished(False, "")

    def _on_failed_to_start(self):
        self.log.emit("无法启动 yt-dlp，请确认当前 Python 环境已安装 yt-dlp")
        self._emit_finished(False, "")


class PipelineJob(ProcessJob):
    """在子进程中运行 python -m videotrx.pipeline，并解析它输出的 @ 协议行。"""

    progress = Signal(str, int)
    stage = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._result_message = ""

    def start(self, audio: str | None = None):
        if self.is_running():
            return

        self._reset()
        self._result_message = ""
        env = QProcessEnvironment.systemEnvironment()
        env.insert("PYTHONIOENCODING", "utf-8")
        env.insert("PYTHONUNBUFFERED", "1")
        self._process.setProcessEnvironment(env)
        self._process.setWorkingDirectory(str(paths.ROOT))

        args = ["-m", "videotrx.pipeline"]
        if audio:
            args.extend(["--audio", audio])
        self._process.start(sys.executable, args)

    def _handle_line(self, line: str):
        kind, _, text = line.partition(" ")
        text = text.strip()
        if kind == "@progress":
            name, _, value = text.partition(" ")
            try:
                self.progress.emit(name, max(0, min(100, int(value))))
            except ValueError:
                self.log.emit(line)
        elif kind == "@stage":
            self.stage.emit(text)
        elif kind in ("@done", "@error"):
            self._result_message = text
        else:
            self.log.emit(line)

    def _on_exit(self, exit_code: int, normal_exit: bool):
        if exit_code == 0 and normal_exit:
            self._emit_finished(True, self._result_message or "字幕流程完成")
        elif self._stopped:
            self._emit_finished(False, "字幕流程已停止")
        else:
            self._emit_finished(False, self._result_message or f"字幕流程退出，代码 {exit_code}")

    def _on_failed_to_start(self):
        self._emit_finished(False, "无法启动字幕流程")


class BurnJob(QObject):
    log = Signal(str)
    progress = Signal(int)
    finished = Signal(bool, str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._probe = QProcess(self)
        self._encoder_probe = QProcess(self)
        self._ffmpeg = QProcess(self)

        self._probe.finished.connect(self._on_probe_finished)
        self._probe.errorOccurred.connect(self._on_probe_error)

        self._encoder_probe.finished.connect(self._on_encoder_probe_finished)
        self._encoder_probe.errorOccurred.connect(self._on_ffmpeg_error)

        self._ffmpeg.readyReadStandardOutput.connect(self._read_progress)
        self._ffmpeg.readyReadStandardError.connect(self._read_ffmpeg_log)
        self._ffmpeg.finished.connect(self._on_ffmpeg_finished)
        self._ffmpeg.errorOccurred.connect(self._on_ffmpeg_error)

        self._video_path = ""
        self._subtitle_path = ""
        self._output_path = ""
        self._duration = 0.0
        self._progress_buffer = ""
        self._encoder_index = 0
        self._completion_emitted = False
        self._stopped = False

    def is_running(self) -> bool:
        return any(
            process.state() != QProcess.ProcessState.NotRunning
            for process in (self._probe, self._encoder_probe, self._ffmpeg)
        )

    def start(self, video_path: str, subtitle_path: str, output_dir: str):
        if self.is_running():
            return

        self._video_path = str(Path(video_path).resolve()) if video_path else ""
        self._subtitle_path = str(Path(subtitle_path).resolve())
        output_folder = Path(output_dir)
        output_folder.mkdir(parents=True, exist_ok=True)
        self._output_path = str(output_folder / f"{Path(video_path).stem}_with_subtitles.mp4")
        self._duration = 0.0
        self._progress_buffer = ""
        self._completion_emitted = False
        self._stopped = False
        self.progress.emit(0)
        self.log.emit("读取视频时长")

        self._probe.start(
            paths.tool("ffprobe"),
            [
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                self._video_path,
            ],
        )

    def stop(self):
        self._stopped = True
        for process in (self._probe, self._encoder_probe, self._ffmpeg):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.terminate()
        QTimer.singleShot(1500, self._kill_if_running)

    def _kill_if_running(self):
        for process in (self._probe, self._encoder_probe, self._ffmpeg):
            if process.state() != QProcess.ProcessState.NotRunning:
                process.kill()

    def _on_probe_finished(self, exit_code: int, _exit_status):
        if self._completion_emitted:
            return

        if exit_code != 0:
            error_text = bytes(self._probe.readAllStandardError()).decode("utf-8", errors="replace").strip()
            self.log.emit(error_text or "ffprobe 读取视频时长失败")
            self._emit_finished(False, "")
            return

        duration_text = bytes(self._probe.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        try:
            self._duration = float(duration_text)
        except ValueError:
            self.log.emit("无法解析视频时长")
            self._emit_finished(False, "")
            return

        if _detected_encoder is not None:
            self._start_burn()
            return
        self.log.emit("检测可用的视频编码器")
        self._try_encoder(0)

    def _try_encoder(self, index: int):
        """用 ffmpeg 试编码几帧黑屏，退出码为 0 即视为该编码器可用。"""
        self._encoder_index = index
        _name, encoder_args = VIDEO_ENCODERS[index]
        self._encoder_probe.start(
            paths.tool("ffmpeg"),
            [
                "-hide_banner",
                "-f",
                "lavfi",
                "-i",
                "color=c=black:s=640x360:d=0.2",
                *encoder_args,
                "-frames:v",
                "3",
                "-f",
                "null",
                "-",
            ],
        )

    def _on_encoder_probe_finished(self, exit_code: int, exit_status):
        global _detected_encoder
        if self._completion_emitted:
            return
        if self._stopped:
            self.log.emit("烧录已停止")
            self._emit_finished(False, "")
            return
        if exit_code == 0 and exit_status == QProcess.ExitStatus.NormalExit:
            _detected_encoder = VIDEO_ENCODERS[self._encoder_index]
            self._start_burn()
        elif self._encoder_index + 1 < len(VIDEO_ENCODERS):
            self._try_encoder(self._encoder_index + 1)
        else:
            self.log.emit("没有可用的视频编码器")
            self._emit_finished(False, "")

    def _start_burn(self):
        encoder_name, encoder_args = _detected_encoder
        self.log.emit(f"使用编码器：{encoder_name}")

        subtitle = Path(self._subtitle_path)
        escaped_name = subtitle.name.replace("'", r"\'")
        subtitle_filter = f"ass=filename='{escaped_name}'"

        self.log.emit("开始烧录字幕")
        self._ffmpeg.setWorkingDirectory(str(subtitle.parent))
        self._ffmpeg.start(
            paths.tool("ffmpeg"),
            [
                "-y",
                "-i",
                self._video_path,
                "-vf",
                subtitle_filter,
                *encoder_args,
                "-c:a",
                "aac",
                "-b:a",
                "320k",
                "-ar",
                "48000",
                "-progress",
                "pipe:1",
                "-nostats",
                self._output_path,
            ],
        )

    def _read_progress(self):
        text = bytes(self._ffmpeg.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._progress_buffer += text

        while "\n" in self._progress_buffer:
            line, self._progress_buffer = self._progress_buffer.split("\n", 1)
            if "=" not in line:
                continue
            key, value = line.strip().split("=", 1)
            if key in {"out_time_us", "out_time_ms"} and self._duration > 0:
                try:
                    current_seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                percent = min(99, int(current_seconds / self._duration * 100))
                self.progress.emit(percent)
            elif key == "progress" and value == "end":
                self.progress.emit(100)

    def _read_ffmpeg_log(self):
        text = bytes(self._ffmpeg.readAllStandardError()).decode("utf-8", errors="replace").strip()
        if text:
            self.log.emit(text)

    def _on_ffmpeg_finished(self, exit_code: int, _exit_status):
        if self._completion_emitted:
            return
        if exit_code == 0:
            self.progress.emit(100)
            self._emit_finished(True, self._output_path)
            return

        message = "烧录已停止" if self._stopped else f"FFmpeg 退出，代码 {exit_code}"
        self.log.emit(message)
        self._emit_finished(False, "")

    def _on_probe_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.log.emit("无法启动 ffprobe")
            self._emit_finished(False, "")

    def _on_ffmpeg_error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self.log.emit("无法启动 FFmpeg")
            self._emit_finished(False, "")

    def _emit_finished(self, success: bool, path: str):
        if self._completion_emitted:
            return
        self._completion_emitted = True
        self.finished.emit(success, path)
