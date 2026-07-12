"""Flatbed / document scanner capture for Luma.

Provides functions to enumerate connected WIA scanners and acquire a
single page as a base64-encoded PNG image, using Windows Image
Acquisition (WIA) Automation via comtypes (bundled with NVDA).

Both ``list_scanners`` and ``scan_image`` are **blocking** — device
enumeration can take a second or two and an actual scan 10-30 seconds —
so they must only be called from a background thread via
``worker.run_in_background``.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import shutil
import tempfile

log = logging.getLogger(__name__)

# WIA DeviceInfo.Type value for scanners (WiaDeviceType.ScannerDeviceType).
_WIA_DEVICE_TYPE_SCANNER = 1

# WIA transfer format GUIDs (wiaFormatBMP / wiaFormatPNG).  BMP is the
# only format every scanner driver is required to support; the result
# is converted to PNG in Python afterwards.
_WIA_FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"

# Scan item property IDs (WIA_IPS_*) applied best-effort before a scan.
_WIA_IPS_CUR_INTENT = 6146  # 1 = colour
_WIA_IPS_XRES = 6147  # horizontal DPI
_WIA_IPS_YRES = 6148  # vertical DPI

# 200 DPI keeps an A4 page around 1700x2300 px — plenty for the vision
# models (capture.optimize_image downscales to 1536 px anyway) while
# scanning noticeably faster than the common 300 DPI driver default.
_SCAN_DPI = 200


def _create_device_manager():
	"""Create the WIA device manager COM object.

	Raises on failure (e.g. the WIA service is disabled); callers catch
	and log.  COM must already be initialised on the calling thread.
	"""
	import comtypes.client

	return comtypes.client.CreateObject("WIA.DeviceManager")


def _get_property(properties, name):
	"""Return the value of the WIA property called *name*, or ``None``."""
	try:
		for prop in properties:
			if prop.Name == name:
				return prop.Value
	except Exception:
		log.debug("WIA property lookup for %r failed", name, exc_info=True)
	return None


def _set_property_by_id(properties, property_id: int, value) -> None:
	"""Set a WIA property by numeric ID, ignoring driver refusals."""
	try:
		for prop in properties:
			if int(prop.PropertyID) == property_id:
				prop.Value = value
				return
	except Exception:
		log.debug(
			"Could not set WIA property %d = %r", property_id, value,
			exc_info=True,
		)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def list_scanners() -> list[tuple[str, str]]:
	"""Return ``(device_id, name)`` for every connected WIA scanner.

	**Blocking** — WIA enumeration can take a couple of seconds.  Must
	be called from a background thread.  Returns an empty list when no
	scanner is found or WIA is unavailable.
	"""
	import comtypes

	comtypes.CoInitialize()
	try:
		manager = _create_device_manager()
		scanners: list[tuple[str, str]] = []
		for info in manager.DeviceInfos:
			try:
				if int(info.Type) != _WIA_DEVICE_TYPE_SCANNER:
					continue
				device_id = str(info.DeviceID)
				name = _get_property(info.Properties, "Name")
				if not name:
					# Translators: Fallback name for a scanner whose driver
					# reports no name. {number} is the 1-based device number.
					name = _("Scanner {number}").format(number=len(scanners) + 1)
				scanners.append((device_id, str(name)))
			except Exception:
				log.exception("Error reading a WIA device entry")
		return scanners
	except Exception:
		log.exception("WIA scanner enumeration failed")
		return []
	finally:
		comtypes.CoUninitialize()


def scan_image(device_id: str) -> str | None:
	"""Scan one page from the scanner *device_id* as base64 PNG.

	**Blocking** — a scan takes 10-30 seconds depending on the device.
	Must be called from a background thread (via
	``worker.run_in_background``).

	Returns the base64-encoded PNG string, or ``None`` on failure.
	"""
	import comtypes

	comtypes.CoInitialize()
	try:
		manager = _create_device_manager()
		device = None
		for info in manager.DeviceInfos:
			if str(info.DeviceID) == device_id:
				device = info.Connect()
				break
		if device is None:
			log.error("Scanner %s not found", device_id)
			return None

		# The first child item of a scanner device is the flatbed /
		# feeder scan item.
		item = None
		for child in device.Items:
			item = child
			break
		if item is None:
			log.error("Scanner %s exposes no scan item", device_id)
			return None

		# Request a colour scan at a moderate resolution.  Drivers may
		# reject any of these; the scan then proceeds with defaults.
		_set_property_by_id(item.Properties, _WIA_IPS_CUR_INTENT, 1)
		_set_property_by_id(item.Properties, _WIA_IPS_XRES, _SCAN_DPI)
		_set_property_by_id(item.Properties, _WIA_IPS_YRES, _SCAN_DPI)

		image_file = item.Transfer(_WIA_FORMAT_BMP)
		if image_file is None:
			log.error("WIA transfer returned no image for %s", device_id)
			return None

		# WIA's ImageFile can only save to disk (and refuses to
		# overwrite), so go through a fresh temp directory.
		temp_dir = tempfile.mkdtemp(prefix="luma_scan_")
		try:
			ext = str(getattr(image_file, "FileExtension", "") or "bmp")
			scan_path = os.path.join(temp_dir, "scan." + ext)
			image_file.SaveFile(scan_path)
			return _file_to_base64_png(scan_path)
		finally:
			shutil.rmtree(temp_dir, ignore_errors=True)
	except Exception:
		log.exception("Scanning from %s failed", device_id)
		return None
	finally:
		comtypes.CoUninitialize()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _file_to_base64_png(path: str) -> str | None:
	"""Load an image file and return it as a base64-encoded PNG string."""
	try:
		import wx

		image = wx.Image(path, wx.BITMAP_TYPE_ANY)
		if not image.IsOk():
			log.error("Could not decode scanned image %s", path)
			return None
		stream = io.BytesIO()
		image.SaveFile(stream, wx.BITMAP_TYPE_PNG)
		data = stream.getvalue()
		if not data:
			return None
		return base64.b64encode(data).decode("ascii")
	except Exception:
		log.exception("Failed to convert scanned image to PNG")
		return None
