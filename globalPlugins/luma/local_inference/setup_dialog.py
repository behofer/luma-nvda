"""Setup & management dialog for local Gemma 4 inference.

On first run this acts as a wizard: detect hardware → pick a model →
download the llama.cpp binary + GGUF files.

Once something is installed, the same dialog doubles as a management
console: switch to a different variant, stop the running server, or
delete the downloaded model to free disk space.

All long-running work (hardware detection, downloads, file deletion,
server stop) runs in background threads; UI callbacks are routed back
to the main thread via ``wx.CallAfter`` (the standard
:func:`worker.run_in_background` pattern).
"""

from __future__ import annotations

import logging
import threading

import wx

import ui

from .. import config as luma_config
from ..worker import run_in_background
from .hardware import (
    HardwareInfo,
    ModelRecommendation,
    detect_hardware,
    estimate_download_size,
    recommend_model,
    variant_label,
)
from .downloader import (
    download_and_extract_binary,
    download_model,
    estimate_binary_size,
    get_backend,
    get_downloaded_model,
    is_binary_installed,
    remove_model,
)
from .server_manager import get_manager as get_server_manager

log = logging.getLogger(__name__)


def _format_bytes(n: int) -> str:
    """Format byte count as human-readable string."""
    if n >= 1024 ** 3:
        return f"{n / (1024 ** 3):.1f} GB"
    if n >= 1024 ** 2:
        return f"{n / (1024 ** 2):.0f} MB"
    return f"{n / 1024:.0f} KB"


# ---------------------------------------------------------------------------
# Model choice labels
# ---------------------------------------------------------------------------
#
# Download sizes reflect Q8_0 (E-models) or UD-Q4_K_XL (26B MoE) plus
# the shared BF16 mmproj (~1 GB).  See hardware.MODEL_SIZES for exact
# Content-Length values.

_VARIANT_LABELS: dict[str, str] = {
    "e2b": "Gemma 4 E2B  (~5.6 GB)",
    "e4b": "Gemma 4 E4B  (~8.5 GB)",
    "26b-a4b": "Gemma 4 26B-A4B  (~17 GB)",
}

_VARIANT_IDS = list(_VARIANT_LABELS.keys())

# How often (ms) to refresh the status line while the dialog is open.
# The server can stop on its own via the idle timer, so we poll to
# keep the status and Stop-button state honest.
_REFRESH_INTERVAL_MS = 2000


class SetupDialog(wx.Dialog):
    """First-time setup wizard and ongoing management dialog."""

    def __init__(self, parent: wx.Window | None = None) -> None:
        # Translators: Title of the local inference setup dialog.
        super().__init__(
            parent,
            title=_("Local Inference"),
            style=wx.DEFAULT_DIALOG_STYLE | wx.RESIZE_BORDER,
        )
        self._hw: HardwareInfo | None = None
        self._rec: ModelRecommendation | None = None
        self._downloading = False
        self._busy = False  # True while Stop/Delete/download is in flight.
        self._cancel_event = threading.Event()
        self._build_ui()
        self.CentreOnScreen()
        # Kick off hardware detection immediately.
        wx.CallAfter(self._run_detection)
        # Poll server state so Stop / status stay accurate across the
        # idle-unload timer firing in the background.
        self._refresh_timer = wx.Timer(self)
        self.Bind(wx.EVT_TIMER, self._on_refresh_tick, self._refresh_timer)
        self._refresh_timer.Start(_REFRESH_INTERVAL_MS)
        self.Bind(wx.EVT_CLOSE, self._on_close)

    # ------------------------------------------------------------------
    # UI construction
    # ------------------------------------------------------------------

    def _build_ui(self) -> None:
        self._panel = panel = wx.Panel(self)
        sizer = wx.BoxSizer(wx.VERTICAL)

        # -- Hardware info (read-only, populated after detection) --
        # Translators: Label above the hardware detection results.
        sizer.Add(
            wx.StaticText(panel, label=_("&Hardware:")),
            flag=wx.LEFT | wx.TOP, border=10,
        )
        self._hw_text = wx.TextCtrl(
            panel,
            style=wx.TE_MULTILINE | wx.TE_READONLY | wx.TE_DONTWRAP,
        )
        self._hw_text.SetMinSize((-1, 80))
        # Translators: Shown while hardware is being detected.
        self._hw_text.SetValue(_("Detecting hardware..."))
        sizer.Add(
            self._hw_text, proportion=1,
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10,
        )

        # -- Model selection --
        # Translators: Label above the model selection radio buttons.
        sizer.Add(
            wx.StaticText(panel, label=_("&Model:")),
            flag=wx.LEFT | wx.TOP, border=10,
        )
        choices = list(_VARIANT_LABELS.values())
        self._model_radio = wx.RadioBox(
            panel,
            choices=choices,
            majorDimension=1,
            style=wx.RA_SPECIFY_COLS,
        )
        self._model_radio.Bind(wx.EVT_RADIOBOX, self._on_model_changed)
        sizer.Add(
            self._model_radio,
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT, border=10,
        )

        # -- Status line (populated by _update_state) --
        self._status_text = wx.StaticText(panel, label="")
        sizer.Add(
            self._status_text,
            flag=wx.LEFT | wx.TOP, border=10,
        )

        # -- Download info --
        self._download_label = wx.StaticText(panel, label="")
        sizer.Add(
            self._download_label,
            flag=wx.LEFT | wx.TOP, border=5,
        )

        # -- Progress bar --
        self._progress = wx.Gauge(panel, range=1000)
        self._progress.Hide()
        sizer.Add(
            self._progress,
            flag=wx.EXPAND | wx.LEFT | wx.RIGHT | wx.TOP, border=10,
        )

        self._progress_label = wx.StaticText(panel, label="")
        self._progress_label.Hide()
        sizer.Add(
            self._progress_label,
            flag=wx.LEFT | wx.TOP, border=5,
        )

        # -- Buttons --
        btn_sizer = wx.BoxSizer(wx.HORIZONTAL)

        # Translators: Button to download (or re-download) the model.
        self._btn_download = wx.Button(panel, label=_("&Download"))
        self._btn_download.Bind(wx.EVT_BUTTON, self._on_download)
        self._btn_download.Disable()
        btn_sizer.Add(self._btn_download, flag=wx.RIGHT, border=8)

        # Translators: Button to stop the running inference server.
        self._btn_stop = wx.Button(panel, label=_("S&top Server"))
        self._btn_stop.Bind(wx.EVT_BUTTON, self._on_stop_server)
        self._btn_stop.Disable()
        btn_sizer.Add(self._btn_stop, flag=wx.RIGHT, border=8)

        # Translators: Button to delete the downloaded model files.
        self._btn_delete = wx.Button(panel, label=_("D&elete Model"))
        self._btn_delete.Bind(wx.EVT_BUTTON, self._on_delete_model)
        self._btn_delete.Disable()
        btn_sizer.Add(self._btn_delete, flag=wx.RIGHT, border=8)

        # Translators: Button to close the dialog (or cancel a download).
        self._btn_close = wx.Button(panel, id=wx.ID_CANCEL, label=_("Close"))
        self._btn_close.Bind(wx.EVT_BUTTON, self._on_cancel)
        btn_sizer.Add(self._btn_close)

        sizer.Add(btn_sizer, flag=wx.ALIGN_RIGHT | wx.ALL, border=10)

        panel.SetSizer(sizer)
        self.SetSize((560, 500))
        self.SetMinSize((500, 420))

    # ------------------------------------------------------------------
    # Hardware detection
    # ------------------------------------------------------------------

    def _run_detection(self) -> None:
        run_in_background(
            detect_hardware,
            on_success=self._on_detection_done,
            on_error=self._on_detection_error,
            silent=True,
        )

    def _on_detection_done(self, hw: HardwareInfo) -> None:
        self._hw = hw
        self._rec = recommend_model(hw)

        lines = [
            # Translators: RAM line in hardware summary.
            _("RAM: {total:.1f} GB total, {avail:.1f} GB available").format(
                total=hw.total_ram_gb, avail=hw.available_ram_gb,
            ),
        ]
        gpu = hw.best_gpu
        if gpu:
            # Translators: GPU line in hardware summary.
            lines.append(
                _("GPU: {name} ({vram:.1f} GB, {type})").format(
                    name=gpu.name,
                    vram=gpu.vram_gb,
                    type=_("discrete") if gpu.is_discrete else _("integrated"),
                ),
            )
        else:
            # Translators: Shown when no GPU is detected.
            lines.append(_("GPU: None detected"))
        lines.append("")
        lines.append(self._rec.reason)
        self._hw_text.SetValue("\n".join(lines))

        # Pre-select the installed variant if any; otherwise the
        # hardware-recommended variant.
        preferred = get_downloaded_model()
        if preferred is None and self._rec.supported:
            preferred = self._rec.variant
        if preferred is not None:
            try:
                self._model_radio.SetSelection(_VARIANT_IDS.index(preferred))
            except ValueError:
                pass

        self._update_state()

    def _on_detection_error(self, exc: Exception) -> None:
        log.error("Hardware detection failed", exc_info=exc)
        # Translators: Shown when hardware detection fails.
        self._hw_text.SetValue(
            _("Hardware detection failed: {error}").format(error=str(exc)),
        )
        self._update_state()

    # ------------------------------------------------------------------
    # Model selection
    # ------------------------------------------------------------------

    def _selected_variant(self) -> str | None:
        idx = self._model_radio.GetSelection()
        if idx == wx.NOT_FOUND:
            return None
        return _VARIANT_IDS[idx]

    def _on_model_changed(self, _event: wx.CommandEvent) -> None:
        self._update_state()

    def _selected_backend(self) -> str:
        """Return the backend the recommendation picked for this hardware."""
        if self._rec and self._rec.backend in ("cuda", "vulkan", "cpu"):
            return self._rec.backend
        return "cpu"

    # ------------------------------------------------------------------
    # State refresh — the single source of truth for button / label state
    # ------------------------------------------------------------------

    def _on_refresh_tick(self, _event: wx.TimerEvent) -> None:
        # Only poll when idle — mid-download the flags are already in
        # the right state and the progress UI is driving itself.
        if not self._busy:
            self._update_state()

    def _update_state(self) -> None:
        """Refresh status line, download size, and button enablement."""
        installed = get_downloaded_model()
        selected = self._selected_variant()
        running = get_server_manager().is_running
        busy = self._busy
        downloading = self._downloading

        # -- Status line --------------------------------------------------
        if downloading:
            # Translators: Status line shown while a download is running.
            status = _("Downloading...")
        elif installed and running:
            # Translators: Status when local inference server is running.
            status = _("Server running — {model}").format(
                model=variant_label(installed),
            )
        elif installed:
            # Translators: Status when model is installed but server is idle.
            status = _("Ready — {model}").format(
                model=variant_label(installed),
            )
        else:
            # Translators: Status when nothing is installed yet.
            status = _("Not set up")
        self._status_text.SetLabel(status)

        # -- Download label (size estimate) -------------------------------
        if selected is not None and not downloading:
            total = estimate_download_size(selected)
            need_binary = (
                not is_binary_installed()
                or get_backend() != self._selected_backend()
            )
            if need_binary:
                total += estimate_binary_size(self._selected_backend())

            if installed == selected and not need_binary:
                # Translators: Label next to size when the chosen model is
                # already installed.  {size} is a human-readable size.
                self._download_label.SetLabel(
                    _("Re-download: ~{size}").format(size=_format_bytes(total)),
                )
            elif installed is not None and installed != selected:
                # Translators: Label for switching to a different model.
                self._download_label.SetLabel(
                    _("Switch download: ~{size}").format(
                        size=_format_bytes(total),
                    ),
                )
            else:
                # Translators: Label showing initial download size estimate.
                self._download_label.SetLabel(
                    _("Total download: ~{size}").format(
                        size=_format_bytes(total),
                    ),
                )
        elif not downloading:
            self._download_label.SetLabel("")

        # -- Button enablement -------------------------------------------
        can_download = selected is not None and not busy
        self._btn_download.Enable(can_download)
        self._btn_stop.Enable(running and not busy)
        self._btn_delete.Enable(installed is not None and not busy)

        # Close button toggles to "Cancel" while downloading so the user
        # can interrupt long-running transfers.
        if downloading:
            self._btn_close.SetLabel(_("&Cancel"))
        else:
            self._btn_close.SetLabel(_("&Close"))

    # ------------------------------------------------------------------
    # Download
    # ------------------------------------------------------------------

    def _on_download(self, _event: wx.CommandEvent) -> None:
        if self._busy:
            return
        variant = self._selected_variant()
        if variant is None:
            return

        self._downloading = True
        self._busy = True
        self._cancel_event.clear()
        self._progress.Show()
        self._progress_label.Show()
        self._progress.SetValue(0)
        self._panel.GetSizer().Layout()
        self._update_state()

        run_in_background(
            self._download_all,
            args=(variant, self._selected_backend()),
            on_success=self._on_download_done,
            on_error=self._on_download_error,
            silent=True,
            cancel_event=self._cancel_event,
        )

    def _download_all(self, variant: str, backend: str) -> tuple[str, str]:
        """Download binary + model.  Runs in a background thread."""
        # The server may have the current model's files mmap'd — stop
        # it before touching any files on disk, whether we're
        # re-downloading the same variant or switching to a new one.
        get_server_manager().stop()

        # If switching variants, clean up the previous model's files
        # first so they don't linger on disk.
        installed = get_downloaded_model()
        if installed is not None and installed != variant:
            remove_model()

        # Download the binary if missing or if the backend changed.
        need_binary = (
            not is_binary_installed() or get_backend() != backend
        )
        if need_binary:
            wx.CallAfter(
                self._set_progress_text,
                # Translators: Progress status during binary download.
                _("Downloading inference engine ({backend})...")
                .format(backend=backend),
            )
            download_and_extract_binary(
                backend,
                on_progress=lambda done, total: wx.CallAfter(
                    self._update_progress, done, total,
                ),
            )

        wx.CallAfter(
            self._set_progress_text,
            # Translators: Progress status during model download.
            _("Downloading model..."),
        )
        model_path, mmproj_path = download_model(
            variant,
            on_progress=lambda done, total: wx.CallAfter(
                self._update_progress, done, total,
            ),
        )
        return model_path, mmproj_path

    def _update_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = min(int(done / total * 1000), 1000)
            self._progress.SetValue(pct)
            self._progress_label.SetLabel(
                f"{_format_bytes(done)} / {_format_bytes(total)}"
            )
        else:
            self._progress.Pulse()
            self._progress_label.SetLabel(_format_bytes(done))

    def _set_progress_text(self, text: str) -> None:
        self._progress_label.SetLabel(text)

    def _on_download_done(self, result: tuple[str, str]) -> None:
        model_path, mmproj_path = result
        variant = self._selected_variant() or ""
        backend = self._selected_backend()

        luma_config.set("local_inference", {
            "model_variant": variant,
            "model_path": model_path,
            "mmproj_path": mmproj_path,
            "backend": backend,
        })
        luma_config.save()
        # Enable the local preset so it's immediately usable.
        luma_config.set_preset_api_key("local", "enabled")

        self._downloading = False
        self._busy = False
        self._progress.Hide()
        self._progress_label.Hide()
        self._panel.GetSizer().Layout()
        self._update_state()

        # Translators: Spoken when local inference setup completes.
        # {model} is the human-readable model name.
        ui.message(
            _("Local inference ready — {model}.")
            .format(model=variant_label(variant)),
        )

    def _on_download_error(self, exc: Exception) -> None:
        self._downloading = False
        self._busy = False
        self._progress.Hide()
        self._progress_label.Hide()
        self._panel.GetSizer().Layout()
        self._update_state()

        log.error("Download failed", exc_info=exc)
        wx.MessageBox(
            # Translators: Error message when download fails.
            _("Download failed: {error}").format(error=str(exc)),
            # Translators: Title of the download error dialog.
            _("Download Error"),
            wx.OK | wx.ICON_ERROR,
            self,
        )

    # ------------------------------------------------------------------
    # Stop server
    # ------------------------------------------------------------------

    def _on_stop_server(self, _event: wx.CommandEvent) -> None:
        if self._busy:
            return
        self._busy = True
        self._update_state()
        run_in_background(
            get_server_manager().stop,
            on_success=self._on_stop_done,
            on_error=self._on_action_error,
            silent=True,
        )

    def _on_stop_done(self, _result: None) -> None:
        self._busy = False
        self._update_state()
        # Translators: Spoken after the local inference server is stopped.
        ui.message(_("Local inference server stopped."))

    # ------------------------------------------------------------------
    # Delete model
    # ------------------------------------------------------------------

    def _on_delete_model(self, _event: wx.CommandEvent) -> None:
        if self._busy:
            return
        installed = get_downloaded_model()
        if installed is None:
            return

        result = wx.MessageBox(
            # Translators: Confirmation prompt before deleting the
            # downloaded model.  {model} is the model's human name.
            _("Delete the downloaded {model} model? This frees the "
              "disk space but you'll need to re-download to use local "
              "inference again.").format(model=variant_label(installed)),
            # Translators: Title of the delete-model confirmation dialog.
            _("Delete Model"),
            wx.YES_NO | wx.ICON_QUESTION,
            self,
        )
        if result != wx.YES:
            return

        self._busy = True
        self._update_state()
        run_in_background(
            self._delete_model_worker,
            on_success=self._on_delete_done,
            on_error=self._on_action_error,
            silent=True,
        )

    @staticmethod
    def _delete_model_worker() -> None:
        """Stop the server (so files unlock) then delete the model files."""
        get_server_manager().stop()
        remove_model()

    def _on_delete_done(self, _result: None) -> None:
        # Clear the stored model paths from config so the provider no
        # longer thinks a model is configured.
        cfg = dict(luma_config.get("local_inference", {}))
        cfg["model_variant"] = ""
        cfg["model_path"] = ""
        cfg["mmproj_path"] = ""
        luma_config.set("local_inference", cfg)
        luma_config.save()

        self._busy = False
        self._update_state()
        # Translators: Spoken after the downloaded model is deleted.
        ui.message(_("Local inference model deleted."))

    # ------------------------------------------------------------------
    # Generic error handler for Stop / Delete
    # ------------------------------------------------------------------

    def _on_action_error(self, exc: Exception) -> None:
        self._busy = False
        self._update_state()
        log.error("Action failed", exc_info=exc)
        wx.MessageBox(
            # Translators: Generic error message for stop/delete actions.
            _("Action failed: {error}").format(error=str(exc)),
            # Translators: Generic error dialog title.
            _("Error"),
            wx.OK | wx.ICON_ERROR,
            self,
        )

    # ------------------------------------------------------------------
    # Close / cancel
    # ------------------------------------------------------------------

    def _on_cancel(self, _event: wx.CommandEvent) -> None:
        if self._downloading:
            # Button is labelled "Cancel" in this state — abort the
            # transfer but keep the dialog open so the user can see the
            # resulting state.
            self._cancel_event.set()
            return
        self._refresh_timer.Stop()
        self.EndModal(wx.ID_CANCEL)

    def _on_close(self, event: wx.CloseEvent) -> None:
        self._refresh_timer.Stop()
        event.Skip()
