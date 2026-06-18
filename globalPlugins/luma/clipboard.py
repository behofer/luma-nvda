"""Clipboard content retrieval for Luma.

Provides functions to get text or image content from the system clipboard.
"""

from __future__ import annotations

import base64
import io
import logging

import wx

log = logging.getLogger(__name__)


def get_clipboard_text() -> str | None:
	"""Return the current text content of the clipboard, or None."""
	import api
	try:
		text = api.getClipData()
		if text and text.strip():
			return text.strip()
	except Exception:
		pass
	return None


def get_clipboard_image() -> str | None:
	"""Return clipboard image as a base64-encoded PNG string, or None.

	Uses wxPython's clipboard API to check for bitmap data. If found,
	converts it to PNG and returns as base64. This handles images
	from Print Screen, Snipping Tool, or any application that copies
	images to the clipboard.
	"""
	try:
		if not wx.TheClipboard.Open():
			return None
		try:
			if not wx.TheClipboard.IsSupported(wx.DataFormat(wx.DF_BITMAP)):
				return None
			bmp_data = wx.BitmapDataObject()
			if not wx.TheClipboard.GetData(bmp_data):
				return None
		finally:
			wx.TheClipboard.Close()

		bmp = bmp_data.GetBitmap()
		if not bmp.IsOk():
			return None

		image = bmp.ConvertToImage()
		stream = io.BytesIO()
		image.SaveFile(stream, wx.BITMAP_TYPE_PNG)
		data = stream.getvalue()
		if not data:
			return None
		return base64.b64encode(data).decode("ascii")
	except Exception:
		log.exception("Failed to read image from clipboard")
		return None


def get_clipboard_content() -> tuple[str, str | None]:
	"""Return clipboard content as (content_type, data).

	Checks for image first, then text. Returns one of:
	- ("image", base64_png_string)
	- ("text", text_string)
	- ("empty", None)
	"""
	image = get_clipboard_image()
	if image:
		return "image", image

	text = get_clipboard_text()
	if text:
		return "text", text

	return "empty", None
