# Luma — AI Companion for NVDA

**Luma** is an NVDA add-on that brings AI-powered visual description, screen
analysis, and conversational chat to blind users. It supports multiple AI
providers out of the box — OpenAI, Anthropic (Claude), Google Gemini, and
OpenRouter — as well as any custom OpenAI-compatible endpoint, plus on-device
inference. Highly customizable "skills" defined via simple Markdown files let
you tailor Luma to your workflow.

- **NVDA compatibility:** 2025.1 — 2026.1
- **License:** GNU General Public License v2 or later

---

## Installation

1. Download the latest `luma-<version>.nvda-addon` from the
   [Releases page](https://github.com/behofer/luma-nvda/releases).
2. Open the downloaded file. NVDA will prompt you to install the add-on.
   (Alternatively: NVDA menu → Tools → Manage add-ons → Install, then pick the
   file.)
3. Restart NVDA when prompted.

---

## Features

* **Screen & Object Description:** Capture the entire screen, the current
  application window, or a specific navigator object and send it to a
  vision-capable AI model for description.
* **Clipboard Processing:** Process text or image content from the clipboard
  with any skill.
* **Camera & Scanner Capture:** Capture a frame from any connected webcam or
  scan a page from a flatbed/document scanner and process it with a skill —
  great for describing physical objects, printed documents, or your
  surroundings.
* **File Recognition:** Process the file selected in File Explorer (images,
  PDFs, PowerPoint slides).
* **Text Chat:** Have a back-and-forth conversation with an AI, optionally
  including a screenshot as context.
* **Customizable Skills:** Define reusable prompt templates as Markdown files
  with YAML frontmatter. Each skill can specify its own provider, model, and
  temperature.
* **Preset & Custom Providers:** OpenAI, Anthropic, Google Gemini, OpenRouter,
  and Ollama are built in. Add any OpenAI-compatible endpoint, or run models
  on-device with the bundled local inference engine.
* **Custom Shortcuts:** Map any of 10 shortcut keys to a skill and scope
  combination for instant one-press execution.

---

## Quick Start

1. **Set up a provider:** NVDA menu → Tools → Luma → Providers. Select a preset
   (e.g. OpenAI) and click "Set API Key…", or add a custom OpenAI-compatible
   provider.
2. **Set defaults:** Tools → Luma → Settings; choose your default provider and
   model.
3. **Select a skill:** Press `NVDA+Shift+Space` to cycle through skills.
4. **Use it:** Press `NVDA+Shift+G` to describe the screen, or `NVDA+Shift+R`
   to describe the navigator object.

See [`doc/en/readme.md`](doc/en/readme.md) for the full user guide, keyboard
shortcuts, and skill format.

---

## Building from source

The add-on is a standard NVDA add-on package — a ZIP archive (with
`manifest.ini` at its root) renamed to `.nvda-addon`. To build it:

```sh
python build.py
```

This produces `luma-<version>.nvda-addon` in the repository root, where
`<version>` is read from `manifest.ini`.

---

## License

Luma is distributed under the GNU General Public License v2 or later. See
[`LICENSE`](LICENSE) for the full text.
