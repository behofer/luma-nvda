"""Hardware detection for local inference.

Detects system RAM, GPU vendor/VRAM, and recommends the best Gemma 4
model variant and llama.cpp backend for the user's hardware.  All
detection uses zero external dependencies (ctypes + subprocess).

Backend selection policy
------------------------
- Discrete NVIDIA with ≥ 4 GB VRAM → ``cuda`` (uses official
  llama.cpp CUDA 12.4 build).
- Any other GPU (AMD, Intel, integrated Radeon / Iris / UHD, older
  NVIDIA) → ``vulkan``.  This covers the Geekom-class mini-PCs with
  Radeon 780M where GPU offload via Vulkan is materially faster than
  CPU when BIOS-side GPU memory is configured.
- No GPU detected → ``cpu``.
"""

from __future__ import annotations

import ctypes
import logging
import subprocess
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GpuInfo:
	"""Detected GPU information."""

	name: str
	vendor: str  # "NVIDIA", "AMD", "Intel", or "Unknown"
	vram_bytes: int
	is_discrete: bool

	@property
	def vram_gb(self) -> float:
		return self.vram_bytes / (1024 ** 3)


@dataclass(frozen=True)
class HardwareInfo:
	"""Complete hardware profile for model selection."""

	total_ram_bytes: int
	available_ram_bytes: int
	gpus: list[GpuInfo] = field(default_factory=list)

	@property
	def total_ram_gb(self) -> float:
		return self.total_ram_bytes / (1024 ** 3)

	@property
	def available_ram_gb(self) -> float:
		return self.available_ram_bytes / (1024 ** 3)

	@property
	def best_gpu(self) -> GpuInfo | None:
		"""Return the most capable GPU, preferring discrete over integrated."""
		if not self.gpus:
			return None
		discrete = [g for g in self.gpus if g.is_discrete]
		if discrete:
			return max(discrete, key=lambda g: g.vram_bytes)
		return max(self.gpus, key=lambda g: g.vram_bytes)


@dataclass(frozen=True)
class ModelRecommendation:
	"""Recommended model variant and inference backend."""

	variant: str | None  # "e2b", "e4b", "26b-a4b", or None
	backend: str  # "cpu", "cuda", "vulkan"
	reason: str  # Human-readable explanation

	@property
	def supported(self) -> bool:
		return self.variant is not None


# ---------------------------------------------------------------------------
# Gemma 4 model catalog (Unsloth GGUFs)
# ---------------------------------------------------------------------------
#
# Unsloth's recommended quants (see unsloth.ai/docs/models/gemma-4):
#   - E2B / E4B: Q8_0 (dense + PLE — small models, full 8-bit keeps quality)
#   - 26B-A4B: UD-Q4_K_XL (MoE — Unsloth Dynamic 4-bit)
#
# Each variant ships alongside a BF16 mmproj file (~1 GB) for the SigLIP
# vision encoder.  Vision is loaded by llama-server via ``--mmproj``.
#
# Sizes below are the actual Content-Length bytes served by HuggingFace
# as of 2026-04-17 — verified via HEAD requests during the rewrite.

MODEL_REGISTRY: dict[str, dict] = {
	"e2b": {
		"repo": "unsloth/gemma-4-E2B-it-GGUF",
		"model_file": "gemma-4-E2B-it-Q8_0.gguf",
		"mmproj_file": "mmproj-BF16.gguf",
	},
	"e4b": {
		"repo": "unsloth/gemma-4-E4B-it-GGUF",
		"model_file": "gemma-4-E4B-it-Q8_0.gguf",
		"mmproj_file": "mmproj-BF16.gguf",
	},
	"26b-a4b": {
		"repo": "unsloth/gemma-4-26B-A4B-it-GGUF",
		"model_file": "gemma-4-26B-A4B-it-UD-Q4_K_XL.gguf",
		"mmproj_file": "mmproj-BF16.gguf",
	},
}

# (model_gguf_bytes, mmproj_gguf_bytes)
MODEL_SIZES: dict[str, tuple[int, int]] = {
	"e2b": (5_048_350_368, 986_833_856),        # ~4.7 GB + ~941 MB
	"e4b": (8_192_950_976, 991_552_448),        # ~7.6 GB + ~946 MB
	"26b-a4b": (17_090_276_672, 1_194_828_384), # ~15.9 GB + ~1.1 GB
}

# Minimum usable memory (RAM or VRAM) to load + run the model.
# Accounts for model + mmproj + KV cache + runtime overhead.
MODEL_MIN_MEMORY_GB: dict[str, float] = {
	"e2b": 8.0,
	"e4b": 12.0,
	"26b-a4b": 24.0,
}


# ---------------------------------------------------------------------------
# RAM detection (pure ctypes — zero deps)
# ---------------------------------------------------------------------------

class _MEMORYSTATUSEX(ctypes.Structure):
	_fields_ = [
		("dwLength", ctypes.c_ulong),
		("dwMemoryLoad", ctypes.c_ulong),
		("ullTotalPhys", ctypes.c_ulonglong),
		("ullAvailPhys", ctypes.c_ulonglong),
		("ullTotalPageFile", ctypes.c_ulonglong),
		("ullAvailPageFile", ctypes.c_ulonglong),
		("ullTotalVirtual", ctypes.c_ulonglong),
		("ullAvailVirtual", ctypes.c_ulonglong),
		("ullAvailExtendedVirtual", ctypes.c_ulonglong),
	]


def _detect_ram() -> tuple[int, int]:
	"""Return ``(total_bytes, available_bytes)`` of physical RAM."""
	stat = _MEMORYSTATUSEX()
	stat.dwLength = ctypes.sizeof(stat)
	ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
	return stat.ullTotalPhys, stat.ullAvailPhys


# ---------------------------------------------------------------------------
# GPU detection (WMI via PowerShell — all vendors)
# ---------------------------------------------------------------------------

# Name patterns that indicate an integrated GPU (shared system RAM).
_INTEGRATED_PATTERNS = (
	" M ",    # Radeon 780M, 680M, etc.
	"780M", "760M", "740M", "680M", "660M",
	"UHD",    # Intel UHD Graphics
	"Iris",   # Intel Iris
	"Vega",   # AMD Vega (APU integrated)
	"Radeon Graphics",  # Generic AMD APU integrated
	"Radeon(TM) Graphics",
)


def _is_integrated(gpu_name: str) -> bool:
	"""Heuristic: return True if the GPU name suggests integrated graphics."""
	upper = gpu_name.upper()
	for pattern in _INTEGRATED_PATTERNS:
		if pattern.upper() in upper:
			return True
	return False


def _vendor_from_compat(compat: str) -> str:
	"""Map WMI AdapterCompatibility to a short vendor name."""
	lower = compat.lower()
	if "nvidia" in lower:
		return "NVIDIA"
	if "amd" in lower or "advanced micro" in lower or "ati" in lower:
		return "AMD"
	if "intel" in lower:
		return "Intel"
	return "Unknown"


def _detect_gpus() -> list[GpuInfo]:
	"""Detect all GPUs via WMI ``Win32_VideoController``."""
	try:
		result = subprocess.run(
			[
				"powershell", "-NoProfile", "-Command",
				"Get-CimInstance Win32_VideoController"
				" | Select-Object Name, AdapterRAM, AdapterCompatibility"
				" | ForEach-Object {"
				"  \"$($_.Name)|$($_.AdapterRAM)|$($_.AdapterCompatibility)\""
				"}",
			],
			capture_output=True,
			text=True,
			timeout=10,
			creationflags=0x08000000,  # CREATE_NO_WINDOW
		)
	except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
		log.warning("GPU detection via PowerShell failed", exc_info=True)
		return []

	gpus: list[GpuInfo] = []
	for line in result.stdout.strip().splitlines():
		parts = line.strip().split("|")
		if len(parts) < 3:
			continue
		name = parts[0].strip()
		try:
			vram = int(parts[1].strip())
		except (ValueError, TypeError):
			vram = 0
		vendor = _vendor_from_compat(parts[2].strip())
		# Skip virtual / remote desktop adapters.
		if "microsoft" in name.lower() or "remote" in name.lower():
			continue
		gpus.append(GpuInfo(
			name=name,
			vendor=vendor,
			vram_bytes=vram,
			is_discrete=not _is_integrated(name),
		))
	return gpus


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_hardware() -> HardwareInfo:
	"""Detect RAM and GPU hardware.  Safe to call from any thread."""
	total, available = _detect_ram()
	gpus = _detect_gpus()
	log.info(
		"Hardware: RAM %.1f/%.1f GB, GPUs: %s",
		available / (1024 ** 3),
		total / (1024 ** 3),
		", ".join(f"{g.name} ({g.vram_gb:.1f} GB)" for g in gpus) or "none",
	)
	return HardwareInfo(
		total_ram_bytes=total,
		available_ram_bytes=available,
		gpus=gpus,
	)


def _pick_backend(gpu: GpuInfo | None) -> str:
	"""Map detected GPU to an llama.cpp backend string.

	Discrete NVIDIA with dedicated VRAM → CUDA.  Anything else that
	looks like a GPU (integrated Radeon, Iris, old NVIDIA without
	reported VRAM) → Vulkan — llama.cpp's Vulkan backend works across
	all modern GPU vendors and is materially faster than pure CPU on
	integrated graphics once BIOS-side UMA / GPU memory is configured.
	"""
	if gpu is None:
		return "cpu"
	if gpu.vendor == "NVIDIA" and gpu.is_discrete and gpu.vram_gb >= 4:
		return "cuda"
	return "vulkan"


def _pick_variant(usable_gb: float) -> str | None:
	"""Return the largest variant that fits in *usable_gb* of memory."""
	# Check from largest to smallest so we always return the best fit.
	for variant in ("26b-a4b", "e4b", "e2b"):
		if usable_gb >= MODEL_MIN_MEMORY_GB[variant]:
			return variant
	return None


def recommend_model(hw: HardwareInfo) -> ModelRecommendation:
	"""Choose the best Gemma 4 variant + backend for the detected hardware.

	Memory budgeting:

	- Discrete NVIDIA: use VRAM as the budget (leaves system RAM free).
	- Integrated GPU / Vulkan: fall back to available system RAM since
	  the GPU shares it.  Reserve 2 GB for OS + NVDA.
	- CPU only: available RAM minus 2 GB overhead.
	"""
	gpu = hw.best_gpu
	backend = _pick_backend(gpu)

	if backend == "cuda" and gpu is not None:
		usable_gb = gpu.vram_gb
		variant = _pick_variant(usable_gb)
		if variant is None:
			# Discrete NVIDIA with < 8 GB VRAM — fall back to CPU RAM
			# budget, since llama.cpp can partial-offload but we still
			# need enough system memory for the remainder.
			usable_gb = max(hw.available_ram_gb - 2.0, 0.0)
			variant = _pick_variant(usable_gb)
			if variant is not None:
				return ModelRecommendation(
					variant=variant,
					backend="cuda",
					# Translators: Hardware recommendation reason.  {gpu}
					# is the GPU name, {vram} is VRAM in GB, {variant} is
					# e.g. "Gemma 4 E4B".
					reason=_(
						"{gpu} has {vram:.0f} GB VRAM — partial GPU "
						"offload with the {variant} model."
					).format(
						gpu=gpu.name, vram=gpu.vram_gb,
						variant=_variant_label(variant),
					),
				)
		else:
			return ModelRecommendation(
				variant=variant,
				backend="cuda",
				# Translators: Hardware recommendation reason.
				reason=_(
					"{gpu} has {vram:.0f} GB VRAM — running the "
					"{variant} model on CUDA for best quality."
				).format(
					gpu=gpu.name, vram=gpu.vram_gb,
					variant=_variant_label(variant),
				),
			)

	if backend == "vulkan" and gpu is not None:
		# Integrated / non-NVIDIA GPU: memory budget is system RAM since
		# the GPU shares it.  Reserve 2 GB for OS + NVDA.
		usable_gb = max(hw.available_ram_gb - 2.0, 0.0)
		variant = _pick_variant(usable_gb)
		if variant is not None:
			return ModelRecommendation(
				variant=variant,
				backend="vulkan",
				# Translators: Hardware recommendation reason.  {gpu} is
				# the GPU name, {ram} is RAM in GB, {variant} is e.g.
				# "Gemma 4 E2B".
				reason=_(
					"{gpu} supports Vulkan — offloading the "
					"{variant} model to the GPU (shared memory pool: "
					"{ram:.0f} GB)."
				).format(
					gpu=gpu.name, ram=hw.available_ram_gb,
					variant=_variant_label(variant),
				),
			)

	# CPU path (no GPU, or not enough memory for Vulkan).
	usable_gb = max(hw.available_ram_gb - 2.0, 0.0)
	variant = _pick_variant(usable_gb)
	if variant is not None:
		return ModelRecommendation(
			variant=variant,
			backend="cpu",
			# Translators: Hardware recommendation reason.  {ram} is RAM
			# in GB, {variant} is e.g. "Gemma 4 E2B".
			reason=_(
				"{ram:.0f} GB RAM available — running the {variant} "
				"model on CPU."
			).format(
				ram=hw.available_ram_gb,
				variant=_variant_label(variant),
			),
		)

	# Not enough memory for anything.
	return ModelRecommendation(
		variant=None,
		backend="cpu",
		# Translators: Shown when the system doesn't have enough memory.
		reason=_(
			"Not enough memory for local Gemma 4 inference (need at "
			"least 8 GB available RAM). Consider using a cloud "
			"provider instead."
		),
	)


def estimate_download_size(variant: str) -> int:
	"""Return total download size in bytes for the given model variant."""
	model_bytes, mmproj_bytes = MODEL_SIZES.get(variant, (0, 0))
	return model_bytes + mmproj_bytes


# ---------------------------------------------------------------------------
# Variant labels (also used by setup_dialog.py)
# ---------------------------------------------------------------------------

_VARIANT_LABELS: dict[str, str] = {
	"e2b": "Gemma 4 E2B",
	"e4b": "Gemma 4 E4B",
	"26b-a4b": "Gemma 4 26B-A4B",
}


def _variant_label(variant: str) -> str:
	return _VARIANT_LABELS.get(variant, variant)


def variant_label(variant: str) -> str:
	"""Public human-readable label for a variant id."""
	return _variant_label(variant)
