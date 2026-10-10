from __future__ import annotations

import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QFormLayout,
    QGridLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from videotrx import paths
from videotrx.gui.jobs import BurnJob, PipelineJob, YtDlpJob


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("VideoTrX")
        self.resize(900, 760)
        self.output_dir = paths.OUTPUT_DIR
        self.archive_dir = paths.ARCHIVE_DIR

        self.audio_job = YtDlpJob("audio", self)
        self.video_job = YtDlpJob("video", self)
        self.pipeline_job = PipelineJob(self)
        self.burn_job = BurnJob(self)
        self.manual_video_jobs: list[YtDlpJob] = []
        self.video_path = ""

        self._build_ui()
        self._connect_signals()

    def _build_ui(self):
        central = QWidget(self)
        self.setCentralWidget(central)
        root = QVBoxLayout(central)

        form_group = QGroupBox("任务输入")
        form = QFormLayout(form_group)
        self.video_url_edit = QLineEdit()
        self.video_url_edit.setPlaceholderText("原始视频链接")
        form.addRow("视频链接", self.video_url_edit)
        root.addWidget(form_group)

        button_group = QGroupBox("操作")
        buttons = QGridLayout(button_group)
        self.start_button = QPushButton("开始")
        self.continue_button = QPushButton("继续任务")
        self.download_video_button = QPushButton("下载最佳视频")
        self.stop_button = QPushButton("停止当前操作")
        self.burn_button = QPushButton("执行烧录")
        self.open_output_button = QPushButton("打开 output 文件夹")
        for column, button in enumerate((self.start_button, self.continue_button, self.download_video_button, self.stop_button, self.burn_button, self.open_output_button)):
            buttons.addWidget(button, 0, column)
        root.addWidget(button_group)

        progress_group = QGroupBox("状态与进度")
        progress = QGridLayout(progress_group)
        self.stage_label = QLabel("等待输入")
        self.audio_progress = self._progress_bar()
        self.video_progress = self._progress_bar()
        self.transcribe_progress = self._progress_bar()
        self.pipeline_progress = self._progress_bar()
        self.burn_progress = self._progress_bar()
        rows = (
            ("当前阶段", self.stage_label),
            ("音频下载", self.audio_progress),
            ("最佳视频下载", self.video_progress),
            ("转写", self.transcribe_progress),
            ("字幕流程", self.pipeline_progress),
            ("字幕烧录", self.burn_progress),
        )
        for row, (label, widget) in enumerate(rows):
            progress.addWidget(QLabel(label), row, 0)
            progress.addWidget(widget, row, 1)
        root.addWidget(progress_group)

        log_group = QGroupBox("日志")
        log_layout = QVBoxLayout(log_group)
        self.log_output = QPlainTextEdit()
        self.log_output.setReadOnly(True)
        log_layout.addWidget(self.log_output)
        root.addWidget(log_group, 1)

    @staticmethod
    def _progress_bar():
        bar = QProgressBar()
        bar.setRange(0, 100)
        bar.setValue(0)
        return bar

    def _connect_signals(self):
        self.start_button.clicked.connect(self._start_full_workflow)
        self.continue_button.clicked.connect(self._continue_task)
        self.download_video_button.clicked.connect(self._run_manual_video_download)
        self.stop_button.clicked.connect(self._stop_current_operation)
        self.burn_button.clicked.connect(self._burn_subtitle)
        self.open_output_button.clicked.connect(self._open_output_dir)
        self.audio_job.log.connect(self._append_log)
        self.audio_job.progress.connect(self.audio_progress.setValue)
        self.audio_job.finished.connect(self._on_audio_finished)
        self.video_job.log.connect(self._append_log)
        self.video_job.progress.connect(self.video_progress.setValue)
        self.video_job.finished.connect(self._on_video_finished)
        self.pipeline_job.log.connect(self._append_log)
        self.pipeline_job.progress.connect(self._on_pipeline_progress)
        self.pipeline_job.stage.connect(self._on_pipeline_stage)
        self.pipeline_job.finished.connect(self._on_pipeline_finished)
        self.burn_job.log.connect(self._append_log)
        self.burn_job.progress.connect(self.burn_progress.setValue)
        self.burn_job.finished.connect(self._on_burn_finished)

    def _start_full_workflow(self):
        url = self.video_url_edit.text().strip()
        if not url:
            self._append_log("请填写视频链接")
            return
        if self.audio_job.is_running() or self.video_job.is_running() or self.pipeline_job.is_running():
            self._append_log("当前任务仍在执行")
            return
        self._reset_local_workspace()
        self.video_path = ""
        self._reset_progress()
        self._set_buttons_enabled(False)
        self._set_stage("下载最佳音频")
        self.audio_job.start(url, str(self.output_dir))
        self.video_job.start(url, str(self.output_dir))

    def _reset_local_workspace(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.archive_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        for path in list(self.output_dir.iterdir()):
            if path.is_file() and path.suffix.lower() == ".ass":
                shutil.move(str(path), str(self.archive_dir / f"{timestamp}_{path.name}"))
        for path in list(self.output_dir.iterdir()):
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    def _reset_progress(self):
        for bar in (self.audio_progress, self.video_progress, self.transcribe_progress, self.pipeline_progress, self.burn_progress):
            bar.setValue(0)

    def _set_buttons_enabled(self, enabled: bool):
        self.start_button.setEnabled(enabled)
        self.continue_button.setEnabled(enabled)

    def _on_audio_finished(self, success: bool, path: str):
        if not success or not path:
            self._set_buttons_enabled(True)
            self._set_stage("音频下载失败或已停止")
            return
        self._start_pipeline(path)

    def _continue_task(self):
        if self.pipeline_job.is_running():
            return
        self._set_buttons_enabled(False)
        self._start_pipeline()

    def _start_pipeline(self, audio: str | None = None):
        self._set_stage("字幕流程启动")
        self.pipeline_job.start(audio)

    def _on_pipeline_stage(self, text: str):
        self._set_stage(text)
        self._append_log(f"阶段：{text}")

    def _on_pipeline_progress(self, kind: str, value: int):
        bar = self.transcribe_progress if kind == "transcribe" else self.pipeline_progress
        bar.setValue(value)

    def _on_pipeline_finished(self, success: bool, message: str):
        self._set_buttons_enabled(True)
        self._set_stage("字幕生成完成" if success else "字幕流程失败")
        self._append_log(message)

    def _on_video_finished(self, success: bool, path: str):
        if success and path:
            self.video_path = path
            self._append_log(f"最佳视频：{path}")

    def _run_manual_video_download(self):
        url = self.video_url_edit.text().strip()
        if not url:
            self._append_log("请填写视频链接")
            return
        job = YtDlpJob("video", self)
        self.manual_video_jobs.append(job)
        job.log.connect(self._append_log)
        job.progress.connect(self.video_progress.setValue)
        job.finished.connect(lambda success, path, current_job=job: self._on_manual_video_finished(current_job, success, path))
        job.start(url, str(self.output_dir))

    def _on_manual_video_finished(self, job, success, path):
        if success and path:
            self.video_path = path
        if job in self.manual_video_jobs:
            self.manual_video_jobs.remove(job)
        job.deleteLater()

    def _burn_subtitle(self):
        subtitle_path = paths.FINAL_ASS
        if not subtitle_path.is_file():
            self._append_log("output 目录中没有 src_trans.ass")
            return
        video_path = self.video_path
        if not video_path or not Path(video_path).is_file():
            candidates = sorted(self.output_dir.glob("*_best.*"), key=lambda path: path.stat().st_mtime, reverse=True)
            video_path = str(candidates[0]) if candidates else ""
        self._set_stage("本地烧录字幕")
        self.burn_job.start(video_path, str(subtitle_path), str(self.output_dir))

    def _open_output_dir(self):
        self.output_dir.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.output_dir)))

    def _on_burn_finished(self, success: bool, path: str):
        self._set_stage("字幕烧录完成" if success else "字幕烧录失败或已停止")
        if success:
            self._append_log(f"烧录视频：{path}")

    def _stop_current_operation(self):
        self.audio_job.stop()
        self.video_job.stop()
        for job in list(self.manual_video_jobs):
            job.stop()
        self.pipeline_job.stop()
        self.burn_job.stop()
        self._set_buttons_enabled(True)
        self._set_stage("已手动停止当前操作")

    def _set_stage(self, text: str):
        self.stage_label.setText(text)

    def _append_log(self, text: str):
        if text:
            self.log_output.appendPlainText(text.rstrip())
