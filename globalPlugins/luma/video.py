"""Video URL detection, parsing, and download helpers for Luma.

Handles browser URL auto-detection, platform classification
(YouTube, Instagram, Twitter/X, TikTok), direct link extraction
for social media platforms, and video file download.
"""

from __future__ import annotations

import http.cookiejar
import json
import logging
import os
import re
import tempfile
import urllib.parse
import urllib.request

log = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Supported platforms
# ------------------------------------------------------------------

PLATFORM_YOUTUBE = "youtube"
PLATFORM_INSTAGRAM = "instagram"
PLATFORM_TWITTER = "twitter"
PLATFORM_TIKTOK = "tiktok"

_BROWSER_USER_AGENT = (
	"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
	"AppleWebKit/537.36 (KHTML, like Gecko) "
	"Chrome/144.0.0.0 Safari/537.36"
)


# ------------------------------------------------------------------
# Browser URL auto-detection
# ------------------------------------------------------------------

def detect_browser_url() -> str | None:
	"""Try to read the current page URL from NVDA's virtual buffer.

	Returns the URL string if the focused application is a browser
	with an active browse-mode document, otherwise ``None``.
	"""
	try:
		import api

		focus = api.getFocusObject()
		tree = getattr(focus, "treeInterceptor", None)
		if tree is None:
			return None

		# BrowseModeDocumentTreeInterceptor exposes the document URL
		# via documentConstantIdentifier on its rootNVDAObject.
		root = getattr(tree, "rootNVDAObject", None)
		if root is None:
			return None

		# Try IAccessible2 document attributes first — works in
		# Chrome, Edge, and Firefox.
		ia2_attrs = getattr(root, "IA2Attributes", {})
		if isinstance(ia2_attrs, dict):
			url = ia2_attrs.get("url", "")
			if url:
				return url

		# Fallback: try the value property (some browsers expose the
		# URL as the document's value).
		value = getattr(root, "value", None)
		if value and isinstance(value, str) and value.startswith("http"):
			return value

	except Exception:
		log.debug("Browser URL auto-detection failed.", exc_info=True)

	return None


# ------------------------------------------------------------------
# URL parsing and platform detection
# ------------------------------------------------------------------

def parse_video_url(url: str) -> tuple[str, str]:
	"""Validate and classify a video URL.

	Returns ``(platform, normalized_url)`` where *platform* is one of
	the ``PLATFORM_*`` constants.

	Raises :class:`ValueError` if the URL is not a supported video
	platform.
	"""
	url = url.strip()
	if not url:
		raise ValueError(_("No URL provided."))

	parsed = urllib.parse.urlparse(url)
	domain = parsed.netloc.lower()
	if not domain:
		# Try adding a scheme if missing.
		url = "https://" + url
		parsed = urllib.parse.urlparse(url)
		domain = parsed.netloc.lower()

	if not domain:
		raise ValueError(_("Invalid URL."))

	# YouTube
	if any(d in domain for d in ("youtube.com", "youtu.be")):
		return PLATFORM_YOUTUBE, _normalise_youtube_url(url, parsed)

	# Instagram
	if "instagram.com" in domain:
		return PLATFORM_INSTAGRAM, url

	# Twitter / X
	if any(d in domain for d in ("twitter.com", "x.com")):
		return PLATFORM_TWITTER, url

	# TikTok
	if "tiktok.com" in domain:
		return PLATFORM_TIKTOK, url

	# Translators: Error when the video platform is not supported.
	raise ValueError(
		_(
			"Unsupported platform. "
			"Only YouTube, Instagram, Twitter, and TikTok are supported."
		)
	)


def _normalise_youtube_url(url: str, parsed: urllib.parse.ParseResult) -> str:
	"""Normalise various YouTube URL formats to a canonical URL."""
	# youtu.be/VIDEO_ID → full URL
	if "youtu.be" in parsed.netloc:
		video_id = parsed.path.lstrip("/").split("/")[0]
		if video_id:
			return f"https://www.youtube.com/watch?v={video_id}"

	# youtube.com/shorts/VIDEO_ID
	path = parsed.path.lower()
	if "/shorts/" in path:
		parts = parsed.path.split("/shorts/")
		if len(parts) > 1:
			video_id = parts[1].split("/")[0].split("?")[0]
			if video_id:
				return f"https://www.youtube.com/watch?v={video_id}"

	# youtube.com/live/VIDEO_ID
	if "/live/" in path:
		parts = parsed.path.split("/live/")
		if len(parts) > 1:
			video_id = parts[1].split("/")[0].split("?")[0]
			if video_id:
				return f"https://www.youtube.com/watch?v={video_id}"

	# Already a standard youtube.com/watch?v= URL or embed URL.
	return url


# ------------------------------------------------------------------
# Social media direct link extraction
# ------------------------------------------------------------------

def get_direct_video_link(url: str, platform: str) -> str | None:
	"""Extract a direct download link for the given platform.

	Returns the direct MP4 URL, or ``None`` if extraction fails.
	Not needed for YouTube (Gemini accepts YouTube URLs directly).
	"""
	if platform == PLATFORM_INSTAGRAM:
		return _get_instagram_download_link(url)
	if platform == PLATFORM_TWITTER:
		return _get_twitter_download_link(url)
	if platform == PLATFORM_TIKTOK:
		return _get_tiktok_download_link(url)
	return None


def _get_twitter_download_link(tweet_url: str) -> str | None:
	"""Extract a direct video download link from a Twitter/X URL."""
	cj = http.cookiejar.CookieJar()
	opener = urllib.request.build_opener(
		urllib.request.HTTPCookieProcessor(cj),
	)
	base_url = "https://savetwitter.net/en4"
	api_url = "https://savetwitter.net/api/ajaxSearch"
	headers = {
		"User-Agent": _BROWSER_USER_AGENT,
		"X-Requested-With": "XMLHttpRequest",
		"Referer": base_url,
	}
	try:
		# Initialise cookies.
		req_init = urllib.request.Request(base_url, headers=headers)
		opener.open(req_init, timeout=30)
		# Request download link.
		params = {"q": tweet_url, "lang": "en", "cftoken": ""}
		data = urllib.parse.urlencode(params).encode("utf-8")
		req_post = urllib.request.Request(
			api_url, data=data, headers=headers, method="POST",
		)
		with opener.open(req_post, timeout=60) as response:
			res_data = json.loads(response.read().decode("utf-8"))
			if res_data.get("status") == "ok":
				html = res_data.get("data", "")
				match = re.search(
					r'href="(https?://dl\.snapcdn\.app/[^"]+)"', html,
				)
				if match:
					return match.group(1)
	except Exception:
		log.debug("Twitter link extraction failed.", exc_info=True)
	return None


def _get_instagram_download_link(insta_url: str) -> str | None:
	"""Extract a direct video download link from an Instagram URL."""
	cj = http.cookiejar.CookieJar()
	opener = urllib.request.build_opener(
		urllib.request.HTTPCookieProcessor(cj),
	)
	headers = {
		"User-Agent": _BROWSER_USER_AGENT,
		"X-Requested-With": "XMLHttpRequest",
		"Referer": "https://anon-viewer.com/",
		"Accept": "*/*",
	}
	opener.addheaders = list(headers.items())
	try:
		# Initialise cookies.
		opener.open("https://anon-viewer.com/", timeout=30)
		# Determine API URL based on content type.
		if "/stories/" in insta_url:
			parts = insta_url.split("/")
			username = parts[parts.index("stories") + 1]
			api_url = (
				f"https://anon-viewer.com/content.php"
				f"?url={username}&method=allstories"
			)
		else:
			encoded_url = urllib.parse.quote(insta_url, safe="")
			api_url = f"https://anon-viewer.com/content.php?url={encoded_url}"

		response = opener.open(api_url, timeout=60)
		if response.getcode() == 200:
			res_content = response.read().decode("utf-8")
			data = json.loads(res_content)
			html_text = data.get("html", "")
			# Try media.php link first.
			match = re.search(
				r'href="([^"]+anon-viewer\.com/media\.php\?media=[^"]+)"',
				html_text,
			)
			if match:
				return match.group(1).replace("&amp;", "&")
			# Fallback to <source> tag.
			source_match = re.search(r'<source src="([^"]+)"', html_text)
			if source_match:
				return source_match.group(1).replace("&amp;", "&")
	except Exception:
		log.debug("Instagram link extraction failed.", exc_info=True)
	return None


def _get_tiktok_download_link(tiktok_url: str) -> str | None:
	"""Extract a direct video download link from a TikTok URL."""
	api_url = "https://www.tikwm.com/api/"
	headers = {
		"User-Agent": _BROWSER_USER_AGENT,
		"X-Requested-With": "XMLHttpRequest",
	}
	try:
		params = {"url": tiktok_url, "hd": "1"}
		data = urllib.parse.urlencode(params).encode("utf-8")
		req = urllib.request.Request(
			api_url, data=data, headers=headers, method="POST",
		)
		with urllib.request.urlopen(req, timeout=120) as response:
			res = json.loads(response.read().decode("utf-8"))
			if res.get("code") == 0:
				play_url = res["data"]["play"]
				if not play_url.startswith("http"):
					play_url = "https://www.tikwm.com" + play_url
				return play_url
	except Exception:
		log.debug("TikTok link extraction failed.", exc_info=True)
	return None


# ------------------------------------------------------------------
# Video download
# ------------------------------------------------------------------

def download_video(url: str) -> str:
	"""Download a video from *url* to a temporary file.

	Returns the path to the temp ``.mp4`` file.  The **caller** is
	responsible for deleting the file when done.

	Raises :class:`ConnectionError` on download failure.
	"""
	req = urllib.request.Request(
		url, headers={"User-Agent": _BROWSER_USER_AGENT},
	)
	try:
		with urllib.request.urlopen(req, timeout=120) as response:
			fd, path = tempfile.mkstemp(suffix=".mp4")
			os.close(fd)
			try:
				with open(path, "wb") as f:
					while True:
						chunk = response.read(8192)
						if not chunk:
							break
						f.write(chunk)
				return path
			except Exception:
				# Clean up partial file on error.
				try:
					os.remove(path)
				except OSError:
					pass
				raise
	except Exception as exc:
		log.error("Video download failed: %s", exc)
		# Translators: Error when a video file could not be downloaded.
		raise ConnectionError(
			_("Could not download the video. Please try again.")
		) from exc
