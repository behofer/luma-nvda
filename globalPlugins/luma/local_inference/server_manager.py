"""llama-server subprocess lifecycle manager.

Replaces the former ``model_manager`` (which embedded llama-cpp-python
in-process) with an out-of-process ``llama-server.exe`` — the official
ggml-org binary that exposes an OpenAI-compatible HTTP API.  This
removes Python binding drama entirely:

- Gemma 4 works because the binary tracks llama.cpp master.
- Vision uses ``libmtmd`` via ``--mmproj`` — no Llava chat handler
  shim in Python.
- The provider layer just POSTs to ``http://127.0.0.1:<port>/v1``.

The server is started on demand (first inference call), probed for
readiness via ``/health``, kept warm with an idle timer, and
terminated cleanly on NVDA exit.

All public methods are thread-safe.  The singleton is process-wide
— ``get_manager()`` always returns the same instance.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# Context window — 4096 is plenty for single-image + prompt workflows
# and keeps the KV cache small.
_CTX_SIZE = 4096

# Vision ubatch / batch — Gemma 4's vision encoder uses non-causal
# attention so the full image must fit in a single ubatch.  2048 is the
# Unsloth-recommended value.
_UBATCH = 2048
_BATCH = 2048

# Image token budget — tuned for OCR quality per Unsloth docs.
_IMG_MIN_TOKENS = 1120
_IMG_MAX_TOKENS = 1120

# Idle timeout before the server is shut down (seconds).
_DEFAULT_IDLE_TIMEOUT = 600  # 10 minutes

# How long to wait for ``/health`` to report ``status: ok`` after launch.
_STARTUP_TIMEOUT = 180  # 3 minutes — covers slow first-time model mmap.
_HEALTH_POLL_INTERVAL = 0.5

# Windows: hide the console window the subprocess would otherwise pop up.
_CREATE_NO_WINDOW = 0x08000000


class ServerManager:
	"""Manages a single ``llama-server.exe`` subprocess."""

	def __init__(self) -> None:
		self._lock = threading.Lock()
		self._process: subprocess.Popen | None = None
		self._port: int | None = None

		# Configuration (set via ``configure``).
		self._server_exe: str | None = None
		self._model_path: str | None = None
		self._mmproj_path: str | None = None
		self._backend: str = "cpu"
		self._idle_timeout: float = _DEFAULT_IDLE_TIMEOUT

		# Idle timer (re-armed after each request completes).
		self._idle_timer: threading.Timer | None = None

	# ------------------------------------------------------------------
	# Configuration
	# ------------------------------------------------------------------

	def configure(
		self,
		*,
		server_exe: str,
		model_path: str,
		mmproj_path: str,
		backend: str = "cpu",
		idle_timeout: float = _DEFAULT_IDLE_TIMEOUT,
	) -> None:
		"""Set the paths and backend.  Does NOT start the server yet.

		If the server is already running and any path/backend changed,
		the running instance is stopped so the next call restarts it
		with the new config.
		"""
		with self._lock:
			changed = (
				server_exe != self._server_exe
				or model_path != self._model_path
				or mmproj_path != self._mmproj_path
				or backend != self._backend
			)
			if changed and self._process is not None:
				self._stop_locked()
			self._server_exe = server_exe
			self._model_path = model_path
			self._mmproj_path = mmproj_path
			self._backend = backend
			self._idle_timeout = idle_timeout

	# ------------------------------------------------------------------
	# Lifecycle
	# ------------------------------------------------------------------

	def ensure_running(self) -> str:
		"""Start the server if it isn't already, then return its base URL.

		Returns the OpenAI-compatible base URL (``http://127.0.0.1:<port>/v1``).
		Safe to call from any background thread; never call from the
		main thread since startup can block for ~tens of seconds.
		"""
		with self._lock:
			if self._is_alive_locked():
				self._reset_idle_timer_locked()
				return self._base_url_locked()
			self._start_locked()
			self._reset_idle_timer_locked()
			return self._base_url_locked()

	def mark_used(self) -> None:
		"""Re-arm the idle timer.  Call after each successful request."""
		with self._lock:
			if self._is_alive_locked():
				self._reset_idle_timer_locked()

	def stop(self) -> None:
		"""Stop the server (called on NVDA shutdown)."""
		with self._lock:
			self._stop_locked()

	@property
	def is_running(self) -> bool:
		with self._lock:
			return self._is_alive_locked()

	@property
	def base_url(self) -> str | None:
		"""Return the OpenAI-compat base URL if running, else ``None``."""
		with self._lock:
			if self._is_alive_locked():
				return self._base_url_locked()
			return None

	# ------------------------------------------------------------------
	# Internal — must be called with ``_lock`` held
	# ------------------------------------------------------------------

	def _is_alive_locked(self) -> bool:
		if self._process is None:
			return False
		if self._process.poll() is not None:
			# Process exited — clean up our state.
			log.warning(
				"llama-server exited unexpectedly (code %s)",
				self._process.returncode,
			)
			self._process = None
			self._port = None
			self._cancel_idle_timer_locked()
			return False
		return True

	def _base_url_locked(self) -> str:
		assert self._port is not None
		return f"http://127.0.0.1:{self._port}/v1"

	def _start_locked(self) -> None:
		if not self._server_exe or not os.path.isfile(self._server_exe):
			raise RuntimeError(
				# Translators: Error when llama-server.exe is missing.
				_("The local inference engine is not installed. "
				  "Please run the setup wizard."),
			)
		if not self._model_path or not os.path.isfile(self._model_path):
			raise RuntimeError(
				# Translators: Error when the local model file is missing.
				_("The local model file is missing. Please re-run the "
				  "setup wizard."),
			)

		port = _find_free_port()
		ngl = 99 if self._backend in ("cuda", "vulkan") else 0

		args: list[str] = [
			self._server_exe,
			"-m", self._model_path,
			"-c", str(_CTX_SIZE),
			"-ngl", str(ngl),
			"--host", "127.0.0.1",
			"--port", str(port),
			"--jinja",
			"-ub", str(_UBATCH),
			"-b", str(_BATCH),
		]
		if self._mmproj_path and os.path.isfile(self._mmproj_path):
			args += [
				"--mmproj", self._mmproj_path,
				"--image-min-tokens", str(_IMG_MIN_TOKENS),
				"--image-max-tokens", str(_IMG_MAX_TOKENS),
			]

		# Environment: put the binary's directory first on PATH so the
		# llama .dll(s) and (for CUDA) cudart DLLs next to the exe are
		# found even if the user's PATH doesn't include it.
		env = os.environ.copy()
		exe_dir = os.path.dirname(self._server_exe)
		env["PATH"] = exe_dir + os.pathsep + env.get("PATH", "")

		log.info(
			"Starting llama-server on port %d (backend=%s, ngl=%d)",
			port, self._backend, ngl,
		)
		log.debug("llama-server args: %s", args)

		try:
			# ``creationflags=CREATE_NO_WINDOW`` hides the console window
			# that the exe would otherwise pop up on Windows.
			# ``stdout``/``stderr`` piped to DEVNULL so the child doesn't
			# block when its pipes fill up.
			process = subprocess.Popen(
				args,
				stdin=subprocess.DEVNULL,
				stdout=subprocess.DEVNULL,
				stderr=subprocess.DEVNULL,
				cwd=exe_dir,
				env=env,
				creationflags=_CREATE_NO_WINDOW,
			)
		except OSError as exc:
			raise RuntimeError(
				# Translators: Error when the local inference engine
				# fails to spawn.  {error} is the OS error message.
				_("Could not start the local inference engine: {error}")
				.format(error=str(exc)),
			) from exc

		self._process = process
		self._port = port

		# Poll /health until the server is ready or we time out.
		try:
			self._wait_for_health_locked()
		except Exception:
			# Startup failed — kill the orphan before propagating so we
			# don't leave a zombie server bound to the port.
			self._stop_locked()
			raise

	def _wait_for_health_locked(self) -> None:
		"""Block until the server is ready or startup times out.

		llama-server's ``/health`` returns:

		- HTTP 200 ``{"status": "ok"}`` — ready.
		- HTTP 503 ``{"status": "loading model"}`` — still starting.
		- Connection refused — socket not open yet.
		"""
		assert self._port is not None
		url = f"http://127.0.0.1:{self._port}/health"
		deadline = time.monotonic() + _STARTUP_TIMEOUT

		while time.monotonic() < deadline:
			# Did the child process die while we were waiting?
			if self._process is None or self._process.poll() is not None:
				code = self._process.returncode if self._process else "?"
				raise RuntimeError(
					# Translators: Error shown when the local inference
					# engine exits during startup.  {code} is the OS exit
					# code.
					_("The local inference engine exited during startup "
					  "(code {code}).").format(code=code),
				)

			try:
				with urllib.request.urlopen(url, timeout=2) as resp:
					if resp.status == 200:
						log.info(
							"llama-server ready on port %d", self._port,
						)
						return
			except urllib.error.HTTPError as exc:
				# 503 = model still loading; keep polling.
				if exc.code != 503:
					log.debug("Health probe HTTP %s", exc.code)
			except (urllib.error.URLError, OSError, TimeoutError):
				# Socket not open yet, or transient — keep polling.
				pass

			time.sleep(_HEALTH_POLL_INTERVAL)

		raise RuntimeError(
			# Translators: Error when the local inference engine doesn't
			# become ready within the startup timeout.
			_("The local inference engine did not become ready within "
			  "{timeout} seconds.").format(timeout=_STARTUP_TIMEOUT),
		)

	def _stop_locked(self) -> None:
		self._cancel_idle_timer_locked()
		if self._process is None:
			self._port = None
			return
		log.info("Stopping llama-server (pid=%s)", self._process.pid)
		try:
			self._process.terminate()
			try:
				self._process.wait(timeout=5)
			except subprocess.TimeoutExpired:
				log.warning("llama-server did not terminate; killing")
				self._process.kill()
				self._process.wait(timeout=5)
		except OSError:
			log.warning("Error stopping llama-server", exc_info=True)
		finally:
			self._process = None
			self._port = None

	# ------------------------------------------------------------------
	# Idle timer
	# ------------------------------------------------------------------

	def _reset_idle_timer_locked(self) -> None:
		self._cancel_idle_timer_locked()
		if self._idle_timeout > 0:
			t = threading.Timer(self._idle_timeout, self._on_idle)
			t.daemon = True
			t.start()
			self._idle_timer = t

	def _cancel_idle_timer_locked(self) -> None:
		if self._idle_timer is not None:
			self._idle_timer.cancel()
			self._idle_timer = None

	def _on_idle(self) -> None:
		log.info("Idle timeout reached — stopping llama-server")
		with self._lock:
			self._stop_locked()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_free_port() -> int:
	"""Ask the OS for a free TCP port on localhost.

	There is a benign race between ``close()`` and the child binding
	the port — the window is microseconds on Windows and no other
	process is likely to grab exactly this port in that interval.
	"""
	with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
		s.bind(("127.0.0.1", 0))
		return s.getsockname()[1]


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_manager: ServerManager | None = None


def get_manager() -> ServerManager:
	"""Return the process-wide :class:`ServerManager` singleton."""
	global _manager
	if _manager is None:
		_manager = ServerManager()
	return _manager
